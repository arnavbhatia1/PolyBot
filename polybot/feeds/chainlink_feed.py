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


class ChainlinkFeed:
    """Streams Chainlink BTC/USD from Polymarket RTDS and captures 5-min boundary strikes."""

    def __init__(self) -> None:
        self._price: float = 0.0
        self._last_update: float = 0.0     # local receipt time
        self._last_payload_ts: float = 0.0 # RTDS-reported ts when present
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._running: bool = False
        self._boundary_prices: "OrderedDict[int, float]" = OrderedDict()
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
        return self._boundary_prices.get(window_ts)

    def _record_boundary(self, observed_ts: float) -> None:
        if self._price <= 0:
            return
        next_boundary_ts = int(observed_ts // 300) * 300 + 300
        self._boundary_prices[next_boundary_ts] = self._price
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
        if self._ws:
            await self._ws.close()
        for t in (self._task, self._watchdog_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _watchdog(self) -> None:
        """Force WS reconnect when no updates arrive for STALE_TIMEOUT_S."""
        while self._running and self._last_update == 0:
            await asyncio.sleep(2)
        while self._running:
            await asyncio.sleep(10)
            if self._last_update > 0 and (time.time() - self._last_update) > STALE_TIMEOUT_S:
                if self._ws is not None:
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
        while self._running:
            ping_task: asyncio.Task | None = None
            try:
                async with websockets.connect(RTDS_WS_URL, ping_interval=PING_INTERVAL_S, compression=None) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "chainlink")
                    self.staleness.reset()
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*"}],
                    }))
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
                            # Prefer the RTDS-reported timestamp when present; falls
                            # back to wall-clock so the boundary record is robust to
                            # a missing payload field.
                            payload_ts = payload.get("timestamp") or payload.get("ts")
                            observed_ts = float(payload_ts) if payload_ts is not None else now
                            self._last_payload_ts = observed_ts
                            self.staleness.observe(now)
                            self._record_boundary(observed_ts)
                        except (ValueError, TypeError):
                            pass
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                if not self._running:
                    break
                logger.warning("ChainlinkFeed: WS disconnected (%s), reconnecting in 5s", e)
                self._ws = None
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ChainlinkFeed: unexpected error: %s", e, exc_info=True)
                self._ws = None
                await asyncio.sleep(5)
            finally:
                if ping_task and not ping_task.done():
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
