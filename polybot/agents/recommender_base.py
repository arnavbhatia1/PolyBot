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
    "atr_sigma_ratio":       0.05,
    "logit_scale":           0.25,
    "student_t_df":          1,
    "momentum_weight":       0.02,
    "regime_weight":         0.005,
    "flow_weight":           0.01,
    "spot_flow_weight":      0.01,
    "liquidation_weight":    0.01,
    "prev_margin_weight":    0.005,
    "min_atr":               1.0,
    "kelly_fraction":        0.01,
    "min_model_probability": 0.01,
    "min_edge":              0.005,
    "min_kelly":             0.002,
}

_CAP = 5         # max changes adopted per cycle
_MIN_N = 50      # min trades before any proposal


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
        deduped.sort(key=lambda c: -abs(float(c.get("predicted_delta_sharpe_7d", 0.0))))
        return self._envelope(deduped[:_CAP], reasoning=reasoning, confidence=confidence)

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

    def _rule_exploratory(self) -> None:
        """Probe every tunable param. Direction = empirical best, or rotate if no history."""
        cycle = datetime.now(timezone.utc).timetuple().tm_yday
        for param, step in EXPLORE_STEPS.items():
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

            cur = float(self.cfg.get(param, _d(param)))
            new_val: Any = cur + (step if direction == "up" else -step)
            if param == "student_t_df":
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 4)

            evidence = up_bt if direction == "up" else dn_bt
            predicted = max(0.005, abs(evidence) * 0.5) if evidence is not None else 0.005
            self._propose(param, new_val,
                          f"exploratory {direction} step",
                          predicted_delta=predicted,
                          ci=(-0.012, max(0.020, predicted * 2)))
