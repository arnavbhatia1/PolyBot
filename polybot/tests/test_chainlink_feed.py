import asyncio
import logging
import time

import pytest
import websockets

from polybot.feeds.chainlink_feed import ChainlinkFeed


class TestChainlinkFeed:
    def test_initial_state(self):
        f = ChainlinkFeed()
        assert f.price == 0.0
        assert f.age_seconds == float("inf")

    def test_get_strike_no_data(self):
        f = ChainlinkFeed()
        assert f.get_strike(1776000000) is None

    def test_price_update(self):
        f = ChainlinkFeed()
        f._price = 71500.0
        f._last_update = time.time()
        assert f.price == 71500.0
        assert f.age_seconds < 1.0

    def test_boundary_capture(self):
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300   # a past boundary (not the start window)
        f._price = 71234.56
        f._record_boundary(boundary_ts + 1)                   # first report just after the boundary
        assert f.get_strike(boundary_ts) == 71234.56

    def test_boundary_first_at_or_after_wins(self):
        """The FIRST report AT/AFTER a boundary defines that window's strike —
        Polymarket's price_to_beat is the first Chainlink btc/usd report at/after the
        window-boundary timestamp (matched at +0ms). Later in-window reports must NOT
        overwrite it."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._price = 71000.0
        f._record_boundary(boundary_ts + 1)      # first report at/after the boundary
        f._price = 72000.0
        f._record_boundary(boundary_ts + 120)    # later ticks in the same window
        f._price = 73000.0
        f._record_boundary(boundary_ts + 290)
        # The first at/after the boundary defines the strike, not the last before the next.
        assert f.get_strike(boundary_ts) == 71000.0

    def test_boundary_captured_flag(self):
        """boundary_captured flips True only once the first at/after-boundary report
        lands — the signal that get_strike is returning the LOCKED value, not the fallback."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        assert f.boundary_captured(boundary_ts) is False
        f._price = 71000.0
        f._record_boundary(boundary_ts + 1)
        assert f.boundary_captured(boundary_ts) is True
        assert f.boundary_captured(f._start_window_ts) is False   # start window never captured

    def test_strike_reliable_tight_gap(self):
        """A boundary report landing right after the previous report (~1Hz cadence)
        is trustworthy — no delivery hole, our capture == Polymarket's first report."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._price = 70990.0
        f._record_boundary(boundary_ts - 1)      # last report before the boundary
        f._price = 71000.0
        f._record_boundary(boundary_ts + 1)      # first at/after — 2s gap
        assert f.strike_reliable(boundary_ts) is True

    def test_strike_reliable_delivery_hole(self):
        """A 38s+ hole around the boundary (measured live: strike locked $35 off
        Polymarket's price_to_beat) means our first-received report is likely NOT
        Polymarket's first — untrusted for sniper capital."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._price = 62853.77
        f._record_boundary(boundary_ts - 38)
        f._price = 62803.25
        f._record_boundary(boundary_ts + 40)     # 78s hole spanning the boundary
        assert f.boundary_captured(boundary_ts) is True   # still locked...
        assert f.strike_reliable(boundary_ts) is False    # ...but not trusted

    def test_strike_reliable_requires_capture_and_history(self):
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        assert f.strike_reliable(boundary_ts) is False    # nothing captured
        f._price = 71000.0
        f._record_boundary(boundary_ts + 1)               # feed's FIRST-ever report
        assert f.strike_reliable(boundary_ts) is False    # no prev report to bound the hole

    def test_epoch_seconds_normalizes_rtds_milliseconds(self):
        """RTDS payload timestamps arrive in epoch ms (e.g. 1781031482000);
        second-space values pass through unchanged."""
        assert ChainlinkFeed._epoch_seconds(1781031482000.0) == 1781031482.0
        assert ChainlinkFeed._epoch_seconds(1781031482.0) == 1781031482.0

    def test_boundary_capture_from_ms_payload(self):
        """A boundary recorded from a normalized ms timestamp must be retrievable
        by a second-space get_strike lookup — un-normalized ms keys never match."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._price = 71234.56
        f._record_boundary(ChainlinkFeed._epoch_seconds((boundary_ts + 10) * 1000.0))
        # Fresh-price fallback must not mask the captured boundary: change the
        # live price after capture and require the boundary value back.
        f._price = 99999.0
        f._last_update = time.time()
        assert f.get_strike(boundary_ts) == 71234.56

    @pytest.mark.asyncio
    async def test_handshake_rejection_is_reconnectable_not_error(self, monkeypatch, caplog):
        """A server-side handshake rejection (RTDS outage returning HTTP 500 →
        InvalidHandshake) is a routine reconnect: one-line warning, no
        traceback-level error, loop keeps retrying."""
        from polybot.feeds import chainlink_feed as cf_mod

        attempts = 0

        class _RejectingConnect:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                nonlocal attempts
                attempts += 1
                raise websockets.InvalidHandshake("server rejected WebSocket connection: HTTP 500")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(cf_mod.websockets, "connect", _RejectingConnect)

        _real_sleep = asyncio.sleep
        sleeps: list[float] = []

        async def _instant_sleep(s):
            sleeps.append(s)
            await _real_sleep(0)  # still yields, never waits

        monkeypatch.setattr(cf_mod.asyncio, "sleep", _instant_sleep)

        f = ChainlinkFeed()
        f._running = True

        async def _stop_after_three():
            while attempts < 3:
                await _real_sleep(0)
            f._running = False

        with caplog.at_level(logging.WARNING, logger="polybot.feeds.chainlink_feed"):
            await asyncio.gather(f._run(), _stop_after_three())

        assert attempts >= 3, "feed must keep retrying through handshake rejections"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, f"handshake rejection logged as ERROR: {errors}"
        assert any("reconnecting" in r.getMessage() for r in caplog.records)
        # Backoff doubles per consecutive failure (5 -> 10 -> ...), so an
        # extended outage can't hammer RTDS into 429ing us indefinitely.
        assert sleeps[:2] == [5.0, 10.0], f"expected doubling backoff, got {sleeps[:3]}"
        assert f.staleness.connected is False

    @pytest.mark.asyncio
    async def test_429_backs_off_hard_not_a_storm(self, monkeypatch):
        """A 429 (rate limit) must jump the backoff toward the cap so we leave the
        per-IP penalty box instead of hammering it every few seconds — the
        reconnect-storm bug that stalled the feed (and strike) for ~44 min."""
        from polybot.feeds import chainlink_feed as cf_mod

        attempts = 0

        class _RateLimited:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                nonlocal attempts
                attempts += 1
                raise websockets.InvalidHandshake("server rejected WebSocket connection: HTTP 429")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(cf_mod.websockets, "connect", _RateLimited)

        _real_sleep = asyncio.sleep
        sleeps: list[float] = []

        async def _instant_sleep(s):
            sleeps.append(s)
            await _real_sleep(0)

        monkeypatch.setattr(cf_mod.asyncio, "sleep", _instant_sleep)

        f = ChainlinkFeed()
        f._running = True

        async def _stop_after_two():
            while attempts < 2:
                await _real_sleep(0)
            f._running = False

        await asyncio.gather(f._run(), _stop_after_two())

        # First 429 jumps to RECONNECT_MAX_S/2 (30), not the base 5; then doubles to the cap.
        assert sleeps[0] >= cf_mod.RECONNECT_MAX_S / 2, f"429 must back off hard, got {sleeps[:2]}"
