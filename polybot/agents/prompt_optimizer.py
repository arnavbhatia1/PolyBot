import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

class PromptOptimizer:
    def __init__(self, prompts_dir: str, scores_path: str, min_improvement: float = 0.03):
        self.prompts_dir = Path(prompts_dir)
        self.scores_path = Path(scores_path)
        self.min_improvement = min_improvement

    def get_version_scores(self) -> dict:
        if not self.scores_path.exists():
            return {}
        return json.loads(self.scores_path.read_text())

    def get_best_version(self) -> str:
        scores = self.get_version_scores()
        if not scores:
            return "v001"
        return max(scores, key=lambda v: scores[v]["accuracy"])

    def record_score(self, version: str, accuracy: float, total: int):
        scores = self.get_version_scores()
        scores[version] = {"accuracy": round(accuracy, 4), "total": total}
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(json.dumps(scores, indent=2))

    def get_next_version(self) -> str:
        existing = list(self.prompts_dir.glob("v*.txt"))
        if not existing:
            return "v001"
        numbers = []
        for f in existing:
            match = re.match(r"v(\d+)", f.stem)
            if match:
                numbers.append(int(match.group(1)))
        next_num = max(numbers) + 1 if numbers else 1
        return f"v{next_num:03d}"

    def save_prompt(self, version: str, content: str):
        path = self.prompts_dir / f"{version}.txt"
        path.write_text(content)

    def should_adopt(self, current_accuracy: float, candidate_accuracy: float) -> bool:
        return (candidate_accuracy - current_accuracy) >= self.min_improvement
