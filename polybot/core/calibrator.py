"""Isotonic probability calibration.

Despite the legacy class name (`PlattCalibrator`), the internal calibration
function is a **monotone isotonic regression**. Platt scaling — a 2-parameter
sigmoid — was structurally incapable of correcting per-quartile miscalibration
(e.g. "Q4 edge realization 0.56 but Q1–Q3 well-calibrated"); a fit on small
windows could collapse to near-flat slope and crush the model's dynamic range
to a 12-point band. Isotonic learns an arbitrary monotonic step function with
as many knots as the data supports, so the same 7-day window now produces a
useful correction whenever one exists, and an explicit identity fallback when
it doesn't.

Class and method names are preserved so the rest of the codebase doesn't
care which fit family lives behind `calibrate()`.

Fit protocol:
  1. Need at least `min_samples` data points (default 150).
  2. Fit isotonic on (probs, outcomes) with recency `sample_weights`.
  3. Compute weighted log-loss for both isotonic and identity on the same
     pool. Adopt only if isotonic beats identity by `_ISO_IMPROVEMENT_FLOOR`.
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

DEFAULT_PARAMS_PATH = Path("polybot/memory/calibration/platt_params.json")

_EPS = 1e-6  # canonical clip — keep all clipping sites consistent

# Minimum log-loss reduction (in nats per sample) required to adopt an isotonic
# fit over identity. 1e-4 is roughly 1/100 of a "small" calibration miss; below
# this the gain is indistinguishable from sampling noise on a 7-day window.
_ISO_IMPROVEMENT_FLOOR = 1e-4

# Minimum samples for a stable isotonic fit. Higher than Platt's 60 because
# isotonic has more degrees of freedom and overfits more readily on small data.
_DEFAULT_MIN_SAMPLES = 150


def compute_log_loss(probs: list[float], outcomes: list[int]) -> float:
    """Binary cross-entropy loss, identical to the previous Platt module
    implementation so all external call sites keep working."""
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


class PlattCalibrator:
    """Isotonic regression calibrator. Class name is legacy.

    External contract:
      * `calibrate(raw_prob: float) -> float` — apply calibration; identity when
        unfitted.
      * `fit(probs, outcomes, min_samples=150, sample_weights=None) -> bool` —
        attempt to learn the calibration; returns True only if the fit
        beats identity on weighted log-loss by `_ISO_IMPROVEMENT_FLOOR`.
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

        Adoption requires weighted log-loss reduction ≥ ``_ISO_IMPROVEMENT_FLOOR``
        vs identity on the same pool. The pool's weights are recency-decayed
        by the caller (typically 0.97 / day, ~23-day half-life) so the fit
        and the identity-floor check share the same emphasis.

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

        # Adoption gate: did isotonic actually beat identity on this pool?
        # Both losses use the same weights so the comparison is apples-to-apples.
        iso_predictions = iso.predict(probs_arr)
        iso_loss = _weighted_log_loss(iso_predictions, outcomes_arr, w_arr)
        identity_loss = _weighted_log_loss(probs_arr, outcomes_arr, w_arr)
        improvement = identity_loss - iso_loss

        if improvement < _ISO_IMPROVEMENT_FLOOR:
            logger.info(
                f"Isotonic fit does not beat identity (Δlog-loss={improvement:+.5f} "
                f"< floor={_ISO_IMPROVEMENT_FLOOR}); keeping previous state"
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