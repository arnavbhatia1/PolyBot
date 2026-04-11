"""Liquidation pressure estimation from open interest changes.

When OI drops while price moves, it signals forced liquidations:
- OI drop + price drop = long liquidations (bearish pressure)
- OI drop + price rise = short liquidations (bullish pressure)
- OI rising or flat = new positions opening, not liquidations
"""

from __future__ import annotations

import math
from typing import Optional


def compute_liquidation_pressure(
    oi_current: float,
    oi_previous: float,
    price_current: float,
    price_previous: float,
) -> float:
    """Estimate liquidation pressure from OI and price changes.

    Returns a value in [-1, 1]:
        < 0: long liquidations (bearish)
        > 0: short liquidations (bullish)
        0.0: no liquidation signal (OI rising/flat or no price movement)
    """
    if oi_previous <= 0:
        return 0.0

    oi_change_pct = (oi_current - oi_previous) / oi_previous

    # OI rising or flat means new positions, not liquidations
    if oi_change_pct >= 0:
        return 0.0

    oi_drop_pct = abs(oi_change_pct)

    price_change = price_current - price_previous
    if price_previous == 0:
        return 0.0

    # Direction: price drop with OI drop = long liquidation (negative)
    #            price rise with OI drop = short liquidation (positive)
    if price_change == 0:
        return 0.0

    direction = 1.0 if price_change > 0 else -1.0

    return math.tanh(direction * oi_drop_pct * 20)


class LiquidationTracker:
    """Tracks OI and price snapshots to compute liquidation pressure."""

    def __init__(self) -> None:
        self._oi_prev: Optional[float] = None
        self._price_prev: Optional[float] = None
        self._oi_current: Optional[float] = None
        self._price_current: Optional[float] = None

    def update(self, oi: float, price: float, ts: float) -> None:
        """Record a new OI/price snapshot, shifting current to previous."""
        self._oi_prev = self._oi_current
        self._price_prev = self._price_current
        self._oi_current = oi
        self._price_current = price

    def get_pressure(self) -> float:
        """Return current liquidation pressure estimate.

        Returns 0.0 if fewer than 2 snapshots have been recorded.
        """
        if (
            self._oi_prev is None
            or self._price_prev is None
            or self._oi_current is None
            or self._price_current is None
        ):
            return 0.0

        return compute_liquidation_pressure(
            oi_current=self._oi_current,
            oi_previous=self._oi_prev,
            price_current=self._price_current,
            price_previous=self._price_prev,
        )
