"""L6 closed library of bounded transforms of state already tracked by
SignalEngine.compute_probability. No new feeds, no interactions, no runtime
additions. Combined L6 contribution hard-clamped to ±L6_LOGIT_CAP at call site.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class FeatureContext:
    """Frozen per-tick state for derived features. Prevents a buggy feature from mutating engine state."""
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
    """log(ATR_short / ATR_long). Vol regime expansion (+) or collapse (−)."""
    short = ctx.atr_rolling_20 if ctx.atr_rolling_20 > 0 else ctx.atr
    long_ = ctx.atr_long_term_mean if ctx.atr_long_term_mean > 0 else ctx.atr
    if short <= 0 or long_ <= 0:
        return 0.0
    return math.log(short / long_)


def _f_autocorr_signed_mag(ctx: FeatureContext) -> float:
    """regime × tanh(|last_return| × 100). Momentum strength conditional on regime."""
    return ctx.regime * math.tanh(abs(ctx.last_return) * 100.0)


def _f_vol_regime_shift(ctx: FeatureContext) -> float:
    """tanh(ATR_short/ATR_long − 1). Linear sibling of log_atr_ratio."""
    long_ = ctx.atr_long_term_mean if ctx.atr_long_term_mean > 0 else ctx.atr
    short = ctx.atr_rolling_20 if ctx.atr_rolling_20 > 0 else ctx.atr
    if long_ <= 0:
        return 0.0
    return math.tanh((short / long_) - 1.0)


def _f_flow_disagreement(ctx: FeatureContext) -> float:
    """tanh(flow × spot_flow). + when CLOB and CVD agree, − when they fight."""
    return math.tanh(ctx.flow_signal * ctx.spot_flow_signal)


def _f_distance_atr_ratio(ctx: FeatureContext) -> float:
    """tanh(distance / ATR). Bounded alternative shape to L1's z-score near strike."""
    if ctx.atr <= 0:
        return 0.0
    return math.tanh(ctx.distance / ctx.atr)


def _f_time_remaining_logit(ctx: FeatureContext) -> float:
    """Late-window asymmetry. + early, − late, range [-1, 1] over 0-300s."""
    return (ctx.seconds_remaining - 150.0) / 150.0


def _f_liq_signed_sqrt(ctx: FeatureContext) -> float:
    """Softer saturation than L3e's linear input — catches sustained moderate pressure."""
    liq = ctx.liquidation_pressure
    if liq == 0.0:
        return 0.0
    return math.copysign(min(math.sqrt(abs(liq)), 1.0), liq)


def _f_prev_margin_sq(ctx: FeatureContext) -> float:
    """Non-linear carry — extra weight to *large* prior margins vs L5's linear."""
    if ctx.atr <= 0 or ctx.prev_resolution_margin == 0.0:
        return 0.0
    norm = ctx.prev_resolution_margin / ctx.atr
    return math.copysign(min(norm * norm, 1.0), ctx.prev_resolution_margin)


DERIVED_FEATURES: dict[str, Callable[[FeatureContext], float]] = {
    "log_atr_ratio":         _f_log_atr_ratio,
    "autocorr_signed_mag":   _f_autocorr_signed_mag,
    "vol_regime_shift":      _f_vol_regime_shift,
    "flow_disagreement":     _f_flow_disagreement,
    "distance_atr_ratio":    _f_distance_atr_ratio,
    "time_remaining_logit":  _f_time_remaining_logit,
    "liq_signed_sqrt":       _f_liq_signed_sqrt,
    "prev_margin_sq":        _f_prev_margin_sq,
}

# Same order as L3+L3b cap so L6 cannot silently dominate other layers.
L6_LOGIT_CAP: float = 0.25
