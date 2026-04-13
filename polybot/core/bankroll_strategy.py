"""Bankroll strategy: uncertainty-adjusted Kelly + drawdown velocity protection.

Simplified from tier-based ratcheting to two clean mechanisms:
1. Uncertainty discount: f* = f_kelly × (1 - σ²/edge²), floor 0.50
2. Drawdown velocity: if rolling 25-trade PnL drops below -15%, force base Kelly
"""

import math
from collections import deque

DRAWDOWN_VELOCITY_PCT = 0.15
DRAWDOWN_WINDOW = 25


def _wilson_lower(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score 95% confidence interval lower bound."""
    if n <= 0:
        return 0.0
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - spread) / denom


class DrawdownVelocityTracker:
    """Tracks rolling PnL to detect fast drawdowns.

    If cumulative gain_pct over the last DRAWDOWN_WINDOW trades drops below
    -DRAWDOWN_VELOCITY_PCT, signals that Kelly should be forced to base.
    """

    def __init__(self, window: int = DRAWDOWN_WINDOW,
                 threshold: float = DRAWDOWN_VELOCITY_PCT) -> None:
        self.window = window
        self.threshold = threshold
        self._gains: deque[float] = deque(maxlen=window)

    def record_trade(self, gain_pct: float) -> None:
        self._gains.append(gain_pct)

    def is_velocity_breach(self) -> bool:
        if len(self._gains) < 10:
            return False
        return sum(self._gains) < -self.threshold

    @property
    def rolling_pnl(self) -> float:
        return sum(self._gains) if self._gains else 0.0


def compute_uncertainty_discount(trade_count: int, avg_edge: float) -> float:
    """Uncertainty discount on Kelly: f* = f_kelly × (1 - σ²/edge²).

    Floor at 0.50 (never discount more than 50%).
    At 100 trades with 6% edge: floor keeps at 0.50.
    At 500 trades: ~0.87. At 1000 trades: ~0.97.
    """
    if trade_count <= 0 or avg_edge <= 0:
        return 0.50
    sigma_edge = 0.50 / math.sqrt(trade_count)
    ratio = (sigma_edge * sigma_edge) / (avg_edge * avg_edge)
    discount = max(0.50, 1.0 - ratio)
    return min(1.0, discount)
