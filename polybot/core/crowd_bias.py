"""Crowd bias signal: exploits documented prediction market behavioral biases.

Three structural biases that don't decay:
1. Favorite-Longshot Bias (FLB) — markets overvalue longshots
2. Recency bias — crowds over-extrapolate streaks
3. Round number anchoring — crowds over-react near psychological levels

Returns a composite bias signal in [-1, +1] that can be used as
a logit-space adjustment (positive = bullish for Up).
"""
from __future__ import annotations

import math
import logging
from collections import deque

logger = logging.getLogger(__name__)


class CrowdBiasTracker:
    """Tracks and exploits crowd behavioral biases on Polymarket."""

    def __init__(self, max_history: int = 50) -> None:
        self._resolution_history: deque[str] = deque(maxlen=max_history)  # "Up" or "Down"

    def record_resolution(self, side: str) -> None:
        """Record a window resolution result for recency tracking."""
        self._resolution_history.append(side)

    def compute_flb_adjustment(self, market_price_up: float, market_price_down: float) -> float:
        """Favorite-Longshot Bias: fade extreme market prices.

        When market prices a side at 10-35%, it's overvalued (actual is lower).
        When market prices a side at 65-90%, it's undervalued (actual is higher).

        Returns adjustment in [-0.03, +0.03] in probability space:
        - Positive: the Up side is undervalued (favorite bias)
        - Negative: the Down side is undervalued
        """
        # The FLB correction: extreme prices are biased toward 50%
        # At 90%: actual is ~92%, so we add +2% (toward the favorite)
        # At 10%: actual is ~8%, so we subtract -2% (away from the longshot)

        # Compute the FLB bias for the Up contract
        if market_price_up >= 0.65:
            # Favorite: market undervalues it slightly
            flb = (market_price_up - 0.50) * 0.06  # ~+3% at 90% market
        elif market_price_up <= 0.35:
            # Longshot: market overvalues it slightly
            flb = (market_price_up - 0.50) * 0.06  # ~-3% at 10% market
        else:
            flb = 0.0  # Near 50/50, no FLB

        return max(-0.03, min(0.03, flb))

    def compute_recency_fade(self) -> float:
        """Recency bias: fade streaks of 3+ consecutive same-direction resolutions.

        After 3+ "Up" in a row, market over-prices Up. Fade it.
        After 3+ "Down" in a row, market over-prices Down. Fade it.

        Returns signal in [-1, +1]:
        - Positive: fade suggests Up (recent streak was Down)
        - Negative: fade suggests Down (recent streak was Up)
        """
        if len(self._resolution_history) < 3:
            return 0.0

        # Count consecutive from the end
        last = self._resolution_history[-1]
        streak = 0
        for res in reversed(self._resolution_history):
            if res == last:
                streak += 1
            else:
                break

        if streak < 3:
            return 0.0

        # Fade the streak: strength increases with streak length
        # 3 in a row: mild fade. 5+: strong fade.
        strength = min(1.0, (streak - 2) * 0.3)  # 0.3 at 3, 0.6 at 4, 0.9 at 5, 1.0 at 6+

        if last == "Up":
            return -strength  # Fade Up streak -> bearish signal
        else:
            return strength   # Fade Down streak -> bullish signal

    def compute_round_number_signal(self, strike: float) -> float:
        """Round number anchoring: near round thousands, crowd overestimates deviation.

        When strike is near $X0,000 or $X1,000 etc., crowd behavior is exaggerated.
        Returns a volatility dampening factor in [0.9, 1.0]:
        - Near round number: 0.9 (crowd overestimates movement, reduce confidence)
        - Far from round number: 1.0 (no adjustment)
        """
        if strike <= 0:
            return 1.0
        # Distance to nearest $1000
        remainder = strike % 1000
        dist_to_round = min(remainder, 1000 - remainder)

        # Within $50 of a round $1000: apply dampening
        if dist_to_round < 50:
            return 0.92  # 8% reduction
        elif dist_to_round < 100:
            return 0.96
        return 1.0

    def compute_composite(self, market_price_up: float, market_price_down: float,
                          strike: float) -> dict:
        """Compute all bias signals and a composite adjustment.

        Returns dict with individual signals and composite:
            flb: float (-0.03 to +0.03)
            recency_fade: float (-1 to +1)
            round_number_dampening: float (0.9 to 1.0)
            composite_logit_adjustment: float (applied in logit space)
        """
        flb = self.compute_flb_adjustment(market_price_up, market_price_down)
        recency = self.compute_recency_fade()
        round_num = self.compute_round_number_signal(strike)

        # Composite: FLB is a probability adjustment, recency is a directional signal
        # Convert to logit adjustment: FLB directly, recency scaled by 0.02
        composite = flb + recency * 0.02  # recency gets small weight

        return {
            "flb": round(flb, 4),
            "recency_fade": round(recency, 4),
            "round_number_dampening": round(round_num, 4),
            "composite_logit_adjustment": round(composite, 4),
        }
