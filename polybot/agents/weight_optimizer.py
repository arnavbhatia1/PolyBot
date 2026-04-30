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


def _lag1_autocorr(values: list[float]) -> float:
    """1-lag autocorrelation of a returns series. Returns 0 when undefined."""
    if len(values) < 3:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    num = sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n))
    den = sum((v - mean) ** 2 for v in values)
    return num / den if den > 0 else 0.0


def _sharpe_z_test(old_sharpe: float, new_sharpe: float, n_trades: int,
                   returns: list[float] | None = None) -> float:
    """Z-score for Sharpe ratio improvement (Jobson-Korkie 1981 SE approximation).

    When ``returns`` is supplied, inflates the standard error by
    ``sqrt(1 + 2 × max(0, autocorr_1lag))`` — the iid assumption of the
    vanilla Jobson-Korkie SE overstates confidence when outcomes are
    positively autocorrelated (which they are for BTC 5-min regimes).
    """
    if n_trades < 2:
        return 0.0
    se = math.sqrt((1.0 + 0.5 * old_sharpe ** 2) / max(n_trades, 1))
    if returns and len(returns) >= 3:
        rho = _lag1_autocorr(returns)
        se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
    return (new_sharpe - old_sharpe) / se if se > 0 else 0.0


class WeightOptimizer:
    def __init__(self, weights_dir: str, scores_path: str, min_improvement: float = 0.03) -> None:
        self.weights_dir: Path = Path(weights_dir)
        self.scores_path: Path = Path(scores_path)
        self.min_improvement: float = min_improvement  # absolute floor (legacy compat)
        self.se_floor_coefficient: float = 0.25  # scaled by scheduler to 0.15 in crisis mode

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
                     n_trades: int = 0,
                     candidate_returns: list[float] | None = None) -> tuple[bool, str, float]:
        """Noise-scaled adoption check for Sharpe improvement.

        Returns (adopt, reason, z_score). z is delta / JK_SE — exposed as a
        structured field so callers don't have to parse it out of `reason`.

        Gates (all must pass):
          1. candidate_sharpe > 0
          2. n_trades >= 100
          3. delta >= max(min_improvement, se_floor_coefficient × JK_SE)

        The walk-forward fold-consistency gate was removed — it had been
        relaxed to "≥1 of 4 folds improve", which is implied by any positive
        aggregate delta and so contributed no independent statistical
        confirmation. Fold Sharpes are still computed and logged as
        diagnostics in `change_info["fold_sharpes"]`.
        """
        delta = candidate_sharpe - current_sharpe

        if candidate_sharpe <= 0:
            return False, f"candidate Sharpe {candidate_sharpe:.3f} <= 0", 0.0

        if n_trades < 100:
            return False, (
                f"only {n_trades} candidate trades (need 100) — your min_model_probability "
                f"or min_edge may be filtering too aggressively in the backtest"
            ), 0.0

        # Jobson-Korkie SE, autocorr-adjusted.
        se = math.sqrt((1.0 + 0.5 * current_sharpe ** 2) / max(n_trades, 1))
        if candidate_returns and len(candidate_returns) >= 3:
            rho = _lag1_autocorr(candidate_returns)
            se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))

        z = delta / se if se > 0 else 0.0
        dynamic_floor = max(self.min_improvement, self.se_floor_coefficient * se)

        if delta < dynamic_floor:
            return False, (
                f"delta {delta:+.4f} below floor {dynamic_floor:.4f} "
                f"(abs_floor={self.min_improvement:.3f}, SE={se:.3f})"
            ), z

        return True, f"delta={delta:+.4f} floor={dynamic_floor:.4f} z={z:.2f} n={n_trades}", z

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
