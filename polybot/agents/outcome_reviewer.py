import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

class OutcomeReviewer:
    def __init__(self, outcomes_dir: str):
        self.outcomes_dir = Path(outcomes_dir)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)

    def record_outcome(self, position_id, market_id, question, side, signal_score,
                       profitable, entry_price, exit_price, log_return, weight_version,
                       category="", indicator_snapshot: dict | None = None):
        record = {"position_id": position_id, "market_id": market_id, "question": question,
                  "side": side, "signal_score": signal_score,
                  "correct": profitable, "entry_price": entry_price,
                  "exit_price": exit_price, "log_return": log_return,
                  "weight_version": weight_version, "category": category,
                  "indicator_snapshot": indicator_snapshot or {},
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        filename = f"{position_id}_{market_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
        filepath = self.outcomes_dir / filename
        filepath.write_text(json.dumps(record, indent=2))
        logger.info(f"Recorded outcome for position {position_id}: profitable={profitable}")

    def load_all_outcomes(self) -> list[dict]:
        outcomes = []
        for filepath in self.outcomes_dir.glob("*.json"):
            try:
                outcomes.append(json.loads(filepath.read_text()))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load outcome {filepath}: {e}")
        return sorted(outcomes, key=lambda x: x.get("timestamp", ""))
