"""BaseRecommender — shared logic for Claude/Local recommenders.

Every cycle, probe every tunable param. Walk-forward + z-test in the weight
optimizer decides which probes improve Sharpe and get adopted. Subclasses
implement ``recommend()`` to add reactive candidates on top of the shared
exploratory probe.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from polybot.config.param_registry import CLAMP_RANGES, default_for as _d

EXPLORE_STEPS: dict[str, float] = {
    "atr_sigma_ratio":       0.15,
    "logit_scale":           0.50,
    "student_t_df":          1,
    "momentum_weight":       0.04,
    "regime_weight":         0.01,
    "flow_weight":           0.02,
    "spot_flow_weight":      0.02,
    "prev_margin_weight":    0.01,
    "min_atr":               3.0,
    "kelly_fraction":        0.02,
    "min_model_probability": 0.02,
    "min_edge":              0.01,
    "min_kelly":             0.004,
    "regime_momentum_threshold":  0.04,
    "final_logit_clamp":          0.50,
    "l5_regime_damp_cap":         0.10,
    "atr_regime_shift_threshold": 0.10,
    "exit_edge_threshold":        0.02,
    "derived_log_atr_ratio_weight":        0.01,
    "derived_autocorr_signed_mag_weight":  0.01,
    "derived_flow_disagreement_weight":    0.01,
}

# Forced one-time exploration of audit-identified values. Each (param, value)
# fires exactly once — when the directional table has no prior record AND the
# live value isn't already there. Drives 3.4(2) L6 turn-on probes and 3.5
# `exit_edge_threshold` sweep grounded in counterfactual data.
STRUCTURAL_PROBES: list[tuple[str, float, str]] = [
    ("exit_edge_threshold", -0.08, "structural probe — counterfactual hold-better at edge ≈ -0.08"),
    ("exit_edge_threshold", -0.05, "structural probe — counterfactual hold-better at edge ≈ -0.05"),
    ("exit_edge_threshold", -0.03, "structural probe — counterfactual hold-better at edge ≈ -0.03"),
    ("derived_log_atr_ratio_weight",       0.005, "structural probe — L6 feature never raised off zero"),
    ("derived_autocorr_signed_mag_weight", 0.005, "structural probe — L6 feature never raised off zero"),
    ("derived_flow_disagreement_weight",   0.005, "structural probe — L6 feature never raised off zero"),
]

_CAP = 5         # max changes adopted per cycle
_MIN_N = 50      # min trades before any proposal

# Adaptive step ramping. When a param's recent probes return |Δ Sharpe| under the
# adoption noise floor, EXPLORE_STEPS at the base size will never adopt — the delta
# is statistically indistinguishable from baseline. Ramp the step up so the pipeline
# can escape the soft-local bowl. Reset to base on any adoption (handled implicitly
# by directional_table evidence becoming a non-trivial bt_delta).
_RAMP_NOISE_FLOOR_FALLBACK = 0.003
_RAMP_PER_DEAD_DIRECTION = 0.5
_RAMP_MAX = 3.0


def empirical_noise_floor(baseline_jk_se: float | None) -> float:
    """Adoption-z × empirical JK_SE — tracks real sample variance per cycle.
    Falls back to the legacy 0.003 constant when the scheduler hasn't precomputed
    baseline JK_SE yet (first cycle after restart)."""
    if baseline_jk_se is None or baseline_jk_se <= 0:
        return _RAMP_NOISE_FLOOR_FALLBACK
    return max(_RAMP_NOISE_FLOOR_FALLBACK, 0.3 * float(baseline_jk_se))


def _clamp(value: Any, param: str) -> Any:
    if param not in CLAMP_RANGES:
        return value
    lo, hi, cast = CLAMP_RANGES[param]
    try:
        return cast(max(lo, min(hi, cast(value))))
    except (TypeError, ValueError):
        return value


class BaseRecommender:
    SOURCE_NAME: str = "base"

    def __init__(self, analysis: dict[str, Any], current_config: dict[str, Any]) -> None:
        self.analysis = analysis or {}
        self.cfg = current_config or {}
        self.proposals: list[dict[str, Any]] = []
        self.manual_obs: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self._dir_table = self._parse_dir_table(self.analysis.get("directional_table", ""))
        self._failed_values = self._parse_failures(self.analysis.get("cumulative_failures", {}))

    def recommend(self) -> dict[str, Any]:
        raise NotImplementedError

    # ---- output ---- #

    def _envelope(self, changes: list[dict[str, Any]] | None = None,
                  reasoning: str = "", confidence: str = "medium") -> dict[str, Any]:
        return {
            "changes": changes or [],
            "manual_observations": self.manual_obs,
            "risk_warnings": self.warnings[:3],
            "reasoning": reasoning or f"{self.SOURCE_NAME} recommender",
            "confidence": confidence,
        }

    def _finalize(self, reasoning: str = "", confidence: str = "medium") -> dict[str, Any]:
        deduped = self._dedupe(self.proposals)
        recently_tested = set(self.analysis.get("recently_tested_params", []))

        # Guarantee at least 2 slots go to params not tested in the last 3 cycles.
        # Prevents the same 5 params being proposed every run when nothing adopts.
        fresh = [p for p in deduped if p.get("param") not in recently_tested]
        stale = [p for p in deduped if p.get("param") in recently_tested]
        for bucket in (fresh, stale):
            bucket.sort(key=lambda c: -abs(float(c.get("predicted_delta_sharpe_7d", 0.0))))

        guaranteed = fresh[:2]
        remaining = (fresh[2:] + stale)
        remaining.sort(key=lambda c: -abs(float(c.get("predicted_delta_sharpe_7d", 0.0))))
        result = (guaranteed + remaining)[:_CAP]
        return self._envelope(result, reasoning=reasoning, confidence=confidence)

    def _insufficient(self, n: int) -> dict[str, Any]:
        self.warnings.append(f"Only {n} trades — insufficient data (need >={_MIN_N})")
        return self._envelope(reasoning=f"insufficient data (N<{_MIN_N})", confidence="low")

    # ---- proposal API ---- #

    def _propose(self, param: str, value: Any, reason: str,
                 predicted_delta: float = 0.005,
                 ci: tuple[float, float] = (-0.012, 0.025)) -> bool:
        if param in CLAMP_RANGES:
            value = _clamp(value, param)
        if param != "weights" and isinstance(value, (int, float)):
            cur = self.cfg.get(param)
            if cur is not None and abs(float(value) - float(cur)) < 1e-6:
                return False
        self.proposals.append({
            "param": param,
            "value": value,
            "reason": reason,
            "predicted_delta_sharpe_7d": round(predicted_delta, 4),
            "confidence_interval": [round(ci[0], 4), round(ci[1], 4)],
        })
        return True

    def _emit_manual(self, param: str, current: Any, suggested: Any,
                     reason: str, n_evidence: int) -> None:
        """Operator-review suggestion. Drops if n<50 (mirrors adoption floor)."""
        if n_evidence < _MIN_N:
            return
        self.manual_obs.append({
            "param": param, "current": current, "suggested": suggested,
            "reason": reason, "n_evidence": n_evidence,
            "source": self.SOURCE_NAME.lower(),
        })

    # ---- parsing + guards ---- #

    def _parse_dir_table(self, table_str: str) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        if not table_str:
            return out
        for line in table_str.splitlines():
            s = line.strip()
            if not s or s.startswith(("##", "Param", "-", "Use", "Directions", "'DECAYS")):
                continue
            m = re.match(
                r"^(\S+)\s+(↑|↓)\s+(\d+)\s+(\d+)\s+([+\-]?[\d.]+|n/a|—)\s+([+\-]?[\d.]+|n/a|—)(.*)$",
                s,
            )
            if not m:
                continue
            param, arrow, n, _adopted, bt, _live, note = m.groups()
            try:
                bt_v = float(bt) if bt not in ("n/a", "—") else None
            except ValueError:
                bt_v = None
            out[(param, "up" if arrow == "↑" else "down")] = {
                "n": int(n), "bt_delta": bt_v, "decays": "DECAYS" in note,
            }
        return out

    def _parse_failures(self, failures: dict[str, list[str]]) -> dict[str, set[float]]:
        out: dict[str, set[float]] = defaultdict(set)
        for param, attempts in (failures or {}).items():
            for a in attempts:
                m = re.match(r"^([\-\+]?[\d.]+)", str(a))
                if m:
                    try:
                        out[param].add(float(m.group(1)))
                    except ValueError:
                        pass
        return out

    def _direction_ok(self, param: str, direction: str) -> bool:
        """Block (param, direction) pairs with empirical evidence of failure."""
        entry = self._dir_table.get((param, direction))
        if entry is None:
            return True
        if entry["decays"]:
            return False
        if entry["bt_delta"] is not None and entry["bt_delta"] < -0.005 and entry["n"] >= 3:
            return False
        return True

    def _value_failed(self, param: str, value: float, atol: float = 1e-3) -> bool:
        return any(abs(f - value) < atol for f in self._failed_values.get(param, set()))

    def _rule_structural_probes(self) -> None:
        """Forced one-cycle exploration of audit-identified values. Fires once
        per (param, value) — when there's no live evidence yet AND the value
        isn't already the running config. Skips after evidence appears."""
        for param, value, reason in STRUCTURAL_PROBES:
            if param not in CLAMP_RANGES:
                continue
            cur = self.cfg.get(param)
            try:
                if cur is not None and abs(float(cur) - float(value)) < 1e-6:
                    continue
            except (TypeError, ValueError):
                pass
            if self._value_failed(param, value):
                continue
            self._propose(param, value, reason, predicted_delta=0.010, ci=(-0.005, 0.030))

    def _dedupe(self, props: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for p in props:
            param = p.get("param")
            if not param:
                continue
            cur = seen.get(param)
            if cur is None or abs(p.get("predicted_delta_sharpe_7d", 0.0)) > abs(cur.get("predicted_delta_sharpe_7d", 0.0)):
                seen[param] = p
        return list(seen.values())

    # ---- the core rule ---- #

    def _step_ramp(self, up: dict | None, dn: dict | None) -> float:
        """Adaptive multiplier on EXPLORE_STEPS. Noise floor is the empirical
        cycle-baseline JK_SE × ADOPTION_Z_FLOOR — not a static 0.003."""
        noise_floor = empirical_noise_floor(self.analysis.get("baseline_jk_se"))
        dead = 0
        for entry in (up, dn):
            if entry is None:
                continue
            bt = entry.get("bt_delta")
            n = entry.get("n", 0) or 0
            if bt is None or n < 1:
                continue
            if abs(float(bt)) < noise_floor:
                dead += 1
        if dead == 0:
            return 1.0
        return min(_RAMP_MAX, 1.0 + _RAMP_PER_DEAD_DIRECTION * dead)

    def _rule_exploratory(self) -> None:
        """Probe every tunable param. Direction = empirical best, or rotate if no history."""
        cycle = datetime.now(timezone.utc).timetuple().tm_yday
        for param, base_step in EXPLORE_STEPS.items():
            up = self._dir_table.get((param, "up"))
            dn = self._dir_table.get((param, "down"))
            up_bt = up["bt_delta"] if up and up.get("bt_delta") is not None and not up.get("decays") else None
            dn_bt = dn["bt_delta"] if dn and dn.get("bt_delta") is not None and not dn.get("decays") else None

            if up_bt is not None and dn_bt is not None:
                direction = "up" if up_bt >= dn_bt else "down"
            elif up_bt is not None:
                direction = "up" if up_bt >= 0 else "down"
            elif dn_bt is not None:
                direction = "down" if dn_bt >= 0 else "up"
            else:
                direction = "up" if (cycle + hash(param)) % 2 == 0 else "down"

            if not self._direction_ok(param, direction):
                direction = "down" if direction == "up" else "up"
                if not self._direction_ok(param, direction):
                    continue

            step = base_step * self._step_ramp(up, dn)
            cur = float(self.cfg.get(param, _d(param)))
            new_val: Any = cur + (step if direction == "up" else -step)
            if param == "student_t_df":
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 4)

            evidence = up_bt if direction == "up" else dn_bt
            predicted = max(0.005, abs(evidence) * 0.5) if evidence is not None else 0.005
            self._propose(param, new_val,
                          f"exploratory {direction} step (×{step/base_step:.1f})" if step != base_step
                          else f"exploratory {direction} step",
                          predicted_delta=predicted,
                          ci=(-0.012, max(0.020, predicted * 2)))
