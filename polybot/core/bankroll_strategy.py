"""Bankroll acceleration: dynamic Kelly fraction based on track record.

As the bot proves a positive win rate over more trades, Kelly ratchets up.
If win rate drops, it drops back to the base tier. Uses Wilson score 95%
lower bound (not point estimate) to avoid ratcheting on luck.

Drawdown velocity trigger: if drawdown exceeds 15% in rolling 25 trades,
immediately drops to base Kelly regardless of win rate. This catches
regime changes 20-30 trades faster than Wilson score alone.
"""

import math
from collections import deque

KELLY_TIERS: list[tuple[int, float, float]] = [
    # (min_trades, min_win_rate_lower_bound, kelly_fraction)
    (750, 0.57, 0.25),
    (400, 0.56, 0.22),
    (200, 0.55, 0.18),
]

# Drawdown velocity: if PnL drops this much in DRAWDOWN_WINDOW trades, force base Kelly
DRAWDOWN_VELOCITY_PCT = 0.15   # 15% drawdown
DRAWDOWN_WINDOW = 25           # in this many trades


def _wilson_lower(p: float, n: int, z: float = 1.96) -> float:
    """Wilson score 95% confidence interval lower bound.

    More conservative than point estimate — at 200 trades with 60% WR,
    lower bound is ~53%, not 60%. Prevents ratcheting on luck.
    """
    if n <= 0:
        return 0.0
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - spread) / denom


class DrawdownVelocityTracker:
    """Tracks rolling PnL to detect fast drawdowns.

    If cumulative gain_pct over the last DRAWDOWN_WINDOW trades drops below
    -DRAWDOWN_VELOCITY_PCT, signals that Kelly should be forced to base tier.
    This catches regime changes 20-30 trades faster than Wilson score.
    """

    def __init__(self, window: int = DRAWDOWN_WINDOW,
                 threshold: float = DRAWDOWN_VELOCITY_PCT) -> None:
        self.window = window
        self.threshold = threshold
        self._gains: deque[float] = deque(maxlen=window)

    def record_trade(self, gain_pct: float) -> None:
        """Record a trade's gain_pct (e.g., +0.30 for 30% win, -1.0 for total loss)."""
        self._gains.append(gain_pct)

    def is_velocity_breach(self) -> bool:
        """True if rolling drawdown exceeds threshold.

        Computes: sum of gain_pct over last N trades.
        If sum < -threshold, we're in a fast drawdown.
        """
        if len(self._gains) < 10:  # need minimum 10 trades
            return False
        rolling_pnl = sum(self._gains)
        return rolling_pnl < -self.threshold

    @property
    def rolling_pnl(self) -> float:
        """Current rolling PnL sum over the window."""
        return sum(self._gains) if self._gains else 0.0


def compute_kelly_tier(trade_count: int, win_rate: float, base_kelly: float = 0.15,
                       drawdown_breach: bool = False) -> float:
    """Check tiers from highest to lowest using Wilson score lower bound.

    The observed win_rate is NOT used directly — the 95% CI lower bound is.
    If drawdown_breach is True, forces base_kelly regardless of stats.
    """
    if drawdown_breach:
        return base_kelly
    if trade_count <= 0:
        return base_kelly
    wlb = _wilson_lower(win_rate, trade_count)
    for min_trades, min_wr, kelly in KELLY_TIERS:
        if trade_count >= min_trades and wlb >= min_wr:
            return kelly
    return base_kelly


def compute_uncertainty_discount(trade_count: int, avg_edge: float) -> float:
    """Uncertainty discount on Kelly: accounts for edge estimation error.

    f* = f_kelly * (1 - sigma_edge^2 / edge^2)
    sigma_edge = 0.50 / sqrt(N)  (binary outcome std dev)

    Floor at 0.50 (never discount more than 50%) to prevent the multiplier chain
    from compounding to near-zero sizing. At 100 trades with 6% edge the raw
    formula gives 0.31, but the floor keeps it at 0.50.
    At 500 trades: ~0.87. At 1000 trades: ~0.97.
    """
    if trade_count <= 0 or avg_edge <= 0:
        return 0.50  # minimum 50% of Kelly when no data
    sigma_edge = 0.50 / math.sqrt(trade_count)
    ratio = (sigma_edge * sigma_edge) / (avg_edge * avg_edge)
    discount = max(0.50, 1.0 - ratio)  # floor at 50% of Kelly
    return min(1.0, discount)
