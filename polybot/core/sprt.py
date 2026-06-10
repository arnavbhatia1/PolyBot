"""Sequential Probability Ratio Test (SPRT) accumulator — an entry gate that
requires evidence accumulated across ticks, not a single-point reading.
Each tick contributes to a log-likelihood ratio (LLR). Strong signals cross
the decision boundary in 5-7 ticks; weak signals need more evidence.
"""

import math
import time


class SPRTAccumulator:
    """SPRT accumulator tracking directional evidence for Up vs Down.

    ``alpha``/``beta``: Type I/II error rates. ``min_interval_s``: min seconds
    between observations — BTC ticks are autocorrelated (rho ~0.2-0.4), so
    feeding every tick inflates evidence.
    """

    def __init__(self, alpha: float = 0.05, beta: float = 0.10, min_interval_s: float = 5.0):
        self.alpha = alpha
        self.beta = beta
        self.min_interval_s = min_interval_s
        self.upper_bound = math.log((1.0 - beta) / alpha)
        self._up_evidence: float = 0.0
        self._down_evidence: float = 0.0
        self._observation_count: int = 0
        self._status: str = "ACCUMULATING"
        self._last_update_ts: float = 0.0

    @property
    def llr(self) -> float:
        """Current log-likelihood ratio (max of directional evidence)."""
        return max(self._up_evidence, self._down_evidence)

    def update(self, prob_up: float) -> str:
        """Add one observation; return "ENTER" (crossed upper boundary),
        "SKIP" (declared noise), or "ACCUMULATING"."""
        if self._status != "ACCUMULATING":
            return self._status

        now = time.time()
        if now - self._last_update_ts < self.min_interval_s:
            return self._status  # too soon, skip this observation
        self._last_update_ts = now
        self._observation_count += 1

        # Evidence increment log(max(p, 1-p) / 0.5): stronger signals add more;
        # prob_up == 0.5 adds nothing.
        prob_down = 1.0 - prob_up

        if prob_up > 0.5:
            increment = math.log(prob_up / 0.5)
            self._up_evidence += increment
        elif prob_up < 0.5:
            increment = math.log(prob_down / 0.5)
            self._down_evidence += increment
        # prob_up == 0.5: no evidence added to either side

        if self.llr >= self.upper_bound:
            self._status = "ENTER"
        # SKIP: weak/conflicted after 12 obs × 5s = 60s into the window; the 15%
        # threshold avoids nuking choppy-but-tradeable windows.
        elif self._observation_count >= 12 and self.llr < self.upper_bound * 0.15:
            self._status = "SKIP"

        return self._status

    def get_status(self) -> str:
        """Return current decision status without adding an observation."""
        return self._status

    def get_confidence(self) -> float:
        """Normalized confidence in [0, 1] = LLR / upper_bound, clamped."""
        if self.upper_bound <= 0:
            return 0.0
        return max(0.0, min(1.0, self.llr / self.upper_bound))

    def observation_count(self) -> int:
        """Number of observations fed so far this window."""
        return self._observation_count

    def favored_side(self) -> str:
        """Return the side with more accumulated evidence."""
        if self._up_evidence >= self._down_evidence:
            return "Up"
        return "Down"

    def reset(self) -> None:
        """Clear all state for a new window."""
        self._up_evidence = 0.0
        self._down_evidence = 0.0
        self._observation_count = 0
        self._status = "ACCUMULATING"
        self._last_update_ts = 0.0
