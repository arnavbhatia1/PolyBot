"""Coinbase Exchange real-time BTC price feed.

Provides a faster BTC/USD price than Binance.US by consuming the public
Coinbase Exchange WebSocket ticker channel. Coinbase is the #1 US exchange
by volume and typically leads Binance.US by 0.5-2 seconds on price moves.

No authentication required for the public feed.

Usage:
    feed = CoinbaseFeed()
    await feed.start()
    # Later:
    price = feed.state.price       # Latest BTC-USD trade price
    age = feed.state.age_seconds   # Seconds since last update
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-feed.exchange.coinbase.com"
RECONNECT_BASE = 1
RECONNECT_MAX = 30


@dataclass
class CoinbaseState:
    """Live state from Coinbase Exchange ticker."""
    price: float = 0.0
    updated_at: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    volume_24h: float = 0.0

    @property
    def age_seconds(self) -> float:
        """Seconds since last price update."""
        if self.updated_at == 0:
            return float("inf")
        return time.time() - self.updated_at

    @property
    def spread(self) -> float:
        """Current bid-ask spread in USD."""
        if self.best_bid > 0 and self.best_ask > 0:
            return self.best_ask - self.best_bid
        return 0.0


class CoinbaseFeed:
    """WebSocket consumer for Coinbase Exchange BTC-USD ticker.

    Subscribes to the public ticker channel which fires on every trade.
    Provides real-time last price, best bid/ask, and 24h volume.
    Reconnects automatically with exponential backoff.
    """

    def __init__(self, ws_url: str = WS_URL, product_id: str = "BTC-USD") -> None:
        self.ws_url = ws_url
        self.product_id = product_id
        self.state = CoinbaseState()
        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start consuming the Coinbase ticker stream."""
        self._running = True
        self._task = asyncio.create_task(self._connect_ws())
        logger.debug("CoinbaseFeed starting for %s", self.product_id)

    async def stop(self) -> None:
        """Cleanly shut down."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("CoinbaseFeed stopped")

    async def _connect_ws(self) -> None:
        """Persistent WebSocket connection with exponential backoff."""
        import websockets

        backoff = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=60,
                    compression=None,
                ) as ws:
                    self._ws = ws
                    _sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None
                    if _sock is not None:
                        try: _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except Exception: pass
                    backoff = RECONNECT_BASE

                    # Subscribe to ticker channel (fires on every trade)
                    sub = json.dumps({
                        "type": "subscribe",
                        "product_ids": [self.product_id],
                        "channels": ["ticker"],
                    })
                    await ws.send(sub)
                    logger.debug("Coinbase WebSocket connected, subscribed to %s ticker",
                                 self.product_id)

                    # BTC-USD ticker fires on every trade — many per second.
                    # 30s of no data = unambiguously dead; force reconnect.
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=90.0)
                        except asyncio.TimeoutError:
                            logger.warning("Coinbase WS idle >90s, forcing reconnect")
                            break
                        self._handle_message(json.loads(msg))

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Coinbase WebSocket error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Process a Coinbase ticker message.

        Ticker message format:
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "70727.50",
            "best_bid": "70727.00",
            "best_ask": "70728.00",
            "volume_24h": "245532.79",
            "side": "buy",
            "time": "2026-04-12T23:57:20.061Z",
            "trade_id": 370843401,
            "last_size": "0.0042"
        }
        """
        if data.get("type") != "ticker":
            return
        if data.get("product_id") != self.product_id:
            return

        now = time.time()

        price = data.get("price")
        if price is not None:
            try:
                self.state.price = float(price)
                self.state.updated_at = now
            except (ValueError, TypeError):
                pass

        bid = data.get("best_bid")
        if bid is not None:
            try:
                self.state.best_bid = float(bid)
            except (ValueError, TypeError):
                pass

        ask = data.get("best_ask")
        if ask is not None:
            try:
                self.state.best_ask = float(ask)
            except (ValueError, TypeError):
                pass

        vol = data.get("volume_24h")
        if vol is not None:
            try:
                self.state.volume_24h = float(vol)
            except (ValueError, TypeError):
                pass
