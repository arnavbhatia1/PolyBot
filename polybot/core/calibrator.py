"""Platt scaling probability calibration.

Fits a 2-parameter sigmoid to map raw model probabilities to calibrated ones:
    calibrated = 1 / (1 + exp(A * logit(raw) + B))

Identity (no calibration): A = -1.0, B = 0.0
Minimum 100 outcomes required to fit.
"""
from __future__ import annotations

import json
import math
import logging
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = Path("polybot/memory/calibration/platt_params.json")


def compute_log_loss(probs: list[float], outcomes: list[int]) -> float:
    """Binary cross-entropy loss."""
    total = 0.0
    for p, y in zip(probs, outcomes):
        p = max(1e-10, min(1 - 1e-10, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(probs) if probs else float("inf")


class PlattCalibrator:

    def __init__(self, a: float = -1.0, b: float = 0.0) -> None:
        self.a: float = a
        self.b: float = b

    @property
    def is_identity(self) -> bool:
        return self.a == -1.0 and self.b == 0.0

    def calibrate(self, raw_prob: float) -> float:
        """Apply Platt scaling. With defaults (a=-1, b=0), returns raw_prob unchanged."""
        raw_prob = max(1e-6, min(1 - 1e-6, raw_prob))
        logit = math.log(raw_prob / (1.0 - raw_prob))
        return 1.0 / (1.0 + math.exp(self.a * logit + self.b))

    def fit(self, probs: list[float], outcomes: list[int],
            min_samples: int = 100) -> bool:
        """Fit calibration parameters from historical data. Returns True if successful."""
        if len(probs) < min_samples:
            logger.info(f"Platt calibration: {len(probs)} samples < {min_samples} minimum, skipping")
            return False

        probs_arr = np.clip(np.array(probs), 1e-6, 1 - 1e-6)
        outcomes_arr = np.array(outcomes, dtype=float)
        logits = np.log(probs_arr / (1 - probs_arr))

        def neg_log_likelihood(params):
            a, b_param = params
            p = 1.0 / (1.0 + np.exp(a * logits + b_param))
            p = np.clip(p, 1e-10, 1 - 1e-10)
            return -np.sum(outcomes_arr * np.log(p) + (1 - outcomes_arr) * np.log(1 - p))

        result = minimize(neg_log_likelihood, x0=[-1.0, 0.0], method="L-BFGS-B")
        if result.success:
            self.a = float(result.x[0])
            self.b = float(result.x[1])
            logger.info(f"Platt calibration fit: a={self.a:.4f}, b={self.b:.4f}")
            return True
        logger.warning(f"Platt calibration failed to converge: {result.message}")
        return False

    def save(self, path: Path | None = None) -> None:
        path = path or DEFAULT_PARAMS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"a": self.a, "b": self.b}, indent=2))

    def load(self, path: Path | None = None) -> None:
        path = path or DEFAULT_PARAMS_PATH
        if path.exists():
            data = json.loads(path.read_text())
            self.a = data.get("a", -1.0)
            self.b = data.get("b", 0.0)
            logger.info(f"Platt calibration loaded: a={self.a:.4f}, b={self.b:.4f}")
