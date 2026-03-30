import numpy as np

def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def compute_rsi_signal(closes: np.ndarray, period: int = 14,
                       overbought: float = 70, oversold: float = 30) -> dict:
    if len(closes) < period + 1:
        return {"rsi": 50.0, "score": 0.0}
    rsi = compute_rsi(closes, period)
    if rsi >= overbought:
        score = -((rsi - overbought) / (100 - overbought))
    elif rsi <= oversold:
        score = (oversold - rsi) / oversold
    else:
        mid = (overbought + oversold) / 2
        score = -(rsi - mid) / (overbought - mid) * 0.3
    return {"rsi": round(rsi, 2), "score": round(max(-1.0, min(1.0, score)), 4)}
