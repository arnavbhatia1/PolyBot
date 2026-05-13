"""Chainlink BTC/USD oracle (Polymarket RTDS WS). The resolution price source + 5-min strike capture."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

logger = logging.getLogger("polybot")

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_S = 5         # WebSocket-level ping (library handles)
APP_PING_INTERVAL_S = 10    # Application-level PING to keep RTDS subscription alive
STALE_TIMEOUT_S = 20        # Force reconnect if no Chainlink update in this many seconds


class ChainlinkFeed:
    """Streams Chainlink BTC/USD from Polymarket RTDS and captures 5-min boundary strikes."""

    def __init__(self) -> None:
        self._price: float = 0.0
        self._last_update: float = 0.0
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._running: bool = False
        # Strike capture: {window_ts: chainlink_price_at_boundary}
        self._boundary_prices: dict[int, float] = {}
        self._last_boundary_ts: int = 0

    @property
    def price(self) -> float:
        return self._price

    @property
    def age_seconds(self) -> float:
        if self._last_update <= 0:
            return float("inf")
        return time.time() - self._last_update

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > 30

    def get_strike(self, window_ts: int) -> float | None:
        """Get the Chainlink price captured at a 5-min window boundary.

        Returns None if not captured (feed wasn't running at that boundary).
        """
        return self._boundary_prices.get(window_ts)

    def _check_boundary(self) -> None:
        """Keep the upcoming window's strike current with the latest Chainlink price."""
        if self._price <= 0:
            return
        now_ts = int(time.time())
        boundary_ts = (now_ts // 300) * 300
        next_boundary_ts = boundary_ts + 300
        self._boundary_prices[next_boundary_ts] = self._price

        if boundary_ts != self._last_boundary_ts:
            self._last_boundary_ts = boundary_ts
            logger.debug(
                f"ChainlinkFeed: boundary crossed, next strike ${self._boundary_prices.get(next_boundary_ts, 0):,.2f}"
            )
            cutoff = now_ts - 600
            self._boundary_prices = {
                k: v for k, v in self._boundary_prices.items() if k > cutoff
            }

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        logger.debug("ChainlinkFeed: starting RTDS WebSocket for btc/usd")

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
        logger.debug("ChainlinkFeed: stopped")

    async def _watchdog(self) -> None:
        """Force WS reconnect when no Chainlink updates arrive for STALE_TIMEOUT_S."""
        while self._running and self._last_update == 0:
            await asyncio.sleep(2)
        while self._running:
            await asyncio.sleep(10)
            if self._last_update > 0 and (time.time() - self._last_update) > STALE_TIMEOUT_S:
                age = time.time() - self._last_update
                if self._ws is not None:
                    logger.warning(
                        f"ChainlinkFeed: no update in {age:.0f}s — forcing reconnect"
                    )
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None
                # After closing, give _run a moment to reconnect before re-checking.
                await asyncio.sleep(5)

    async def _app_ping(self, ws: Any) -> None:
        """Send application-level PING messages to keep the RTDS subscription active."""
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
                async with websockets.connect(RTDS_WS_URL, ping_interval=PING_INTERVAL_S) as ws:
                    self._ws = ws
                    # Subscribe to Chainlink BTC/USD (resolution oracle)
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                        }],
                    }))
                    logger.debug("ChainlinkFeed: subscribed to crypto_prices_chainlink btc/usd")
                    ping_task = asyncio.create_task(self._app_ping(ws))

                    async for raw in ws:
                        if not self._running:
                            break
                        if raw == "PONG":
                            continue
                        try:
                            msg = json.loads(raw)
                            payload = msg.get("payload", {})
                            symbol = payload.get("symbol", "")
                            value = payload.get("value")
                            if symbol == "btc/usd" and value is not None:
                                self._price = float(value)
                                self._last_update = time.time()
                                self._check_boundary()
                        except (json.JSONDecodeError, ValueError, TypeError):
                            pass
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f"ChainlinkFeed: WS disconnected ({e}), reconnecting in 5s")
                self._ws = None
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ChainlinkFeed: unexpected error: {e}", exc_info=True)
                self._ws = None
                await asyncio.sleep(5)
            finally:
                if ping_task and not ping_task.done():
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
