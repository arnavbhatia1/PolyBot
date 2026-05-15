"""BaseRecommender: shared logic between LocalRecommender and ClaudeRecommender.

Both subclasses produce the same output schema:
    {changes: [...], manual_observations: [...], key_findings: [...],
     risk_warnings: [...], reasoning: str, confidence: str}

The base class is the single source of truth for "always-explore" behaviour:
on every cycle, every tunable param gets a small probe in the best historical
direction (or a rotating exploration when no history exists). Walk-forward +
z-test in the weight optimizer decides which probes (if any) actually improve
Sharpe and get adopted.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any
from polybot.config.param_registry import CLAMP_RANGES, default_for as _d

# Parameter families — each cycle's REACTIVE proposals should span >=3 of these
# so the pipeline doesn't pile changes onto a single mechanism. Exploratory
# probes (bypass_families=True) ignore this limit by design.
FAMILIES: dict[str, list[str]] = {
    "volatility_core": ["atr_sigma_ratio", "student_t_df", "logit_scale", "min_atr"],
    "flow_stack":      ["flow_weight", "spot_flow_weight", "liquidation_weight"],
    "momentum_regime": ["momentum_weight", "regime_weight", "prev_margin_weight"],
    "sizing":          ["kelly_fraction"],
    "gates":           ["min_edge", "min_kelly", "min_model_probability"],
}

# Step sizes used by the always-on exploratory probe: roughly 5-15% of each
# param's typical range. Conservative enough to clear the adoption gate when
# direction is right; small enough that a wrong direction loses only ~1d
# before the early-rollback trigger reverts it.
EXPLORE_STEPS: dict[str, float] = {
    "atr_sigma_ratio":       0.05,
    "logit_scale":           0.25,
    "student_t_df":          1,       # integer-quantized
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


def _clamp(value: float, param: str) -> Any:
    if param not in CLAMP_RANGES:
        return value
    lo, hi, cast = CLAMP_RANGES[param]
    try:
        return cast(max(lo, min(hi, cast(value))))
    except (TypeError, ValueError):
        return value


def _family_of(param: str) -> str | None:
    for fam, members in FAMILIES.items():
        if param in members:
            return fam
    return None


class BaseRecommender:
    """Shared state + helpers + exploratory probe. Subclasses override
    ``recommend()`` to produce candidates; everything else is identical.
    """

    # Subclasses override this to identify themselves in reasoning text.
    SOURCE_NAME: str = "base"

    def __init__(self, analysis: dict[str, Any], current_config: dict[str, Any]) -> None:
        self.analysis = analysis or {}
        self.cfg = current_config or {}
        self.findings: list[str] = []
        self.warnings: list[str] = []
        self.proposals: list[dict[str, Any]] = []
        self.manual_obs: list[dict[str, Any]] = []
        self._families_used: set[str] = set()

        # Pre-parse the directional table once so per-param lookups are O(1).
        self._dir_table = self._parse_directional_table(
            self.analysis.get("directional_table", "")
        )

        # Cumulative failures: {param: ["1.5 (Δ=+0.0012)", ...]}
        self._failed_values = self._parse_cumulative_failures(
            self.analysis.get("cumulative_failures", {})
        )

        # 2x noise floor on Sharpe (mirrors the adoption gate's noise reference)
        self._noise = self._compute_noise()

        # Adoption floor — what every candidate must clear in walk-forward.
        self._adoption_floor = float(
            self.analysis.get("adoption_dynamic_floor")
            or self.analysis.get("adoption_abs_floor")
            or 0.010
        )

        # Params blocked this cycle: negative backtest delta last cycle.
        self._blocked_params: set[str] = set()
        self._build_blocked_params()

        # Conservative mode: flip True when >50% of recent adoptions are decaying.
        # Caps proposals to 1 per cycle.
        self._decay_conservative: bool = False
        self._check_decay_mode()

    # ------------------------------------------------------------------ #
    #  Subclasses MUST override recommend()                              #
    # ------------------------------------------------------------------ #

    def recommend(self) -> dict[str, Any]:
        raise NotImplementedError("Subclasses must implement recommend()")

    # ------------------------------------------------------------------ #
    #  Shared: envelope + finalization                                   #
    # ------------------------------------------------------------------ #

    def _envelope(self, changes: list[dict[str, Any]] | None = None,
                  confidence: str = "medium", reasoning: str = "") -> dict[str, Any]:
        return {
            "changes": changes or [],
            "manual_observations": self.manual_obs,
            "key_findings": self.findings[:5],
            "risk_warnings": self.warnings[:3],
            "reasoning": reasoning or f"{self.SOURCE_NAME} recommender.",
            "confidence": confidence,
        }

    def _finalize(self) -> dict[str, Any]:
        """Dedupe self.proposals by param, sort by predicted delta, cap, return envelope.

        Called by every subclass after candidate generation is complete.
        """
        deduped = self._dedupe_by_param(self.proposals)
        deduped.sort(key=lambda c: -abs(float(c.get("predicted_delta_sharpe_7d", 0.0))))
        cap = 1 if self._decay_conservative else 5
        return self._envelope(
            changes=deduped[:cap],
            confidence=self._confidence_label(deduped),
            reasoning=self._compose_reasoning(deduped),
        )

    # ------------------------------------------------------------------ #
    #  Shared: noise + directional history parsing                       #
    # ------------------------------------------------------------------ #

    def _compute_noise(self) -> dict[str, float]:
        n = int(self.analysis.get("baseline_n_trades") or
                self.analysis.get("overall", {}).get("total_trades", 0) or 1)
        baseline = float(self.analysis.get("baseline_kelly_sharpe") or 0.0)
        sharpe_se = math.sqrt((1.0 + 0.5 * baseline ** 2) / max(n, 1))
        wr_se = math.sqrt(0.25 / max(n, 1))
        return {
            "n": n,
            "sharpe_2x": 2.0 * sharpe_se,
            "wr_2x": 2.0 * wr_se,
            "per_ind_2x": 2.0 * math.sqrt(0.25 / max(n // 5, 1)),
            "quartile_2x": 2.0 * math.sqrt(0.25 / max(n // 4, 1)),
        }

    def _parse_directional_table(self, table_str: str) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        if not table_str:
            return out
        for line in table_str.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("##", "Param", "-", "Use", "Directions", "'DECAYS")):
                continue
            m = re.match(
                r"^(\S+)\s+(↑|↓)\s+(\d+)\s+(\d+)\s+([+\-]?[\d.]+|n/a|—)\s+([+\-]?[\d.]+|n/a|—)(.*)$",
                stripped,
            )
            if not m:
                continue
            param, dir_arrow, n_tests, adopted, bt, live, note = m.groups()
            direction = "up" if dir_arrow == "↑" else "down"
            try:
                bt_v = float(bt) if bt not in ("n/a", "—") else None
            except ValueError:
                bt_v = None
            try:
                live_v = float(live) if live not in ("n/a", "—") else None
            except ValueError:
                live_v = None
            out[(param, direction)] = {
                "n": int(n_tests),
                "adopted": int(adopted),
                "bt_delta": bt_v,
                "live_delta": live_v,
                "decays": "DECAYS" in note,
            }
        return out

    def _parse_cumulative_failures(self, failures: dict[str, list[str]]) -> dict[str, set[float]]:
        out: dict[str, set[float]] = defaultdict(set)
        for param, attempts in (failures or {}).items():
            for attempt in attempts:
                m = re.match(r"^([\-\+]?[\d.]+)", str(attempt))
                if m:
                    try:
                        out[param].add(float(m.group(1)))
                    except ValueError:
                        pass
        return out

    def _direction_ok(self, param: str, direction: str) -> tuple[bool, str]:
        entry = self._dir_table.get((param, direction))
        if entry is None:
            return True, "prior (no empirical evidence)"
        if entry["decays"]:
            return False, "DECAYS in 7d live"
        if entry["bt_delta"] is not None and entry["bt_delta"] < -0.005 and entry["n"] >= 3:
            return False, f"avg BT Δ {entry['bt_delta']:+.3f} over {entry['n']} tests"
        return True, f"BT Δ avg {entry['bt_delta']} over {entry['n']} tests"

    def _value_already_failed(self, param: str, value: float, atol: float = 1e-3) -> bool:
        for failed in self._failed_values.get(param, set()):
            if abs(failed - value) < atol:
                return True
        return False

    def _build_blocked_params(self) -> None:
        """Block params that had negative backtest delta last cycle.

        Parses `last_per_change_results` strings produced by the pipeline run
        log formatter — format: ``param=value baseline=X candidate=Y ...``
        """
        for result_str in self.analysis.get("last_per_change_results", []) or []:
            s = str(result_str)
            m_param = re.match(r"(\w+)=", s)
            if not m_param:
                continue
            param = m_param.group(1)
            bm = re.search(r"baseline=([\-\d.]+)", s)
            cm = re.search(r"candidate=([\-\d.]+)", s)
            if bm and cm:
                try:
                    if float(cm.group(1)) < float(bm.group(1)):
                        self._blocked_params.add(param)
                        continue
                except ValueError:
                    pass
            if "worse" in s.lower():
                self._blocked_params.add(param)

    def _check_decay_mode(self) -> None:
        """Set conservative mode if >50% of recent adoptions are DECAYED/REVERSED.

        Parses the format_decay_analysis() text output from pipeline_tracker.
        """
        decay_text = str(self.analysis.get("decay_analysis", "") or "")
        if not decay_text:
            return
        decayed = decay_text.count("DECAYED")
        reversed_ = decay_text.count("REVERSED")
        persisted = decay_text.count("PERSISTED")
        partial = decay_text.count("PARTIAL")
        total = decayed + reversed_ + persisted + partial
        if total >= 3 and (decayed + reversed_) > total * 0.5:
            self._decay_conservative = True
            self.warnings.append(
                f"Decay mode: {decayed + reversed_}/{total} recent adoptions "
                f"DECAYED/REVERSED — capping proposals to 1"
            )

    # ------------------------------------------------------------------ #
    #  Shared: proposal API                                              #
    # ------------------------------------------------------------------ #

    def _propose(self, param: str, value: Any, reason: str,
                 predicted_delta: float, ci: tuple[float, float] = (-0.01, 0.05),
                 bypass_families: bool = False) -> bool:
        """Add a candidate to self.proposals if it passes the local guardrails.

        Local guardrails (cheap, deterministic):
          - param not in _blocked_params (recent negative delta)
          - family diversity (unless bypass_families=True for exploratory)
          - value clamped into CLAMP_RANGES
          - value differs from current
          - value not in _failed_values

        The REAL adoption decision is the walk-forward backtest + z-test in the
        weight optimizer. _propose just gates obvious non-starters.
        """
        if param in self._blocked_params:
            return False
        family = _family_of(param)
        if family and family in self._families_used and not bypass_families:
            return False
        if param in CLAMP_RANGES:
            value = _clamp(value, param)
        if param != "weights" and isinstance(value, (int, float)):
            current = self.cfg.get(param)
            if current is not None and abs(float(value) - float(current)) < 1e-6:
                return False
            if self._value_already_failed(param, float(value)):
                return False
        self.proposals.append({
            "param": param,
            "value": value,
            "reason": reason,
            "predicted_delta_sharpe_7d": round(predicted_delta, 4),
            "confidence_interval": [round(ci[0], 4), round(ci[1], 4)],
        })
        if family and not bypass_families:
            self._families_used.add(family)
        return True

    def _decisive_value(self, param: str, current: float, direction: str,
                        min_step: float | None = None) -> float:
        rel = 0.15 * abs(current)
        step = max(rel, min_step or 0.0)
        if param in CLAMP_RANGES:
            lo, hi, _cast = CLAMP_RANGES[param]
            range_step = 0.25 * (hi - lo)
            step = max(step, range_step * 0.5)
        sign = 1.0 if direction == "up" else -1.0
        return float(current + sign * step)

    def _dedupe_by_param(self, props: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for p in props:
            param = p.get("param")
            if not param:
                continue
            cur = seen.get(param)
            if cur is None or abs(p.get("predicted_delta_sharpe_7d", 0.0)) > abs(cur.get("predicted_delta_sharpe_7d", 0.0)):
                seen[param] = p
        return list(seen.values())

    def _confidence_label(self, changes: list[dict[str, Any]]) -> str:
        if not changes:
            return "low"
        avg = sum(abs(c.get("predicted_delta_sharpe_7d", 0.0)) for c in changes) / len(changes)
        if avg >= 2 * self._adoption_floor:
            return "medium"
        return "low"

    def _compose_reasoning(self, changes: list[dict[str, Any]]) -> str:
        if not changes:
            return ("No high-conviction changes after dedupe; "
                    "all exploratory probes were blocked by guardrails this cycle.")
        fams = sorted({_family_of(c.get("param", "")) for c in changes if _family_of(c.get("param", ""))})
        return (f"{self.SOURCE_NAME} recommender. "
                f"Proposing {len(changes)} change(s) across {len(fams)} families: {', '.join(fams)}. "
                f"Walk-forward backtest + z-test (>=0.3) decides adoption.")

    # ------------------------------------------------------------------ #
    #  Shared: the always-on exploratory rule                            #
    # ------------------------------------------------------------------ #

    def _rule_exploratory(self) -> None:
        """Always probe every tunable param with a small step.

        Every cycle, propose a small nudge on each tunable param in the
        empirically best historical direction (from the directional table).
        Falls back to alternating exploration when no history exists.

        The walk-forward backtest + z-test in the weight optimizer is the only
        real gate on adoption. This rule's job is just to ensure candidates
        EXIST every cycle so the bot keeps learning even on good-Sharpe days.

        Subclasses can layer extra rules on top (LocalRecommender does reactive
        pattern detection; ClaudeRecommender merges Claude's LLM-proposed
        changes) but _rule_exploratory runs identically for both.
        """
        from datetime import datetime, timezone
        cycle_seed = datetime.now(timezone.utc).timetuple().tm_yday

        for param, step in EXPLORE_STEPS.items():
            if param in self._blocked_params:
                continue
            up = self._dir_table.get((param, "up"))
            down = self._dir_table.get((param, "down"))
            up_score = (up["bt_delta"] if up and up["bt_delta"] is not None
                                       and not up.get("decays") else None)
            down_score = (down["bt_delta"] if down and down["bt_delta"] is not None
                                            and not down.get("decays") else None)

            if up_score is not None and down_score is not None:
                direction = "up" if up_score >= down_score else "down"
            elif up_score is not None:
                direction = "up" if up_score >= 0 else "down"
            elif down_score is not None:
                direction = "down" if down_score >= 0 else "up"
            else:
                direction = "up" if (cycle_seed + hash(param)) % 2 == 0 else "down"

            ok, _why = self._direction_ok(param, direction)
            if not ok:
                direction = "down" if direction == "up" else "up"
                ok, _why = self._direction_ok(param, direction)
                if not ok:
                    continue

            cur = float(self.cfg.get(param, _d(param)))
            new_val: Any = cur + (step if direction == "up" else -step)
            if param == "student_t_df":
                new_val = int(round(new_val))

            evidence = up_score if direction == "up" else down_score
            predicted = max(0.005, abs(evidence) * 0.5) if evidence is not None else 0.005

            self._propose(
                param, new_val,
                f"exploratory {direction} step (always-test continuous learning)",
                predicted_delta=predicted,
                ci=(-0.012, max(0.020, predicted * 2)),
                bypass_families=True,
            )

    # ------------------------------------------------------------------ #
    #  Shared: manual-observation emission                               #
    # ------------------------------------------------------------------ #

    def _emit_manual(self, param: str, current: Any, suggested: Any, reason: str,
                     evidence: dict[str, Any], confidence: str = "medium") -> None:
        """Emit a `manual_observations` entry. Operator-only suggestion — cannot be
        adopted via `changes`. Drops silently when evidence n < 50 (insufficient)."""
        try:
            if int(evidence.get("n", 0)) < 50:
                return
        except (TypeError, ValueError):
            return
        self.manual_obs.append({
            "param": param,
            "current": current,
            "suggested": suggested,
            "reason": reason,
            "evidence": evidence,
            "confidence": confidence,
            "source_channel": self.SOURCE_NAME.lower(),
        })
