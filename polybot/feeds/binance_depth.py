"""Binance L2 depth feed: streams top-20 levels via WS for depth-USD checks."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)


def compute_depth_usd(
    bids: list[list[str]],
    asks: list[list[str]],
    levels: int = 20,
) -> float:
    """Total USD value across the top N bid + ask levels."""
    total = 0.0
    for level in bids[:levels]:
        total += float(level[0]) * float(level[1])
    for level in asks[:levels]:
        total += float(level[0]) * float(level[1])
    return total


class BinanceDepthFeed:
    """WebSocket subscription to ``btcusdt@depth20@100ms`` (top 20 L2 levels)."""

    def __init__(
        self,
        symbol: str = "btcusdt",
        ws_url: str = "wss://stream.binance.com:9443/ws",
        **_unused: Any,
    ) -> None:
        self.symbol = symbol
        self.ws_url = ws_url
        self.top_bids: list[list[str]] = []
        self.top_asks: list[list[str]] = []
        self._running: bool = False
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None

    def get_depth_usd(self, levels: int = 20) -> float:
        return compute_depth_usd(self.top_bids, self.top_asks, levels)

    async def start(self) -> None:
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.debug("BinanceDepthFeed started")

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
        logger.debug("BinanceDepthFeed stopped")

    async def _ws_loop(self) -> None:
        import websockets

        stream = f"{self.ws_url}/{self.symbol}@depth20@100ms"
        backoff = 1
        # Stream delivers @100ms — anything past a few seconds of silence is dead.
        # 10s idle is a generous floor; ping_interval=20 also catches protocol stalls.
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    _sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None
                    if _sock is not None:
                        try: _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except Exception: pass
                    backoff = 1
                    logger.debug(f"Binance depth WS connected: {stream}")
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        except asyncio.TimeoutError:
                            logger.warning("depth WS idle >60s, forcing reconnect")
                            break
                        data = json.loads(msg)
                        bids = data.get("bids")
                        asks = data.get("asks")
                        # Only commit when both sides are present in the same
                        # frame — otherwise compute_depth_usd would mix bids
                        # from snapshot T with asks from snapshot T+N.
                        if bids is not None and asks is not None:
                            self.top_bids = bids
                            self.top_asks = asks
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"Depth WS error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
