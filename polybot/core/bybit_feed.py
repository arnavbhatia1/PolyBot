"""Bybit BTC perpetual futures feed.

Provides three signals from leveraged futures market data:
1. Price lead — perp vs spot divergence (leveraged traders react first)
2. Funding rate — contrarian crowding indicator
3. Staleness detection — spot lagging perp indicates latency arbitrage window

Pure functions for computation, BybitState dataclass for state,
BybitFeed class for WebSocket consumption.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Bybit public linear perpetual WebSocket
WS_URL = "wss://stream.bybit.com/v5/public/linear"
REST_URL = "https://api.bybit.com/v5/market/tickers"
FUNDING_POLL_INTERVAL = 300  # seconds — REST backup for funding rate
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

    Subscribes to tickers.BTCUSDT on the linear perpetual stream.
    Updates BybitState with lastPrice and fundingRate from tick messages.
    REST polls funding rate every 300s as backup.
    """

    def __init__(self, ws_url: str = WS_URL, rest_url: str = REST_URL) -> None:
        self.ws_url: str = ws_url
        self.rest_url: str = rest_url
        self.state: BybitState = BybitState()
        self._running: bool = False
        self._ws: Any = None
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Launch WebSocket connection and REST polling as background tasks."""
        self._running = True
        self._tasks.append(asyncio.create_task(self._connect_ws()))
        self._tasks.append(asyncio.create_task(self._poll_funding()))

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
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    backoff = RECONNECT_BASE
                    logger.debug(f"Bybit WebSocket connected: {self.ws_url}")

                    # Subscribe to BTCUSDT perpetual tickers
                    sub_msg = json.dumps({
                        "op": "subscribe",
                        "args": ["tickers.BTCUSDT"],
                    })
                    await ws.send(sub_msg)

                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(json.loads(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Bybit WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Process a Bybit tickers message.

        Expected format:
        {
            "topic": "tickers.BTCUSDT",
            "type": "snapshot" | "delta",
            "data": {
                "lastPrice": "73050.00",
                "fundingRate": "0.0001",
                "nextFundingTime": "1712793600000",
                ...
            }
        }
        """
        topic = data.get("topic", "")
        if topic != "tickers.BTCUSDT":
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

    async def _poll_funding(self) -> None:
        """REST backup: poll funding rate every FUNDING_POLL_INTERVAL seconds."""
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self.rest_url}/tickers",
                        params={"category": "linear", "symbol": "BTCUSDT"},
                    )
                    resp.raise_for_status()
                    result = resp.json().get("result", {})
                    items = result.get("list", [])
                    if items:
                        ticker = items[0]
                        funding = ticker.get("fundingRate")
                        if funding is not None:
                            self.state.funding_rate = float(funding)
                            self.state.funding_updated = time.time()
                        next_ft = ticker.get("nextFundingTime")
                        if next_ft is not None:
                            self.state.next_funding_time = float(next_ft) / 1000.0
                        logger.debug(
                            f"Bybit REST funding: rate={self.state.funding_rate}, "
                            f"next={self.state.next_funding_time}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Bybit REST funding poll failed: {e}")

            try:
                await asyncio.sleep(FUNDING_POLL_INTERVAL)
            except asyncio.CancelledError:
                break
