import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

class OutcomeReviewer:
    def __init__(self, outcomes_dir: str):
        self.outcomes_dir = Path(outcomes_dir)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)

    def _evaluate(self, predicted_probability: float, actual_outcome: bool) -> dict:
        actual_value = 1.0 if actual_outcome else 0.0
        correct = (predicted_probability >= 0.5) == actual_outcome
        error = abs(predicted_probability - actual_value)
        return {"correct": correct, "error": round(error, 4)}

    def record_outcome(self, position_id, market_id, question, side, predicted_probability,
                       actual_outcome, entry_price, exit_price, log_return, prompt_version,
                       category="", indicator_snapshot: dict | None = None):
        evaluation = self._evaluate(predicted_probability, actual_outcome)
        record = {"position_id": position_id, "market_id": market_id, "question": question,
                  "side": side, "predicted_probability": predicted_probability,
                  "actual_outcome": actual_outcome, "entry_price": entry_price,
                  "exit_price": exit_price, "log_return": log_return,
                  "prompt_version": prompt_version, "category": category,
                  "indicator_snapshot": indicator_snapshot or {},
                  "correct": evaluation["correct"], "error": evaluation["error"],
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        filename = f"{position_id}_{market_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
        filepath = self.outcomes_dir / filename
        filepath.write_text(json.dumps(record, indent=2))
        logger.info(f"Recorded outcome for position {position_id}: correct={evaluation['correct']}")

    def load_all_outcomes(self) -> list[dict]:
        outcomes = []
        for filepath in self.outcomes_dir.glob("*.json"):
            try:
                outcomes.append(json.loads(filepath.read_text()))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load outcome {filepath}: {e}")
        return sorted(outcomes, key=lambda x: x.get("timestamp", ""))
