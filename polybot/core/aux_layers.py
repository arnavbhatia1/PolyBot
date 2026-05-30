"""Shared model math for L1 (vol-autocorr scale), L3/L3b (flow combine),
and the L3b raw signal.

`signal_engine` (live), `main.py` (live), and `agents/scheduler.py` (backtest
replay) all call these, so the probability computation is identical across paths
and the optimizer can never tune against a model production doesn't run.
"""
from __future__ import annotations

import math

from scipy.special import stdtr as _stdtr


# Minimum Student-t df. Pipeline range is 3-8; df ≤ 2 has undefined variance and a
# t_scale = sqrt(df/(df-2)) discontinuity (was 1.0 at df ≤ 2, jumping to √3 = 1.73
# at df = 3). Single source so live (signal_engine) and replay (scheduler) clamp
# identically.
MIN_STUDENT_T_DF = 3

_COINBASE_CVD_SCALE = 30.0       # ≈ typical 60s Coinbase BTC volume at baseline vol
_TAKER_MIN_N = 20                # min trades in window for taker ratio to count

# The L3b cascade scale is regime-relative: scaled by current volatility vs its
# long-run mean (atr / atr_long_term_mean), clamped, so flow saturates against the
# current regime instead of a fixed constant.
_VOL_FACTOR_LO = 0.5
_VOL_FACTOR_HI = 3.0

# L1 √t vol scaling assumes i.i.d. returns; BTC 1-min returns aren't. vol_scaled is
# multiplied by the AR(1) terminal-SD ratio √((1+ρ)/(1−ρ)), ρ clamped here.
_AC_VOL_CLAMP = 0.5

# Flow-family redundancy: L3/L3b are correlated views of one BTC move. Same-
# direction corroborators beyond the strongest count at (1 − this). Joint clamp last.
_FLOW_REDUNDANCY = 0.5
_FLOW_CLAMP = 0.50


def regime_vol_factor(atr: float | None, atr_long_term_mean: float | None) -> float:
    """Current volatility vs its long-run mean (clamped) — the regime scale for L3b.
    Returns 1.0 when either input is missing/non-positive, so telemetry lacking the
    ATR fields reproduces the fixed-scale behavior.
    """
    if not atr or atr <= 0 or not atr_long_term_mean or atr_long_term_mean <= 0:
        return 1.0
    return max(_VOL_FACTOR_LO, min(_VOL_FACTOR_HI, atr / atr_long_term_mean))


def autocorr_vol_scale(regime: float) -> float:
    """L1 AR(1) terminal-SD multiplier from the lag-1 return autocorrelation."""
    ac = max(-_AC_VOL_CLAMP, min(_AC_VOL_CLAMP, regime))
    return math.sqrt((1.0 + ac) / (1.0 - ac))


def student_t_cdf(t: float, df: float) -> float:
    """Student-t CDF for L1. Single implementation shared by live
    (`signal_engine`) and replay (`scheduler`) so the two paths can't drift
    across scipy entry points (`special.stdtr` vs `stats.t.cdf`)."""
    return float(_stdtr(df, t))


def combine_flow_family(flow_c: float, spot_c: float) -> float:
    """L3+L3b combined logit. Per direction, keep the strongest contribution at
    full weight and discount same-direction corroborators by _FLOW_REDUNDANCY (two
    venues watching one move); opposing signals offset. Clamped to ±_FLOW_CLAMP.
    """
    total = 0.0
    for side in ([c for c in (flow_c, spot_c) if c > 0.0],
                 [c for c in (flow_c, spot_c) if c < 0.0]):
        if side:
            dominant = max(side, key=abs)
            total += dominant + (1.0 - _FLOW_REDUNDANCY) * (sum(side) - dominant)
    return max(-_FLOW_CLAMP, min(_FLOW_CLAMP, total))


def compute_spot_flow_signal(cvd_60s: float | None,
                              taker_60s: float | None = None,
                              taker_n: int = 0,
                              vol_factor: float = 1.0) -> float:
    """L3b spot-venue flow signal ∈ [-1, 1]. Returns 0 when CVD is None (feed cold).

    cvd_60s in BTC units (signed). taker_60s in [0, 1]; counted only when
    taker_n ≥ _TAKER_MIN_N. vol_factor scales the CVD saturation point to the
    current volatility regime; 1.0 reproduces the fixed-scale behavior.
    """
    if cvd_60s is None:
        return 0.0
    scale = _COINBASE_CVD_SCALE * (vol_factor if vol_factor > 0 else 1.0)
    cvd_comp = math.tanh(cvd_60s / scale) * 0.8
    taker_comp = (
        (taker_60s - 0.5) * 2.0 * 0.2
        if (taker_60s is not None and taker_n >= _TAKER_MIN_N)
        else 0.0
    )
    return max(-1.0, min(1.0, cvd_comp + taker_comp))
