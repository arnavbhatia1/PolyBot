"""Isotonic probability calibration. Replaces Platt (2-param sigmoid couldn't
correct per-quartile miscalibration on thin windows). Adopts only if bootstrap-CI
lower bound of weighted log-loss improvement vs identity > 0; else stays identity.
"""
from __future__ import annotations

import json
import math
import logging
from pathlib import Path
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = Path("polybot/memory/calibration/isotonic_params.json")

_EPS = 1e-6  # canonical clip — keep all clipping sites consistent

# Bootstrap CI gate: how many resamples to draw, and the lower-percentile bound
# the improvement-over-identity must clear. 100 resamples / lower-80% bound > 0
# replaces a static 1e-4 floor that was within sampling noise on a 7-day window.
_BOOTSTRAP_N = 100
_BOOTSTRAP_LOWER_PCT = 20

# Minimum samples for a stable isotonic fit. Isotonic has many degrees of
# freedom and overfits readily on small data — keep this generous.
_DEFAULT_MIN_SAMPLES = 150


def compute_log_loss(probs: list[float], outcomes: list[int]) -> float:
    """Binary cross-entropy loss."""
    total = 0.0
    for p, y in zip(probs, outcomes):
        p = max(_EPS, min(1 - _EPS, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(probs) if probs else float("inf")


def _weighted_log_loss(probs: np.ndarray, outcomes: np.ndarray, weights: np.ndarray) -> float:
    """Weighted binary cross-entropy. Internal helper for the adoption gate."""
    p = np.clip(probs, _EPS, 1.0 - _EPS)
    loss = -(outcomes * np.log(p) + (1 - outcomes) * np.log(1 - p))
    return float(np.sum(weights * loss) / np.sum(weights))


class IsotonicCalibrator:
    """Monotone isotonic-regression probability calibrator."""

    def __init__(self) -> None:
        self._iso = None  # sklearn IsotonicRegression instance or None
        self._n_samples: int = 0
        self._log_loss_improvement: float = 0.0

    # ---- public read-only state ----

    @property
    def is_identity(self) -> bool:
        return self._iso is None

    @property
    def n_knots(self) -> int:
        """Number of monotonic knots in the fitted isotonic. 0 when unfitted."""
        if self._iso is None:
            return 0
        return int(len(getattr(self._iso, "X_thresholds_", [])))

    @property
    def log_loss_improvement(self) -> float:
        """Last adopted fit's weighted log-loss gain vs identity (in nats).
        0.0 when at identity."""
        return self._log_loss_improvement

    @property
    def state_hash(self) -> str:
        """12-char digest of fitted thresholds (or "identity"). Lets backtests
        stratify by calibrator-in-effect at fill time.
        """
        if self._iso is None:
            return "identity"
        import hashlib
        x = np.round(self._iso.X_thresholds_, 6).tobytes()
        y = np.round(self._iso.y_thresholds_, 6).tobytes()
        return hashlib.blake2b(x + y, digest_size=6).hexdigest()

    # ---- application ----

    def calibrate(self, raw_prob: float) -> float:
        """Apply isotonic calibration. Returns input unchanged when unfitted."""
        if self._iso is None:
            return raw_prob
        clipped = max(_EPS, min(1.0 - _EPS, raw_prob))
        return float(self._iso.predict(np.array([clipped]))[0])

    # ---- fitting ----

    def fit(self, probs: list[float], outcomes: list[int],
            min_samples: int = _DEFAULT_MIN_SAMPLES,
            sample_weights: list[float] | None = None) -> bool:
        """Fit isotonic. Returns True iff bootstrap-CI lower bound vs identity > 0.
        Rejection leaves state unchanged (keeps previous fit or identity).
        """
        if len(probs) < min_samples:
            logger.info(f"Isotonic calibration: {len(probs)} samples < {min_samples} minimum, skipping")
            return False

        probs_arr = np.clip(np.asarray(probs, dtype=float), _EPS, 1.0 - _EPS)
        outcomes_arr = np.asarray(outcomes, dtype=float)
        if sample_weights is not None and len(sample_weights) == len(probs):
            w_arr = np.asarray(sample_weights, dtype=float)
            total = w_arr.sum()
            if total <= 0:
                logger.warning("Isotonic calibration: non-positive total sample weight; skipping")
                return False
            w_arr = w_arr / total * len(w_arr)  # normalise so total weight = n
        else:
            w_arr = np.ones(len(probs))

        try:
            from sklearn.isotonic import IsotonicRegression
            iso = IsotonicRegression(out_of_bounds="clip", y_min=_EPS, y_max=1.0 - _EPS)
            iso.fit(probs_arr, outcomes_arr, sample_weight=w_arr)
        except Exception as e:
            logger.warning(f"Isotonic fit failed: {e}")
            return False

        # Healthy fit must be able to output across at least [0.2, 0.8].
        y_min = float(iso.y_thresholds_[0])
        y_max = float(iso.y_thresholds_[-1])
        if y_min > 0.2 or y_max < 0.8:
            logger.info(
                f"Isotonic fit rejected: output range [{y_min:.3f}, {y_max:.3f}] "
                f"does not span [0.2, 0.8] — directionally asymmetric"
            )
            return False

        # Adoption gate: bootstrap CI on log-loss improvement vs identity.
        # Refitting on N resamples accounts for the isotonic step-function variance
        # that the previous static 1e-4 threshold ignored.
        iso_predictions = iso.predict(probs_arr)
        improvement = (_weighted_log_loss(probs_arr, outcomes_arr, w_arr)
                       - _weighted_log_loss(iso_predictions, outcomes_arr, w_arr))

        rng = np.random.default_rng(42)
        n = len(probs_arr)
        boot_improvements: list[float] = []
        for _ in range(_BOOTSTRAP_N):
            idx = rng.integers(0, n, n)
            p_b, o_b, w_b = probs_arr[idx], outcomes_arr[idx], w_arr[idx]
            if w_b.sum() <= 0 or len(np.unique(o_b)) < 2:
                continue
            try:
                iso_b = IsotonicRegression(out_of_bounds="clip", y_min=_EPS, y_max=1.0 - _EPS)
                iso_b.fit(p_b, o_b, sample_weight=w_b)
                boot_improvements.append(
                    _weighted_log_loss(p_b, o_b, w_b)
                    - _weighted_log_loss(iso_b.predict(p_b), o_b, w_b)
                )
            except Exception:
                continue

        ci_lower = float(np.percentile(boot_improvements, _BOOTSTRAP_LOWER_PCT)) if boot_improvements else 0.0
        if ci_lower <= 0:
            logger.info(
                f"Isotonic fit not significant (Δlog-loss={improvement:+.5f}, "
                f"bootstrap lower-{100-_BOOTSTRAP_LOWER_PCT}% CI={ci_lower:+.5f}); keeping previous state"
            )
            return False

        self._iso = iso
        self._n_samples = int(len(probs))
        self._log_loss_improvement = float(improvement)
        logger.debug(
            f"Isotonic adopted: n={self._n_samples}, "
            f"Δlog-loss={improvement:+.4f}, knots={self.n_knots}"
        )
        return True

    # ---- persistence ----

    def save(self, path: Path | None = None) -> None:
        path = path or DEFAULT_PARAMS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._iso is None:
            payload: dict = {"type": "identity"}
        else:
            payload = {
                "type": "isotonic",
                "x_thresholds": self._iso.X_thresholds_.tolist(),
                "y_thresholds": self._iso.y_thresholds_.tolist(),
                "n_samples": self._n_samples,
                "log_loss_improvement": round(self._log_loss_improvement, 4),
            }
        path.write_text(json.dumps(payload, indent=2))

    def load(self, path: Path | None = None) -> None:
        path = path or DEFAULT_PARAMS_PATH
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"Calibrator state failed to parse ({e}); using identity")
            return

        # Reset to identity baseline first so a partial/garbled file can't half-load.
        self._iso = None
        self._n_samples = 0
        self._log_loss_improvement = 0.0

        if data.get("type") == "isotonic" and "x_thresholds" in data and "y_thresholds" in data:
            try:
                from sklearn.isotonic import IsotonicRegression
                x_thr = np.asarray(data["x_thresholds"], dtype=float)
                y_thr = np.asarray(data["y_thresholds"], dtype=float)
                if len(x_thr) == 0 or len(x_thr) != len(y_thr):
                    raise ValueError(f"degenerate thresholds (x={len(x_thr)}, y={len(y_thr)})")
                iso = IsotonicRegression(out_of_bounds="clip", y_min=_EPS, y_max=1.0 - _EPS)
                # Re-fitting on the threshold pairs recovers the function exactly
                # — they're already monotonic so isotonic is identity on its own knots.
                iso.fit(x_thr, y_thr)
                self._iso = iso
                self._n_samples = int(data.get("n_samples", 0))
                self._log_loss_improvement = float(data.get("log_loss_improvement", 0.0))
                logger.debug(
                    f"Isotonic calibrator loaded: n={self._n_samples}, knots={len(x_thr)}"
                )
            except Exception as e:
                logger.warning(f"Failed to rehydrate isotonic state ({e}); falling back to identity")
                self._iso = None