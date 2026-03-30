import numpy as np

def compute_obv(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    if len(closes) < 2:
        return np.array([0.0])
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    return obv

def compute_obv_signal(closes: np.ndarray, volumes: np.ndarray, slope_period: int = 5) -> dict:
    if len(closes) < slope_period + 1:
        return {"obv_slope": 0.0, "price_slope": 0.0, "score": 0.0}
    obv = compute_obv(closes, volumes)
    obv_slope = float(obv[-1] - obv[-slope_period]) / slope_period
    price_slope = float(closes[-1] - closes[-slope_period]) / slope_period
    if obv_slope == 0:
        score = 0.0
    elif (obv_slope > 0 and price_slope > 0):
        score = min(1.0, abs(obv_slope) / (abs(obv_slope) + 1))
    elif (obv_slope < 0 and price_slope < 0):
        score = -min(1.0, abs(obv_slope) / (abs(obv_slope) + 1))
    else:
        score = 0.0
    return {"obv_slope": round(obv_slope, 2), "price_slope": round(price_slope, 4), "score": round(score, 4)}
