import json
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

class BiasDetector:
    def __init__(self, biases_path: str):
        self.biases_path = Path(biases_path)

    def detect(self, outcomes: list[dict], min_samples: int = 3) -> dict[str, float]:
        """Detect per-indicator accuracy biases from trade outcomes.

        For each indicator, computes win rate when that indicator's score
        was strongly positive vs strongly negative. Identifies indicators
        that mislead more than they help.
        """
        if len(outcomes) < min_samples:
            return {}

        indicator_names = ["rsi", "macd", "stochastic", "obv", "vwap"]
        biases = {}

        for ind in indicator_names:
            bullish_wins = 0
            bullish_total = 0
            bearish_wins = 0
            bearish_total = 0

            for o in outcomes:
                snap = o.get("indicator_snapshot", {})
                score = snap.get(ind, {}).get("score", 0)
                correct = o.get("correct", False)

                if score > 0.1:  # Indicator said bullish
                    bullish_total += 1
                    if correct:
                        bullish_wins += 1
                elif score < -0.1:  # Indicator said bearish
                    bearish_total += 1
                    if correct:
                        bearish_wins += 1

            # Compute accuracy when this indicator has a strong opinion
            total = bullish_total + bearish_total
            if total < min_samples:
                continue

            accuracy = (bullish_wins + bearish_wins) / total if total > 0 else 0
            # Bias: how much this indicator's accuracy deviates from 50% (coin flip)
            # Positive = indicator is useful, negative = indicator is misleading
            biases[ind] = round(accuracy - 0.5, 4)

        return biases

    def save(self, biases: dict[str, float]):
        self.biases_path.parent.mkdir(parents=True, exist_ok=True)
        self.biases_path.write_text(json.dumps(biases, indent=2))
