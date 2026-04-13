"""Bankroll acceleration: dynamic Kelly fraction based on track record.

As the bot proves a positive win rate over more trades, Kelly ratchets up.
If win rate drops, it drops back to the base tier. Uses Wilson score 95%
lower bound (not point estimate) to avoid ratcheting on luck.
"""

import math

KELLY_TIERS: list[tuple[int, float, float]] = [
    # (min_trades, min_win_rate_lower_bound, kelly_fraction)
    (750, 0.57, 0.25),
    (400, 0.56, 0.22),
    (200, 0.55, 0.18),
]


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


def compute_kelly_tier(trade_count: int, win_rate: float, base_kelly: float = 0.15) -> float:
    """Check tiers from highest to lowest using Wilson score lower bound.

    The observed win_rate is NOT used directly — the 95% CI lower bound is.
    This means you need both enough trades AND a convincingly high win rate
    to ratchet up. At 200 trades with 60% WR, Wilson lower = 53% < 55% = no ratchet.
    At 400 trades with 60% WR, Wilson lower = 55% >= 55% = ratchet to 0.22.
    """
    if trade_count <= 0:
        return base_kelly
    wlb = _wilson_lower(win_rate, trade_count)
    for min_trades, min_wr, kelly in KELLY_TIERS:
        if trade_count >= min_trades and wlb >= min_wr:
            return kelly
    return base_kelly
