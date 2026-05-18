"""Liquidation pressure estimation from open interest changes.

When OI drops while price moves, it signals forced liquidations:
- OI drop + price drop = long liquidations (bearish pressure)
- OI drop + price rise = short liquidations (bullish pressure)
- OI rising or flat = new positions opening, not liquidations
"""

from __future__ import annotations
import math
from typing import Optional


# OI delta is normalized to %/minute so 2 snapshots 5s apart and 60s apart produce comparable signals.
_OI_DROP_PER_MIN_K = 8.0

def compute_liquidation_pressure(
    oi_current: float,
    oi_previous: float,
    price_current: float,
    price_previous: float,
    elapsed_seconds: float = 1.0,
) -> float:
    """Estimate liquidation pressure from OI and price changes.

    `elapsed_seconds` is the gap between the two OI snapshots. Without this,
    a 5% drop in 5s and a 5% drop in 60s produced identical signals — the
    former is a panic cascade, the latter is a slow grind.

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

    elapsed = max(1.0, float(elapsed_seconds))
    oi_drop_per_min = abs(oi_change_pct) * (60.0 / elapsed)

    price_change = price_current - price_previous
    if price_previous == 0:
        return 0.0

    # price drop with OI drop = long liquidation (negative)
    # price rise with OI drop = short liquidation (positive)
    if price_change == 0:
        return 0.0

    direction = 1.0 if price_change > 0 else -1.0

    return math.tanh(direction * oi_drop_per_min * _OI_DROP_PER_MIN_K)


class LiquidationTracker:
    """Tracks OI and price snapshots to compute liquidation pressure."""

    def __init__(self) -> None:
        self._oi_prev: Optional[float] = None
        self._price_prev: Optional[float] = None
        self._ts_prev: Optional[float] = None
        self._oi_current: Optional[float] = None
        self._price_current: Optional[float] = None
        self._ts_current: Optional[float] = None

    def update(self, oi: float, price: float, ts: float) -> None:
        """Record a new OI/price snapshot, shifting current to previous."""
        self._oi_prev = self._oi_current
        self._price_prev = self._price_current
        self._ts_prev = self._ts_current
        self._oi_current = oi
        self._price_current = price
        self._ts_current = ts

    def get_pressure(self) -> float:
        """Return current liquidation pressure estimate."""
        if (
            self._oi_prev is None
            or self._price_prev is None
            or self._oi_current is None
            or self._price_current is None
            or self._ts_prev is None
            or self._ts_current is None
        ):
            return 0.0

        return compute_liquidation_pressure(
            oi_current=self._oi_current,
            oi_previous=self._oi_prev,
            price_current=self._price_current,
            price_previous=self._price_prev,
            elapsed_seconds=self._ts_current - self._ts_prev,
        )
