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

    def test_strike_reliable_exact_boundary_only(self):
        """The TWAP topic ticks ON integer seconds — the true price_to_beat
        report carries ts == boundary exactly. Only that capture is trusted;
        a boundary+1s capture is a hole-shifted LATER value (real dollars off
        mid-burst) and must be vetoed."""
        f = ChainlinkFeed()
        boundary_ts = ((int(time.time()) // 300) - 1) * 300
        f._record_boundary(boundary_ts - 1, 70990.0)      # last report before the boundary
        f._record_boundary(boundary_ts, 71000.0)          # ON the boundary — the official report
        assert f.strike_reliable(boundary_ts) is True
        f2 = ChainlinkFeed()
        f2._record_boundary(boundary_ts - 1, 70990.0)
        f2._record_boundary(boundary_ts + 1, 71001.0)     # 1s hole — later second's average
        assert f2.strike_reliable(boundary_ts) is False

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

    def test_twap_60_time_weighted_step_function(self):
        f = ChainlinkFeed()
        end = 1786060000.0
        # Value 100 holds [end-60, end-40], 200 holds [end-40, end-20], 300 holds [end-20, end]
        f._reports.extend([
            (end - 72.0, 100.0),   # anchor at/before window start
            (end - 40.0, 200.0),
            (end - 20.0, 300.0),
        ])
        assert f.twap_60(end_ts=end) == pytest.approx((100 + 200 + 300) / 3)

    def test_twap_60_none_until_window_fully_covered(self):
        f = ChainlinkFeed()
        end = 1786060000.0
        f._reports.append((end - 12.0, 500.0))   # no anchor near the window start
        assert f.twap_60(end_ts=end) is None     # partial average must not masquerade
        assert ChainlinkFeed().twap_60(end_ts=end) is None

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
        close = now + 10.0                        # 10s remaining -> w = 5/6
        t0 = close - 60.0
        # 64000 holds [t0, t0+30), 64060 holds [t0+30, now]; reports every <=10s
        # so the coverage guard passes. A = (64000*30 + 64060*20) / 50 = 64024.
        f._reports.append((t0 - 1.0, 64000.0))
        for off in (9.0, 18.0, 27.0):
            f._reports.append((t0 + off, 64000.0))
        for off in (30.0, 38.0, 46.0, 50.0):
            f._reports.append((t0 + off, 64060.0))
        f._price = 64120.0
        f._last_update = now
        proj = f.projected_final_twap(close, now=now)
        assert proj == pytest.approx((5 / 6) * 64024.0 + (1 / 6) * 64120.0)

    def test_projected_final_twap_none_outside_zone_or_stale(self):
        f = ChainlinkFeed()
        now = time.time()
        f._reports.append((now - 70.0, 64000.0))
        f._price = 64100.0
        f._last_update = now
        assert f.projected_final_twap(now + 61.0, now=now) is None   # zone not started
        assert f.projected_final_twap(now - 1.0, now=now) is None    # window closed
        f._last_update = now - 10.0                                  # stale spot
        assert f.projected_final_twap(now + 10.0, now=now) is None

    def test_projected_final_twap_none_on_delivery_hole(self):
        """A raw outage inside the averaging span leaves a poisoned average
        behind a perfectly fresh spot — the freshness gate cannot see it
        (measured: a 68s hole projected a $24 error onto a $0.14 photo-finish).
        The coverage guard must void the projection instead."""
        f = ChainlinkFeed()
        now = time.time()
        close = now + 10.0
        t0 = close - 60.0
        f._reports.extend([(t0 - 1.0, 64000.0), (t0 + 2.0, 64000.0)])
        f._reports.extend([(now - 1.0, 64180.0), (now, 64183.0)])  # resumes late
        f._price = 64183.0
        f._last_update = now
        assert f.projected_final_twap(close, now=now) is None

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
            twap_report = _j.dumps({"topic": "crypto_prices_twap_sixty",
                                    "payload": {"symbol": "btc/usd", "value": 63990.5,
                                                "timestamp": (boundary + 2) * 1000,
                                                "window_s": 60}})
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


class TestTwapFrozen:
    """The resolution source stalling is invisible to the raw-stream freshness
    gate and to the receipt-based reconnect watchdog, so it needs its own guard.
    Reproduces 2026-08-10 04:15 UTC: official value repeated for 35s while raw
    climbed $18, leaving our reconstruction $5.59 off the served final."""

    def _feed(self, value_since_ago, raw_points):
        f = ChainlinkFeed()
        now = time.time()
        f.twap_official = 65003.4548
        f._twap_value_since = now - value_since_ago
        for ago, price in raw_points:
            f._reports.append((now - ago, price))
        return f

    def test_flags_the_observed_stall(self):
        f = self._feed(35.0, [(30.0, 65000.79), (15.0, 65006.5), (1.0, 65019.19)])
        assert f.twap_frozen() is True

    def test_quiet_market_is_not_a_stall(self):
        """A genuinely flat market freezes the average legitimately — raw must
        have actually travelled for this to fire."""
        f = self._feed(35.0, [(30.0, 65003.40), (1.0, 65003.90)])
        assert f.twap_frozen() is False

    def test_normal_repeat_does_not_fire(self):
        """The relay appears to poll rather than stream, so ~11% of consecutive
        reports legitimately carry the previous value. A short repeat is normal."""
        f = self._feed(5.0, [(4.0, 65000.0), (1.0, 65020.0)])
        assert f.twap_frozen() is False

    def test_no_raw_evidence_does_not_fire(self):
        """With no raw reports spanning the freeze there is nothing to compare —
        other gates own that case; this one must not guess."""
        f = self._feed(35.0, [])
        assert f.twap_frozen() is False

    def test_cold_feed_does_not_fire(self):
        f = ChainlinkFeed()
        assert f.twap_frozen() is False

    def test_value_change_resets_the_clock(self):
        f = self._feed(35.0, [(30.0, 65000.79), (1.0, 65019.19)])
        assert f.twap_frozen() is True
        now = time.time()
        f.twap_official = 65017.4591        # a fresh value arrives
        f._twap_value_since = now
        assert f.twap_frozen() is False


class TestSpotBridge:
    """The Binance delta bridge: level from Chainlink, movement from Binance —
    the basis cancels in the delta, and every failure mode collapses to 0.0
    (plain projection), never to a guess."""

    def _feed(self, cl_obs_ts, binance):
        f = ChainlinkFeed()
        f._last_report_obs_ts = cl_obs_ts
        for ts, px in binance:
            f._binance.append((ts, px))
        return f

    def test_delta_is_binance_movement_since_the_last_report(self):
        f = self._feed(100.0, [(99.0, 64050.0), (100.0, 64057.0), (101.5, 64087.0)])
        # anchor = binance at/before the report's payload ts (64057), newest 64087
        assert f.spot_bridge_delta() == pytest.approx(30.0)

    def test_basis_never_enters(self):
        # Binance trades $57 above the Chainlink composite — irrelevant: only
        # the DELTA crosses the bridge.
        f = self._feed(100.0, [(100.0, 64057.0), (101.5, 64057.0)])
        assert f.spot_bridge_delta() == pytest.approx(0.0)

    def test_cold_ring_fails_to_plain(self):
        assert self._feed(100.0, []).spot_bridge_delta() == 0.0

    def test_no_anchor_coverage_fails_to_plain(self):
        # every binance tick is NEWER than the report: no anchor -> no bridge
        f = self._feed(100.0, [(101.0, 64060.0), (102.0, 64070.0)])
        assert f.spot_bridge_delta() == 0.0

    def test_binance_older_than_report_fails_to_plain(self):
        f = self._feed(100.0, [(98.0, 64050.0), (99.0, 64051.0)])
        assert f.spot_bridge_delta() == 0.0

    def test_decimal_slip_tick_fails_to_plain_and_warns(self, caplog):
        # A 10x slip in one relay tick once injected a -$58,509 delta straight
        # into the side the ladder rests on.
        import logging
        f = self._feed(100.0, [(100.0, 64050.0), (101.5, 6405.0)])
        with caplog.at_level(logging.WARNING):
            assert f.spot_bridge_delta() == 0.0
        assert any("BRIDGE OFF" in r.getMessage() for r in caplog.records)

    def test_stale_anchor_fails_to_plain(self):
        # A ring hole leaves the anchor 6s behind the report: the delta would
        # re-add movement the Chainlink report already carries.
        f = self._feed(100.0, [(94.0, 64050.0), (101.5, 64080.0)])
        assert f.spot_bridge_delta() == 0.0

    def test_bridged_projection_moves_by_weighted_delta(self):
        now = time.time()
        close = now + 10.0          # k=10s -> w = 5/6
        f = self._feed(now, [(now - 1.0, 64000.0), (now, 64000.0), (now + 0.5, 64030.0)])
        f._price = 64000.0
        f._last_update = now
        for i in range(56):
            f._reports.append((now - 55 + i, 64000.0))
        plain = f.projected_final_twap(close, now=now)
        fast = f.projected_final_twap(close, now=now, bridged=True)
        assert plain == pytest.approx(64000.0)
        # spot weight (1-w) = 1/6 at k=10 -> bridged shifts by 30 * 1/6 = 5
        assert fast - plain == pytest.approx(5.0, abs=0.5)
