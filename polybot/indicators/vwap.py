from __future__ import annotations

import numpy as np


def compute_vwap(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> float:
    if len(closes) < 2 or np.sum(volumes) == 0:
        return float(closes[-1]) if len(closes) > 0 else 0.0
    typical_price = (highs + lows + closes) / 3.0
    return float(np.sum(typical_price * volumes) / np.sum(volumes))

def compute_vwap_signal(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> dict[str, float]:
    if len(closes) < 3:
        return {"vwap": 0.0, "deviation": 0.0, "score": 0.0}
    vwap = compute_vwap(highs, lows, closes, volumes)
    price = float(closes[-1])
    typical = (highs + lows + closes) / 3.0
    # Volume-weighted std: heavy-volume candles dominate deviation, not outlier thin candles
    total_vol = np.sum(volumes)
    # Frequency-weighted sample std with Bessel correction (n_eff − 1 in the denominator).
    n_eff = max(2, len(typical))
    bessel = total_vol * (n_eff - 1) / n_eff
    if len(typical) > 1 and bessel > 0:
        std = float(np.sqrt(np.sum(volumes * (typical - vwap) ** 2) / bessel))
    else:
        std = 1.0
    if std == 0:
        std = 1.0
    deviation = (price - vwap) / std
    score = -deviation * 0.3
    score = max(-1.0, min(1.0, score))
    return {"vwap": round(vwap, 2), "deviation": round(deviation, 4), "score": round(score, 4)}
