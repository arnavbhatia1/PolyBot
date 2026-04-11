"""Sequential Probability Ratio Test (SPRT) accumulator for evidence-based entry.

Replaces single-point edge threshold with sequential evidence accumulation.
Each tick contributes to a log-likelihood ratio (LLR). Strong signals cross
the decision boundary in 5-7 ticks; weak signals need more evidence.
"""

import math


class SPRTAccumulator:
    """SPRT accumulator that tracks directional evidence for Up vs Down.

    Parameters
    ----------
    alpha : float
        Type I error rate (false positive). Default 0.05.
    beta : float
        Type II error rate (false negative). Default 0.10.
    """

    def __init__(self, alpha: float = 0.05, beta: float = 0.10):
        self.alpha = alpha
        self.beta = beta
        self.upper_bound = math.log((1.0 - beta) / alpha)
        self.lower_bound = math.log(beta / (1.0 - alpha))
        self._up_evidence: float = 0.0
        self._down_evidence: float = 0.0
        self._status: str = "ACCUMULATING"

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

        # Check boundaries against the stronger directional evidence
        if self.llr >= self.upper_bound:
            self._status = "ENTER"
        elif self.llr <= self.lower_bound:
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
        self._status = "ACCUMULATING"
