"""Binance.US 1m BTC/USDT candles via WebSocket. Feeds L1 vol scaling + indicator suite."""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from collections import deque
from typing import Any

import numpy as np
import httpx

from polybot.feeds._json import loads as _loads

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
        # Per-field caches — invalidated on add/update_current. Hot path calls
        # these 2-3× per book-update tick; ndarray allocation is wasted work
        # between candle events. Arrays are set read-only via setflags so any
        # accidental in-place mutation by a consumer raises immediately.
        self._closes_cache: np.ndarray | None = None
        self._highs_cache: np.ndarray | None = None
        self._lows_cache: np.ndarray | None = None
        self._volumes_cache: np.ndarray | None = None
        # Monotonic version — bumps on every mutation. Lets downstream caches
        # (IndicatorEngine.compute_all) invalidate correctly. Keying off
        # latest.timestamp would miss update_current mutations since the
        # in-progress candle keeps the same timestamp until it closes.
        self.version: int = 0

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

    def update_current(self, close: float, high: float, low: float, volume: float) -> None:
        if self._candles:
            c = self._candles[-1]
            c.close = close
            c.high = max(c.high, high)
            c.low = min(c.low, low)
            c.volume = volume
            self._invalidate_caches()
            self.version += 1

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
    def __init__(self, symbol: str = "btcusdt", buffer_size: int = 200,
                 ws_url: str = "wss://stream.binance.com:9443/ws",
                 rest_url: str = "https://api.binance.com/api/v3") -> None:
        self.symbol: str = symbol
        self.ws_url: str = ws_url
        self.rest_url: str = rest_url
        self.buffer: CandleBuffer = CandleBuffer(max_size=buffer_size)
        self._running: bool = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def backfill(self) -> None:
        url = f"{self.rest_url}/klines"
        params = {"symbol": self.symbol.upper(), "interval": "1m", "limit": self.buffer.max_size}
        for attempt in range(5):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    klines = resp.json()
                for k in klines:
                    self.buffer.add(Candle(
                        timestamp=int(k[0]), open=float(k[1]), high=float(k[2]),
                        low=float(k[3]), close=float(k[4]), volume=float(k[5]),
                    ))
                logger.debug(f"Backfilled {len(klines)} candles")
                return
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(f"Backfill attempt {attempt+1}/5 failed: {e}, retrying in {wait}s")
                await asyncio.sleep(wait)
        raise ConnectionError("Failed to backfill candles after 5 attempts")

    async def _connect_ws(self) -> None:
        import websockets
        stream = f"{self.ws_url}/{self.symbol}@kline_1m"
        backoff = 1
        # kline updates arrive ~once/second; >180s of silence is a dead stream.
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    _sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None
                    if _sock is not None:
                        try: _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except Exception: pass
                    backoff = 1
                    logger.debug(f"Binance WebSocket connected: {stream}")
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=180.0)
                        except asyncio.TimeoutError:
                            logger.warning("kline WS idle >180s, forcing reconnect")
                            break
                        try:
                            data = _loads(msg)
                        except ValueError:
                            continue
                        self._handle_kline(data)
            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_kline(self, data: dict[str, Any]) -> None:
        k = data.get("k", {})
        if not k:
            return
        candle = Candle(
            timestamp=int(k["t"]), open=float(k["o"]), high=float(k["h"]),
            low=float(k["l"]), close=float(k["c"]), volume=float(k["v"]),
        )
        if k.get("x", False):
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
