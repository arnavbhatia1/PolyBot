"""Multi-state regime detector.

Classifies BTC microstructure as trending_up, trending_down, mean_reverting,
volatile, quiet, or neutral (unknown when data is insufficient).
quiet skips entry (no edge in flat-vol); all others allow entry.
"""

from dataclasses import dataclass
import numpy as np
from polybot.core.returns import lag1_autocorr

@dataclass(frozen=True)
class RegimeState:
    """Immutable regime classification result."""
    name: str
    skip: bool = False

# Pre-built regime states (immutable singletons)
_REGIMES = {
    "trending_up":    RegimeState(name="trending_up"),
    "trending_down":  RegimeState(name="trending_down"),
    "mean_reverting": RegimeState(name="mean_reverting"),
    "volatile":       RegimeState(name="volatile"),
    "quiet":          RegimeState(name="quiet", skip=True),
    "neutral":        RegimeState(name="neutral"),
    "unknown":        RegimeState(name="unknown"),
}

class RegimeDetector:
    """Classifies market regime from price closes and ATR.

    ``lookback``: closes used for autocorr. ``vol_high_pct``/``vol_low_pct``:
    ATR percentiles bounding volatile/quiet. ``autocorr_threshold``: |autocorr|
    needed for trending or mean-reverting. ``trend_consistency``: same-direction
    return fraction to qualify as trending.
    """

    def __init__(
        self,
        lookback: int = 50,
        vol_high_pct: float = 75,
        vol_low_pct: float = 25,
        autocorr_threshold: float = 0.25,
        trend_consistency: float = 0.70,
    ) -> None:
        self.lookback = lookback
        self.vol_high_pct = vol_high_pct
        self.vol_low_pct = vol_low_pct
        self.autocorr_threshold = autocorr_threshold
        self.trend_consistency = trend_consistency

    def classify(
        self,
        closes: np.ndarray,
        atr: float,
        atr_history: list[float],
        autocorr: float | None = None,
    ) -> RegimeState:
        """Classify the current market regime. ``autocorr`` can be passed in
        (signal_engine.last_regime_autocorr) to avoid recomputing it."""
        n = self.lookback
        if len(closes) < n + 2 or len(atr_history) < 1:
            return _REGIMES["unknown"]

        if autocorr is None:
            autocorr = self._compute_autocorr(closes, n)
        vol_pct = self._compute_vol_percentile(atr, atr_history)
        dir_ratio = self._compute_directional_ratio(closes, n)

        # Rules in priority order. ATR regimes (quiet/volatile) are checked first:
        # gentle trends produce a shrinking-denominator autocorr spike, so directional
        # rules can't be trusted over the vol signal.

        # 1. Quiet: ATR well below historical norms -- market is asleep.
        if vol_pct < self.vol_low_pct:
            return _REGIMES["quiet"]

        # 2. Volatile: ATR well above historical norms
        if vol_pct > self.vol_high_pct:
            return _REGIMES["volatile"]

        # 3. Trending: strong directional consistency — direction from PRICE returns,
        #    never CVD (thin-volume CVD can oppose price and misclassify the trend).
        is_trending = (
            autocorr > self.autocorr_threshold
            or dir_ratio > self.trend_consistency
        )
        if is_trending:
            returns = np.diff(closes[-(n + 1):]) / closes[-(n + 1):-1]
            up_count = np.sum(returns > 0)
            down_count = np.sum(returns < 0)
            if up_count > down_count:
                return _REGIMES["trending_up"]
            elif down_count > up_count:
                return _REGIMES["trending_down"]
            # Tie: fall through to mean_reverting/neutral checks

        # 4. Mean-reverting: negative autocorrelation (returns flip sign)
        if autocorr < -self.autocorr_threshold:
            return _REGIMES["mean_reverting"]

        return _REGIMES["neutral"]

    @staticmethod
    def _compute_autocorr(closes: np.ndarray, n: int) -> float:
        """1-lag autocorrelation of the last n returns so signal_engine
        and regime detector can never disagree on the same closes."""
        return lag1_autocorr(closes, n)

    @staticmethod
    def _compute_vol_percentile(atr: float, atr_history: list[float]) -> float:
        """Current ATR's rank in recent history (0-100). Midrank: equal values
        count as half, so atr == all history doesn't yield 0th percentile."""
        n = len(atr_history)
        if n == 0:
            return 50.0
        below = equal = 0
        for v in atr_history:
            if v < atr:
                below += 1
            elif v == atr:
                equal += 1
        return ((below + 0.5 * equal) / n) * 100.0

    @staticmethod
    def _compute_directional_ratio(closes: np.ndarray, n: int) -> float:
        """Fraction of returns in the dominant direction over the lookback
        (1.0 = all one way, 0.5 = balanced; 0.0 on insufficient data)."""
        returns = np.diff(closes[-(n + 1):]) / closes[-(n + 1):-1]
        if len(returns) < 2:
            return 0.0
        pos = np.sum(returns > 0)
        neg = np.sum(returns < 0)
        total = pos + neg
        if total == 0:
            return 0.0
        return max(pos, neg) / total
