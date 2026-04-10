"""Bankroll acceleration: dynamic Kelly fraction based on track record.

As the bot proves a positive win rate over more trades, Kelly ratchets up.
If win rate drops, it drops back to the base tier. Tiers are checked from
highest to lowest -- first match wins.
"""

KELLY_TIERS: list[tuple[int, float, float]] = [
    # (min_trades, min_win_rate, kelly_fraction)
    (500, 0.57, 0.25),
    (250, 0.56, 0.22),
    (100, 0.55, 0.18),
]


def compute_kelly_tier(trade_count: int, win_rate: float, base_kelly: float = 0.15) -> float:
    """Check tiers from highest to lowest. Return first where both count and win rate meet requirements."""
    for min_trades, min_wr, kelly in KELLY_TIERS:
        if trade_count >= min_trades and win_rate >= min_wr:
            return kelly
    return base_kelly
