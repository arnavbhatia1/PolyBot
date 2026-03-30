import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PromptBuilder:
    def __init__(self, prompts_dir: str, biases_path: str | None = None, lessons_path: str | None = None):
        self.prompts_dir = Path(prompts_dir)
        self.biases_path = Path(biases_path) if biases_path else None
        self.lessons_path = Path(lessons_path) if lessons_path else None

    def load_base_prompt(self, version: str) -> str:
        path = self.prompts_dir / f"{version}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Prompt version not found: {path}")
        return path.read_text(encoding="utf-8")

    def _load_biases(self) -> dict[str, float]:
        if not self.biases_path or not self.biases_path.exists():
            return {}
        try:
            return json.loads(self.biases_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load biases: {e}")
            return {}

    def _load_lessons(self) -> dict[str, str]:
        if not self.lessons_path or not self.lessons_path.exists():
            return {}
        try:
            return json.loads(self.lessons_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load lessons: {e}")
            return {}

    def build(self, version: str, category: str = "") -> str:
        parts = [self.load_base_prompt(version)]
        biases = self._load_biases()
        if category and category in biases:
            correction = biases[category]
            direction = "overestimate" if correction < 0 else "underestimate"
            pct = abs(correction * 100)
            parts.append(f"\nBIAS CORRECTION: You historically {direction} {category} markets by {pct:.0f}%. Adjust accordingly.")
        lessons = self._load_lessons()
        if lessons:
            top_lessons = list(lessons.values())[:5]
            parts.append("\nLESSONS FROM PAST TRADES:")
            for i, lesson in enumerate(top_lessons, 1):
                parts.append(f"  {i}. {lesson}")
        return "\n".join(parts)
