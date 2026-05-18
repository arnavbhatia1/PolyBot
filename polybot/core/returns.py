"""Return calculation utilities.

Uses arithmetic gain_pct (pnl / size) throughout — never log returns.
log(0) = -inf for total losses, which breaks Sharpe and other statistics.
"""
from __future__ import annotations

import math
import numpy as np

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

def lag1_autocorr(closes: np.ndarray, lookback: int) -> float:
    """1-lag Pearson autocorrelation of recent percent returns.

    Single source of truth used by both signal_engine (L2) and the regime
    detector — eliminates implementation drift between the two call sites.
    Returns 0.0 when data is insufficient, variance is zero, or result is NaN.
    """
    if len(closes) < lookback + 2:
        return 0.0
    window = closes[-(lookback + 1):]
    returns = np.diff(window) / window[:-1]
    if len(returns) < 6:
        return 0.0
    r1 = returns[:-1]
    r2 = returns[1:]
    d1 = r1 - r1.mean()
    d2 = r2 - r2.mean()
    var1 = float((d1 * d1).sum())
    var2 = float((d2 * d2).sum())
    if var1 <= 0 or var2 <= 0:
        return 0.0
    corr = float((d1 * d2).sum()) / math.sqrt(var1 * var2)
    if corr != corr:  # NaN
        return 0.0
    return max(-1.0, min(1.0, corr))
