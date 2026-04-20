"""Alpha decay tracker: monitors how fast edge (model probability) is changing over time.

Uses linear regression on recent (timestamp, probability) observations to compute
a decay rate. Positive rate = edge growing, negative = edge decaying. Entry timing
logic can use this to decide whether to enter now (decaying edge) or wait (growing edge).
"""

from collections import deque
from typing import Deque, Tuple


class AlphaDecayTracker:
    """Track the rate of change of model probability over a sliding window.

    Args:
        max_age_s: Maximum age of observations in seconds. Older entries are pruned.
    """

    def __init__(self, max_age_s: float = 60.0) -> None:
        self.max_age_s = max_age_s
        self._observations: Deque[Tuple[float, float]] = deque()

    def add_observation(self, ts: float, prob: float) -> None:
        """Record a (timestamp, probability) observation and prune stale entries."""
        self._observations.append((ts, prob))
        self._prune(ts)

    def get_decay_rate(self) -> float:
        """Compute the slope of probability vs time via ordinary least squares.

        Timestamps are normalized relative to the first observation to avoid
        catastrophic floating-point cancellation with large Unix timestamps.

        Returns:
            Slope (prob per second). Positive = edge growing, negative = decaying.
            Returns 0.0 if fewer than 3 observations (insufficient for meaningful fit).
        """
        if len(self._observations) < 3:
            return 0.0

        t0 = self._observations[0][0]
        n = len(self._observations)
        sum_t = 0.0
        sum_p = 0.0
        sum_tp = 0.0
        sum_tt = 0.0

        for ts, prob in self._observations:
            t = ts - t0
            sum_t += t
            sum_p += prob
            sum_tp += t * prob
            sum_tt += t * t

        denom = n * sum_tt - sum_t * sum_t
        if denom == 0.0:
            return 0.0

        slope = (n * sum_tp - sum_t * sum_p) / denom
        return slope

    def reset(self) -> None:
        """Clear all observations."""
        self._observations.clear()

    def _prune(self, current_ts: float) -> None:
        """Remove observations older than max_age_s from the left of the deque."""
        cutoff = current_ts - self.max_age_s
        while self._observations and self._observations[0][0] < cutoff:
            self._observations.popleft()
