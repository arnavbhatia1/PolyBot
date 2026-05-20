"""L3e liquidation pressure from OI drop × price direction.

OI rising = new positions, not liquidations → return 0.
OI dropping + price dropping = long liquidations (bearish).
OI dropping + price rising = short liquidations (bullish).
"""

from __future__ import annotations
import math


# Normalizes OI delta to %/minute so 5s and 60s snapshots produce comparable signals.
_OI_DROP_PER_MIN_K = 8.0


def compute_liquidation_pressure(
    oi_current: float,
    oi_previous: float,
    price_current: float,
    price_previous: float,
    elapsed_seconds: float = 1.0,
) -> float:
    """Estimate liquidation pressure ∈ [-1, 1]. Sign = price direction."""
    if oi_previous <= 0 or price_previous == 0:
        return 0.0

    oi_change_pct = (oi_current - oi_previous) / oi_previous
    if oi_change_pct >= 0:
        return 0.0

    elapsed = max(1.0, float(elapsed_seconds))
    oi_drop_per_min = abs(oi_change_pct) * (60.0 / elapsed)

    price_change = price_current - price_previous
    if price_change == 0:
        return 0.0

    direction = 1.0 if price_change > 0 else -1.0
    return math.tanh(direction * oi_drop_per_min * _OI_DROP_PER_MIN_K)
