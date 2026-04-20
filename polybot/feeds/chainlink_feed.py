"""Real-time Chainlink BTC/USD price via Polymarket RTDS WebSocket.

Polymarket resolves 5-min BTC contracts using the Chainlink BTC/USD oracle,
NOT Binance spot. This feed provides the actual resolution price source so
the probability model uses the same data the market resolves against.

Also captures the Chainlink price at each 5-minute boundary — this is the
strike (priceToBeat) that Polymarket uses for resolution.

Connection: wss://ws-live-data.polymarket.com
Topic: crypto_prices_chainlink, symbol: btc/usd
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

logger = logging.getLogger("polybot")

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_S = 5


class ChainlinkFeed:
    """Streams Chainlink BTC/USD from Polymarket RTDS and captures 5-min boundary strikes."""

    def __init__(self) -> None:
        self._price: float = 0.0
        self._last_update: float = 0.0
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
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
        """If we crossed a 5-min boundary since the last capture, record the current
        price as that boundary's strike.

        The original implementation only captured within 5 s of the boundary, which
        silently dropped strikes when Chainlink's next push arrived >5 s after the
        boundary (common during quiet periods). We now capture on the *first* price
        update after any new boundary is crossed — the first post-boundary Chainlink
        print is the closest approximation of the resolution oracle's boundary value.
        """
        if self._price <= 0:
            return
        now_ts = int(time.time())
        boundary_ts = (now_ts // 300) * 300
        if boundary_ts != self._last_boundary_ts:
            self._boundary_prices[boundary_ts] = self._price
            self._last_boundary_ts = boundary_ts
            lag = now_ts - boundary_ts
            logger.debug(
                f"ChainlinkFeed: captured strike ${self._price:,.2f} "
                f"at boundary {boundary_ts} (lag {lag}s)"
            )
            # Clean old boundaries (keep last 10 minutes)
            cutoff = now_ts - 600
            self._boundary_prices = {
                k: v for k, v in self._boundary_prices.items() if k > cutoff
            }

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("ChainlinkFeed: starting RTDS WebSocket for btc/usd")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.debug("ChainlinkFeed: stopped")

    async def _run(self) -> None:
        while self._running:
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
                    logger.info("ChainlinkFeed: subscribed to crypto_prices_chainlink btc/usd")

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
