"""L6 closed library of bounded transforms of state already tracked by
SignalEngine.compute_probability. Every feature is direction-aware (signs track
realized BTC direction) and naturally bounded; the combined L6 contribution is
hard-clamped to ±L6_LOGIT_CAP at the call site.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class FeatureContext:
    """Frozen per-tick state for derived features."""
    atr: float
    atr_rolling_20: float
    atr_long_term_mean: float
    regime: float
    last_return: float
    flow_signal: float
    spot_flow_signal: float
    liquidation_pressure: float
    prev_resolution_margin: float
    seconds_remaining: float
    distance: float


def _f_log_atr_ratio(ctx: FeatureContext) -> float:
    """Clipped log(ATR_short / ATR_long). Vol regime expansion (+) or collapse (−)."""
    short = ctx.atr_rolling_20 if ctx.atr_rolling_20 > 0 else ctx.atr
    long_ = ctx.atr_long_term_mean if ctx.atr_long_term_mean > 0 else ctx.atr
    if short <= 0 or long_ <= 0:
        return 0.0
    return max(-1.5, min(1.5, math.log(short / long_)))


def _f_autocorr_signed_mag(ctx: FeatureContext) -> float:
    """regime × tanh(last_return × 100). Direction-aware momentum strength
    conditional on regime sign."""
    return ctx.regime * math.tanh(ctx.last_return * 100.0)


def _f_flow_disagreement(ctx: FeatureContext) -> float:
    """tanh(flow + spot_flow). Direction-aware: bullish when both flows are
    bullish, bearish when both are bearish, dampened when they fight."""
    return math.tanh(ctx.flow_signal + ctx.spot_flow_signal)


def _f_liq_signed_sqrt(ctx: FeatureContext) -> float:
    """Softer saturation than L3e's linear input — catches sustained moderate pressure."""
    liq = ctx.liquidation_pressure
    if liq == 0.0:
        return 0.0
    return math.copysign(min(math.sqrt(abs(liq)), 1.0), liq)


DERIVED_FEATURES: dict[str, Callable[[FeatureContext], float]] = {
    "log_atr_ratio":         _f_log_atr_ratio,
    "autocorr_signed_mag":   _f_autocorr_signed_mag,
    "flow_disagreement":     _f_flow_disagreement,
    "liq_signed_sqrt":       _f_liq_signed_sqrt,
}

L6_LOGIT_CAP: float = 0.25
