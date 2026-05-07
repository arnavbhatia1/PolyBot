"""Return calculation utilities.

Uses arithmetic gain_pct (pnl / size) throughout — never log returns.
log(0) = -inf for total losses, which breaks Sharpe and other statistics.
"""
from __future__ import annotations

import math


def log_return(entry_price: float, exit_price: float) -> float:
    if exit_price <= 0 or entry_price <= 0:
        return -10.0  # Total loss in binary market (avoids math.log(0))
    return math.log(exit_price / entry_price)


def gain_pct(entry_price: float, exit_price: float) -> float:
    """Arithmetic return for binary outcomes: (exit - entry) / entry.

    Bounded [-1, +inf) — correct metric for binary options where
    log returns are undefined at exit_price=0.
    """
    if entry_price <= 0:
        return 0.0
    return (exit_price - entry_price) / entry_price
