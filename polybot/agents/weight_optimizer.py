import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

class WeightOptimizer:
    def __init__(self, weights_dir: str, scores_path: str, min_improvement: float = 0.03):
        self.weights_dir = Path(weights_dir)
        self.scores_path = Path(scores_path)
        self.min_improvement = min_improvement

    def get_scores(self) -> dict:
        if not self.scores_path.exists():
            return {}
        return json.loads(self.scores_path.read_text())

    def get_best_version(self) -> str:
        scores = self.get_scores()
        if not scores:
            return "weights_v001"
        return max(scores, key=lambda v: scores[v].get("sharpe", 0))

    def record_score(self, version: str, sharpe: float, total_trades: int, win_rate: float):
        scores = self.get_scores()
        scores[version] = {"sharpe": round(sharpe, 4), "total_trades": total_trades, "win_rate": round(win_rate, 4)}
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(json.dumps(scores, indent=2))

    def save_weights(self, version: str, weights: dict):
        self.weights_dir.mkdir(parents=True, exist_ok=True)
        (self.weights_dir / f"{version}.json").write_text(json.dumps(weights, indent=2))

    def should_adopt(self, current_sharpe: float, candidate_sharpe: float) -> bool:
        return (candidate_sharpe - current_sharpe) >= self.min_improvement

    def get_next_version(self) -> str:
        existing = list(self.weights_dir.glob("weights_v*.json"))
        if not existing:
            return "weights_v001"
        numbers = []
        for f in existing:
            match = re.search(r"v(\d+)", f.stem)
            if match:
                numbers.append(int(match.group(1)))
        return f"weights_v{max(numbers) + 1:03d}" if numbers else "weights_v001"
