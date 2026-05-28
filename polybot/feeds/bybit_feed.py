"""Bybit BTC perpetual feed.

Signals from the leveraged futures market over the public v5 linear WS:
  - perpetual lastPrice (perp price + perp-vs-spot basis)
  - markPrice + indexPrice (fair value and basis-impl signal)
  - fundingRate (positioning pressure)
  - openInterest (drives L3e liquidation pressure)
  - direct per-liquidation events (size + side) on the same socket
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

WS_URL = "wss://stream.bybit.com/v5/public/linear"
RECONNECT_BASE = 1
RECONNECT_MAX = 30


@dataclass
class BybitState:
    perp_price: float = 0.0
    perp_updated: float = 0.0
    mark_price: float = 0.0
    index_price: float = 0.0
    funding_rate: float = 0.0
    open_interest: float = 0.0
    open_interest_prev: float = 0.0
    price_at_oi: float = 0.0
    price_at_oi_prev: float = 0.0
    oi_updated: float = 0.0
    oi_updated_prev: float = 0.0

    @property
    def perp_age_s(self) -> float:
        if self.perp_updated <= 0:
            return float("inf")
        return time.time() - self.perp_updated

    @property
    def basis(self) -> float:
        """perp − index. Positive → perp trading at premium (long demand)."""
        if self.perp_price > 0 and self.index_price > 0:
            return self.perp_price - self.index_price
        return 0.0


class BybitFeed:
    """v5 linear WS — tickers.BTCUSDT + liquidation.BTCUSDT on one connection."""

    def __init__(self, ws_url: str = WS_URL, liq_window_s: float = 60.0) -> None:
        self.ws_url = ws_url
        self.state = BybitState()
        self.staleness = StalenessTracker("bybit")

        # Each entry: (ts, signed_usd_notional). +usd = long liquidation (price down).
        # − usd = short liquidation (price up). Per Bybit v5: order.side == "Buy" means
        # the liquidating order was a buy that closed shorts → short liquidation.
        self._liquidations: deque[tuple[float, float]] = deque()
        self._liq_window_s = liq_window_s

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

    def liquidation_usd_per_min(self) -> tuple[float, float]:
        """Returns (long_liq_usd_per_min, short_liq_usd_per_min) over liq_window_s."""
        now = time.time()
        cutoff = now - self._liq_window_s
        long_usd = short_usd = 0.0
        while self._liquidations and self._liquidations[0][0] < cutoff:
            self._liquidations.popleft()
        for ts, usd in self._liquidations:
            if usd >= 0:
                long_usd += usd
            else:
                short_usd += -usd
        scale = 60.0 / self._liq_window_s
        return long_usd * scale, short_usd * scale

    async def _connect_ws(self) -> None:
        import websockets

        backoff = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=30, compression=None,
                ) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "bybit")
                    backoff = RECONNECT_BASE
                    self.staleness.reset()
                    self._liquidations.clear()

                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": ["tickers.BTCUSDT", "liquidation.BTCUSDT"],
                    }))

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=55.0)
                        except asyncio.TimeoutError:
                            logger.warning("Bybit WS idle >55s, forcing reconnect")
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
                logger.warning("Bybit WS error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, data: dict[str, Any]) -> None:
        topic = data.get("topic", "")
        if topic == "tickers.BTCUSDT":
            self._handle_ticker(data.get("data", {}))
        elif topic == "liquidation.BTCUSDT":
            self._handle_liquidation(data.get("data", {}))

    def _handle_ticker(self, ticker: dict[str, Any]) -> None:
        if not ticker:
            return
        now = time.time()
        self.staleness.observe(now)

        for field, attr in (
            ("lastPrice", "perp_price"),
            ("markPrice", "mark_price"),
            ("indexPrice", "index_price"),
            ("fundingRate", "funding_rate"),
        ):
            val = ticker.get(field)
            if val is None:
                continue
            try:
                setattr(self.state, attr, float(val))
            except (ValueError, TypeError):
                continue
            if field == "lastPrice":
                self.state.perp_updated = now

        oi = ticker.get("openInterest")
        if oi is None:
            return
        try:
            new_oi = float(oi)
        except (ValueError, TypeError):
            return
        if new_oi > 0 and new_oi != self.state.open_interest:
            self.state.open_interest_prev = self.state.open_interest
            self.state.price_at_oi_prev = self.state.price_at_oi
            self.state.oi_updated_prev = self.state.oi_updated
            self.state.open_interest = new_oi
            self.state.price_at_oi = self.state.perp_price
            self.state.oi_updated = now

    def _handle_liquidation(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        """Bybit emits liquidation as an object (v5) with size, price, side."""
        entries = payload if isinstance(payload, list) else [payload]
        now = time.time()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                size = float(entry.get("size", 0))
                price = float(entry.get("price", 0))
            except (ValueError, TypeError):
                continue
            if size <= 0 or price <= 0:
                continue
            usd = size * price
            # side="Buy" → buy-to-close shorts → short liquidation (price-up event).
            side = str(entry.get("side", "")).lower()
            signed = -usd if side == "buy" else usd
            self._liquidations.append((now, signed))
