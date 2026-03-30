import json
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

class BiasDetector:
    def __init__(self, biases_path: str):
        self.biases_path = Path(biases_path)

    def detect(self, outcomes: list[dict], min_samples: int = 3) -> dict[str, float]:
        by_category: dict[str, list[dict]] = defaultdict(list)
        for outcome in outcomes:
            cat = outcome.get("category", "unknown")
            if cat:
                by_category[cat].append(outcome)
        biases = {}
        for category, records in by_category.items():
            if len(records) < min_samples:
                continue
            avg_predicted = sum(r["predicted_probability"] for r in records) / len(records)
            avg_actual = sum(1.0 if r["actual_outcome"] else 0.0 for r in records) / len(records)
            bias = avg_actual - avg_predicted
            biases[category] = round(bias, 4)
        return biases

    def save(self, biases: dict[str, float]):
        self.biases_path.parent.mkdir(parents=True, exist_ok=True)
        self.biases_path.write_text(json.dumps(biases, indent=2))
