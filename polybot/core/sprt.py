"""Sequential Probability Ratio Test (SPRT) accumulator for evidence-based entry.

Replaces single-point edge threshold with sequential evidence accumulation.
Each tick contributes to a log-likelihood ratio (LLR). Strong signals cross
the decision boundary in 5-7 ticks; weak signals need more evidence.
"""

import math
import time


class SPRTAccumulator:
    """SPRT accumulator that tracks directional evidence for Up vs Down.

    Parameters
    ----------
    alpha : float
        Type I error rate (false positive). Default 0.05.
    beta : float
        Type II error rate (false negative). Default 0.10.
    min_interval_s : float
        Minimum seconds between observations. BTC ticks are autocorrelated
        (rho ~0.2-0.4), so feeding every tick inflates evidence. Default 10.0.
    """

    def __init__(self, alpha: float = 0.05, beta: float = 0.10, min_interval_s: float = 10.0):
        self.alpha = alpha
        self.beta = beta
        self.min_interval_s = min_interval_s
        self.upper_bound = math.log((1.0 - beta) / alpha)
        self.lower_bound = math.log(beta / (1.0 - alpha))
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
        """Add one observation and return decision.

        Parameters
        ----------
        prob_up : float
            Model probability that BTC finishes Up (0 to 1).

        Returns
        -------
        str
            "ENTER" if evidence crosses upper boundary,
            "SKIP" if evidence crosses lower boundary,
            "ACCUMULATING" if still collecting evidence.
        """
        if self._status != "ACCUMULATING":
            return self._status

        now = time.time()
        if now - self._last_update_ts < self.min_interval_s:
            return self._status  # too soon, skip this observation
        self._last_update_ts = now
        self._observation_count += 1

        # Evidence increment: how much this tick supports a directional signal.
        # log(max(p, 1-p) / 0.5) — stronger signals add more evidence.
        # When prob_up == 0.5, increment is log(1) = 0 — no evidence.
        prob_down = 1.0 - prob_up

        if prob_up > 0.5:
            increment = math.log(prob_up / 0.5)
            self._up_evidence += increment
        elif prob_up < 0.5:
            increment = math.log(prob_down / 0.5)
            self._down_evidence += increment
        # prob_up == 0.5: no evidence added to either side

        # Check ENTER: strong directional evidence
        if self.llr >= self.upper_bound:
            self._status = "ENTER"
        # Check SKIP: evidence is weak or conflicted after enough observations
        elif self._observation_count >= 4 and self.llr < self.upper_bound * 0.3:
            # After 4+ observations, if LLR hasn't reached 30% of ENTER threshold,
            # the signal is too weak to trade
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
