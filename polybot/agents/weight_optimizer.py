from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _sharpe(returns: list[float]) -> float:
    """Per-trade unannualized Sharpe from a list of gain_pct values."""
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    var = sum((r - avg) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.0
    return avg / std if std > 0 else 0.0


def _sharpe_z_test(old_sharpe: float, new_sharpe: float, n_trades: int) -> float:
    """Z-score for Sharpe ratio improvement (Jobson-Korkie 1981 SE approximation)."""
    if n_trades < 2:
        return 0.0
    se = math.sqrt((1.0 + 0.5 * old_sharpe ** 2) / max(n_trades, 1))
    return (new_sharpe - old_sharpe) / se if se > 0 else 0.0


class WeightOptimizer:
    def __init__(self, weights_dir: str, scores_path: str, min_improvement: float = 0.03) -> None:
        self.weights_dir: Path = Path(weights_dir)
        self.scores_path: Path = Path(scores_path)
        self.min_improvement: float = min_improvement  # absolute floor (legacy compat)

    def get_scores(self) -> dict[str, Any]:
        if not self.scores_path.exists():
            return {}
        return json.loads(self.scores_path.read_text())

    def get_best_version(self) -> str:
        scores = self.get_scores()
        if not scores:
            return "weights_v001"
        return max(scores, key=lambda v: scores[v].get("sharpe", 0))

    def record_score(self, version: str, sharpe: float, total_trades: int, win_rate: float) -> None:
        scores = self.get_scores()
        scores[version] = {"sharpe": round(sharpe, 4), "total_trades": total_trades, "win_rate": round(win_rate, 4)}
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(json.dumps(scores, indent=2))

    def save_weights(self, version: str, weights: dict[str, Any]) -> None:
        self.weights_dir.mkdir(parents=True, exist_ok=True)
        (self.weights_dir / f"{version}.json").write_text(json.dumps(weights, indent=2))

    def should_adopt(self, current_sharpe: float, candidate_sharpe: float,
                     n_trades: int = 0, fold_sharpes: list[float] | None = None) -> tuple[bool, str]:
        """Statistical significance test for Sharpe improvement.

        Uses Jobson-Korkie (1981) SE for Sharpe ratio difference.
        Returns (adopt, reason) tuple.

        Gates:
          1. candidate_sharpe > 0 (don't adopt negative)
          2. delta >= min_improvement (absolute floor)
          3. n_trades >= 100 (minimum sample)
          4. z_score >= 1.65 (95% one-tailed significance)
          5. All walk-forward folds positive (if provided)
        """
        delta = candidate_sharpe - current_sharpe

        if candidate_sharpe <= 0:
            return False, f"candidate Sharpe {candidate_sharpe:.3f} <= 0"

        if delta < self.min_improvement:
            return False, f"delta {delta:.3f} below floor {self.min_improvement}"

        if n_trades < 100:
            return False, f"only {n_trades} trades (need 100)"

        z = _sharpe_z_test(current_sharpe, candidate_sharpe, n_trades)
        if z < 1.65:
            return False, f"z={z:.2f} < 1.65 (not significant at 95%)"

        # Walk-forward consistency: every fold must show improvement
        if fold_sharpes:
            neg_folds = sum(1 for s in fold_sharpes if s <= current_sharpe)
            if neg_folds > 0:
                return False, f"{neg_folds}/{len(fold_sharpes)} folds below baseline"

        return True, f"z={z:.2f} delta={delta:.3f} n={n_trades}"

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
