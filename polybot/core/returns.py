from __future__ import annotations

import math
import numpy as np

def log_return(entry_price: float, exit_price: float) -> float:
    if exit_price <= 0 or entry_price <= 0:
        return -10.0  # Sentinel for total loss; avoids math.log(0) = -inf.
    return math.log(exit_price / entry_price)

def lag1_autocorr(closes: np.ndarray, lookback: int) -> float:
    """Shared by signal_engine L2 and RegimeDetector — prevents drift between callers."""
    if len(closes) < lookback + 2:
        return 0.0
    window = closes[-(lookback + 1):]
    denom = window[:-1]
    if np.any(denom <= 0):
        return 0.0
    returns = np.diff(window) / denom
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
    if corr != corr:  # NaN guard
        return 0.0
    return max(-1.0, min(1.0, corr))
