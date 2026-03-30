import asyncio
import json
import logging
import time
from dataclasses import dataclass
from collections import deque
import numpy as np
import httpx

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

    def __len__(self) -> int:
        return len(self._candles)

    def add(self, candle: Candle):
        self._candles.append(candle)

    def update_current(self, close: float, high: float, low: float, volume: float):
        if self._candles:
            c = self._candles[-1]
            c.close = close
            c.high = max(c.high, high)
            c.low = min(c.low, low)
            c.volume = volume

    def latest(self) -> Candle | None:
        return self._candles[-1] if self._candles else None

    def get_last_n(self, n: int) -> list[Candle]:
        items = list(self._candles)
        return items[-n:] if len(items) >= n else items

    def get_closes(self) -> np.ndarray:
        return np.array([c.close for c in self._candles], dtype=np.float64)

    def get_highs(self) -> np.ndarray:
        return np.array([c.high for c in self._candles], dtype=np.float64)

    def get_lows(self) -> np.ndarray:
        return np.array([c.low for c in self._candles], dtype=np.float64)

    def get_volumes(self) -> np.ndarray:
        return np.array([c.volume for c in self._candles], dtype=np.float64)

    def get_opens(self) -> np.ndarray:
        return np.array([c.open for c in self._candles], dtype=np.float64)

class BinanceFeed:
    def __init__(self, symbol: str = "btcusdt", buffer_size: int = 200,
                 ws_url: str = "wss://stream.binance.com:9443/ws",
                 rest_url: str = "https://api.binance.com/api/v3"):
        self.symbol = symbol
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.buffer = CandleBuffer(max_size=buffer_size)
        self._running = False
        self._ws = None

    async def backfill(self):
        url = f"{self.rest_url}/klines"
        params = {"symbol": self.symbol.upper(), "interval": "1m", "limit": self.buffer.max_size}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            klines = resp.json()
        for k in klines:
            self.buffer.add(Candle(
                timestamp=int(k[0]), open=float(k[1]), high=float(k[2]),
                low=float(k[3]), close=float(k[4]), volume=float(k[5]),
            ))
        logger.info(f"Backfilled {len(klines)} candles")

    async def _connect_ws(self):
        import websockets
        stream = f"{self.ws_url}/{self.symbol}@kline_1m"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(stream) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info(f"Binance WebSocket connected: {stream}")
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_kline(json.loads(msg))
            except Exception as e:
                logger.warning(f"WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_kline(self, data: dict):
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

    async def start(self):
        self._running = True
        await self.backfill()
        asyncio.create_task(self._connect_ws())

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
