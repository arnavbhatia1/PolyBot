"""Binance L2 depth feed with wall detection and spot imbalance.

Provides three pure functions for order book analysis:
1. compute_spot_imbalance — bid/ask volume ratio [-1, +1]
2. compute_wall_pressure — sell vs buy walls near strike [-1, +1]
3. compute_depth_usd — total USD value in top N levels

And one class (BinanceDepthFeed) that manages WebSocket + REST connections
to stream real-time L2 depth data from Binance.US.

Order book levels are Binance format: [price_str, qty_str] string arrays.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def compute_spot_imbalance(bids: list[list[str]], asks: list[list[str]]) -> float:
    """Compute bid/ask volume imbalance from order book levels.

    Args:
        bids: List of [price_str, qty_str] bid levels (Binance format).
        asks: List of [price_str, qty_str] ask levels (Binance format).

    Returns:
        Float from -1 (ask-heavy / bearish) to +1 (bid-heavy / bullish).
        Returns 0.0 for empty books.
    """
    bid_vol = sum(float(level[1]) for level in bids)
    ask_vol = sum(float(level[1]) for level in asks)
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (bid_vol - ask_vol) / total))


def compute_wall_pressure(
    bids: list[list[str]],
    asks: list[list[str]],
    strike: float,
    btc_price: float,
    pct_range: float = 0.001,
) -> float:
    """Measure sell vs buy volume in the zone between price and strike.

    Detects walls (large resting orders) that could resist price movement
    toward or through the strike. Useful for gauging whether BTC can break
    through the strike level.

    Args:
        bids: List of [price_str, qty_str] bid levels.
        asks: List of [price_str, qty_str] ask levels.
        strike: The 5-min contract strike price.
        btc_price: Current BTC spot price.
        pct_range: Fractional range around strike to scan (default 0.1%).

    Returns:
        Float from -1 (support wall below strike, bullish for Up) to
        +1 (resistance wall above strike, bearish for Up).
        Returns 0.0 if no volume in the zone.

    The zone is defined as [btc_price, strike +/- pct_range*strike] when
    price is below strike, or [strike -/- pct_range*strike, btc_price] when
    price is above strike. We scan both sides of the strike and compute
    net resistance vs support.
    """
    spread = strike * pct_range
    zone_low = strike - spread
    zone_high = strike + spread

    # Sell volume in the zone (resistance above) from asks
    sell_vol = 0.0
    for level in asks:
        price = float(level[0])
        qty = float(level[1])
        if zone_low <= price <= zone_high:
            sell_vol += qty

    # Buy volume in the zone (support below) from bids
    buy_vol = 0.0
    for level in bids:
        price = float(level[0])
        qty = float(level[1])
        if zone_low <= price <= zone_high:
            buy_vol += qty

    total = sell_vol + buy_vol
    if total == 0:
        return 0.0

    # Positive = net sell wall (resistance, bearish for Up)
    # Negative = net buy wall (support, bullish for Up)
    return max(-1.0, min(1.0, (sell_vol - buy_vol) / total))


def compute_depth_usd(
    bids: list[list[str]],
    asks: list[list[str]],
    levels: int = 20,
) -> float:
    """Compute total USD value in top N levels of bids and asks.

    Args:
        bids: List of [price_str, qty_str] bid levels (best first).
        asks: List of [price_str, qty_str] ask levels (best first).
        levels: Number of levels to include from each side.

    Returns:
        Total USD depth (sum of price * qty for top N bids + top N asks).
    """
    total = 0.0
    for level in bids[:levels]:
        total += float(level[0]) * float(level[1])
    for level in asks[:levels]:
        total += float(level[0]) * float(level[1])
    return total


# ---------------------------------------------------------------------------
# BinanceDepthFeed class
# ---------------------------------------------------------------------------

class BinanceDepthFeed:
    """Manages WebSocket + REST connections for Binance L2 order book depth.

    Two data sources:
    - WebSocket ``btcusdt@depth20@100ms``: top 20 levels, updated every 100ms.
      Stored in ``top_bids`` / ``top_asks``.
    - REST ``GET /api/v3/depth?limit=1000``: full 1000-level snapshot every 5s.
      Stored in ``full_bids`` / ``full_asks``.

    Usage::

        feed = BinanceDepthFeed()
        await feed.start()
        imb = feed.get_imbalance()              # uses top_bids/top_asks
        wall = feed.get_wall_pressure(strike, btc_price)  # uses full book
        depth = feed.get_depth_usd()            # uses top_bids/top_asks
        await feed.stop()
    """

    def __init__(
        self,
        symbol: str = "btcusdt",
        ws_url: str = "wss://stream.binance.us:9443/ws",
        rest_url: str = "https://api.binance.us/api/v3",
        rest_interval: float = 5.0,
    ) -> None:
        self.symbol = symbol
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.rest_interval = rest_interval

        # Top-of-book from WS (depth20)
        self.top_bids: list[list[str]] = []
        self.top_asks: list[list[str]] = []

        # Full book from REST
        self.full_bids: list[list[str]] = []
        self.full_asks: list[list[str]] = []

        self._running: bool = False
        self._ws: Any = None
        self._ws_task: asyncio.Task | None = None
        self._rest_task: asyncio.Task | None = None

    # -- public getters using pure functions --------------------------------

    def get_imbalance(self) -> float:
        """Spot bid/ask imbalance from top-of-book WS data."""
        return compute_spot_imbalance(self.top_bids, self.top_asks)

    def get_wall_pressure(self, strike: float, btc_price: float, pct_range: float = 0.001) -> float:
        """Wall pressure near strike from full REST book."""
        book_bids = self.full_bids if self.full_bids else self.top_bids
        book_asks = self.full_asks if self.full_asks else self.top_asks
        return compute_wall_pressure(book_bids, book_asks, strike, btc_price, pct_range)

    def get_depth_usd(self, levels: int = 20) -> float:
        """Total USD depth from top-of-book WS data."""
        return compute_depth_usd(self.top_bids, self.top_asks, levels)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start WebSocket and REST polling loops."""
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._rest_task = asyncio.create_task(self._rest_loop())
        logger.debug("BinanceDepthFeed started")

    async def stop(self) -> None:
        """Stop all background tasks and close connections."""
        self._running = False
        if self._ws:
            await self._ws.close()
        for task in (self._ws_task, self._rest_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.debug("BinanceDepthFeed stopped")

    # -- WebSocket loop (depth20 @ 100ms) -----------------------------------

    async def _ws_loop(self) -> None:
        import websockets

        stream = f"{self.ws_url}/{self.symbol}@depth20@100ms"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(stream) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.debug(f"Binance depth WS connected: {stream}")
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_depth_msg(json.loads(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Depth WS error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_depth_msg(self, data: dict[str, Any]) -> None:
        """Parse depth20 WS message: {"bids": [["price","qty"],...], "asks": [...]}."""
        bids = data.get("bids")
        asks = data.get("asks")
        if bids is not None:
            self.top_bids = bids
        if asks is not None:
            self.top_asks = asks

    # -- REST loop (full depth every N seconds) -----------------------------

    async def _rest_loop(self) -> None:
        url = f"{self.rest_url}/depth"
        params = {"symbol": self.symbol.upper(), "limit": 1000}
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    self.full_bids = data.get("bids", [])
                    self.full_asks = data.get("asks", [])
                    logger.debug(f"REST depth snapshot: {len(self.full_bids)} bids, {len(self.full_asks)} asks")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"REST depth fetch failed: {e}")
            try:
                await asyncio.sleep(self.rest_interval)
            except asyncio.CancelledError:
                break
