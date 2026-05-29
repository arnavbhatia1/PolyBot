"""Coinbase Exchange real-time BTC price feed.

Public WebSocket ticker channel (fires on every trade). Provides:
  - latest BTC-USD trade price (fastest US-venue feed)
  - best bid / best ask
  - per-trade CVD + taker ratio (aggressor side × size)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-feed.exchange.coinbase.com"
RECONNECT_BASE = 1
RECONNECT_MAX = 30


@dataclass
class CoinbaseState:
    price: float = 0.0
    updated_at: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0

    @property
    def age_seconds(self) -> float:
        if self.updated_at == 0:
            return float("inf")
        return time.time() - self.updated_at


class CoinbaseFeed:
    """Streams Coinbase BTC-USD ticker; tracks price + spot-venue CVD/taker."""

    def __init__(self, ws_url: str = WS_URL, product_id: str = "BTC-USD",
                 trade_buffer_s: float = 300.0) -> None:
        self.ws_url = ws_url
        self.product_id = product_id
        self.state = CoinbaseState()
        self.staleness = StalenessTracker("coinbase")

        # Per-trade flow: (ts, signed_size). +size = buyer aggressor, -size = seller aggressor.
        self._trade_buffer_s = trade_buffer_s
        self._trades: deque[tuple[float, float]] = deque()

        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
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

    def get_cvd(self, window_s: float = 60.0) -> float:
        """Signed cumulative volume delta over the last window_s seconds."""
        cutoff = time.time() - window_s
        total = 0.0
        for ts, sz in reversed(self._trades):
            if ts < cutoff:
                break
            total += sz
        return total

    def get_cvd_acceleration(self, recent_s: float = 15.0, baseline_s: float = 45.0,
                             min_recent_trades: int = 10) -> float:
        """First derivative of CVD. Returns 0 when the recent window is too thin
        to trust — mirrors BinanceTradeAccumulator.get_cvd_acceleration semantics.
        """
        now = time.time()
        recent = baseline = 0.0
        recent_n = 0
        for ts, sz in reversed(self._trades):
            age = now - ts
            if age <= recent_s:
                recent += sz
                recent_n += 1
            elif age <= recent_s + baseline_s:
                baseline += sz
            else:
                break
        if recent_n < min_recent_trades:
            return 0.0
        return recent / max(recent_s, 1.0) - baseline / max(baseline_s, 1.0)

    def get_taker_ratio(self, window_s: float = 60.0, min_trades: int = 20) -> tuple[float, int]:
        """(buy_fraction, n) over window. Returns (0.5, n) when n < min_trades."""
        cutoff = time.time() - window_s
        buy = total = 0.0
        n = 0
        for ts, sz in reversed(self._trades):
            if ts < cutoff:
                break
            n += 1
            total += abs(sz)
            if sz > 0:
                buy += sz
        if n < min_trades or total <= 0:
            return 0.5, n
        return buy / total, n

    def _prune_trades(self, now: float) -> None:
        cutoff = now - self._trade_buffer_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    async def _connect_ws(self) -> None:
        import websockets

        backoff = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=60, compression=None,
                ) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "coinbase")
                    backoff = RECONNECT_BASE
                    self.staleness.reset()
                    self._trades.clear()

                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": [self.product_id],
                        "channels": ["ticker", "heartbeat"],
                    }))

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
                        except asyncio.TimeoutError:
                            logger.warning("Coinbase WS idle >25s, forcing reconnect")
                            break
                        try:
                            data = _loads(msg)
                        except ValueError:
                            continue
                        self._handle_message(data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Coinbase WS error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "heartbeat":
            self.staleness.observe()
            return
        if kind != "ticker" or data.get("product_id") != self.product_id:
            return

        now = time.time()
        self.staleness.observe(now)

        try:
            price = float(data["price"])
        except (KeyError, ValueError, TypeError):
            return

        self.state.price = price
        self.state.updated_at = now

        bid = data.get("best_bid")
        ask = data.get("best_ask")
        try:
            if bid is not None:
                self.state.best_bid = float(bid)
            if ask is not None:
                self.state.best_ask = float(ask)
        except (ValueError, TypeError):
            pass

        # Per-trade flow: ticker.side is the aggressor (taker) side.
        side = data.get("side")
        last_size = data.get("last_size")
        if side and last_size is not None:
            try:
                sz = float(last_size)
            except (ValueError, TypeError):
                sz = 0.0
            if sz > 0:
                signed = sz if side == "buy" else -sz
                self._trades.append((now, signed))
                self._prune_trades(now)
