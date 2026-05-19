"""L6 — derived feature library.

Each entry maps a feature name to a deterministic function of inputs already
tracked by `SignalEngine.compute_probability`. The signal engine assembles a
`FeatureContext` per tick and dispatches to every feature whose weight is
non-zero in the live config.

Design constraints (enforced by review, not by code):
  * No new feeds — every input is already in scope at compute_probability.
  * Each output is bounded by tanh / log / sqrt / explicit clip, so a single
    feature cannot dominate the logit stack even at the max weight (0.05).
  * No interaction terms — features compose linearly; interactions go in L4.
  * The library is closed — new entries require a code change, not a runtime
    proposal.

L6 contribution is hard-clamped at the call site to ±0.25 logits (same order
as the L3+L3b combined cap), independent of any individual feature's bound.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class FeatureContext:
    """Per-tick view of state available to every derived feature.

    Frozen so a buggy feature cannot mutate engine state. Built once per call
    to `compute_probability`; cheap dataclass instantiation.
    """
    atr: float                      # raw ATR at this tick (>0 guaranteed by caller)
    atr_rolling_20: float           # rolling-20 ATR mean (0 if pre-warmup)
    atr_long_term_mean: float       # rolling-200 ATR mean (0 if pre-warmup)
    regime: float                   # 1-lag autocorr (signed)
    last_return: float              # most recent 1-min return (signed fraction)
    flow_signal: float              # L3 input (book + trade flow combined)
    spot_flow_signal: float         # L3b input (CVD + taker)
    liquidation_pressure: float     # L3e input (Bybit OI signal)
    prev_resolution_margin: float   # L5 input (prev window margin)
    seconds_remaining: float        # 0–300 within the 5-min window
    distance: float                 # btc_price − strike_price (signed)


def _safe_div(num: float, den: float, eps: float = 1.0) -> float:
    """Divide guarding against tiny denominators. eps matches the ATR floor convention."""
    return num / max(abs(den), eps)


def _f_log_atr_ratio(ctx: FeatureContext) -> float:
    """log(ATR_short / ATR_long). Positive in expanding-vol regime, negative
    in collapsing-vol regime. Bounded by log; typically lives in [-2, 2]."""
    short = ctx.atr_rolling_20 if ctx.atr_rolling_20 > 0 else ctx.atr
    long_ = ctx.atr_long_term_mean if ctx.atr_long_term_mean > 0 else ctx.atr
    if short <= 0 or long_ <= 0:
        return 0.0
    return math.log(short / long_)


def _f_autocorr_signed_mag(ctx: FeatureContext) -> float:
    """regime × |last_return|. Captures momentum strength conditional on regime
    direction. Bounded by `regime ∈ [-1, 1]` and last_return clipped via tanh."""
    return ctx.regime * math.tanh(abs(ctx.last_return) * 100.0)


def _f_vol_regime_shift(ctx: FeatureContext) -> float:
    """ATR_short / ATR_long − 1. Linear sibling of log_atr_ratio; differs in
    asymmetric behavior (capped on the downside at -1, unbounded above)."""
    long_ = ctx.atr_long_term_mean if ctx.atr_long_term_mean > 0 else ctx.atr
    short = ctx.atr_rolling_20 if ctx.atr_rolling_20 > 0 else ctx.atr
    if long_ <= 0:
        return 0.0
    return math.tanh((short / long_) - 1.0)


def _f_flow_disagreement(ctx: FeatureContext) -> float:
    """flow_signal × spot_flow_signal. Positive when CLOB and spot CVD agree
    on direction (strong consensus); negative when they fight. Bounded by tanh
    on the product so a single extreme value can't dominate."""
    return math.tanh(ctx.flow_signal * ctx.spot_flow_signal)


def _f_distance_atr_ratio(ctx: FeatureContext) -> float:
    """tanh(distance / ATR). Bounded alternative to the L1 z-score. Captures
    the same family of information but with a different shape near the strike."""
    if ctx.atr <= 0:
        return 0.0
    return math.tanh(ctx.distance / ctx.atr)


def _f_time_remaining_logit(ctx: FeatureContext) -> float:
    """(seconds_remaining − 150) / 150 — late-window asymmetry. Range [-1, 1]
    given the 0–300s window. Positive early in the window, negative late."""
    return (ctx.seconds_remaining - 150.0) / 150.0


def _f_liq_signed_sqrt(ctx: FeatureContext) -> float:
    """sign(liq) × min(√|liq|, 1). Softer saturation than the linear L3e input;
    catches sustained moderate liquidation pressure that linear weighting underweights."""
    liq = ctx.liquidation_pressure
    if liq == 0.0:
        return 0.0
    return math.copysign(min(math.sqrt(abs(liq)), 1.0), liq)


def _f_prev_margin_sq(ctx: FeatureContext) -> float:
    """sign(prev_margin) × min((prev_margin/ATR)², 1). Non-linear carry — gives
    extra weight to *large* prior margins relative to the linear L5 input."""
    if ctx.atr <= 0 or ctx.prev_resolution_margin == 0.0:
        return 0.0
    norm = ctx.prev_resolution_margin / ctx.atr
    return math.copysign(min(norm * norm, 1.0), ctx.prev_resolution_margin)


# Closed library. Order matters only for log readability; consumers iterate.
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

# Hard cap on combined L6 contribution. Same order as L3+L3b's cap so L6 cannot
# silently dominate the other layers.
L6_LOGIT_CAP: float = 0.25
