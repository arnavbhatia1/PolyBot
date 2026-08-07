"""Chainlink BTC/USD oracle (via Polymarket RTDS WS). Resolution price source + 5-min strike capture.

Since 2026-08-07 00:00 UTC the market resolves on the official 30s-TWAP stream:
strike = the stream's value AT the window open, final = its value AT the close
(verified bit-exact against served price_to_beat/final_price, 17/17 windows).
The boundary capture therefore locks the TWAP topic's first report at/after the
boundary; the raw ~1Hz stream feeds the running reconstruction the sniper
projects the final average from."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict, deque
from typing import Any

import websockets

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger("polybot")

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_S = 5            # WebSocket-level ping (library handles)
APP_PING_INTERVAL_S = 10       # Application-level PING to keep RTDS subscription alive
STALE_TIMEOUT_S = 60           # Chainlink mainnet can be quiet for >20s in low-vol; 60s is a true dead-feed signal
RECONNECT_BASE_S = 5.0         # first retry delay; doubles per consecutive failure
RECONNECT_MAX_S = 60.0         # cap — a flat fast retry during an RTDS outage trips their per-IP 429 limiter
STRIKE_TRUST_GAP_S = 2.0       # The TWAP topic ticks ~1Hz on integer seconds, so the true price_to_beat
                               # report carries ts == boundary; a first capture with ts > boundary+2s means
                               # we missed the official one (delivery hole). Pre-boundary gaps don't veto.
SPOT_STALE_S = 3.0             # projected_final_twap refuses a raw price older than this — a stale spot
                               # projects a stale displacement, and the sniper would fire on fiction.


class ChainlinkFeed:
    """Streams Chainlink BTC/USD from Polymarket RTDS and captures 5-min boundary strikes."""

    def __init__(self) -> None:
        self._price: float = 0.0
        self._last_update: float = 0.0     # local receipt time
        self._last_connect: float = 0.0    # when the current WS was established
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._running: bool = False
        self.on_report = None  # micro-tape hook: every RTDS report (recording.MicroTape)
        # Wakes the main loop on every raw report — the sniper's decision clock
        # in the final-30s averaging zone (cleared by the consumer, like
        # coinbase_feed.price_event).
        self.report_event: asyncio.Event = asyncio.Event()
        # Official 30s-TWAP stream (THE resolution source): observed value + its
        # observation ts + local receipt (the topic delivers ~1.6s behind
        # observation — receipt-vs-ts measures that lag continuously).
        self.on_twap = None    # micro-tape hook: every official TWAP report
        self.twap_official: float = 0.0
        self.twap_official_ts: float = 0.0
        self.twap_official_rx: float = 0.0
        # Raw-report ring (~45s of receipt-ts, price) — feeds the running TWAP
        # reconstruction. RECEIPT clock, not payload: the official aggregator
        # weights by arrival spacing (rx-clock ZOH fits it 4× tighter, median
        # $0.07 vs $0.30), and the sniper's frozen margins were measured on it.
        self._reports: deque[tuple[float, float]] = deque()
        # Strike capture: the TWAP topic's first report AT/AFTER each boundary
        # (== served price_to_beat bit-exact; the raw stream's own boundary read
        # differs from it by $10+ — never use raw for the strike).
        self._boundary_prices: "OrderedDict[int, float]" = OrderedDict()
        # boundary_ts -> (first at/after twap-report ts, prev twap-report ts).
        # First-ts gap drives strike_reliable(); prev None = topic's first-ever
        # report = untrusted.
        self._boundary_meta: dict[int, tuple[float, float | None]] = {}
        self._last_twap_ts: float | None = None
        self._last_report_rx: float | None = None
        self._start_window_ts: int = int(time.time() // 300) * 300
        self.staleness = StalenessTracker("chainlink")

    @property
    def price(self) -> float:
        return self._price

    @property
    def age_seconds(self) -> float:
        if self._last_update <= 0:
            return float("inf")
        return time.time() - self._last_update

    @property
    def last_report_rx(self) -> float:
        """Receipt time of the latest raw report — the sniper's decision-tick
        anchor for the per-fill race meter (0.0 before the first report)."""
        return self._last_report_rx or 0.0

    def get_strike(self, window_ts: int) -> float | None:
        if window_ts == self._start_window_ts:
            return None
        captured = self._boundary_prices.get(window_ts)
        if captured is not None:
            return captured
        # Cold-start fallback for the base path: the latest official TWAP value
        # (the strike IS a TWAP-stream value now). Untrusted until captured.
        if self.twap_official > 0 and (time.time() - self.twap_official_rx) < STALE_TIMEOUT_S:
            return self.twap_official
        return None

    def running_avg(self, start: float, end: float) -> float | None:
        """Time-weighted (step-function, last-known-value) average of raw reports
        over [start, end] on the RECEIPT clock — the estimator the sniper's
        frozen margins were measured on. Anchor = last report at/before start,
        or the first one within 2s after it (the bounded anchor error is folded
        into the margins, which were measured with this exact convention)."""
        if end <= start or not self._reports:
            return None
        seed: float | None = None
        pts: list[tuple[float, float]] = []
        for rx, p in self._reports:
            if rx <= start:
                seed = p
            elif rx <= end:
                pts.append((rx, p))
        if seed is None:
            if not pts or pts[0][0] > start + 2.0:
                return None
            seed = pts[0][1]
        acc = 0.0
        prev_t, prev_p = start, seed
        for rx, p in pts:
            acc += prev_p * (rx - prev_t)
            prev_t, prev_p = rx, p
        acc += prev_p * (end - prev_t)
        return acc / (end - start)

    def twap_30(self, end_ts: float | None = None, window_s: float = 30.0) -> float | None:
        """Our reconstruction of the official 30s TWAP over [end−window, end],
        rx-clock. None until the buffer covers the window. Verified continuously
        against the official topic (twap_official)."""
        end = end_ts if end_ts is not None else (self._last_report_rx or 0.0)
        if end <= 0:
            return None
        return self.running_avg(end - window_s, end)

    def projected_final_twap(self, close_ts: float, now: float | None = None) -> float | None:
        """The sniper's projection of the window's resolving 30s TWAP at time
        `now`: observed-average mass + spot carried over the unobserved tail,
        proj = w·A + (1−w)·spot,  w = observed fraction of [close−30, close].
        None outside the averaging zone, on a cold ring, or on a stale spot —
        a None here must read as "cannot fire", never as 0."""
        t = now if now is not None else time.time()
        t0 = close_ts - 30.0
        if t <= t0 or t > close_ts:
            return None
        if self._price <= 0 or self.age_seconds > SPOT_STALE_S:
            return None
        w = (t - t0) / 30.0
        avg = self.running_avg(t0, t)
        if avg is None:
            return None
        return w * avg + (1.0 - w) * self._price

    def boundary_captured(self, window_ts: int) -> bool:
        """True once the first report at/after window_ts has locked the boundary.

        get_strike then returns the locked value, not the live-price fallback.
        Never true for the feed's start window (its boundary was never observed)."""
        return window_ts != self._start_window_ts and window_ts in self._boundary_prices

    def strike_reliable(self, window_ts: int) -> bool:
        """True when the locked boundary value can be trusted to equal price_to_beat.

        Trust = our first at/after-boundary TWAP report's own payload ts within
        STRIKE_TRUST_GAP_S of the boundary; a later capture means the true
        boundary report never reached us — our value is a later second's average,
        and a sniper on a wrong strike is trading noise. Missed reports BEFORE
        the boundary don't veto. False until the boundary is captured, and false
        for the topic's first-ever report (no delivery history)."""
        if not self.boundary_captured(window_ts):
            return False
        meta = self._boundary_meta.get(window_ts)
        if meta is None:
            return False
        first_ts, prev_ts = meta
        if prev_ts is None:          # boundary was the feed's first-ever report
            return False
        return (first_ts - window_ts) <= STRIKE_TRUST_GAP_S

    @staticmethod
    def _epoch_seconds(ts: float) -> float:
        """Normalize an epoch timestamp to seconds.

        RTDS payloads carry milliseconds; boundary keys are seconds — an
        un-normalized value can never match a get_strike() lookup."""
        return ts / 1000.0 if ts > 1e11 else ts

    def _record_boundary(self, observed_ts: float, value: float) -> None:
        if value <= 0:
            return
        # Strike rule: price_to_beat = the TWAP stream's FIRST report AT/AFTER the
        # boundary (ts == boundary on the ~1Hz integer-second topic; verified
        # bit-exact vs served price_to_beat). First write wins — later in-window
        # reports must NOT overwrite it.
        boundary_ts = int(observed_ts // 300) * 300
        if boundary_ts not in self._boundary_prices:
            self._boundary_prices[boundary_ts] = value
            self._boundary_meta[boundary_ts] = (observed_ts, self._last_twap_ts)
        self._last_twap_ts = observed_ts
        cutoff = int(observed_ts) - 7200
        while self._boundary_prices:
            k = next(iter(self._boundary_prices))
            if k > cutoff:
                break
            self._boundary_prices.popitem(last=False)
        for k in [k for k in self._boundary_meta if k <= cutoff]:
            del self._boundary_meta[k]

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        self._running = False
        # Cancel before the first await — stop() runs under a shutdown timeout.
        for t in (self._task, self._watchdog_task):
            if t:
                t.cancel()
        if self._ws:
            await self._ws.close()
        for t in (self._task, self._watchdog_task):
            if t:
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _watchdog(self) -> None:
        """Force a reconnect when a connected socket goes silent for STALE_TIMEOUT_S.

        Fresh sockets get a grace window first: force-closing a silent-but-open
        socket immediately becomes a self-perpetuating reconnect storm against
        the RTDS 429 limiter (backoff never escapes the penalty box)."""
        # Warm-up is BOUNDED: a socket that pings fine but never delivers a report
        # (silent subscribe rejection) would otherwise idle as a permanent zombie.
        while self._running and self._last_update == 0:
            await asyncio.sleep(2)
            connected_for = time.time() - self._last_connect
            if self._ws is not None and connected_for > 2 * STALE_TIMEOUT_S:
                logger.warning(
                    "ChainlinkFeed connected %.0fs with zero reports - Reconnecting",
                    connected_for)
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
                await asyncio.sleep(5)
        while self._running:
            await asyncio.sleep(10)
            stale = self._last_update > 0 and (time.time() - self._last_update) > STALE_TIMEOUT_S
            fresh_connect = (time.time() - self._last_connect) < STALE_TIMEOUT_S
            if stale and self._ws is not None and not fresh_connect:
                logger.warning(
                    "ChainlinkFeed idle for %.0fs — Reconnecting",
                    time.time() - self._last_update,
                )
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
                await asyncio.sleep(5)

    async def _app_ping(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(APP_PING_INTERVAL_S)
                try:
                    await ws.send("PING")
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    async def _run(self) -> None:
        backoff = RECONNECT_BASE_S
        while self._running:
            ping_task: asyncio.Task | None = None
            try:
                async with websockets.connect(RTDS_WS_URL, ping_interval=PING_INTERVAL_S, compression=None) as ws:
                    self._ws = ws
                    self._last_connect = time.time()
                    enable_nodelay(ws, "chainlink")
                    self.staleness.reset()
                    self.staleness.mark_connected()
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": "crypto_prices_chainlink", "type": "*"},
                            # The resolution source from 2026-08-07: 30s TWAP.
                            # Exact filter format per docs (compact, lowercase).
                            {"topic": "crypto_prices_twap_thirty", "type": "update",
                             "filters": "{\"symbol\":\"btc/usd\"}"},
                        ],
                    }))
                    # backoff resets only on real data, NOT on connect — a silent socket
                    # (RTDS rate-limiting us) must keep escalating or we re-trip the 429 limiter.
                    ping_task = asyncio.create_task(self._app_ping(ws))

                    async for raw in ws:
                        if not self._running:
                            break
                        if raw == "PONG":
                            continue
                        try:
                            msg = _loads(raw)
                            payload = msg.get("payload", {})
                            if payload.get("symbol", "") != "btc/usd":
                                continue
                            value = payload.get("value")
                            if value is None:
                                continue
                            now = time.time()
                            payload_ts = payload.get("timestamp") or payload.get("ts")
                            observed_ts = self._epoch_seconds(float(payload_ts)) if payload_ts is not None else now
                            # TWAP messages carry the same symbol — route by topic
                            # STRICTLY, or raw ticks poison the strike capture.
                            if msg.get("topic", "") == "crypto_prices_twap_thirty":
                                self.twap_official = float(value)
                                self.twap_official_ts = observed_ts
                                self.twap_official_rx = now
                                # The strike IS this stream's boundary value.
                                self._record_boundary(observed_ts, self.twap_official)
                                if self.on_twap is not None:
                                    try:
                                        self.on_twap(observed_ts, self.twap_official)
                                    except Exception:
                                        pass
                                continue
                            self._price = float(value)
                            self._last_update = now
                            backoff = RECONNECT_BASE_S      # healthy data — safe to reset
                            self.staleness.observe(now)
                            self._last_report_rx = now
                            self._reports.append((now, self._price))
                            while self._reports and self._reports[0][0] < now - 45.0:
                                self._reports.popleft()
                            self.report_event.set()   # sniper decision clock
                            # Optional micro-tape hook — must not raise into the feed.
                            if self.on_report is not None:
                                try:
                                    self.on_report(observed_ts, self._price)
                                except Exception:
                                    pass
                        except (ValueError, TypeError):
                            pass
            except (websockets.ConnectionClosed, websockets.InvalidHandshake,
                    ConnectionError, OSError) as e:
                # InvalidHandshake = server-side rejection (500 outage / 429 limit) —
                # reconnectable, not a code error.
                if not self._running:
                    break
                # 429: back off HARD toward the cap — leave the per-IP penalty box, don't re-trip it.
                if "429" in str(e):
                    backoff = max(backoff, RECONNECT_MAX_S / 2)
                logger.warning("ChainlinkFeed disconnected (%s) - Reconnecting in %.0fs", type(e).__name__, backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ChainlinkFeed: unexpected error: %s", e, exc_info=True)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
            finally:
                self.staleness.mark_disconnected()
                if ping_task and not ping_task.done():
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
