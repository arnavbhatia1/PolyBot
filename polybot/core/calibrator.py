"""Platt scaling probability calibration.

Fits a 2-parameter sigmoid to map raw model probabilities to:
    calibrated = 1 / (1 + exp(A * logit(raw) + B))

Identity (no calibration): A = -1.0, B = 0.0

`A` is bounded strictly negative — a positive A inverts the model (high raw
prob → low calibrated prob), and `A ≈ 0` produces a constant-probability
output regardless of input. Both are pathological and indicate a data
problem, not a real calibration; the optimizer is constrained and a guard
fallback reverts to identity if A pins near the upper bound.
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

_EPS = 1e-6  # canonical clip — keep all clipping sites consistent
_PLATT_A_BOUNDS = (-5.0, -0.05)
_PLATT_B_BOUNDS = (-5.0, 5.0)
_PLATT_A_DEGENERATE_MARGIN = 0.03

def compute_log_loss(probs: list[float], outcomes: list[int]) -> float:
    """Binary cross-entropy loss."""
    total = 0.0
    for p, y in zip(probs, outcomes):
        p = max(_EPS, min(1 - _EPS, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(probs) if probs else float("inf")

class PlattCalibrator:

    def __init__(self, a: float = -1.0, b: float = 0.0) -> None:
        self.a: float = a
        self.b: float = b

    @property
    def is_identity(self) -> bool:
        # Tolerant check — L-BFGS-B can converge to -0.9999999 etc. which is still
        # effectively identity but would fail strict float equality.
        return abs(self.a - (-1.0)) < 1e-4 and abs(self.b) < 1e-4

    def calibrate(self, raw_prob: float) -> float:
        """Apply Platt scaling. With defaults (a=-1, b=0), returns raw_prob unchanged."""
        raw_prob = max(_EPS, min(1 - _EPS, raw_prob))
        logit = math.log(raw_prob / (1.0 - raw_prob))
        return 1.0 / (1.0 + math.exp(self.a * logit + self.b))

    def fit(self, probs: list[float], outcomes: list[int],mmin_samples: int = 60, sample_weights: list[float] | None = None) -> bool:
        """Fit calibration parameters from historical data. Returns True if successful.

        ``min_samples`` default of 60 matches the scheduler's calibration call
        site (was 200 historically, which silently blocked fitting even when
        the caller wanted to fit on a smaller window).

        ``sample_weights`` applies recency or importance weights to each sample.
        Both are applied multiplicatively in the log-likelihood, equivalent to
        weighted MLE. If None, all samples are weighted equally.

        L-BFGS-B is bounded so `a ∈ [-5, -0.05]` — the fit cannot invert the
        model. If the optimizer pins `a` near the upper bound (essentially
        flattening the sigmoid to a constant output), the fit is rejected and
        the calibrator falls back to identity rather than persist a degenerate
        mapping.
        """
        if len(probs) < min_samples:
            logger.info(f"Platt calibration: {len(probs)} samples < {min_samples} minimum, skipping")
            return False

        probs_arr = np.clip(np.array(probs), _EPS, 1 - _EPS)
        outcomes_arr = np.array(outcomes, dtype=float)
        logits = np.log(probs_arr / (1 - probs_arr))
        if sample_weights is not None and len(sample_weights) == len(probs):
            w_arr = np.array(sample_weights, dtype=float)
            w_arr = w_arr / w_arr.sum() * len(w_arr)  # normalise so total weight = n
        else:
            w_arr = np.ones(len(probs))

        def neg_log_likelihood(params):
            a, b_param = params
            p = 1.0 / (1.0 + np.exp(a * logits + b_param))
            p = np.clip(p, _EPS, 1 - _EPS)
            return -np.sum(w_arr * (outcomes_arr * np.log(p) + (1 - outcomes_arr) * np.log(1 - p)))

        result = minimize(
            neg_log_likelihood,
            x0=[-1.0, 0.0],
            method="L-BFGS-B",
            bounds=[_PLATT_A_BOUNDS, _PLATT_B_BOUNDS],
        )
        if not result.success:
            logger.warning(f"Platt calibration failed to converge: {result.message}")
            return False

        a_fit = float(result.x[0])
        b_fit = float(result.x[1])

        if a_fit > _PLATT_A_BOUNDS[1] - _PLATT_A_DEGENERATE_MARGIN:
            logger.warning(
                f"Platt fit converged at degenerate boundary (a={a_fit:.4f}); "
                f"reverting to identity"
            )
            self.a = -1.0
            self.b = 0.0
            return False

        self.a = a_fit
        self.b = b_fit
        logger.debug(f"Platt calibration fit: a={self.a:.4f}, b={self.b:.4f}")
        return True

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
            logger.debug(f"Platt calibration loaded: a={self.a:.4f}, b={self.b:.4f}")
