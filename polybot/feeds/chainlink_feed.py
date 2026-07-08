"""Chainlink BTC/USD oracle (via Polymarket RTDS WS). Resolution price source + 5-min strike capture."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
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
        self._boundary_prices: "OrderedDict[int, float]" = OrderedDict()
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

    def get_strike(self, window_ts: int) -> float | None:
        if window_ts == self._start_window_ts:
            return None
        captured = self._boundary_prices.get(window_ts)
        if captured is not None:
            return captured
        if self._price > 0 and self.age_seconds < STALE_TIMEOUT_S:
            return self._price
        return None

    @staticmethod
    def _epoch_seconds(ts: float) -> float:
        """Normalize an epoch timestamp to seconds. RTDS payloads carry
        milliseconds; boundary bookkeeping and get_strike() are keyed in
        seconds, so an un-normalized value can never match a lookup."""
        return ts / 1000.0 if ts > 1e11 else ts

    def _record_boundary(self, observed_ts: float) -> None:
        if self._price <= 0:
            return
        # Polymarket's price_to_beat is the FIRST btc/usd report AT/AFTER the window
        # boundary timestamp (the same Chainlink data stream it resolves on, matched at
        # +0ms). So the strike for the window that OPENS at `boundary_ts` is the first
        # report whose timestamp lands at/after it — record once, first write wins
        # (reports arrive in time order); later in-window ticks must NOT overwrite it.
        # (Recording the last tick BEFORE the boundary instead missed the official round
        # by >$8 in a fast open — ~1% of windows flipped side.)
        boundary_ts = int(observed_ts // 300) * 300
        self._boundary_prices.setdefault(boundary_ts, self._price)
        cutoff = int(observed_ts) - 7200
        while self._boundary_prices:
            k = next(iter(self._boundary_prices))
            if k > cutoff:
                break
            self._boundary_prices.popitem(last=False)

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
        """Force a WS reconnect when a *connected* socket delivers no updates for
        STALE_TIMEOUT_S — but give a freshly-opened socket a grace window first.
        Without the grace, a silent-but-open socket gets force-closed within ~15s
        and reconnected immediately; against an RTDS 429 limiter that becomes a
        self-perpetuating reconnect storm (the socket never lives long enough to
        deliver data, so the run loop's backoff never escapes the penalty box)."""
        while self._running and self._last_update == 0:
            await asyncio.sleep(2)
        while self._running:
            await asyncio.sleep(10)
            stale = self._last_update > 0 and (time.time() - self._last_update) > STALE_TIMEOUT_S
            fresh_connect = (time.time() - self._last_connect) < STALE_TIMEOUT_S
            if stale and self._ws is not None and not fresh_connect:
                logger.warning(
                    "ChainlinkFeed: no update in %.0fs — forcing reconnect",
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
                        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*"}],
                    }))
                    # NB: backoff is reset only when real data arrives (in the loop
                    # below), NOT on connect — a socket that opens but stays silent
                    # (RTDS rate-limiting us) must keep escalating so we don't hammer
                    # the 429 limiter and trap ourselves in a reconnect storm.
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
                            self._price = float(value)
                            self._last_update = now
                            backoff = RECONNECT_BASE_S      # healthy data — safe to reset
                            # Prefer the RTDS-reported timestamp when present; falls
                            # back to wall-clock so the boundary record is robust to
                            # a missing payload field.
                            payload_ts = payload.get("timestamp") or payload.get("ts")
                            observed_ts = self._epoch_seconds(float(payload_ts)) if payload_ts is not None else now
                            self.staleness.observe(now)
                            self._record_boundary(observed_ts)
                        except (ValueError, TypeError):
                            pass
            except (websockets.ConnectionClosed, websockets.InvalidHandshake,
                    ConnectionError, OSError) as e:
                # InvalidHandshake covers a server-side rejection (HTTP 500
                # outage or 429 rate limit) — a reconnectable condition, not a
                # code error. Backoff doubles per consecutive failure so an
                # extended outage doesn't keep us in the 429 penalty box.
                if not self._running:
                    break
                # A 429 (rate limit) means back off HARD — jump toward the cap so we
                # leave the per-IP penalty box instead of immediately re-tripping it.
                if "429" in str(e):
                    backoff = max(backoff, RECONNECT_MAX_S / 2)
                logger.warning("ChainlinkFeed: WS disconnected (%s), reconnecting in %.0fs", e, backoff)
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
