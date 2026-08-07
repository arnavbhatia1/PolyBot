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
        assert f.last_report_rx == 0.0

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
        f._record_boundary(boundary_ts + 1, 71234.56)         # first TWAP report just after the boundary
        assert f.get_strike(boundary_ts) == 71234.56

    def test_boundary_first_at_or_after_wins(self):
        """The FIRST TWAP-topic report AT/AFTER a boundary defines that window's
        strike — Polymarket's price_to_beat is the TWAP stream's value at the
        window open (verified bit-exact). Later in-window reports must NOT
        overwrite it."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._record_boundary(boundary_ts + 1, 71000.0)      # first report at/after the boundary
        f._record_boundary(boundary_ts + 120, 72000.0)    # later reports in the same window
        f._record_boundary(boundary_ts + 290, 73000.0)
        # The first at/after the boundary defines the strike, not the last before the next.
        assert f.get_strike(boundary_ts) == 71000.0

    def test_boundary_captured_flag(self):
        """boundary_captured flips True only once the first at/after-boundary report
        lands — the signal that get_strike is returning the LOCKED value, not the fallback."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        assert f.boundary_captured(boundary_ts) is False
        f._record_boundary(boundary_ts + 1, 71000.0)
        assert f.boundary_captured(boundary_ts) is True
        assert f.boundary_captured(f._start_window_ts) is False   # start window never captured

    def test_strike_reliable_tight_gap(self):
        """A boundary report landing on the ~1Hz heartbeat (within 2s of the
        boundary) is trustworthy — our capture == Polymarket's price_to_beat."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._record_boundary(boundary_ts - 1, 70990.0)      # last report before the boundary
        f._record_boundary(boundary_ts + 1, 71000.0)      # first at/after — 1s past boundary
        assert f.strike_reliable(boundary_ts) is True

    def test_strike_reliable_delivery_hole(self):
        """A capture landing long after the boundary means the true boundary
        report never reached us — our value is a later second's average.
        Untrusted for sniper capital."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._record_boundary(boundary_ts - 38, 62853.77)
        f._record_boundary(boundary_ts + 40, 62803.25)    # capture 40s past the boundary
        assert f.boundary_captured(boundary_ts) is True   # still locked...
        assert f.strike_reliable(boundary_ts) is False    # ...but not trusted

    def test_strike_reliable_short_hole_past_boundary_vetoes(self):
        """A small (3-8s) delivery hole whose capture lands a few seconds past
        the boundary skipped ~1Hz reports that included the true price_to_beat
        (measured live at $143+ off, flipping the resolved side). Must veto."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._record_boundary(boundary_ts - 0.5, 64600.0)    # heartbeat healthy pre-boundary
        f._record_boundary(boundary_ts + 3.5, 64731.0)    # capture 3.5s late — missed ~3 reports
        assert f.strike_reliable(boundary_ts) is False

    def test_strike_reliable_pre_boundary_gap_is_harmless(self):
        """Missed reports BEFORE the boundary don't invalidate the capture: if the
        first at/after report lands on the heartbeat, it IS Polymarket's first."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._record_boundary(boundary_ts - 10, 70990.0)     # quiet spell before the boundary
        f._record_boundary(boundary_ts + 0.5, 71000.0)    # on-heartbeat capture
        assert f.strike_reliable(boundary_ts) is True

    def test_strike_reliable_requires_capture_and_history(self):
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        assert f.strike_reliable(boundary_ts) is False    # nothing captured
        f._record_boundary(boundary_ts + 1, 71000.0)      # topic's FIRST-ever report
        assert f.strike_reliable(boundary_ts) is False    # no delivery history yet

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
        f._record_boundary(
            ChainlinkFeed._epoch_seconds((boundary_ts + 10) * 1000.0), 71234.56)
        # The live-TWAP fallback must not mask the captured boundary: change the
        # official value after capture and require the boundary value back.
        f.twap_official = 99999.0
        f.twap_official_rx = time.time()
        assert f.get_strike(boundary_ts) == 71234.56

    def test_get_strike_falls_back_to_fresh_official_twap(self):
        """Before the boundary locks, the base path may read the latest official
        TWAP value (the strike IS a TWAP-stream value); a stale one serves nothing."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f.twap_official = 64123.4
        f.twap_official_rx = time.time()
        assert f.get_strike(boundary_ts) == 64123.4
        f.twap_official_rx = time.time() - 120.0          # stale
        assert f.get_strike(boundary_ts) is None

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
        assert any("Reconnecting" in r.getMessage() for r in caplog.records)
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


class TestTwap:
    """The 30s-TWAP machinery: the running reconstruction (rx-clock, the
    estimator the frozen margins bind to), the sniper projection, and strict
    topic routing (the TWAP topic owns the strike; raw ticks own the price)."""

    def test_twap_30_time_weighted_step_function(self):
        f = ChainlinkFeed()
        end = 1786060000.0
        # Value 100 holds [end-30, end-20], 200 holds [end-20, end-10], 300 holds [end-10, end]
        f._reports.extend([
            (end - 42.0, 100.0),   # anchor at/before window start
            (end - 20.0, 200.0),
            (end - 10.0, 300.0),
        ])
        assert f.twap_30(end_ts=end) == pytest.approx((100 + 200 + 300) / 3)

    def test_twap_30_none_until_window_fully_covered(self):
        f = ChainlinkFeed()
        end = 1786060000.0
        f._reports.append((end - 12.0, 500.0))   # no anchor near the window start
        assert f.twap_30(end_ts=end) is None     # partial average must not masquerade
        assert ChainlinkFeed().twap_30(end_ts=end) is None

    def test_running_avg_accepts_anchor_shortly_after_start(self):
        """The margins were measured with a ≤2s post-start anchor allowed — the
        estimator must match its measurement convention exactly."""
        f = ChainlinkFeed()
        f._reports.extend([(101.5, 100.0), (110.0, 200.0)])
        # anchor at 101.5 (1.5s after start): 100 holds [100,110] err-bounded, 200 holds [110,120]
        assert f.running_avg(100.0, 120.0) == pytest.approx(150.0)
        f2 = ChainlinkFeed()
        f2._reports.append((103.0, 100.0))       # 3s after start — too far, no anchor
        assert f2.running_avg(100.0, 120.0) is None

    def test_projected_final_twap_blends_observed_and_spot(self):
        f = ChainlinkFeed()
        now = time.time()
        close = now + 10.0                        # 10s remaining -> w = 2/3
        t0 = close - 30.0
        f._reports.extend([(t0 - 1.0, 64000.0), (t0 + 5.0, 64060.0)])
        f._price = 64120.0
        f._last_update = now
        # A over [t0, now]: 64000 holds 5s, 64060 holds 15s -> 64045
        proj = f.projected_final_twap(close, now=now)
        assert proj == pytest.approx((2 / 3) * 64045.0 + (1 / 3) * 64120.0)

    def test_projected_final_twap_none_outside_zone_or_stale(self):
        f = ChainlinkFeed()
        now = time.time()
        f._reports.append((now - 40.0, 64000.0))
        f._price = 64100.0
        f._last_update = now
        assert f.projected_final_twap(now + 31.0, now=now) is None   # zone not started
        assert f.projected_final_twap(now - 1.0, now=now) is None    # window closed
        f._last_update = now - 10.0                                  # stale spot
        assert f.projected_final_twap(now + 10.0, now=now) is None

    def test_twap_topic_owns_strike_raw_owns_price(self):
        """Both topics carry the same btc/usd symbol — a routing slip would
        poison the strike with raw ticks (measured $10+ off the served
        price_to_beat) or the price with averaged values."""
        import json as _j

        f = ChainlinkFeed()
        boundary = ((int(time.time()) // 300) - 1) * 300

        async def run():
            class FakeWS:
                def __init__(self, frames):
                    self._frames = list(frames)
                async def send(self, _): pass
                def __aiter__(self): return self
                async def __anext__(self):
                    if not self._frames:
                        raise StopAsyncIteration
                    return self._frames.pop(0)

            raw_report = _j.dumps({"topic": "crypto_prices_chainlink",
                                   "payload": {"symbol": "btc/usd", "value": 64000.0,
                                               "timestamp": (boundary + 1) * 1000}})
            twap_report = _j.dumps({"topic": "crypto_prices_twap_thirty",
                                    "payload": {"symbol": "btc/usd", "value": 63990.5,
                                                "timestamp": (boundary + 2) * 1000,
                                                "window_s": 30}})
            ws = FakeWS([raw_report, twap_report])
            import websockets as _wslib
            class _Ctx:
                async def __aenter__(self): return ws
                async def __aexit__(self, *a): return False
            orig = _wslib.connect
            _wslib.connect = lambda *a, **k: _Ctx()
            try:
                f._running = True
                t = asyncio.create_task(f._run())
                await asyncio.sleep(0.1)
                f._running = False
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            finally:
                _wslib.connect = orig

        asyncio.run(run())
        # Raw report set the price + ring + wake event; the TWAP report set the
        # official fields AND the strike. Never the other way around.
        assert f.price == 64000.0
        assert f.report_event.is_set()
        assert len(f._reports) == 1
        assert f.twap_official == 63990.5
        assert f.twap_official_ts == pytest.approx(boundary + 2)
        assert f.get_strike(boundary) == 63990.5
