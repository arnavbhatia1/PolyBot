"""Binance L2 depth feed — top-20 bid/ask via @depth20@100ms."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger(__name__)


def compute_depth_usd(bids: list[list[str]], asks: list[list[str]], levels: int = 20) -> float:
    total = 0.0
    for level in bids[:levels]:
        total += float(level[0]) * float(level[1])
    for level in asks[:levels]:
        total += float(level[0]) * float(level[1])
    return total


class BinanceDepthFeed:
    """Subscribes to btcusdt@depth20@100ms; commits both sides atomically."""

    def __init__(self, symbol: str = "btcusdt",
                 ws_url: str = "wss://stream.binance.com:9443/ws",
                 **_unused: Any) -> None:
        self.symbol = symbol
        self.ws_url = ws_url
        self.top_bids: list[list[str]] = []
        self.top_asks: list[list[str]] = []
        self.updated_at: float = 0.0
        self.staleness = StalenessTracker("binance_depth")
        self._running: bool = False
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None

    def get_depth_usd(self, levels: int = 20) -> float:
        return compute_depth_usd(self.top_bids, self.top_asks, levels)

    async def start(self) -> None:
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def _ws_loop(self) -> None:
        import websockets

        stream = f"{self.ws_url}/{self.symbol}@depth20@100ms"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "binance_depth")
                    backoff = 1
                    self.staleness.reset()
                    logger.debug("Binance depth WS connected: %s", stream)
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        except asyncio.TimeoutError:
                            logger.warning("depth WS idle >60s, forcing reconnect")
                            break
                        try:
                            data = _loads(msg)
                        except ValueError:
                            continue
                        bids = data.get("bids")
                        asks = data.get("asks")
                        if bids is not None and asks is not None:
                            self.top_bids = bids
                            self.top_asks = asks
                            self.updated_at = time.time()
                            self.staleness.observe(self.updated_at)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Depth WS error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
