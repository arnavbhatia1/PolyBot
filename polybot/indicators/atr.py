import numpy as np

def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    return float(np.mean(tr[-period:]))

def compute_atr_gate(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                     period: int = 14, low_pct: int = 25, high_pct: int = 90, history: int = 100) -> dict:
    if len(closes) < period + 2:
        return {"atr": 0.0, "passes": False, "reason": "insufficient_data"}
    atr_current = compute_atr(highs, lows, closes, period)
    n = min(history, len(closes) - period)
    atr_history = []
    for i in range(n):
        end = len(closes) - i
        if end < period + 1:
            break
        atr_history.append(compute_atr(highs[:end], lows[:end], closes[:end], period))
    if not atr_history:
        return {"atr": atr_current, "passes": False, "reason": "no_history"}
    low_thresh = float(np.percentile(atr_history, low_pct))
    high_thresh = float(np.percentile(atr_history, high_pct))
    if atr_current == 0.0:
        return {"atr": round(atr_current, 2), "passes": False, "reason": "too_quiet"}
    if atr_current < low_thresh:
        return {"atr": round(atr_current, 2), "passes": False, "reason": "too_quiet"}
    if atr_current > high_thresh:
        return {"atr": round(atr_current, 2), "passes": False, "reason": "too_volatile"}
    return {"atr": round(atr_current, 2), "passes": True, "reason": "ok"}
