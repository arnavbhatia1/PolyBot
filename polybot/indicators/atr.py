from __future__ import annotations

import numpy as np


def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    if len(tr) < period:
        return 0.0
    # Wilder's EMA (RMA): seed with SMA, then exponential smoothing (alpha=1/period)
    # Responds faster to vol spikes than SMA — critical for 5-min windows
    atr = float(np.mean(tr[:period]))
    alpha = 1.0 / period
    for i in range(period, len(tr)):
        atr = (1.0 - alpha) * atr + alpha * float(tr[i])
    return atr

def compute_atr_gate(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                     period: int = 14, low_pct: int = 5,
                     history: int = 100) -> dict[str, float | bool | str]:
    """Block flat-vol windows (no edge); accept everything above the floor.

    The earlier symmetric upper bound (95th pct) was firing ~4× more often than
    the lower bound across recent regimes (ATR distribution is right-skewed)
    and was discarding genuine high-vol opportunity. min_atr plus the dynamic
    long-term-mean floor in signal_engine handle L1 sigma scaling end-to-end;
    an extra hard cap on top wasn't earning its keep.
    """
    if len(closes) < period + 2:
        return {"atr": 0.0, "passes": False, "reason": "insufficient_data"}
    # Compute the full TR series once, then build the rolling ATR history by
    # walking Wilder's EMA forward. The previous implementation re-sliced highs/
    # lows/closes per step and called compute_atr again — O(n × history) ATR
    # work every tick. Now O(n + history).
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    if len(tr) < period:
        return {"atr": 0.0, "passes": False, "reason": "insufficient_data"}
    alpha = 1.0 / period
    atr_running = float(np.mean(tr[:period]))
    atr_series: list[float] = [atr_running]
    for i in range(period, len(tr)):
        atr_running = (1.0 - alpha) * atr_running + alpha * float(tr[i])
        atr_series.append(atr_running)
    atr_current = atr_series[-1]
    atr_history = atr_series[-min(history, len(atr_series)):]
    if not atr_history:
        return {"atr": atr_current, "passes": False, "reason": "no_history"}
    low_thresh = float(np.percentile(atr_history, low_pct))
    if atr_current == 0.0 or atr_current < low_thresh:
        return {"atr": round(atr_current, 2), "passes": False, "reason": "too_quiet"}
    return {"atr": round(atr_current, 2), "passes": True, "reason": "ok"}
