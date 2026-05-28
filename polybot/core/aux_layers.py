"""Shared model math for L1 (vol-autocorr scale), L3/L3b/L3e (flow combine),
and the L3b/L3e raw signals.

`signal_engine` (live), `main.py` (live), and `agents/scheduler.py` (backtest
replay) all call these, so the probability computation is identical across paths
and the optimizer can never tune against a model production doesn't run.
"""
from __future__ import annotations

import math


_COINBASE_CVD_SCALE = 30.0       # ≈ typical 60s Coinbase BTC volume at baseline vol
_LIQ_USD_SCALE = 50_000.0        # net liquidation USD/min at _LIQ_PRICE_REF + baseline vol
_LIQ_PRICE_REF = 65_000.0        # BTC price at which _LIQ_USD_SCALE is calibrated
_TAKER_MIN_N = 20                # min trades in window for taker ratio to count

# L3b/L3e cascade scales are regime-relative: scaled by current volatility vs its
# long-run mean (atr / atr_long_term_mean), clamped, so flow saturates against the
# current regime instead of a frozen constant.
_VOL_FACTOR_LO = 0.5
_VOL_FACTOR_HI = 3.0

# L1 √t vol scaling assumes i.i.d. returns; BTC 1-min returns aren't. vol_scaled is
# multiplied by the AR(1) terminal-SD ratio √((1+ρ)/(1−ρ)), ρ clamped here.
_AC_VOL_CLAMP = 0.5

# Flow-family redundancy: L3/L3b/L3e are correlated views of one BTC move. Same-
# direction corroborators beyond the strongest count at (1 − this). Joint clamp last.
_FLOW_REDUNDANCY = 0.5
_FLOW_CLAMP = 0.50


def regime_vol_factor(atr: float | None, atr_long_term_mean: float | None) -> float:
    """Current volatility vs its long-run mean (clamped) — the regime scale for L3b/L3e.
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


def combine_flow_family(flow_c: float, spot_c: float, liq_c: float) -> float:
    """L3+L3b+L3e combined logit. Per direction, keep the strongest contribution at
    full weight and discount same-direction corroborators by _FLOW_REDUNDANCY (three
    venues watching one move); opposing signals offset. Clamped to ±_FLOW_CLAMP.
    """
    total = 0.0
    for side in ([c for c in (flow_c, spot_c, liq_c) if c > 0.0],
                 [c for c in (flow_c, spot_c, liq_c) if c < 0.0]):
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


def compute_liquidation_signal(long_usd_min: float | None,
                                short_usd_min: float | None,
                                btc_price: float | None = None,
                                vol_factor: float = 1.0) -> float:
    """L3e direct-stream liquidation pressure ∈ [-1, 1].

    Net (short_liq − long_liq) USD/min, tanh-saturated at a cascade scale that
    tracks BTC price (so a fixed USD threshold isn't silently recalibrated as price
    drifts) and the current volatility regime. btc_price None → fixed USD scale.
    Sign: short liquidation → price-up (+); long liquidation → price-down (−).
    """
    long_usd = long_usd_min or 0.0
    short_usd = short_usd_min or 0.0
    if long_usd == 0.0 and short_usd == 0.0:
        return 0.0
    price_factor = (btc_price / _LIQ_PRICE_REF) if (btc_price and btc_price > 0) else 1.0
    vf = vol_factor if vol_factor > 0 else 1.0
    scale = _LIQ_USD_SCALE * price_factor * vf
    return math.tanh((short_usd - long_usd) / scale)
