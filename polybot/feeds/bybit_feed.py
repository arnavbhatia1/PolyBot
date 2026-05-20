"""Bybit BTC perpetual futures feed.

Provides three signals from the leveraged futures market:
1. Price lead — perp vs spot divergence (leveraged traders react first)
2. Funding rate — contrarian crowding indicator
3. Open interest changes — liquidation pressure for L3e

All data arrives via the public WebSocket (no geo-block for US IPs).
The REST endpoint is geo-blocked for US; all required fields are in the
v5 tickers.BTCUSDT WS payload so no REST poll is needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

WS_URL = "wss://stream.bybit.com/v5/public/linear"
RECONNECT_BASE = 1
RECONNECT_MAX = 30


def compute_perp_lead(perp_price: float, spot_price: float) -> float:
    """Normalized divergence between perpetual and spot price.

    Returns float in [-1, 1]. Positive = perp leading up (bullish),
    negative = perp leading down (bearish).

    Uses tanh scaling: $100 divergence on $73000 (~0.14%) maps to ~0.5 signal.
    """
    if perp_price <= 0 or spot_price <= 0:
        return 0.0
    pct_diff = (perp_price - spot_price) / spot_price
    return math.tanh(pct_diff * 350)


def compute_funding_signal(funding_rate: float) -> float:
    """Contrarian signal from perpetual funding rate.

    Positive funding (longs crowded, paying shorts) -> negative signal (bearish).
    Negative funding (shorts crowded) -> positive signal (bullish).

    Baseline 0.0001 subtracted (normal positive funding in crypto).
    Scaled with tanh: 0.0005 rate -> ~-0.76 signal.

    Returns float in [-1, 1].
    """
    baseline = 0.0001
    adjusted = funding_rate - baseline
    # Invert: positive funding -> negative signal (contrarian)
    return math.tanh(-adjusted * 2500)


@dataclass
class BybitState:
    """Live state from Bybit perpetual feed."""

    perp_price: float = 0.0
    perp_updated: float = 0.0
    funding_rate: float = 0.0
    funding_updated: float = 0.0
    next_funding_time: float = 0.0
    open_interest: float = 0.0
    open_interest_prev: float = 0.0
    price_at_oi: float = 0.0
    price_at_oi_prev: float = 0.0
    oi_updated: float = 0.0
    oi_updated_prev: float = 0.0

    def is_stale(self, spot_price: float, spot_updated: float,
                 threshold_usd: float = 20.0) -> bool:
        """Detect if spot is stale relative to fresh perp data.

        Returns True when:
        - |perp - spot| > threshold_usd (prices diverged)
        - perp data is fresh (< 3 seconds old)
        - spot data is stale (> 2 seconds old)

        This indicates a latency arbitrage window where perp has moved
        but spot hasn't caught up yet.
        """
        if self.perp_price <= 0 or spot_price <= 0:
            return False

        now = time.time()
        perp_age = now - self.perp_updated
        spot_age = now - spot_updated

        price_diverged = abs(self.perp_price - spot_price) > threshold_usd
        perp_fresh = perp_age < 3.0
        spot_stale = spot_age > 2.0

        return price_diverged and perp_fresh and spot_stale

    def get_lead(self, spot_price: float) -> float:
        """Delegate to compute_perp_lead with current perp price."""
        return compute_perp_lead(self.perp_price, spot_price)

    def get_funding_signal(self) -> float:
        """Delegate to compute_funding_signal with current funding rate."""
        return compute_funding_signal(self.funding_rate)


class BybitFeed:
    """WebSocket consumer for Bybit BTC perpetual futures.

    Subscribes to tickers.BTCUSDT on the v5 linear perpetual stream.
    Updates BybitState with lastPrice, fundingRate, and openInterest
    from WS tick messages — no REST poll needed.
    """

    def __init__(self, ws_url: str = WS_URL) -> None:
        self.ws_url: str = ws_url
        self.state: BybitState = BybitState()
        self._running: bool = False
        self._ws: Any = None
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Launch WebSocket connection."""
        self._running = True
        self._tasks.append(asyncio.create_task(self._connect_ws()))

    async def stop(self) -> None:
        """Cleanly shut down all tasks."""
        self._running = False
        if self._ws:
            await self._ws.close()
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _connect_ws(self) -> None:
        """Persistent WebSocket connection with exponential backoff."""
        import websockets

        backoff = RECONNECT_BASE
        # OI updates ~every 5s; >60s idle is the established staleness gate for L3e,
        # but a 30s recv timeout catches dead streams sooner so reconnect runs before
        # the gate even fires.
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=30) as ws:
                    self._ws = ws
                    backoff = RECONNECT_BASE
                    logger.debug(f"Bybit WebSocket connected: {self.ws_url}")

                    sub_msg = json.dumps({
                        "op": "subscribe",
                        "args": ["tickers.BTCUSDT"],
                    })
                    await ws.send(sub_msg)

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            logger.warning("Bybit WS idle >30s, forcing reconnect")
                            break
                        self._handle_message(json.loads(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Bybit WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Process a Bybit v5 tickers.BTCUSDT message (snapshot or delta).

        Delta messages only contain changed fields — each field is updated
        only when present. openInterest is in the same WS payload, eliminating
        the need for a geo-blocked REST poll.
        """
        if data.get("topic") != "tickers.BTCUSDT":
            return
        ticker = data.get("data", {})
        if not ticker:
            return
        now = time.time()

        last_price = ticker.get("lastPrice")
        if last_price is not None:
            try:
                self.state.perp_price = float(last_price)
                self.state.perp_updated = now
            except (ValueError, TypeError):
                pass

        funding_rate = ticker.get("fundingRate")
        if funding_rate is not None:
            try:
                self.state.funding_rate = float(funding_rate)
                self.state.funding_updated = now
            except (ValueError, TypeError):
                pass

        next_funding = ticker.get("nextFundingTime")
        if next_funding is not None:
            try:
                self.state.next_funding_time = float(next_funding) / 1000.0
            except (ValueError, TypeError):
                pass

        # OI arrives in the WS ticker — no REST needed, no geo-block.
        oi = ticker.get("openInterest")
        if oi is not None:
            try:
                new_oi = float(oi)
                if new_oi > 0 and new_oi != self.state.open_interest:
                    self.state.open_interest_prev = self.state.open_interest
                    self.state.price_at_oi_prev = self.state.price_at_oi
                    self.state.oi_updated_prev = self.state.oi_updated
                    self.state.open_interest = new_oi
                    self.state.price_at_oi = self.state.perp_price
                    self.state.oi_updated = now
            except (ValueError, TypeError):
                pass
