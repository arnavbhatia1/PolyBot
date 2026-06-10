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
        boundary_ts = (int(time.time()) // 300) * 300
        f._price = 71234.56
        f._record_boundary(boundary_ts)
        assert f.get_strike(boundary_ts + 300) == 71234.56

    def test_boundary_last_update_wins(self):
        """The last observation before the next boundary defines the strike,
        matching Polymarket's on-chain latestRoundData() at the boundary block."""
        f = ChainlinkFeed()
        boundary_ts = (int(time.time()) // 300) * 300
        # Three observations within the 5-min window leading up to next_boundary.
        f._price = 71000.0
        f._record_boundary(boundary_ts + 60)
        f._price = 72000.0
        f._record_boundary(boundary_ts + 180)
        f._price = 73000.0
        f._record_boundary(boundary_ts + 290)
        # The latest update before next_boundary defines the strike.
        assert f.get_strike(boundary_ts + 300) == 73000.0

    def test_epoch_seconds_normalizes_rtds_milliseconds(self):
        """RTDS payload timestamps arrive in epoch ms (e.g. 1781031482000);
        second-space values pass through unchanged."""
        assert ChainlinkFeed._epoch_seconds(1781031482000.0) == 1781031482.0
        assert ChainlinkFeed._epoch_seconds(1781031482.0) == 1781031482.0

    def test_boundary_capture_from_ms_payload(self):
        """A boundary recorded from a normalized ms timestamp must be retrievable
        by a second-space get_strike lookup — un-normalized ms keys never match."""
        f = ChainlinkFeed()
        boundary_ts = (int(time.time()) // 300) * 300
        f._price = 71234.56
        f._record_boundary(ChainlinkFeed._epoch_seconds((boundary_ts + 10) * 1000.0))
        # Fresh-price fallback must not mask the captured boundary: change the
        # live price after capture and require the boundary value back.
        f._price = 99999.0
        f._last_update = time.time()
        assert f.get_strike(boundary_ts + 300) == 71234.56

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

        async def _instant_sleep(_s):
            await _real_sleep(0)  # still yields, never waits

        monkeypatch.setattr(cf_mod.asyncio, "sleep", _instant_sleep)

        f = ChainlinkFeed()
        f._running = True

        async def _stop_after_two():
            while attempts < 2:
                await _real_sleep(0)
            f._running = False

        with caplog.at_level(logging.WARNING, logger="polybot.feeds.chainlink_feed"):
            await asyncio.gather(f._run(), _stop_after_two())

        assert attempts >= 2, "feed must keep retrying through handshake rejections"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, f"handshake rejection logged as ERROR: {errors}"
        assert any("reconnecting" in r.getMessage() for r in caplog.records)
        assert f.staleness.connected is False
