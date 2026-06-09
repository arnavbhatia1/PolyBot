"""Isotonic probability calibration with a single bootstrap-CI adoption gate.

Adopts only when the lower-80% CI of OOB log-loss improvement vs identity is
strictly positive — no in-sample fallback (isotonic has O(n) DoF so in-sample
improvement is structurally guaranteed and would let noise through).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
import numpy as np

from polybot.paths import CALIBRATION_PARAMS_PATH

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = CALIBRATION_PARAMS_PATH

_EPS = 1e-6                  # canonical clip — keep all clipping sites consistent
_BOOTSTRAP_N = 300
_BOOTSTRAP_LOWER_PCT = 20    # strict gate: lower-80% CI of OOB improvement must be positive
_DEFAULT_MIN_SAMPLES = 150

# --- Tail-overconfidence guards (operator-owned; calibration is changed by hand
# with tests, never pipeline-tuned). Isotonic overfits sparse extreme bins — a few
# lucky high/low-prob trades pool to ~0/1, and Kelly then MAX-sizes a "certain" bet
# that historically wins ~66%. Two layered guards close that hole:
#   1. Output clamp [_CAL_OUT_LO, _CAL_OUT_HI] — a DATA-JUSTIFIED confidence ceiling.
#      Realized win rates top out ~0.66-0.69 and bottom ~0.16 at this horizon, so no
#      calibrated probability beyond [0.15, 0.85] is ever warranted; capping there
#      bounds Kelly on the highest-conviction trades. The bound never touches an honest
#      fit (which tops ~0.66 — full margin); it only catches a sparse all-win/all-loss
#      tail bin that the prior alone can't fully temper at the very edge.
#   2. Beta-prior smoothing — _PRIOR_FRAC*n pseudo-observations at p=0.5, spread over
#      _PRIOR_ANCHORS points across the range. Dense mid-range barely moves; sparse
#      tails are pulled toward their realized rate. Weight scales with pool size, so
#      it tempers thin windows without crushing them. Tuned (0.10) so raw>=0.97 lands
#      ~0.66 (its realized win rate) at ~zero log-loss cost; it also TIGHTENS the
#      OOB-CI (less tail variance), so the regularized fit is MORE robustly adoptable.
_CAL_OUT_LO = 0.15
_CAL_OUT_HI = 0.85
_PRIOR_FRAC = 0.10
_PRIOR_ANCHORS = 50


def _weighted_log_loss(probs: np.ndarray, outcomes: np.ndarray, weights: np.ndarray) -> float:
    """Weighted binary cross-entropy. Internal helper for the adoption gate."""
    p = np.clip(probs, _EPS, 1.0 - _EPS)
    loss = -(outcomes * np.log(p) + (1 - outcomes) * np.log(1 - p))
    return float(np.sum(weights * loss) / np.sum(weights))


def _make_iso():
    """IsotonicRegression with the calibrated-output clamp baked in (guard #1)."""
    from sklearn.isotonic import IsotonicRegression
    return IsotonicRegression(out_of_bounds="clip", y_min=_CAL_OUT_LO, y_max=_CAL_OUT_HI)


def _augment_with_prior(probs: np.ndarray, outcomes: np.ndarray, weights: np.ndarray):
    """Append Beta-prior pseudo-observations (guard #2): _PRIOR_ANCHORS anchors at
    p=0.5 spread across [_CAL_OUT_LO, _CAL_OUT_HI], carrying _PRIOR_FRAC * (real
    sample count) total weight. Pulls sparse extreme bins toward the center so a
    handful of lucky tail trades can't slam the curve to 0/1; weight scales with the
    pool so thin windows are tempered, not crushed. No-op when the prior is disabled.
    """
    n = len(probs)
    if n == 0 or _PRIOR_FRAC <= 0.0 or _PRIOR_ANCHORS <= 0:
        return probs, outcomes, weights
    anchor_x = np.linspace(_CAL_OUT_LO, _CAL_OUT_HI, _PRIOR_ANCHORS)
    anchor_y = np.full(_PRIOR_ANCHORS, 0.5)
    anchor_w = np.full(_PRIOR_ANCHORS, (_PRIOR_FRAC * n) / _PRIOR_ANCHORS)
    return (np.concatenate([probs, anchor_x]),
            np.concatenate([outcomes, anchor_y]),
            np.concatenate([weights, anchor_w]))


class IsotonicCalibrator:
    """Monotone isotonic-regression probability calibrator."""

    def __init__(self) -> None:
        self._iso = None  # sklearn IsotonicRegression instance or None
        # Cached knot arrays for the np.interp fast path in calibrate(). Kept in
        # sync with _iso via _cache_thresholds() on every fit/load. np.interp over
        # these is numerically identical to IsotonicRegression.predict (both clip
        # then linearly interpolate over the same thresholds) but ~30x cheaper.
        self._x_thr: np.ndarray | None = None
        self._y_thr: np.ndarray | None = None
        self._n_samples: int = 0
        self._log_loss_improvement: float = 0.0
        # Diagnostic state exposed for the operator-visible cal_info dict in the
        # scheduler. Populated on every fit() call regardless of accept/reject so
        # the gate decision is never silent.
        self.last_fit_diagnostics: dict[str, float | int | str] = {}

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
    def lowest_learned_prob(self) -> float:
        """Lowest output the calibrator can return. When fitted, this is
        y_thresholds_[0] — raw inputs at or below the lowest x_threshold are
        clipped to this value, so the calibrated probability cannot fall below
        it. When identity (no fit), returns 0.0 so any "model says dead" override
        is inactive — no fit means no learned floor.
        """
        if self._iso is None:
            return 0.0
        return float(self._iso.y_thresholds_[0])

    @property
    def highest_learned_prob(self) -> float:
        """Highest output the calibrator can return (``y_thresholds_[-1]``); 1.0 at
        identity. The per-side dead floor for a Down hold is ``1 - highest_learned_prob``
        (its calibrated-P(down) minimum), so identity → floor 0.0 → override off.
        """
        if self._iso is None:
            return 1.0
        return float(self._iso.y_thresholds_[-1])

    @property
    def log_loss_improvement(self) -> float:
        """Last adopted fit's weighted log-loss gain vs identity (in nats).
        0.0 when at identity."""
        return self._log_loss_improvement

    # ---- application ----

    def _cache_thresholds(self) -> None:
        """Sync the np.interp knot arrays with the current _iso (clear at identity)."""
        if self._iso is None:
            self._x_thr = None
            self._y_thr = None
        else:
            self._x_thr = np.asarray(self._iso.X_thresholds_, dtype=float)
            self._y_thr = np.asarray(self._iso.y_thresholds_, dtype=float)

    def calibrate(self, raw_prob: float) -> float:
        """Apply isotonic calibration. Returns input unchanged when unfitted."""
        if self._iso is None:
            return raw_prob
        clipped = max(_EPS, min(1.0 - _EPS, raw_prob))
        # np.interp fast path over the cached knots (see _x_thr/_y_thr above): ~25us -> ~0.8us.
        out = float(np.interp(clipped, self._x_thr, self._y_thr))
        # Belt-and-suspenders clamp: fitted knots already lie within the bound, so this
        # only ever fires on a hand-edited/legacy file — never a silently certain bet.
        return min(_CAL_OUT_HI, max(_CAL_OUT_LO, out))

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
            probs_aug, outcomes_aug, w_aug = _augment_with_prior(probs_arr, outcomes_arr, w_arr)
            iso = _make_iso()
            iso.fit(probs_aug, outcomes_aug, sample_weight=w_aug)
        except Exception as e:
            logger.warning(f"Isotonic fit failed: {e}")
            return False

        y_min = float(iso.y_thresholds_[0])
        y_max = float(iso.y_thresholds_[-1])
        if y_min > 0.50 or y_max < 0.55:
            logger.info(
                f"Isotonic fit rejected: output range [{y_min:.3f}, {y_max:.3f}] "
                f"does not span [0.50, 0.55]"
            )
            return False

        # Adoption gate: bootstrap CI on log-loss improvement vs identity.
        # Refitting on N resamples accounts for the isotonic step-function variance
        # that the previous static 1e-4 threshold ignored.
        iso_predictions = iso.predict(probs_arr)
        improvement = (_weighted_log_loss(probs_arr, outcomes_arr, w_arr)
                       - _weighted_log_loss(iso_predictions, outcomes_arr, w_arr))

        # Out-of-bag bootstrap. Reseed each cycle so the CI tracks real sampling
        # variance instead of locking onto one fixed set of 300 resamples.
        rng = np.random.default_rng(int(time.time_ns() & 0xFFFFFFFF))
        n = len(probs_arr)
        all_idx = np.arange(n)
        boot_improvements: list[float] = []
        for _ in range(_BOOTSTRAP_N):
            idx = rng.integers(0, n, n)
            in_bag = np.zeros(n, dtype=bool)
            in_bag[idx] = True
            oob_idx = all_idx[~in_bag]
            if len(oob_idx) < 5:
                continue
            p_b, o_b, w_b = probs_arr[idx], outcomes_arr[idx], w_arr[idx]
            if w_b.sum() <= 0 or len(np.unique(o_b)) < 2:
                continue
            p_oob, o_oob, w_oob = probs_arr[oob_idx], outcomes_arr[oob_idx], w_arr[oob_idx]
            if w_oob.sum() <= 0:
                continue
            w_b_norm = w_b / w_b.sum() * len(w_b)
            w_oob_norm = w_oob / w_oob.sum() * len(w_oob)
            try:
                pb_aug, ob_aug, wb_aug = _augment_with_prior(p_b, o_b, w_b_norm)
                iso_b = _make_iso()
                iso_b.fit(pb_aug, ob_aug, sample_weight=wb_aug)
                boot_improvements.append(
                    _weighted_log_loss(p_oob, o_oob, w_oob_norm)
                    - _weighted_log_loss(iso_b.predict(p_oob), o_oob, w_oob_norm)
                )
            except Exception:
                continue

        ci_lower = float(np.percentile(boot_improvements, _BOOTSTRAP_LOWER_PCT)) if boot_improvements else 0.0
        ci_median = float(np.percentile(boot_improvements, 50)) if boot_improvements else 0.0
        self.last_fit_diagnostics = {
            "n_samples": int(len(probs)),
            "in_sample_improvement_nats": round(float(improvement), 6),
            "oob_ci_lower_nats": round(ci_lower, 6),
            "oob_ci_median_nats": round(ci_median, 6),
            "bootstrap_n_completed": len(boot_improvements),
            "y_min": round(y_min, 4),
            "y_max": round(y_max, 4),
        }
        if ci_lower <= 0:
            self.last_fit_diagnostics["decision"] = "rejected_ci"
            logger.info(
                f"Isotonic fit not significant (lower-{100-_BOOTSTRAP_LOWER_PCT}% CI={ci_lower:+.5f}, "
                f"median={ci_median:+.5f}); keeping previous state"
            )
            return False

        self._iso = iso
        self._cache_thresholds()
        self._n_samples = int(len(probs))
        self._log_loss_improvement = float(improvement)
        self.last_fit_diagnostics["decision"] = "adopted"
        logger.debug(
            f"Isotonic adopted: n={self._n_samples}, Δlog-loss={improvement:+.4f}, "
            f"lower-80% CI={ci_lower:+.5f}, knots={self.n_knots}"
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
        self._x_thr = self._y_thr = None
        self._n_samples = 0
        self._log_loss_improvement = 0.0

        if data.get("type") == "isotonic" and "x_thresholds" in data and "y_thresholds" in data:
            try:
                x_thr = np.asarray(data["x_thresholds"], dtype=float)
                y_thr = np.asarray(data["y_thresholds"], dtype=float)
                if len(x_thr) == 0 or len(x_thr) != len(y_thr):
                    raise ValueError(f"degenerate thresholds (x={len(x_thr)}, y={len(y_thr)})")
                iso = _make_iso()
                # Re-fitting on the threshold pairs recovers the function exactly
                # — they're already monotonic so isotonic is identity on its own knots.
                # The clamp also caps any legacy file whose knots exceed the bound
                # (e.g. a pre-guard curve that slammed toward ~1.0).
                iso.fit(x_thr, y_thr)
                self._iso = iso
                self._cache_thresholds()
                self._n_samples = int(data.get("n_samples", 0))
                self._log_loss_improvement = float(data.get("log_loss_improvement", 0.0))
                logger.debug(
                    f"Isotonic calibrator loaded: n={self._n_samples}, knots={len(x_thr)}"
                )
            except Exception as e:
                logger.warning(f"Failed to rehydrate isotonic state ({e}); falling back to identity")
                self._iso = None