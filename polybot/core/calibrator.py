"""Isotonic probability calibration.

A 2-parameter Platt sigmoid was structurally incapable of correcting
per-quartile miscalibration (e.g. "Q4 edge realization 0.56 but Q1–Q3
well-calibrated") — fits on small windows could collapse to near-flat slope
and crush the model's dynamic range to a 12-point band. Isotonic learns an
arbitrary monotonic step function with as many knots as the data supports,
so the same 7-day window produces a useful correction whenever one exists,
and an explicit identity fallback when it doesn't.

Fit protocol:
  1. Need at least `min_samples` data points (default 150).
  2. Fit isotonic on (probs, outcomes) with recency `sample_weights`.
  3. Compute weighted log-loss for both isotonic and identity on the same
     pool. Adopt only if the bootstrap-CI lower bound of the improvement > 0.
  4. Otherwise revert to identity. This replaces Platt's brittle
     "slope-near-zero" heuristic with a direct beat-identity test.

Storage format (JSON, single file):
  - `type: "isotonic"` with `x_thresholds` / `y_thresholds` for round-trip,
    plus `n_samples` and `log_loss_improvement` for telemetry.
  - `type: "identity"` or absent → calibrator stays at identity.
"""
from __future__ import annotations

import json
import math
import logging
from pathlib import Path
import numpy as np

logger = logging.getLogger(__name__)

# Filename retains the `platt_` prefix only for on-disk continuity with existing
# state snapshots — the contents are isotonic thresholds, not Platt parameters.
DEFAULT_PARAMS_PATH = Path("polybot/memory/calibration/platt_params.json")

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
    """Monotone isotonic-regression probability calibrator.

    External contract:
      * `calibrate(raw_prob: float) -> float` — apply calibration; identity when
        unfitted.
      * `fit(probs, outcomes, min_samples=150, sample_weights=None) -> bool` —
        attempt to learn the calibration; returns True only if the fit
        beats identity on weighted log-loss with the bootstrap CI lower bound > 0.
      * `is_identity: bool` — True when calibration is a no-op.
      * `save(path)` / `load(path)` — JSON round-trip.
    """

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
        """Short stable hash of the calibrator's effective function.

        Returns ``"identity"`` when unfitted, else a 12-char hex digest derived
        from the rounded threshold arrays. Two calibrators that produce the
        same calibration curve (within rounding) hash to the same value, so
        backtests can stratify outcomes by which calibrator was live at fill
        time.
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
        """Fit isotonic on (probs, outcomes). Returns True iff adopted.

        Adoption requires the bootstrap-CI lower bound of the weighted log-loss
        improvement over identity to exceed 0 (100 resamples, lower 80% bound).
        Weights are recency-decayed by the caller (~0.94/day, ~11d half-life)
        so the fit and gate share the same emphasis.

        On rejection the calibrator state is unchanged; it remains identity
        if it was identity, otherwise it keeps the previous fit. This mirrors
        the existing scheduler logic that selectively replaces / reverts.
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
                # Re-fitting on (x_thresholds, y_thresholds) recovers the function
                # exactly: the projection is already monotonic so isotonic acts as
                # identity on its own knots. Verified by round-trip test.
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