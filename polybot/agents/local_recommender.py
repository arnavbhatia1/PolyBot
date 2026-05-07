"""LocalRecommender: rule-based mirror of Claude's strategy analysis.

Used when the Claude API is unavailable. Walks the same `analysis` dict that
ta_evolver passes to Claude and emits the same JSON shape, applying the same
guardrails: 2x noise floor, decisive moves that clear the adoption floor, no
proposals on IMPROVING metrics, direction sourced from the empirical
directional table, cumulative-failures avoidance, diverse parameter families.

Designed to be roughly as deep as Claude's prompt-driven reasoning so the
pipeline keeps learning when the API is down. Does NOT call any LLM.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

# Ranges sourced from param_registry — single source of truth.
from polybot.config.param_registry import CLAMP_RANGES

# Parameter families — each cycle's proposals should span ≥3 of these so the
# pipeline doesn't pile changes onto a single mechanism.
FAMILIES: dict[str, list[str]] = {
    "volatility_core": ["atr_sigma_ratio", "student_t_df", "logit_scale", "min_atr"],
    "flow_stack":      ["flow_weight", "spot_flow_weight", "liquidation_weight"],
    "momentum_regime": ["momentum_weight", "regime_weight", "prev_margin_weight"],
    "sizing":          ["kelly_fraction"],
    "gates":           ["min_edge", "min_kelly", "min_model_probability"],
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


class LocalRecommender:
    """Builds a recommendations dict matching Claude's schema using only the
    analysis context (no LLM call).

    Usage:
        rec = LocalRecommender(analysis, current_config).recommend()
    """

    def __init__(self, analysis: dict[str, Any], current_config: dict[str, Any]) -> None:
        self.analysis = analysis or {}
        self.cfg = current_config or {}
        self.findings: list[str] = []
        self.warnings: list[str] = []
        self.proposals: list[dict[str, Any]] = []
        self.manual_obs: list[dict[str, Any]] = []
        self._families_used: set[str] = set()

        # Pre-parse the directional table once so per-param lookups are O(1).
        # Falls back to empty dict if the formatter wasn't run this cycle.
        self._dir_table = self._parse_directional_table(self.analysis.get("directional_table", ""))

        # Cumulative failures: {param: ["1.5 (Δ=+0.0012)", ...]}
        # Pull out the numeric values so we can avoid retesting them.
        self._failed_values = self._parse_cumulative_failures(
            self.analysis.get("cumulative_failures", {})
        )

        # 2x noise floor on Sharpe (mirrors the prompt's noise reference)
        self._noise = self._compute_noise()

        # Adoption floor — Claude is told this; we use it to size our moves.
        self._adoption_floor = float(
            self.analysis.get("adoption_dynamic_floor")
            or self.analysis.get("adoption_abs_floor")
            or 0.010
        )

        # Params blocked this cycle: had negative backtest delta last cycle, or
        # are currently in the 2-day post-adoption cooldown.
        self._blocked_params: set[str] = set()
        self._build_blocked_params()

        # Conservative mode: flip True when >50% of recent adoptions are decaying.
        # Caps proposals to 1 (mirrors Claude's decay-analysis instruction).
        self._decay_conservative: bool = False
        self._check_decay_mode()

    # -------- public entry point -------- #

    def recommend(self) -> dict[str, Any]:
        overall = self.analysis.get("overall", {})
        n = int(overall.get("total_trades", 0) or 0)

        if n < 50:
            self.warnings.append(f"Only {n} trades — insufficient data, no changes applied")
            return self._envelope(confidence="low", reasoning="Insufficient data (N<50).")

        # Run rule modules in priority order.
        self._rule_scalp_overconfidence()    # L1: counterfactual → atr_sigma_ratio
        self._rule_flow_stack()              # flow_stack: empirical-table best signal
        self._rule_momentum_regime()         # momentum_regime: regime Sharpe gap
        self._rule_gates_from_ghosts()       # gates: profitable ghosts → loosen
        self._rule_indicator_weights()       # L4: reweight if clear winner

        # Manual-only triggers
        self._manual_rule_exit_threshold()
        self._manual_rule_adverse_selection()
        self._manual_rule_flip()

        # Collect key findings from sections not covered by proposal rules
        self._collect_key_findings()

        # Final output: dedupe by param (highest predicted Δ wins per param),
        # cap at 5 (or 1 in decay-conservative mode), attach key_findings.
        deduped = self._dedupe_by_param(self.proposals)
        deduped.sort(key=lambda c: -abs(float(c.get("predicted_delta_sharpe_7d", 0.0))))
        cap = 1 if self._decay_conservative else 5
        return self._envelope(
            changes=deduped[:cap],
            confidence=self._confidence_label(deduped),
            reasoning=self._compose_reasoning(deduped),
        )

    # -------- envelope -------- #

    def _envelope(self, changes: list[dict[str, Any]] | None = None,
                   confidence: str = "medium", reasoning: str = "") -> dict[str, Any]:
        return {
            "changes": changes or [],
            "manual_observations": self.manual_obs,
            "key_findings": self.findings[:5],
            "risk_warnings": self.warnings[:3],
            "reasoning": reasoning or "Local rule-based recommender (Claude unavailable).",
            "confidence": confidence,
        }

    # -------- helpers -------- #

    def _compute_noise(self) -> dict[str, float]:
        n = int(self.analysis.get("baseline_n_trades") or
                self.analysis.get("overall", {}).get("total_trades", 0) or 1)
        baseline = float(self.analysis.get("baseline_kelly_sharpe") or 0.0)
        # Mirror the JK SE formula used in claude_client (same as gate)
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
        """Parse `format_directional_table()` output back into a dict.

        Lines look like: ``param_name           ↑    14    3    +0.012  +0.003``
        Returns: {(param, "up"|"down"): {"n": int, "adopted": int, "bt_delta": float, "live_delta": float, "decays": bool}}
        """
        out: dict[tuple[str, str], dict[str, Any]] = {}
        if not table_str:
            return out
        for line in table_str.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("##", "Param", "-", "Use", "Directions", "'DECAYS")):
                continue
            # Pattern: param dir n adopted bt_delta live_delta [note]
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
        """Pull numeric values out of "1.5 (Δ=+0.0012)" strings."""
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
        """Empirical-table check: should we test this (param, direction)?

        Returns (ok, reason). When the table has no entry, defaults to ok=True
        with reason="prior" (treat as exploratory). When DECAYS or BT delta
        consistently negative, blocks.
        """
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

    def _propose(self, param: str, value: Any, reason: str,
                 predicted_delta: float, ci: tuple[float, float] = (-0.01, 0.05)) -> bool:
        """Add a proposal if it passes guardrails. Returns True if added."""
        # Block params with negative delta last cycle or currently in cooldown.
        if param in self._blocked_params:
            return False
        # Family diversity: stop adding to a family that already has a proposal.
        family = _family_of(param)
        if family and family in self._families_used:
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
        if family:
            self._families_used.add(family)
        return True

    def _decisive_value(self, param: str, current: float, direction: str,
                         min_step: float | None = None) -> float:
        """Pick a value that's far enough from `current` to clear the adoption floor.

        Heuristic: aim for ~12-18% relative move (or `min_step` absolute, whichever
        is larger). We bias toward "decisive" since the adoption gate eats small
        moves; the regime check catches overshoot.
        """
        rel = 0.15 * abs(current)
        step = max(rel, min_step or 0.0)
        # For tiny weights (e.g. flow_weight 0.04), 15% is 0.006 — too small.
        # Force at least 25% of the param's full range as a fallback step.
        if param in CLAMP_RANGES:
            lo, hi, _cast = CLAMP_RANGES[param]
            range_step = 0.25 * (hi - lo)
            step = max(step, range_step * 0.5)  # half the 25% range = ~12% of full range
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
            return ("No high-conviction changes found above 2x noise; "
                    "current configuration appears defensible at this sample size.")
        fams = sorted({_family_of(c.get("param", "")) for c in changes if _family_of(c.get("param", ""))})
        return (f"Local recommender (Claude unavailable). "
                f"Proposing {len(changes)} change(s) across {len(fams)} families: {', '.join(fams)}. "
                f"All proposals sized to clear adoption floor ≈ {self._adoption_floor:.3f} "
                f"and verified against the empirical directional table where available.")

    # -------- rules: one method per parameter family / signal -------- #

    def _rule_scalp_overconfidence(self) -> None:
        """Map counterfactual scalp/hold outcomes to a backtestable proposal.

        scalp_accuracy is collected by counterfactual_tracker and exposed in
        the bias detector, but `exit_edge_threshold` is manual-only. The one
        backtestable lever that maps cleanly: when holds beat scalps the
        entry signal decays during the window, so sharpen L1 (lower
        atr_sigma_ratio). When scalps beat holds the entry is overconfident,
        which Platt corrects on the next cycle — no proposal needed here.
        """
        cf = self.analysis.get("counterfactual_analysis", {})
        n_scalps = int(cf.get("total_scalps_tracked", 0))
        if n_scalps < 100:
            return
        scalp_acc = float(cf.get("scalp_accuracy", 0))
        if scalp_acc >= 0.50:
            return
        if cf.get("net_exit_direction") != "scalp_early":
            return  # hold_long / calibrated → Platt re-fit handles it
        cur = float(self.cfg.get("atr_sigma_ratio", 1.4))
        ok, _why = self._direction_ok("atr_sigma_ratio", "down")
        if not ok:
            return
        new_val = max(1.2, cur - 0.10)
        if self._propose(
            "atr_sigma_ratio", new_val,
            f"scalp_accuracy {scalp_acc:.0%} with holds beating scalps over {n_scalps} trades — "
            f"sharpen L1 so the entry signal holds longer to resolution",
            predicted_delta=0.018,
            ci=(-0.008, 0.040),
        ):
            self.findings.append(
                f"Holds beat scalps (acc {scalp_acc:.0%}, n={n_scalps}) → "
                f"atr_sigma_ratio {cur:.2f}→{new_val:.2f}"
            )

    def _rule_flow_stack(self) -> None:
        """flow_weight / spot_flow_weight / liquidation_weight — chase the strongest signal in flow_stack."""
        # Prefer the signal with the strongest historical bt_delta in the directional table.
        best: tuple[str, str, float] | None = None
        for param in ("flow_weight", "spot_flow_weight", "liquidation_weight"):
            for direction in ("up", "down"):
                entry = self._dir_table.get((param, direction))
                if entry and entry["bt_delta"] is not None and entry["n"] >= 2 and not entry["decays"]:
                    if best is None or entry["bt_delta"] > best[2]:
                        best = (param, direction, entry["bt_delta"])
        if best is None:
            # No empirical evidence — skip the family entirely. Don't roll dice.
            return
        param, direction, bt = best
        if bt <= 0.005:  # too weak even by historical avg
            return
        cur = float(self.cfg.get(param, 0.04))
        new_val = self._decisive_value(param, cur, direction, min_step=0.02)
        self._propose(
            param, new_val,
            f"empirical table shows {param} {direction} avg BT Δ {bt:+.3f} — strongest flow_stack signal",
            predicted_delta=max(0.012, bt * 0.5),
            ci=(-0.008, max(0.025, bt)),
        )

    def _rule_momentum_regime(self) -> None:
        """momentum_weight / regime_weight — adjust based on regime breakdown."""
        by_regime = self.analysis.get("by_regime", {})
        trending = by_regime.get("trending", {})
        reverting = by_regime.get("reverting", {})
        if trending.get("n", 0) < 50 and reverting.get("n", 0) < 50:
            return
        t_sharpe = float(trending.get("sharpe", 0))
        r_sharpe = float(reverting.get("sharpe", 0))
        # Fade indicators stronger if mean-reverting outperforms trending.
        if reverting.get("n", 0) >= 50 and r_sharpe - t_sharpe > self._noise["sharpe_2x"]:
            cur = float(self.cfg.get("momentum_weight", -0.02))
            target = max(-0.10, cur - 0.04)  # more negative = fade harder
            ok, why = self._direction_ok("momentum_weight", "down")
            if ok and target != cur:
                self._propose(
                    "momentum_weight", target,
                    f"reverting Sharpe {r_sharpe:+.3f} > trending {t_sharpe:+.3f} by >2σ — fade harder",
                    predicted_delta=0.014,
                    ci=(-0.010, 0.030),
                )
        elif trending.get("n", 0) >= 50 and t_sharpe - r_sharpe > self._noise["sharpe_2x"]:
            # Trending wins; the regime-conditional flip already amplifies in trending,
            # so the right move is to raise regime_weight (not momentum_weight, which
            # would flip in trending and could fight the runtime amplifier).
            cur = float(self.cfg.get("regime_weight", 0.03))
            target = self._decisive_value("regime_weight", cur, "up", min_step=0.015)
            ok, why = self._direction_ok("regime_weight", "up")
            if ok:
                self._propose(
                    "regime_weight", target,
                    f"trending Sharpe {t_sharpe:+.3f} > reverting {r_sharpe:+.3f} by >2σ — strengthen L2",
                    predicted_delta=0.015,
                    ci=(-0.008, 0.035),
                )

    def _rule_gates_from_ghosts(self) -> None:
        """Entry gates — open up gates that block profitable ghosts; tighten gates that block losers."""
        ghost = self.analysis.get("ghost_analysis", {})
        by_gate = (ghost or {}).get("by_gate", {})
        if not by_gate:
            return
        # min_edge gate
        for gate_key, param, lo_dir in [
            ("low_edge",         "min_edge",              "down"),  # blocked low-edge ghosts; lower bar opens entries
            ("low_kelly",        "min_kelly",             "down"),
            ("low_prob",         "min_model_probability", "down"),
        ]:
            stats = by_gate.get(gate_key)
            if not stats or stats.get("count", 0) < 100:
                continue
            pct_profit = float(stats.get("pct_profitable", 0))
            sim_pnl = float(stats.get("simulated_pnl", 0))
            if pct_profit > 0.60 and sim_pnl > 0:
                cur = float(self.cfg.get(param, 0.0))
                # Modest 10-15% loosen, since gate effects compound.
                target = round(cur * 0.90, 4) if cur > 0 else cur
                if target != cur and not self._value_already_failed(param, target):
                    self._propose(
                        param, target,
                        f"{gate_key} ghosts: {pct_profit:.0%} profitable, sim_pnl=${sim_pnl:+.1f} — gate over-filtering",
                        predicted_delta=0.012,
                        ci=(-0.008, 0.030),
                    )
                    self.findings.append(f"{gate_key} blocked {stats['count']} profitable ghosts → loosen {param}")
                    break  # one gate change per cycle is plenty

    def _rule_indicator_weights(self) -> None:
        """L4 indicator mix — only act when an indicator is consistently >65% accurate.

        Mirrors Claude's rule: skip unless there's a clear winner (L4's amplitude
        is small so small reweights don't move Sharpe).
        """
        per_ind = self.analysis.get("per_indicator", {})
        if not per_ind:
            return
        threshold = 0.50 + self._noise["per_ind_2x"]
        winners = {ind: stats for ind, stats in per_ind.items()
                   if float(stats.get("accuracy", 0)) > max(threshold, 0.65)
                   and int(stats.get("sample_size", 0)) >= 30}
        if not winners:
            return
        cur = dict(self.cfg.get("weights", {}) or {})
        if not cur:
            return
        new_w = dict(cur)
        # Push +0.03 onto each winner, redistribute uniformly elsewhere
        bonus = 0.03 * len(winners)
        for w_ind in winners:
            new_w[w_ind] = min(0.50, new_w.get(w_ind, 0.20) + 0.03)
        losers = [k for k in new_w if k not in winners]
        if losers:
            decrement = bonus / len(losers)
            for lo in losers:
                new_w[lo] = max(0.05, new_w[lo] - decrement)
        # Renormalize to 1.0
        total = sum(new_w.values())
        if total > 0:
            new_w = {k: round(v / total, 4) for k, v in new_w.items()}
            largest = max(new_w, key=new_w.get)
            new_w[largest] = round(1.0 - sum(v for k, v in new_w.items() if k != largest), 4)
        self._propose(
            "weights", new_w,
            f"indicators >65% accurate at N>=30: {', '.join(winners)} — reweight toward winners",
            predicted_delta=0.005,
            ci=(-0.005, 0.012),
        )

    # -------- manual-only suggestions -------- #

    def _emit_manual(self, param: str, current: Any, suggested: Any, reason: str,
                     evidence: dict[str, Any], confidence: str = "medium") -> None:
        # Strict: drop if evidence n < 50 (matches Claude's rules)
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
            "source_channel": "local",
        })

    def _manual_rule_exit_threshold(self) -> None:
        cf = self.analysis.get("counterfactual_analysis", {})
        n_scalps = int(cf.get("total_scalps_tracked", 0))
        if n_scalps < 50:
            return
        scalp_acc = float(cf.get("scalp_accuracy", 0))
        net_dir = cf.get("net_exit_direction", "calibrated")
        cur = self.cfg.get("exit_edge_threshold", -0.10)
        if net_dir == "scalp_early":
            self._emit_manual(
                "exit_edge_threshold", cur, max(-0.25, float(cur) - 0.05),
                "Counterfactual: holds beat scalps — make scalp threshold more negative (harder to scalp)",
                {"metric": "net_exit_direction", "value": net_dir, "n": n_scalps,
                 "source": "counterfactual_analysis"},
                confidence="medium" if scalp_acc < 0.45 else "low",
            )
        elif net_dir == "hold_long":
            self._emit_manual(
                "exit_edge_threshold", cur, round(min(0.0, float(cur) + 0.03), 4),
                "Counterfactual: scalps beat holds — relax scalp threshold (easier to scalp)",
                {"metric": "net_exit_direction", "value": net_dir, "n": n_scalps,
                 "source": "counterfactual_analysis"},
                confidence="medium",
            )

    def _manual_rule_adverse_selection(self) -> None:
        ghost = self.analysis.get("ghost_analysis", {})
        gate = ghost.get("by_gate", {}).get("adverse_rate_30s")
        if not gate:
            return
        n = int(gate.get("count", 0))
        if n < 50:
            return
        pct_profit = float(gate.get("pct_profitable", 0))
        sim_pnl = float(gate.get("simulated_pnl", 0))
        cur = float(self.cfg.get("adverse_selection_threshold", 0.55))
        if pct_profit > 0.60 and sim_pnl > 0:
            self._emit_manual(
                "adverse_selection_threshold", cur, round(min(0.75, cur + 0.05), 3),
                f"adverse_rate_30s gate: {pct_profit:.0%} profitable ghosts, sim_pnl=${sim_pnl:+.1f} — gate over-filtering",
                {"metric": "ghost.adverse_rate_30s.pct_profitable", "value": pct_profit, "n": n,
                 "source": "ghost_analysis.by_gate.adverse_rate_30s"},
                confidence="medium",
            )

    # -------- blocked-params and decay-mode init helpers -------- #

    def _build_blocked_params(self) -> None:
        """Block params that had negative backtest delta last cycle OR are in cooldown.

        Mirrors Claude's rule #5: 'If last cycle a param showed negative delta in a
        direction, don't repeat it.' Also mirrors the adoption-gate cooldown (2 days).
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

        active = str(self.analysis.get("active_adoptions", "") or "")
        for line in active.splitlines():
            stripped = line.strip()
            if "IN_COOLDOWN" in stripped:
                m = re.match(r"(\w+)\s*:", stripped)
                if m:
                    self._blocked_params.add(m.group(1))
            elif stripped.startswith("ALSO IN COOLDOWN:"):
                rest = stripped[len("ALSO IN COOLDOWN:"):].strip()
                for p in rest.split(","):
                    p = p.strip()
                    if p:
                        self._blocked_params.add(p)

    def _check_decay_mode(self) -> None:
        """Set conservative mode if >50% of recent adoptions are DECAYED or REVERSED.

        Mirrors Claude's instruction: 'If the decay analysis shows >50% of adoptions
        are decaying, prioritize an empty or very small changes list.'
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
                f"Decay mode: {decayed + reversed_}/{total} recent adoptions DECAYED/REVERSED "
                f"— capping proposals to 1"
            )

    # -------- key findings from sections not covered by proposal rules -------- #

    def _collect_key_findings(self) -> None:
        """Surface diagnostics that don't produce backtestable proposals:
        current-regime divergence, side imbalance, gate stats, execution quality, Platt meta.
        """
        # Current-regime divergence (last ~100 trades vs overall history)
        cur_reg = self.analysis.get("current_regime", {})
        overall_wr = float(self.analysis.get("overall", {}).get("win_rate", 0))
        if cur_reg and int(cur_reg.get("n_trades", 0)) >= 30 and overall_wr:
            reg_wr = float(cur_reg.get("win_rate", 0))
            if abs(reg_wr - overall_wr) > 2 * self._noise["wr_2x"]:
                direction = "improving" if reg_wr > overall_wr else "deteriorating"
                self.findings.append(
                    f"Recent {cur_reg['n_trades']} trades {direction}: "
                    f"WR {reg_wr:.0%} vs overall {overall_wr:.0%}"
                )

        # Side imbalance (UP vs DOWN)
        side = self.analysis.get("side_analysis", {})
        if len(side) >= 2:
            items = list(side.items())
            wr0 = float(items[0][1].get("win_rate", 0))
            wr1 = float(items[1][1].get("win_rate", 0))
            n0 = int(items[0][1].get("count", 0))
            n1 = int(items[1][1].get("count", 0))
            if min(n0, n1) >= 50 and abs(wr0 - wr1) > 2 * self._noise["wr_2x"]:
                better = items[0][0] if wr0 > wr1 else items[1][0]
                worse = items[1][0] if wr0 > wr1 else items[0][0]
                self.findings.append(
                    f"Side imbalance: {better} ({max(wr0, wr1):.0%} WR) >> "
                    f"{worse} ({min(wr0, wr1):.0%} WR) — check directional model bias"
                )

        # Platt meta-warning
        platt_meta = str(self.analysis.get("platt_meta_warning", "") or "")
        if platt_meta:
            self.findings.append(f"Platt meta: {platt_meta[:120]}")

        # Execution quality
        eq = self.analysis.get("execution_quality", {})
        if eq:
            avg_slip = float(eq.get("avg_fill_slippage", 0) or 0)
            if avg_slip > 0.005:
                self.warnings.append(
                    f"avg_fill_slippage {avg_slip:+.4f} > 0.005 — slippage eating realized edge"
                )
                self.findings.append(
                    f"High slippage {avg_slip:+.4f}: raise logit_scale to self-filter "
                    f"marginal entries, or review kelly_fraction"
                )
            fok_rate = eq.get("fok_fill_rate")
            if fok_rate is not None and float(fok_rate) < 0.80:
                self.findings.append(
                    f"FOK fill rate {float(fok_rate):.0%} "
                    f"({eq.get('fok_total_attempts', 0)} attempts) — many orders rejected"
                )

        # Gate skip stats
        gate_stats = self.analysis.get("gate_skip_stats", {})
        if gate_stats:
            counts = {k: v for k, v in gate_stats.items()
                      if k != "total_skips" and isinstance(v, (int, float)) and v > 0}
            if counts:
                total_skips = gate_stats.get("total_skips", sum(counts.values()))
                top_gate, top_count = max(counts.items(), key=lambda x: x[1])
                manual_only = {"adverse_selection", "adverse_rate_30s"}
                note = "manual-only lever" if top_gate in manual_only else "consider loosening"
                self.findings.append(
                    f"Top gate: {top_gate} blocks {top_count}/{total_skips} skips "
                    f"({top_count / max(total_skips, 1):.0%}) — {note}"
                )

        # Blocked params notice (transparent to operator about what was skipped)
        if self._blocked_params:
            blocked_list = sorted(self._blocked_params)[:4]
            self.findings.append(
                f"Skipped (negative delta last cycle or cooldown): {', '.join(blocked_list)}"
            )

    def _manual_rule_flip(self) -> None:
        """Flip-trade evaluation. flip_enabled (kill switch) and flip_edge_premium
        (extra edge for re-entry) are manual-only. If flips Sharpe lags base by
        a meaningful margin, recommend tightening or disabling."""
        flip_data = self.analysis.get("flip_analysis", {})
        if not flip_data:
            return
        base = flip_data.get("base", {})
        flip = flip_data.get("flip", {})
        if flip.get("n", 0) < 50 or base.get("n", 0) < 50:
            return
        flip_sharpe = float(flip.get("sharpe", 0))
        base_sharpe = float(base.get("sharpe", 0))
        gap = base_sharpe - flip_sharpe
        if gap > 2 * self._noise["sharpe_2x"] and flip_sharpe < 0:
            # Flip is materially worse AND outright losing — recommend disabling
            self._emit_manual(
                "flip_enabled", self.cfg.get("flip_enabled", True), False,
                f"Flip-trade Sharpe {flip_sharpe:+.3f} (n={flip.get('n', 0)}) trails "
                f"base {base_sharpe:+.3f} by {gap:.3f} and is negative — kill flips",
                {"metric": "flip_analysis.sharpe_gap", "value": gap,
                 "n": flip.get("n", 0), "source": "flip_analysis"},
                confidence="high",
            )
        elif gap > 2 * self._noise["sharpe_2x"]:
            # Flip lags but is still profitable — raise the premium
            cur = float(self.cfg.get("flip_edge_premium", 0.015))
            self._emit_manual(
                "flip_edge_premium", cur, round(min(0.05, cur + 0.01), 4),
                f"Flip-trade Sharpe {flip_sharpe:+.3f} trails base {base_sharpe:+.3f} "
                f"by {gap:.3f} — raise the re-entry edge premium",
                {"metric": "flip_analysis.sharpe_gap", "value": gap,
                 "n": flip.get("n", 0), "source": "flip_analysis"},
                confidence="medium",
            )

