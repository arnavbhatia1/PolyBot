"""Focused regression tests for Pillar 1 ingestion fixes.

One test per leak. Each test would have failed against the pre-fix code and
passes against the post-fix code.
"""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker
from polybot.feeds.binance_trades import BinanceTradeAccumulator
from polybot.feeds.binance_depth import BinanceDepthFeed
from polybot.feeds.binance_feed import BinanceFeed
from polybot.feeds.binance_forceorder import BinanceForceOrderFeed
from polybot.feeds.bybit_feed import BybitFeed
from polybot.feeds.clob_ws import ClobWebSocket, TRADE_BUFFER_MAXLEN
from polybot.feeds.coinbase_feed import CoinbaseFeed
from polybot.core.returns import lag1_autocorr


# ---- Shared helpers ----

class _NoTransport:
    transport = None


# ---- LEAK-CRIT-1 — Chainlink last-update-before-boundary semantics ----
# Covered by polybot/tests/test_chainlink_feed.py::test_boundary_last_update_wins


# ---- LEAK-MED-1 — reconnect clears rolling accumulators ----

def test_binance_trade_accumulator_clear_on_reconnect():
    acc = BinanceTradeAccumulator(max_age_s=300)
    now = time.time()
    for _ in range(20):
        acc.add_trade(price=70000.0, qty=0.1, is_buyer_maker=False, ts=now)
    assert acc.trade_count == 20
    assert acc.get_cvd(window_s=120) > 0
    acc.clear()
    assert acc.trade_count == 0
    assert acc.get_cvd(window_s=120) == 0
    assert acc.latest_age_s == float("inf")


def test_bybit_liquidations_clear_on_reconnect():
    feed = BybitFeed()
    now = time.time()
    feed._liquidations.append((now, 5000.0))
    feed._liquidations.append((now, -3000.0))
    feed._liquidations.clear()  # simulates what _connect_ws does
    long_usd, short_usd = feed.liquidation_usd_per_min()
    assert long_usd == 0.0 and short_usd == 0.0


def test_binance_forceorder_events_clear_on_reconnect():
    feed = BinanceForceOrderFeed()
    now = time.time()
    feed._events.append((now, 10000.0))
    feed._events.clear()
    long_usd, short_usd = feed.liquidation_usd_per_min()
    assert long_usd == 0.0 and short_usd == 0.0


def test_binance_fast_closes_clear_on_reconnect():
    feed = BinanceFeed()
    for px in (70000.0, 70010.0, 70020.0):
        feed.fast_closes.add(px)
    assert len(feed.fast_closes) == 3
    feed.fast_closes.clear()
    assert len(feed.fast_closes) == 0
    assert feed.fast_realized_vol(60.0) == 0.0


# ---- Shared infra — Sweep B ----

def test_enable_nodelay_returns_false_when_no_socket():
    assert enable_nodelay(_NoTransport(), "test_feed") is False


def test_staleness_tracker_basic_percentiles():
    t = StalenessTracker("x", maxlen=10)
    now = 1000.0
    for i in range(8):
        t.observe(now + i * 0.5)  # 7 gaps, each 0.5s
    snap = t.snapshot()
    assert snap["n"] == 7
    assert snap["p50"] == 0.5
    assert snap["p95"] == 0.5
    assert snap["max"] == 0.5


def test_staleness_tracker_reset_breaks_gap_across_reconnect():
    """After reset(), the next observation does NOT form a gap with the
    pre-reset timestamp — so reconnect gaps don't pollute the inter-arrival
    distribution."""
    t = StalenessTracker("x")
    t.observe(100.0)
    t.observe(101.0)
    pre_n = t.snapshot()["n"]
    assert pre_n == 1
    t.reset()
    t.observe(200.0)  # would create a 99s gap without reset
    assert t.snapshot()["n"] == pre_n  # no new gap added
    t.observe(200.5)
    assert t.snapshot()["n"] == pre_n + 1  # normal 0.5s gap added


# ---- CLOB freshness + reset ----

def test_clob_trade_buffer_maxlen_500():
    ws = ClobWebSocket()
    # populate one token's buffer beyond the old 100 cap
    for i in range(TRADE_BUFFER_MAXLEN + 50):
        ws._on_last_trade({
            "asset_id": "tok1",
            "price": "0.5", "size": "1", "side": "BUY",
        })
    assert len(ws.trade_buffer["tok1"]) == TRADE_BUFFER_MAXLEN


def test_clob_reset_per_token_state():
    ws = ClobWebSocket()
    ws._on_book({"asset_id": "a", "bids": [["0.5", "10"]], "asks": [["0.51", "10"]]})
    ws._on_best_bid_ask({"asset_id": "a", "best_bid": "0.5", "best_ask": "0.51", "spread": "0.01"})
    ws._on_last_trade({"asset_id": "a", "price": "0.5", "size": "1", "side": "BUY"})
    assert "a" in ws.books and "a" in ws.best_bid_ask and "a" in ws.last_trade and "a" in ws.trade_buffer
    ws._reset_per_token_state()
    assert ws.books == {} and ws.best_bid_ask == {} and ws.last_trade == {} and ws.trade_buffer == {}


