from __future__ import annotations

import numpy as np


def compute_stochastic(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                       k_period: int = 14, d_smoothing: int = 3) -> tuple[float, float]:
    if len(closes) < k_period + d_smoothing:
        return 50.0, 50.0
    k_values = []
    for i in range(k_period - 1, len(closes)):
        h = np.max(highs[i - k_period + 1:i + 1])
        l = np.min(lows[i - k_period + 1:i + 1])
        if h == l:
            k_values.append(50.0)
        else:
            k_values.append(((closes[i] - l) / (h - l)) * 100)
    k_arr = np.array(k_values)
    d_val = float(np.mean(k_arr[-d_smoothing:])) if len(k_arr) >= d_smoothing else float(k_arr[-1])
    return float(k_arr[-1]), d_val

def compute_stochastic_signal(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                              k_period: int = 14, d_smoothing: int = 3,
                              overbought: float = 80, oversold: float = 20) -> dict[str, float]:
    if len(closes) < k_period + d_smoothing:
        return {"k": 50.0, "d": 50.0, "score": 0.0}
    k, d = compute_stochastic(highs, lows, closes, k_period, d_smoothing)
    mid = (overbought + oversold) / 2.0
    if k >= overbought:
        # Continuous: -0.3 at boundary → -1.0 at 100
        base = 0.3
        extra = (k - overbought) / (100 - overbought) * (1.0 - base)
        score = -(base + extra)
    elif k <= oversold:
        # Continuous: +0.3 at boundary → +1.0 at 0
        base = 0.3
        extra = (oversold - k) / oversold * (1.0 - base)
        score = base + extra
    else:
        # Linear in neutral zone: 0 at mid, ±0.3 at boundaries
        score = -(k - mid) / (overbought - mid) * 0.3
    return {"k": round(k, 2), "d": round(d, 2), "score": round(max(-1.0, min(1.0, score)), 4)}
