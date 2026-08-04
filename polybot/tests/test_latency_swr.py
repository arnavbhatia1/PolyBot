"""Stale-while-revalidate on the wake path: a Coinbase-tick wake must never
wait on Gamma. Serving stale + background refresh replaced the inline fetches
that put a full HTTP RTT in front of the sniper evaluation (the 300-550ms
tick-to-submit p90 tail)."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

import polybot.main as main
from polybot.feeds.market_scanner import BTCMarketScanner


def _contract(**over):
    c = {"slug": "btc-updown-5m-1785700000", "price_up": 0.5, "price_down": 0.5,
         "end_date": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
         "seconds_remaining": 120.0}
    c.update(over)
    return c


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _clean_main_state():
    main._contract_price_cache.clear()
    main._contract_refresh_inflight.clear()
    yield
    main._contract_price_cache.clear()
    main._contract_refresh_inflight.clear()


@pytest.mark.asyncio
async def test_contract_prices_serves_stale_instantly_and_refreshes_in_background(monkeypatch):
    mid = "btc-updown-5m-1785700000"
    stale = _contract(price_up=0.4)
    main._contract_price_cache[mid] = (time.time() - 10.0, stale)  # TTL (5s) expired

    calls = []

    async def fake_fetch(scanner, market_id, http_client=None):
        calls.append(market_id)
        main._contract_price_cache[market_id] = (time.time(), _contract(price_up=0.9))
        return main._contract_price_cache[market_id][1]

    monkeypatch.setattr(main, "_fetch_contract_prices", fake_fetch)
    got = await main._get_contract_prices(object(), mid, http_client=object())
    assert got["price_up"] == 0.4  # served stale, no waiting
    assert mid in main._contract_refresh_inflight or calls  # refresh kicked
    await _drain()
    assert calls == [mid]
    assert mid not in main._contract_refresh_inflight
    assert main._contract_price_cache[mid][1]["price_up"] == 0.9  # background landed


@pytest.mark.asyncio
async def test_contract_prices_blocks_only_without_servable_cache(monkeypatch):
    mid = "btc-updown-5m-1785700000"

    async def fake_fetch(scanner, market_id, http_client=None):
        return _contract(price_up=0.7)

    monkeypatch.setattr(main, "_fetch_contract_prices", fake_fetch)
    got = await main._get_contract_prices(object(), mid, http_client=object())
    assert got["price_up"] == 0.7  # no cache -> inline fetch


@pytest.mark.asyncio
async def test_scanner_serves_stale_contract_and_refreshes_in_background(monkeypatch):
    sc = BTCMarketScanner(cache_seconds=5)
    cached = _contract()
    sc._cached_contract = cached
    sc._cache_time = time.time() - 60.0  # TTL long expired, window still live

    calls = []

    async def fake_fetch(http_client=None):
        calls.append(1)
        return _contract(price_up=0.8)

    monkeypatch.setattr(sc, "_fetch_active_contract", fake_fetch)
    got = await sc.find_active_contract(http_client=object())
    assert got is cached  # served instantly
    assert 60.0 <= got["seconds_remaining"] <= 121.0  # recomputed locally
    await _drain()
    assert calls == [1]
    assert sc._contract_refresh_inflight is False


@pytest.mark.asyncio
async def test_scanner_blocks_when_cached_window_expired(monkeypatch):
    sc = BTCMarketScanner(cache_seconds=5)
    sc._cached_contract = _contract(
        end_date=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())
    sc._cache_time = time.time() - 60.0

    async def fake_fetch(http_client=None):
        return _contract(price_up=0.6)

    monkeypatch.setattr(sc, "_fetch_active_contract", fake_fetch)
    got = await sc.find_active_contract(http_client=object())
    assert got["price_up"] == 0.6  # dead window -> inline fetch for the new one
