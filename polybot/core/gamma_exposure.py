"""Gamma Exposure (GEX) computation from Deribit options data.

Computes net dealer gamma exposure to determine whether the options market
is likely to stabilize (positive GEX) or amplify (negative GEX) spot moves.
"""

import math
from typing import List, Dict, Any

# Standard normal PDF: (1/sqrt(2*pi)) * exp(-0.5 * x^2)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _bs_gamma(spot: float, strike: float, iv: float, t_years: float) -> float:
    """Black-Scholes gamma for a single option.

    gamma = N'(d1) / (S * sigma * sqrt(T))
    where d1 = (ln(S/K) + 0.5*sigma^2*T) / (sigma*sqrt(T))

    Returns 0.0 for degenerate inputs (zero spot, zero vol, zero time).
    """
    if spot <= 0 or iv <= 0 or t_years <= 0:
        return 0.0

    sqrt_t = math.sqrt(t_years)
    sigma_sqrt_t = iv * sqrt_t

    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / sigma_sqrt_t
    return _norm_pdf(d1) / (spot * sigma_sqrt_t)


def compute_net_gex(options: List[Dict[str, Any]], spot_price: float) -> float:
    """Compute net gamma exposure normalized to [-1, 1].

    For each option:
      dollar_gamma = gamma * OI * spot^2 * 0.01
      Calls contribute positive GEX (dealers are long gamma -> buy dips, sell rips).
      Puts contribute negative GEX (dealers are short gamma -> amplify moves).

    Total is normalized via tanh(total / 1e6).

    Args:
        options: List of dicts with keys: strike, type ("call"/"put"),
                 oi (open interest), iv (implied vol), expiry_hours.
        spot_price: Current BTC spot price.

    Returns:
        Net GEX in [-1, 1]. Positive = stabilizing, negative = amplifying.
    """
    if not options or spot_price <= 0:
        return 0.0

    total_gex = 0.0
    for opt in options:
        t_years = opt["expiry_hours"] / (365.0 * 24.0)
        gamma = _bs_gamma(spot_price, opt["strike"], opt["iv"], t_years)
        dollar_gamma = gamma * opt["oi"] * spot_price * spot_price * 0.01

        if opt["type"] == "call":
            total_gex += dollar_gamma
        else:
            total_gex -= dollar_gamma

    return math.tanh(total_gex / 1e6)


def classify_gex(
    gex: float, threshold: float = 0.15
) -> Dict[str, Any]:
    """Classify GEX regime for signal integration.

    Args:
        gex: Net GEX value in [-1, 1] from compute_net_gex.
        threshold: Boundary between neutral and directional regimes.

    Returns:
        Dict with:
          - regime: "stabilizing" | "amplifying" | "neutral"
          - trade_bias: multiplier for momentum signals
            0.7 (stabilizing dampens momentum),
            1.3 (amplifying boosts momentum),
            1.0 (neutral, no adjustment).
    """
    if gex > threshold:
        return {"regime": "stabilizing", "trade_bias": 0.7}
    elif gex < -threshold:
        return {"regime": "amplifying", "trade_bias": 1.3}
    else:
        return {"regime": "neutral", "trade_bias": 1.0}