def test_clob_book_fresh_helpers():
    ws = ClobWebSocket()
    ws._on_book({"asset_id": "a", "bids": [["0.5", "10"]], "asks": [["0.51", "10"]]})
    ws._on_book({"asset_id": "b", "bids": [["0.5", "10"]], "asks": [["0.51", "10"]]})
    assert ws.book_fresh("a") and ws.book_fresh("b")
    assert ws.both_books_fresh("a", "b")
    # Backdate one book to simulate staleness.
    ws.books["a"]["ts"] -= 60
    assert not ws.book_fresh("a", max_age_s=10.0)
    assert not ws.both_books_fresh("a", "b", max_age_s=10.0)


# ---- BinanceDepthFeed.get_imbalance ----

def test_binance_depth_get_imbalance():
    feed = BinanceDepthFeed()
    # 70% bid-weighted top 2: imbalance ≈ +0.4
    feed.top_bids = [["70000", "7"], ["69999", "0"]]
    feed.top_asks = [["70001", "3"], ["70002", "0"]]
    val = feed.get_imbalance(levels=2)
    expected = (70000 * 7 - 70001 * 3) / (70000 * 7 + 70001 * 3)
    assert val == pytest.approx(expected, abs=1e-4)


def test_binance_depth_get_imbalance_empty():
    feed = BinanceDepthFeed()
    assert feed.get_imbalance(levels=5) == 0.0


# ---- Bybit liquidation USD/min ----

def test_bybit_liquidation_signed_long_short():
    feed = BybitFeed()
    feed._handle_liquidation({"size": "1", "price": "70000", "side": "Sell"})  # long-liq → +usd
    feed._handle_liquidation({"size": "0.5", "price": "70000", "side": "Buy"})  # short-liq → -usd
    long_usd, short_usd = feed.liquidation_usd_per_min()
    # window_s=60 default; per-event sum scaled to /min ≡ same numbers at window=60
    assert long_usd == pytest.approx(70000.0, abs=1.0)
    assert short_usd == pytest.approx(35000.0, abs=1.0)


# ---- LEAK-CRIT-2 — schema parity via _build_aux_signals ----

def test_build_aux_signals_returns_none_when_feeds_missing():
    from polybot.main import _build_aux_signals
    out = _build_aux_signals(None, None, None, None, None, None)
    # 13 fields total; every field except coinbase_taker_n must be None.
    assert out["binance_book_imbalance_5"] is None
    assert out["cross_venue_gap"] is None
    assert out["coinbase_cvd_60s"] is None
    assert out["coinbase_taker_60s"] is None
    assert out["coinbase_taker_n"] == 0
    assert out["fast_realized_vol_60s"] is None
    assert out["bybit_funding_rate"] is None
    assert out["bybit_basis"] is None
    assert out["bybit_mark_price"] is None
    assert out["bybit_liq_long_usd_min"] is None
    assert out["bybit_liq_short_usd_min"] is None
    assert out["binance_liq_long_usd_min"] is None
    assert out["binance_liq_short_usd_min"] is None


# ---- LEAK-MED-2 fast_realized_vol gating ----

def test_fast_realized_vol_returns_zero_below_3_samples():
    feed = BinanceFeed()
    feed.fast_closes.add(70000.0)
    feed.fast_closes.add(70001.0)
    # Only 2 samples → not enough for std-dev.
    assert feed.fast_realized_vol(60.0) == 0.0


def test_fast_realized_vol_positive_with_samples():
    feed = BinanceFeed()
    # 6 samples with non-trivial variation
    for px in (70000.0, 70050.0, 69950.0, 70100.0, 69900.0, 70050.0):
        feed.fast_closes.add(px)
    assert feed.fast_realized_vol(60.0) > 0.0


# ---- LEAK on lag1_autocorr divide-by-zero ----

def test_lag1_autocorr_returns_zero_on_zero_close():
    # Place a zero inside the lookback window (positions 1..6 land in closes[-7:]
    # at lookback=6) so it lands in `denom = window[:-1]` and the guard fires.
    closes = np.array([70000.0, 0.0, 70001.0, 70002.0, 70003.0, 70004.0, 70005.0, 70006.0])
    assert lag1_autocorr(closes, lookback=6) == 0.0


def test_lag1_autocorr_handles_well_formed_input():
    closes = np.array([70000.0, 70010.0, 70005.0, 70015.0, 70008.0, 70020.0, 70012.0, 70025.0])
    val = lag1_autocorr(closes, lookback=6)
    assert -1.0 <= val <= 1.0


# ---- Coinbase ----

def test_coinbase_cvd_taker_round_trip():
    feed = CoinbaseFeed()
    base = {"type": "ticker", "product_id": "BTC-USD", "best_bid": "70000", "best_ask": "70001"}
    feed._handle_message({**base, "price": "70000.5", "side": "buy", "last_size": "0.1"})
    feed._handle_message({**base, "price": "70000.6", "side": "buy", "last_size": "0.2"})
    feed._handle_message({**base, "price": "70000.4", "side": "sell", "last_size": "0.05"})
    assert feed.get_cvd(window_s=60) == pytest.approx(0.25, abs=1e-6)
    ratio, n = feed.get_taker_ratio(window_s=60, min_trades=3)
    assert n == 3
    assert ratio == pytest.approx(0.3 / 0.35, abs=1e-3)
