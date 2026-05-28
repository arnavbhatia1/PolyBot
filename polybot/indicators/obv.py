from __future__ import annotations

import math
import numpy as np


def compute_obv(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    """Windowed-cumulative signed volume — NOT true session-start OBV.

    True OBV is a running cumulative from listing/session start. This routine
    resets to 0 at the start of the supplied closes window. That's fine here
    because the only downstream consumer (compute_obv_signal) uses the SLOPE
    over `slope_period`, which is window-anchor-invariant. Treat the array's
    absolute level as meaningless; only deltas matter.
    """
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

# Typical 1-minute Binance BTC volume scale — tanh saturates around this so OBV
# slope reads carry graded magnitude information across the practical range.
_OBV_VOLUME_SCALE = 30.0


def compute_obv_signal(closes: np.ndarray, volumes: np.ndarray, slope_period: int = 5) -> dict[str, float]:
    if len(closes) < slope_period + 1:
        return {"obv_slope": 0.0, "price_slope": 0.0, "score": 0.0}
    obv = compute_obv(closes, volumes)
    # Slope = ΔY / ΔX with ΔX = (slope_period − 1) periods between endpoints.
    span = max(1, slope_period - 1)
    obv_slope = float(obv[-1] - obv[-slope_period]) / span
    price_slope = float(closes[-1] - closes[-slope_period]) / span
    mag = math.tanh(abs(obv_slope) / _OBV_VOLUME_SCALE)
    if obv_slope == 0:
        score = 0.0
    elif (obv_slope > 0) == (price_slope > 0):
        # Confirmation: agree direction.
        score = mag if obv_slope > 0 else -mag
    else:
        # Divergence — volume leading the opposite direction (leading signal, half weight).
        score = 0.5 * (mag if obv_slope > 0 else -mag)
    return {"obv_slope": round(obv_slope, 2), "price_slope": round(price_slope, 4), "score": round(score, 4)}
