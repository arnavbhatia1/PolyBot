"""Binance aggTrade feed → CVD + taker ratio + exchange-latency telemetry."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger(__name__)

_CACHE_MAX_ENTRIES = 16


@dataclass
class AggTrade:
    price: float
    qty: float
    is_buyer_maker: bool  # True = seller aggressor (bearish), False = buyer aggressor (bullish)
    ts: float             # local receipt time


class BinanceTradeAccumulator:
    """Rolling window of aggTrades with cached CVD / taker / CVD-accel queries."""

    def __init__(self, max_age_s: float = 300) -> None:
        self.max_age_s = max_age_s
        self._trades: deque[AggTrade] = deque()
        self._cache: dict[tuple, tuple[tuple, float]] = {}
        self._last_received_at: float = 0.0

    def clear(self) -> None:
        """Wipe rolling state. Call after any WS reconnect so windowed analytics
        don't bridge across a gap (e.g. CVD-accel comparing baseline trades from
        before the disconnect against fresh recent trades)."""
        self._trades.clear()
        self._cache.clear()
        self._last_received_at = 0.0

    def add_trade(self, price: float, qty: float, is_buyer_maker: bool,
                  ts: float) -> None:
        self._trades.append(AggTrade(price, qty, is_buyer_maker, ts))
        self._last_received_at = time.time()
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.max_age_s
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def _cache_key(self) -> tuple:
        if not self._trades:
            return (0, 0.0)
        return (len(self._trades), self._trades[-1].ts)

    def _cache_get(self, key: tuple) -> float | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        cached_key, value = entry
        if cached_key != self._cache_key():
            return None
        return value

    def _cache_put(self, key: tuple, value: float) -> None:
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            # FIFO: drop the oldest key.
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (self._cache_key(), value)

    def get_cvd(self, window_s: float = 120) -> float:
        """Net taker-buy minus taker-sell over the window. >0 = buyers aggressive."""
        key = ("cvd", window_s)
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        cutoff = time.time() - window_s
        cvd = 0.0
        for t in reversed(self._trades):
            if t.ts < cutoff:
                break
            cvd += -t.qty if t.is_buyer_maker else t.qty
        self._cache_put(key, cvd)
        return cvd

    def get_taker_ratio(self, window_s: float = 60, min_trades: int = 20) -> float:
        """Fraction of aggressive-buy volume. Returns 0.5 when sample is too thin."""
        key = ("taker", window_s, min_trades)
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        cutoff = time.time() - window_s
        count = 0
        buy_vol = 0.0
        total_vol = 0.0
        for t in reversed(self._trades):
            if t.ts < cutoff:
                break
            count += 1
            total_vol += t.qty
            if not t.is_buyer_maker:
                buy_vol += t.qty
        ratio = 0.5 if (count < min_trades or total_vol == 0) else buy_vol / total_vol
        self._cache_put(key, ratio)
        return ratio

    @property
    def latest_price(self) -> float:
        return self._trades[-1].price if self._trades else 0.0

    @property
    def latest_age_s(self) -> float:
        if self._last_received_at <= 0:
            return float("inf")
        return time.time() - self._last_received_at


class BinanceTradesFeed:
    """Consumes wss://stream.binance.com:9443/ws/<symbol>@aggTrade."""

    def __init__(self, accumulator: BinanceTradeAccumulator,
                 symbol: str = "btcusdt",
                 ws_url: str = "wss://stream.binance.com:9443/ws") -> None:
        self.accumulator = accumulator
        self.symbol = symbol
        self.ws_url = ws_url
        self.staleness = StalenessTracker("binance_trades")
        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    def _handle_message(self, data: dict[str, Any]) -> None:
        if data.get("e") != "aggTrade":
            return
        try:
            price = float(data["p"])
            qty = float(data["q"])
            is_buyer_maker = bool(data["m"])
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse aggTrade: {e}")
            return
        # float() parses "NaN"/"Infinity"; drop non-finite values so they can't
        # poison the CVD accumulator.
        if not (math.isfinite(price) and math.isfinite(qty)):
            logger.warning("Dropping non-finite aggTrade price/qty")
            return
        now = time.time()
        self.staleness.observe(now)
        self.accumulator.add_trade(price, qty, is_buyer_maker, now)

    async def _connect_ws(self) -> None:
        import websockets
        stream = f"{self.ws_url}/{self.symbol}@aggTrade"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "binance_trades")
                    backoff = 1
                    # Windowed analytics must not bridge the gap.
                    self.accumulator.clear()
                    self.staleness.reset()
                    self.staleness.mark_connected()
                    logger.debug(f"Binance aggTrade WebSocket connected: {stream}")
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
                        except asyncio.TimeoutError:
                            logger.warning("aggTrade WS idle >25s, forcing reconnect")
                            break
                        try:
                            data = _loads(msg)
                        except ValueError:
                            continue
                        self._handle_message(data)
            except Exception as e:
                if not self._running:
                    break
                self.staleness.mark_disconnected()
                logger.warning(f"aggTrade WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connect_ws())

    async def stop(self) -> None:
        self._running = False
        # Cancel before the first await — stop() runs under a shutdown timeout.
        if self._task:
            self._task.cancel()
        if self._ws:
            await self._ws.close()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
