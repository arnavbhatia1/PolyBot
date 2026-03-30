import numpy as np

def compute_ema(closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) < period:
        return closes.copy()
    alpha = 2.0 / (period + 1)
    ema = np.empty_like(closes)
    ema[0] = closes[0]
    for i in range(1, len(closes)):
        ema[i] = alpha * closes[i] + (1 - alpha) * ema[i - 1]
    return ema

def compute_ema_signal(closes: np.ndarray, fast_period: int = 9, slow_period: int = 21,
                       chop_threshold: float = 0.001) -> dict:
    if len(closes) < slow_period + 1:
        return {"trend": "insufficient_data", "fast_ema": 0.0, "slow_ema": 0.0}
    fast = compute_ema(closes, fast_period)
    slow = compute_ema(closes, slow_period)
    fast_val = float(fast[-1])
    slow_val = float(slow[-1])
    mid = (fast_val + slow_val) / 2.0
    if mid == 0:
        return {"trend": "chop", "fast_ema": fast_val, "slow_ema": slow_val}
    diff_pct = abs(fast_val - slow_val) / mid
    if diff_pct < chop_threshold:
        trend = "chop"
    elif fast_val > slow_val:
        trend = "bullish"
    else:
        trend = "bearish"
    return {"trend": trend, "fast_ema": fast_val, "slow_ema": slow_val}
