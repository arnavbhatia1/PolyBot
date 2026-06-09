"""Binary-option exit threshold. Unlike European options, binary payoff kinks at 0/1:
deep ITM near expiry has negative time value (want $1 resolution), deep OTM has
exhausted time value (cut losses), ATM has standard sqrt(time) optionality.
"""
from __future__ import annotations

import math

from polybot.execution.base import DEFAULT_FEE_RATE

_PRICE_VOL_PER_MIN = 0.07


class ExitBoundary:
    def compute_exit_threshold(self, seconds_remaining: float, fee_rate: float = DEFAULT_FEE_RATE, market_price: float = 0.5) -> float:
        """Minimum holding_edge to justify holding. More negative = more patient.

        The boundary is purely a function of time-remaining, the current market
        price (ITM/ATM/OTM regime), and the fee — entry price does not enter the
        binary-payoff time-value math, so it is deliberately not a parameter.
        """
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


def effective_exit_threshold(exit_threshold: float, seconds_remaining: float,
                             market_price_for_side: float,
                             fee_rate: float = DEFAULT_FEE_RATE,
                             market_mid_for_side: float | None = None,
                             boundary: ExitBoundary | None = None) -> float:
    """The blended threshold the scalp decision actually fires on.

    ATM trusts the ExitBoundary curve; deeper ITM weights toward the more
    patient deep-loss floor. Shared by evaluate_hold (live) and the
    exit_edge_threshold counterfactual replay (scheduler) so a candidate
    threshold is scored against the same fire criterion live uses.
    ``market_mid_for_side`` anchors ITM depth when available (live); the
    replay only has the recorded trade price, the same fallback live uses
    when the mid is missing.
    """
    itm_ref = (market_mid_for_side
               if market_mid_for_side and market_mid_for_side > 0
               else market_price_for_side)
    itm_depth = max(0.0, (itm_ref - 0.5) / 0.5)
    deep_loss_floor = exit_threshold * (1.0 + 0.5 * itm_depth)
    optimal = (boundary or _DEFAULT_BOUNDARY).compute_exit_threshold(
        seconds_remaining, fee_rate, market_price_for_side)
    return ((1 - itm_depth) * max(deep_loss_floor, optimal)
            + itm_depth * min(deep_loss_floor, optimal))


_DEFAULT_BOUNDARY = ExitBoundary()
