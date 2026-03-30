import numpy as np
from polybot.indicators.ema import compute_ema

def compute_macd(closes: np.ndarray, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> tuple[float, float, float]:
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    fast_ema = compute_ema(closes, fast)
    slow_ema = compute_ema(closes, slow)
    macd_line = fast_ema - slow_ema
    signal_line = compute_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1])

def compute_macd_signal(closes: np.ndarray, fast: int = 12, slow: int = 26,
                        signal_period: int = 9) -> dict:
    if len(closes) < slow + signal_period:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "score": 0.0}
    macd_val, sig_val, hist = compute_macd(closes, fast, slow, signal_period)
    price_range = float(np.max(closes[-slow:]) - np.min(closes[-slow:]))
    if price_range == 0:
        score = 0.0
    else:
        score = hist / price_range * 5.0
    score = max(-1.0, min(1.0, score))
    return {"macd": round(macd_val, 4), "signal": round(sig_val, 4),
            "histogram": round(hist, 4), "score": round(score, 4)}
