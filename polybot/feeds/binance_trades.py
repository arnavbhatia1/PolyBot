"""Binance.US aggTrade feed: CVD, taker ratio, large-trade and volume-surge detection."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from polybot.feeds._json import loads as _loads

logger = logging.getLogger(__name__)


@dataclass
class AggTrade:
    price: float
    qty: float
    is_buyer_maker: bool  # True = seller aggressor (bearish), False = buyer aggressor (bullish)
    ts: float  # Local receipt time (Unix seconds) — matches the other feeds' staleness semantics


class BinanceTradeAccumulator:
    """Rolling window accumulator for aggregate trades.

    Stores trades up to max_age_s and provides analytical queries
    over configurable time windows.
    """

    def __init__(self, max_age_s: float = 300) -> None:
        self.max_age_s = max_age_s
        self._trades: deque[AggTrade] = deque()
        self._cache: dict[tuple, tuple[tuple, float]] = {}
        # Local receipt time of the most recent trade. Mirrors the per-trade
        # ts field so windowing and staleness queries share one clock.
        self._last_received_at: float = 0.0

    def add_trade(self, price: float, qty: float, is_buyer_maker: bool, ts: float) -> None:
        """Append a trade and prune expired entries.

        Callers should pass local receipt time so windowing and staleness
        share one clock. Tests may pass synthetic ts values to control
        pruning behavior.
        """
        self._trades.append(AggTrade(price=price, qty=qty, is_buyer_maker=is_buyer_maker, ts=ts))
        self._last_received_at = time.time()
        self._prune()

    def _prune(self) -> None:
        """Remove trades older than max_age_s from the front of the deque."""
        cutoff = time.time() - self.max_age_s
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def _cache_key(self) -> tuple:
        """Snapshot of accumulator state used as cache key. Changes when a trade
        is appended (different len + latest_ts) or pruned (different len)."""
        if not self._trades:
            return (0, 0.0)
        return (len(self._trades), self._trades[-1].ts)

    def _window(self, window_s: float) -> list[AggTrade]:
        """Return trades within the last window_s seconds.

        Retained for callers that need the materialized list (e.g.
        get_large_trades). Hot-path consumers (get_cvd, get_taker_ratio) use
        single-pass iteration directly to avoid this list allocation.
        """
        cutoff = time.time() - window_s
        return [t for t in self._trades if t.ts >= cutoff]

    def get_cvd(self, window_s: float = 120) -> float:
        """Cumulative Volume Delta over the window.

        Positive = net aggressive buying (bullish).
        Negative = net aggressive selling (bearish).

        is_buyer_maker=False means buyer was taker (aggressor) -> +qty
        is_buyer_maker=True means seller was taker (aggressor) -> -qty
        """
        key = self._cache_key()
        cached = self._cache.get(("cvd", window_s))
        if cached is not None and cached[0] == key:
            return cached[1]
        cutoff = time.time() - window_s
        cvd = 0.0
        for t in reversed(self._trades):
            if t.ts < cutoff:
                break
            cvd += -t.qty if t.is_buyer_maker else t.qty
        self._cache[("cvd", window_s)] = (key, cvd)
        return cvd

    def get_cvd_acceleration(self, recent_s: float = 15.0, baseline_s: float = 45.0, min_recent_trades: int = 3) -> float:
        """First derivative of CVD: rate of change in buying pressure.

        Compares CVD rate in the recent window vs an older baseline window.
        Positive = buying accelerating, Negative = buying decelerating.

        Returns 0 when fewer than `min_recent_trades` trades fall inside the recent window (On Binance.US the 15s window contains 0-3)
        """
        now = time.time()
        recent_cvd = 0.0
        baseline_cvd = 0.0
        recent_count = 0
        for t in self._trades:
            if t.ts >= now - recent_s:
                recent_cvd += t.qty if not t.is_buyer_maker else -t.qty
                recent_count += 1
            elif t.ts >= now - recent_s - baseline_s:
                baseline_cvd += t.qty if not t.is_buyer_maker else -t.qty

        if recent_count < min_recent_trades:
            return 0.0

        recent_rate = recent_cvd / max(recent_s, 1.0)
        baseline_rate = baseline_cvd / max(baseline_s, 1.0)

        if baseline_rate == 0 and recent_rate == 0:
            return 0.0
        return recent_rate - baseline_rate

    def get_taker_ratio(self, window_s: float = 60, min_trades: int = 5) -> float:
        """Fraction of volume from aggressive buyers [0, 1].

        1.0 = all aggressive buying, 0.0 = all aggressive selling.
        Returns 0.5 when fewer than `min_trades` trades are in the window — a
        single whale trade would otherwise return 1.0 or 0.0 and trigger
        spurious signals downstream.
        """
        key = self._cache_key()
        cache_id = ("taker", window_s, min_trades)
        cached = self._cache.get(cache_id)
        if cached is not None and cached[0] == key:
            return cached[1]
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
        if count < min_trades or total_vol == 0:
            ratio = 0.5
        else:
            ratio = buy_vol / total_vol
        self._cache[cache_id] = (key, ratio)
        return ratio

    @property
    def trade_count(self) -> int:
        """Total trades currently in the buffer."""
        return len(self._trades)

    @property
    def latest_price(self) -> float:
        """Price of the most recent aggregated trade (0.0 if no trades)."""
        return self._trades[-1].price if self._trades else 0.0

    @property
    def latest_age_s(self) -> float:
        """Age of the most recent trade in seconds, measured against local
        receipt time so staleness semantics match the other WS feeds.
        Returns inf if no trade has ever been received.
        """
        if self._last_received_at <= 0:
            return float("inf")
        return time.time() - self._last_received_at


class BinanceTradesFeed:
    """WebSocket consumer for Binance aggTrade stream.

    Connects to wss://stream.binance.com:9443/ws/btcusdt@aggTrade and feeds
    parsed trades into a BinanceTradeAccumulator.

    aggTrade message format:
        {"e":"aggTrade","p":"73000.50","q":"0.123","m":true,"T":1234567890123}

    m=true  -> buyer was maker, seller was aggressor (bearish)
    m=false -> buyer was taker/aggressor (bullish)
    T       -> millisecond timestamp, divided by 1000 for seconds
    """

    def __init__(self, accumulator: BinanceTradeAccumulator,
                 symbol: str = "btcusdt",
                 ws_url: str = "wss://stream.binance.com:9443/ws") -> None:
        self.accumulator = accumulator
        self.symbol = symbol
        self.ws_url = ws_url
        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Parse an aggTrade message and feed into the accumulator."""
        if data.get("e") != "aggTrade":
            return
        try:
            price = float(data["p"])
            qty = float(data["q"])
            is_buyer_maker = bool(data["m"])
            # Stamp with local receipt time so windowed analytics (CVD,
            # CVD-accel, taker ratio) share one clock with latest_age_s and
            # the rest of the staleness machinery. Exchange T is discarded —
            # the WS stream is already ordered by arrival.
            self.accumulator.add_trade(price, qty, is_buyer_maker, time.time())
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse aggTrade: {e}")

    async def _connect_ws(self) -> None:
        import websockets
        stream = f"{self.ws_url}/{self.symbol}@aggTrade"
        backoff = 1
        # ping_interval/timeout catch protocol-level stalls; the per-recv idle timeout
        # catches the "TCP alive but data frozen" failure mode that Binance.US exhibits.
        while self._running:
            try:
                async with websockets.connect(stream, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    _sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None
                    if _sock is not None:
                        try: _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except Exception: pass
                    backoff = 1
                    logger.debug(f"Binance aggTrade WebSocket connected: {stream}")
                    while self._running:
                        try:
                            # Reconnect before the 30s L3b staleness skip fires
                            # — aggTrade feeds CVD/taker and the CVD-decel gate,
                            # and a silent stall would gate the bot off the
                            # market for the entire skip window. 25s is the
                            # tightest value that doesn't trip on Binance.US's
                            # natural low-volume quiet periods.
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
                logger.warning(f"aggTrade WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self) -> None:
        """Start consuming the aggTrade stream."""
        self._running = True
        self._task = asyncio.create_task(self._connect_ws())

    async def stop(self) -> None:
        """Stop the WebSocket consumer."""
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
