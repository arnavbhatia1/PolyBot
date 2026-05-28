"""Binary-option exit threshold. Unlike European options, binary payoff kinks at 0/1:
deep ITM near expiry has negative time value (want $1 resolution), deep OTM has
exhausted time value (cut losses), ATM has standard sqrt(time) optionality.
"""
from __future__ import annotations

import math
_PRICE_VOL_PER_MIN = 0.07


class ExitBoundary:
    def compute_exit_threshold(self, seconds_remaining: float, entry_price: float, fee_rate: float = 0.018, market_price: float = 0.5) -> float:
        """Minimum holding_edge to justify holding. More negative = more patient."""
        minutes_remaining = max(seconds_remaining / 60.0, 0.01)
        fee_cost = fee_rate * market_price * (1.0 - market_price)
        base_time_value = _PRICE_VOL_PER_MIN * math.sqrt(minutes_remaining) * 0.4

        urgency_premium = 0.0
        if market_price >= 0.70:
            itm_depth = (market_price - 0.50) / 0.50
            resolution_premium = max(0.0, itm_depth * 0.05 * (1.0 - minutes_remaining / 5.0))
            time_value = base_time_value * (1.0 - itm_depth * 0.5) + resolution_premium
        elif market_price <= 0.30:
            otm_depth = (0.50 - market_price) / 0.50
            time_value = base_time_value * (1.0 - otm_depth * 0.7)
            urgency = max(0.0, 1.0 - minutes_remaining / 2.0)
            urgency_premium = otm_depth * urgency * 0.45
        else:
            time_value = base_time_value

        # OTM urgency can push threshold positive — forces exit even when model is optimistic.
        threshold = -(time_value + fee_cost) + urgency_premium
        upper_cap = 0.30 if urgency_premium > 0 else -0.01
        return max(-0.30, min(upper_cap, threshold))
