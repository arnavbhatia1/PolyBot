"""Binance futures liquidation stream (btcusdt@forceOrder).

Direct measurement of cascade events: each message is one liquidation order with
side, qty, and price. Sole source for L3e.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger(__name__)

WS_URL = "wss://fstream.binance.com/ws"
RECONNECT_BASE = 1
RECONNECT_MAX = 60


class BinanceForceOrderFeed:
    """Streams per-liquidation events for BTCUSDT futures."""

    def __init__(self, symbol: str = "btcusdt", ws_url: str = WS_URL,
                 window_s: float = 60.0) -> None:
        self.symbol = symbol
        self.ws_url = ws_url
        self._window_s = window_s
        # (ts, signed_usd). Binance order.side == "SELL" → liquidating sells closed longs → long liquidation (price-down).
        # order.side == "BUY"  → liquidating buys closed shorts → short liquidation (price-up).
        # Signed convention: long_liq = +usd, short_liq = −usd.
        self._events: deque[tuple[float, float]] = deque()
        self.staleness = StalenessTracker("binance_forceorder")
        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._connected_since: float = 0.0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

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

    def liquidation_usd_per_min(self) -> tuple[float, float]:
        now = time.time()
        cutoff = now - self._window_s
        long_usd = short_usd = 0.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        for _ts, usd in self._events:
            if usd >= 0:
                long_usd += usd
            else:
                short_usd += -usd
        scale = 60.0 / self._window_s
        return long_usd * scale, short_usd * scale

    async def _run(self) -> None:
        import websockets

        stream = f"{self.ws_url}/{self.symbol}@forceOrder"
        backoff = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "binance_forceorder")
                    backoff = RECONNECT_BASE
                    self.staleness.reset()
                    self._events.clear()
                    self._connected_since = time.time()
                    logger.debug("Binance forceOrder WS connected: %s", stream)
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=300.0)
                        except asyncio.TimeoutError:
                            # Liquidations are sparse — long silences are normal.
                            continue
                        try:
                            data = _loads(msg)
                        except ValueError:
                            continue
                        self._handle(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected_since = 0.0
                if not self._running:
                    break
                logger.warning("forceOrder WS error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle(self, data: dict[str, Any]) -> None:
        if data.get("e") != "forceOrder":
            return
        order = data.get("o", {})
        if not order:
            return
        try:
            qty = float(order["q"])
            price = float(order["p"])
            side = str(order.get("S", "")).upper()
        except (KeyError, ValueError, TypeError):
            return
        if qty <= 0 or price <= 0:
            return
        usd = qty * price
        signed = usd if side == "SELL" else -usd
        now = time.time()
        self._events.append((now, signed))
        self.staleness.observe(now)
