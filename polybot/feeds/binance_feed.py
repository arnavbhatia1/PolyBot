"""Binance kline feed.

Primary stream: 1m candles → ATR / indicators / L1 vol scaling.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np
import httpx

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleBuffer:
    def __init__(self, max_size: int = 200):
        self.max_size = max_size
        self._candles: deque[Candle] = deque(maxlen=max_size)
        self._closes_cache: np.ndarray | None = None
        self._highs_cache: np.ndarray | None = None
        self._lows_cache: np.ndarray | None = None
        self._volumes_cache: np.ndarray | None = None
        self.version: int = 0
        self._last_received_at: float = 0.0

    def __len__(self) -> int:
        return len(self._candles)

    def _invalidate_caches(self) -> None:
        self._closes_cache = None
        self._highs_cache = None
        self._lows_cache = None
        self._volumes_cache = None

    def add(self, candle: Candle) -> None:
        self._candles.append(candle)
        self._invalidate_caches()
        self.version += 1
        self._last_received_at = time.time()

    def update_current(self, close: float, high: float, low: float, volume: float) -> None:
        if self._candles:
            c = self._candles[-1]
            c.close = close
            c.high = max(c.high, high)
            c.low = min(c.low, low)
            c.volume = volume
            self._invalidate_caches()
            self.version += 1
            self._last_received_at = time.time()

    @property
    def latest_age_s(self) -> float:
        if self._last_received_at <= 0.0:
            return float("inf")
        return time.time() - self._last_received_at

    def latest(self) -> Candle | None:
        return self._candles[-1] if self._candles else None

    def get_last_n(self, n: int) -> list[Candle]:
        items = list(self._candles)
        return items[-n:] if len(items) >= n else items

    def get_closes(self) -> np.ndarray:
        if self._closes_cache is None:
            arr = np.array([c.close for c in self._candles], dtype=np.float64)
            arr.setflags(write=False)
            self._closes_cache = arr
        return self._closes_cache

    def get_highs(self) -> np.ndarray:
        if self._highs_cache is None:
            arr = np.array([c.high for c in self._candles], dtype=np.float64)
            arr.setflags(write=False)
            self._highs_cache = arr
        return self._highs_cache

    def get_lows(self) -> np.ndarray:
        if self._lows_cache is None:
            arr = np.array([c.low for c in self._candles], dtype=np.float64)
            arr.setflags(write=False)
            self._lows_cache = arr
        return self._lows_cache

    def get_volumes(self) -> np.ndarray:
        if self._volumes_cache is None:
            arr = np.array([c.volume for c in self._candles], dtype=np.float64)
            arr.setflags(write=False)
            self._volumes_cache = arr
        return self._volumes_cache


class BinanceFeed:
    """WS-streamed 1-minute candles on a combined-streams connection."""

    def __init__(self, symbol: str = "btcusdt", buffer_size: int = 200,
                 ws_url: str = "wss://stream.binance.com:9443/ws",
                 rest_url: str = "https://api.binance.com/api/v3") -> None:
        self.symbol: str = symbol
        self.ws_url: str = ws_url
        self.rest_url: str = rest_url
        self.buffer: CandleBuffer = CandleBuffer(max_size=buffer_size)
        self.staleness: StalenessTracker = StalenessTracker("binance_kline")
        self._running: bool = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def backfill(self) -> bool:
        """Returns True iff at least one candle was loaded. Non-fatal on failure —
        the WS stream alone will fill the buffer eventually."""
        url = f"{self.rest_url}/klines"
        params = {"symbol": self.symbol.upper(), "interval": "1m", "limit": self.buffer.max_size}
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    klines = resp.json()
                for k in klines:
                    self.buffer.add(Candle(
                        timestamp=int(k[0]), open=float(k[1]), high=float(k[2]),
                        low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                    ))
                logger.debug("Backfilled %d candles", len(klines))
                return len(klines) > 0
            except Exception as e:
                wait = 2 ** attempt
                logger.warning("Backfill attempt %d/3 failed: %s, retrying in %ds", attempt + 1, e, wait)
                await asyncio.sleep(wait)
        logger.warning("REST backfill failed after 3 attempts — relying on WS to warm the buffer")
        return False

    async def _connect_ws(self) -> None:
        import websockets

        if not self.ws_url.endswith("/ws"):
            raise ValueError(f"BinanceFeed.ws_url must end with '/ws' (got: {self.ws_url})")
        stream = f"{self.ws_url[:-3]}/stream?streams={self.symbol}@kline_1m"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "binance_kline")
                    backoff = 1
                    self.staleness.reset()
                    self.staleness.mark_connected()
                    logger.debug("Binance kline WS connected: %s", stream)
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=75.0)
                        except asyncio.TimeoutError:
                            logger.warning("kline WS idle >75s, forcing reconnect")
                            break
                        try:
                            envelope = _loads(msg)
                        except ValueError:
                            continue
                        # Combined-stream payload: {"stream": ..., "data": ...};
                        # non-{stream,data} control frames are skipped.
                        if "stream" in envelope and "data" in envelope:
                            self._route(envelope["stream"], envelope["data"])
            except Exception as e:
                if not self._running:
                    break
                self.staleness.mark_disconnected()
                logger.warning("WebSocket error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _route(self, stream: str, data: dict[str, Any]) -> None:
        if stream.endswith("@kline_1m"):
            self._handle_kline_1m(data)

    def _handle_kline_1m(self, data: dict[str, Any]) -> None:
        k = data.get("k", {})
        if not k:
            return
        self.staleness.observe()
        candle = Candle(
            timestamp=int(k["t"]), open=float(k["o"]), high=float(k["h"]),
            low=float(k["l"]), close=float(k["c"]), volume=float(k["v"]),
        )
        latest = self.buffer.latest()
        if latest is None or latest.timestamp != candle.timestamp:
            self.buffer.add(candle)
        else:
            self.buffer.update_current(close=candle.close, high=candle.high,
                                        low=candle.low, volume=candle.volume)

    async def start(self) -> None:
        self._running = True
        await self.backfill()
        self._task = asyncio.create_task(self._connect_ws())

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
            self._task = None
