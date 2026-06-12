from __future__ import annotations

import numpy as np


def compute_atr_gate(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                     period: int = 14, low_pct: int = 5,
                     history: int = 100) -> dict[str, float | bool | str]:
    """Block flat-vol windows (no edge); accept everything above the floor.

    Lower-bound only: an upper cap fired ~4× more often (ATR is right-skewed) and
    discarded genuine high-vol opportunity. min_atr plus the dynamic long-term-mean
    floor in signal_engine already handle L1 sigma scaling end-to-end.
    """
    if len(closes) < period + 2:
        return {"atr": 0.0, "passes": False, "reason": "insufficient_data"}
    # Compute the TR series once, then build the rolling ATR history by walking
    # Wilder's EMA forward — O(n + history) per tick, not O(n × history).
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
