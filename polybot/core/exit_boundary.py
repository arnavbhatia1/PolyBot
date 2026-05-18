"""Optimal exit boundary for binary options.

Binary options have different time value dynamics than European options:
- Deep ITM (market_price > 0.75): time value is NEGATIVE late in window.
  You want resolution at $1, not continuation with downside risk.
  The exit threshold should RISE (more tolerant of holding) as you approach expiry.
- Near ATM (0.40-0.60): time value is positive — volatility could help.
  Standard sqrt(time) optionality applies.
- Deep OTM (market_price < 0.25): time value is the only value.
  Very patient early, but exit if no recovery materializes.

The key difference from European options: binary payoff has a kink at 0 and 1.
A 90-cent binary with 30s left has almost no upside optionality (max $0.10 more)
but significant downside risk. Holding is correct — exiting wastes the near-certain $1.
"""
from __future__ import annotations

import math
import logging

logger = logging.getLogger(__name__)

class ExitBoundary:
    """Time-varying exit threshold for binary option positions.

    Accounts for the asymmetric payoff structure of binary contracts:
    - Winners near expiry: HOLD (negative time value, want $1 resolution)
    - Losers near expiry: EXIT (no time value left to recover)
    - ATM early: HOLD (positive time value from volatility)
    """

    def __init__(self, df: int = 5, price_vol_per_min: float = 0.07) -> None:
        self.df = df
        self.price_vol_per_min = price_vol_per_min

    def compute_exit_threshold(self, seconds_remaining: float, entry_price: float, fee_rate: float = 0.018, market_price: float = 0.5) -> float:
        """Compute minimum holding_edge to justify holding.

        For binary options, the threshold depends on WHERE the market price is:

        Deep ITM (market > 0.70): threshold gets MORE negative near expiry
          (more tolerant of adverse edge — you want $1 resolution)
        ATM (0.40-0.60): sqrt(time) optionality, standard convex curve
        Deep OTM (market < 0.30): threshold gets LESS negative near expiry
          (less tolerant — cut losses, time value exhausted)

        Returns: minimum holding_edge (model_prob - market_price).
                 More negative = more patient. Less negative = more eager to exit.
        """
        minutes_remaining = max(seconds_remaining / 60.0, 0.01)

        # Fee cost of exiting now
        fee_cost = fee_rate * market_price * (1.0 - market_price)

        # Base time value: sqrt(time) optionality (ATM case)
        base_time_value = self.price_vol_per_min * math.sqrt(minutes_remaining) * 0.4

        # Binary payoff adjustment: modify time value based on moneyness
        urgency_premium = 0.0  # only set in the OTM branch below
        if market_price >= 0.70:
            # Deep ITM: time value is NEGATIVE near expiry.
            itm_depth = (market_price - 0.50) / 0.50  # 0.0 at ATM, 1.0 at $1
            # Reduce time value (less reason to exit) and add resolution premium
            resolution_premium = itm_depth * 0.05 * (1.0 - minutes_remaining / 5.0)
            resolution_premium = max(0, resolution_premium)  # only near expiry
            time_value = base_time_value * (1.0 - itm_depth * 0.5) + resolution_premium

        elif market_price <= 0.30:
            # Deep OTM: time value decays faster. Less reason to hold.
            otm_depth = (0.50 - market_price) / 0.50  # 0.0 at ATM, 1.0 at $0
            time_value = base_time_value * (1.0 - otm_depth * 0.7)
            urgency = max(0.0, 1.0 - minutes_remaining / 2.0)
            urgency_premium = otm_depth * urgency * 0.45

        else:
            # ATM zone: standard sqrt(time) optionality
            time_value = base_time_value
            urgency_premium = 0.0

        # Threshold: tolerate adverse edge up to (time_value + fee_cost).
        # OTM urgency can push the threshold positive, forcing exit even when the model is still optimistic
        threshold = -(time_value + fee_cost) + urgency_premium
        upper_cap = 0.30 if urgency_premium > 0 else -0.01
        return max(-0.30, min(upper_cap, threshold))

    def should_exit(self, seconds_remaining: float, market_price: float, entry_price: float, fee_rate: float = 0.018, model_prob: float | None = None) -> tuple[bool, float]:
        """Whether to exit based on binary option optimal boundary. Returns: (should_exit, boundary_price)"""
        if seconds_remaining <= 0:
            return False, 0.0  # At expiry, let it resolve

        prob = model_prob if model_prob is not None else market_price
        threshold = self.compute_exit_threshold(
            seconds_remaining, entry_price, fee_rate, market_price)

        holding_edge = prob - market_price
        should = holding_edge <= threshold

        return should, market_price + threshold
