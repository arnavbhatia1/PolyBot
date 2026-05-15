"""Track pipeline recommendation outcomes — did past changes actually help?

Logs each adoption with predicted Sharpe delta. On subsequent runs, fills in
actual 1d/3d/7d/14d/30d Sharpe from real outcomes so Claude can see its own
track record and detect decay (overfit adoptions that fade within 2 weeks).

Also maintains a run log (pipeline_run_log.json) that records ALL changes tested
each cycle (adopted + rejected) with direction and backtest delta. This powers:
  - format_directional_table(): empirical per-direction evidence table for Claude
  - format_prediction_accuracy(): how accurate were Claude's own predictions?
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    var = sum((r - avg) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.0
    return avg / std if std > 0 else 0.0


class PipelineTracker:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.run_log_path = self.path.parent / "pipeline_run_log.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Adoption records (pipeline_history.json)                           #
    # ------------------------------------------------------------------ #

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(records, indent=2))

    def record_adoption(self, source: str, version: str,
                        baseline_sharpe: float, predicted_sharpe: float,
                        changes: dict[str, tuple[Any, Any]],
                        reason: str = "",
                        run_predicted_delta: float | None = None) -> None:
        """Log a new adoption event.

        run_predicted_delta: sum of predicted_delta_sharpe_7d for all adopted
        changes (from Claude's own predictions). Used for accuracy tracking.
        """
        records = self._load()
        record: dict[str, Any] = {
            "date": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "version": version,
            "baseline_sharpe": round(baseline_sharpe, 4),
            "predicted_sharpe": round(predicted_sharpe, 4),
            "changes": {k: [old, new] for k, (old, new) in changes.items()},
            "reason": reason,
            "review_7d": None,
            "review_14d": None,
            "review_30d": None,
        }
        if run_predicted_delta is not None:
            record["run_predicted_delta"] = round(run_predicted_delta, 4)
        records.append(record)
        self._save(records)

    def review_past_adoptions(self, outcomes: list[dict[str, Any]]) -> None:
        """Fill in actual Sharpe for adoptions old enough to evaluate.

        Review windows: 7d (rollback trigger + prediction accuracy), 14d (decay), 30d (trend).
        After both 7d and 14d are filled, computes decay status and retention ratio.
        DECAYED = Sharpe at 14d < 50% of Sharpe at 7d.
        """
        records = self._load()
        if not records or not outcomes:
            return

        now = datetime.now(timezone.utc)
        changed = False

        # (key, days, min_trades, compute_prediction_accuracy)
        WINDOWS = [
            ("review_7d",  7,  10, True),
            ("review_14d", 14, 10, False),
            ("review_30d", 30, 30, False),
        ]

        for rec in records:
            try:
                adopt_dt = datetime.fromisoformat(rec["date"])
            except (ValueError, KeyError):
                continue

            version = rec.get("version", "")
            baseline = float(rec.get("baseline_sharpe", 0.0))
            age_days = (now - adopt_dt).total_seconds() / 86400

            for key, days, min_trades, check_prediction in WINDOWS:
                if rec.get(key) is not None or age_days < days:
                    continue
                rets = self._returns_in_window(outcomes, version, adopt_dt,
                                               adopt_dt + timedelta(days=days))
                if len(rets) < min_trades:
                    continue

                actual_sharpe = round(_sharpe(rets), 4)
                actual_delta = round(actual_sharpe - baseline, 4)
                review: dict[str, Any] = {
                    "sharpe": actual_sharpe,
                    "delta_sharpe": actual_delta,
                    "trades": len(rets),
                    "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
                }

                if check_prediction:
                    run_pred = rec.get("run_predicted_delta")
                    if run_pred is not None:
                        directional_hit = (actual_delta * float(run_pred)) > 0
                        review["prediction_hit"] = directional_hit
                        review["prediction_error"] = round(abs(actual_delta - float(run_pred)), 4)
                        review["predicted_delta"] = round(float(run_pred), 4)
                    if len(rets) >= 100 and actual_sharpe < baseline - 0.05:
                        if not rec.get("rollback_recommended"):
                            rec["rollback_recommended"] = True
                            rec["rollback_reason"] = (
                                f"7d Sharpe {actual_sharpe:.3f} trails baseline "
                                f"{baseline:.3f} (n={len(rets)})"
                            )
                        logger.warning(
                            f"[ROLLBACK RECOMMENDED — 7d] {version}: Sharpe "
                            f"{actual_sharpe:.3f} trails baseline {baseline:.3f} "
                            f"(n={len(rets)})"
                        )

                rec[key] = review
                changed = True
                logger.info(f"Pipeline review: {version} {key} Sharpe={actual_sharpe:.3f} "
                           f"({len(rets)} trades, baseline={baseline:.3f})")

            # Decay check: once both 7d and 14d are filled
            r7 = rec.get("review_7d")
            r14 = rec.get("review_14d")
            if r7 and r14 and not rec.get("decay_computed"):
                s7 = float(r7.get("delta_sharpe", 0))
                s14 = float(r14.get("delta_sharpe", 0))
                if s7 > 0.001:
                    retention = s14 / s7
                    rec["decay_retention_14d"] = round(retention, 3)
                    if retention < 0.50:
                        rec["decay_status"] = "DECAYED"
                    elif retention >= 0.80:
                        rec["decay_status"] = "PERSISTED"
                    else:
                        rec["decay_status"] = "PARTIAL"
                elif s7 <= 0 and s14 <= 0:
                    rec["decay_status"] = "REVERSED"
                    rec["decay_retention_14d"] = None
                else:
                    rec["decay_status"] = "PARTIAL"
                    rec["decay_retention_14d"] = None
                rec["decay_computed"] = True
                changed = True

        if changed:
            self._save(records)

    @staticmethod
    def _returns_in_window(outcomes: list[dict[str, Any]], version: str,
                           start: datetime, end: datetime) -> list[float]:
        rets = []
        for o in outcomes:
            ts = o.get("timestamp", "")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start <= dt < end:
                rets.append(o.get("gain_pct", 0))
        return rets

    def get_track_record(self) -> list[dict[str, Any]]:
        """Return adoption history for Claude context."""
        return self._load()

    # ------------------------------------------------------------------ #
    #  Run log (pipeline_run_log.json) — ALL changes tested per cycle     #
    # ------------------------------------------------------------------ #

    def _load_runs(self) -> list[dict[str, Any]]:
        if not self.run_log_path.exists():
            return []
        try:
            return json.loads(self.run_log_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save_runs(self, runs: list[dict[str, Any]]) -> None:
        self.run_log_path.write_text(json.dumps(runs, indent=2))

    def record_pipeline_run(
        self,
        source: str,
        baseline_sharpe: float,
        per_change_results: list[dict[str, Any]],
    ) -> None:
        """Log ALL changes tested this cycle (adopted + rejected).

        per_change_results: info["per_change"] from _run_weight_optimizer, each entry
        must include old_value (added by scheduler) and optionally predicted_delta_sharpe_7d.
        """
        runs = self._load_runs()
        entry: dict[str, Any] = {
            "date": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "baseline_sharpe": round(baseline_sharpe, 4),
            "changes": [],
        }
        for c in per_change_results:
            param = c.get("param", "")
            new_value = c.get("value")
            old_value = c.get("old_value")
            decision = c.get("decision", "rejected")
            cand_s = c.get("candidate_sharpe")
            delta = round(float(cand_s) - baseline_sharpe, 4) if cand_s is not None else None

            direction = "unchanged"
            if param != "weights" and new_value is not None and old_value is not None:
                try:
                    if float(new_value) > float(old_value):
                        direction = "up"
                    elif float(new_value) < float(old_value):
                        direction = "down"
                except (TypeError, ValueError):
                    pass

            change_entry: dict[str, Any] = {
                "param": param,
                "old_value": old_value,
                "new_value": new_value,
                "direction": direction,
                "decision": decision,
                "backtest_delta_sharpe": delta,
            }
            for pred_key in ("predicted_delta_sharpe_7d", "confidence_interval"):
                if c.get(pred_key) is not None:
                    change_entry[pred_key] = c[pred_key]

            entry["changes"].append(change_entry)

        runs.append(entry)
        self._save_runs(runs)

    def get_recently_tested_params(self, n_cycles: int = 3) -> set[str]:
        """Return params tested in the last n_cycles pipeline runs."""
        runs = self._load_runs()
        recent = runs[-n_cycles:] if len(runs) >= n_cycles else runs
        tested: set[str] = set()
        for run in recent:
            for c in run.get("changes", []):
                p = c.get("param", "")
                if p:
                    tested.add(p)
        return tested

    def get_cumulative_failures(self, max_per_param: int = 5) -> dict[str, list[str]]:
        """Derive {param: ["value (delta)", ...]} for all historically rejected changes.

        Reads from pipeline_run_log.json so survives restarts and isn't duplicated
        in-memory. Most recent rejections first, capped per-param to keep prompts tight.
        """
        result: dict[str, list[str]] = {}
        runs = self._load_runs()
        for run in reversed(runs):  # newest first
            for c in run.get("changes", []):
                if c.get("decision") != "rejected":
                    continue
                param = c.get("param", "")
                if not param:
                    continue
                val = c.get("new_value", "?")
                delta = c.get("backtest_delta_sharpe")
                entry = f"{val} (Δ={delta:+.4f})" if isinstance(delta, (int, float)) else f"{val}"
                bucket = result.setdefault(param, [])
                if entry not in bucket and len(bucket) < max_per_param:
                    bucket.append(entry)
        return result

    # ------------------------------------------------------------------ #
    #  Claude context formatters                                          #
    # ------------------------------------------------------------------ #

    def format_directional_table(self) -> str:
        """Build empirical per-direction adoption table from run log.

        Groups all tested changes by (param, direction), shows N tests, adoption
        count, avg backtest delta, and avg 7d live delta where available. This
        replaces hardcoded "test HIGHER flow_weight" rules — Claude reasons from data.
        """
        runs = self._load_runs()
        if not runs:
            return ""

        # Map adoption date prefix -> 7d delta for cross-referencing
        adoption_7d: dict[str, float] = {}
        for rec in self._load():
            r7 = rec.get("review_7d")
            if r7 and r7.get("delta_sharpe") is not None:
                adoption_7d[rec.get("date", "")[:10]] = float(r7["delta_sharpe"])

        groups: dict[tuple, dict] = defaultdict(lambda: {
            "n_tests": 0, "n_adopted": 0,
            "backtest_deltas": [], "live_deltas_7d": [],
        })

        for run in runs[-30:]:
            date_prefix = run.get("date", "")[:10]
            live_7d = adoption_7d.get(date_prefix)
            for c in run.get("changes", []):
                param = c.get("param", "")
                direction = c.get("direction", "unchanged")
                if param == "weights" or direction == "unchanged":
                    continue
                key = (param, direction)
                g = groups[key]
                g["n_tests"] += 1
                is_adopted = c.get("decision") == "adopted"
                if is_adopted:
                    g["n_adopted"] += 1
                delta = c.get("backtest_delta_sharpe")
                if delta is not None:
                    g["backtest_deltas"].append(delta)
                if live_7d is not None and is_adopted:
                    g["live_deltas_7d"].append(live_7d)

        if not groups:
            return ""

        lines = ["## Empirical Parameter Direction Table (last 30 cycles)"]
        lines.append("Use this to decide which direction to test. Negative avg BT Δ = stop testing that direction.")
        lines.append("7d Live Δ = realized live Sharpe delta after adoption (— = no adopted runs yet).\n")
        header = f"{'Param':<28} {'Dir':<5} {'Tests':>6} {'Adopted':>8} {'Avg BT Δ':>10} {'7d Live Δ':>10}"
        lines.append(header)
        lines.append("-" * 72)

        for key in sorted(groups.keys()):
            param, direction = key
            g = groups[key]
            n = g["n_tests"]
            adopted = g["n_adopted"]
            bt_deltas = g["backtest_deltas"]
            live_deltas = g["live_deltas_7d"]
            avg_bt = sum(bt_deltas) / len(bt_deltas) if bt_deltas else None
            avg_live = sum(live_deltas) / len(live_deltas) if live_deltas else None

            dir_str = "↑" if direction == "up" else "↓"
            bt_str = f"{avg_bt:+.3f}" if avg_bt is not None else "  n/a"
            live_str = f"{avg_live:+.3f}" if avg_live is not None else "  —"
            note = " (unvalidated)" if n < 3 else (
                " *** DECAYS" if avg_live is not None and avg_bt is not None and avg_live < avg_bt * 0.3 else ""
            )
            lines.append(f"{param:<28} {dir_str:<5} {n:>6} {adopted:>8} {bt_str:>10} {live_str:>10}{note}")

        lines.append("\nDirections with < 3 tests lack evidence — treat as prior, not fact.")
        lines.append("'DECAYS' = live performance well below backtest — likely overfitting to recent noise.")
        return "\n".join(lines)

    def format_prediction_accuracy(self) -> str:
        """Show Claude's own prediction track record for self-calibration.

        Only populated once at least 3 adoptions have been reviewed at 7 days.
        """
        records = [r for r in self._load() if r.get("run_predicted_delta") is not None]
        reviewed = [r for r in records
                    if r.get("review_7d") and r["review_7d"].get("prediction_hit") is not None]

        if len(reviewed) < 3:
            return ""

        hits = sum(1 for r in reviewed if r["review_7d"]["prediction_hit"])
        errors = [r["review_7d"]["prediction_error"]
                  for r in reviewed if r["review_7d"].get("prediction_error") is not None]
        pred_deltas = [float(r["run_predicted_delta"]) for r in reviewed]
        actual_deltas = [float(r["review_7d"]["delta_sharpe"]) for r in reviewed]

        directional_acc = hits / len(reviewed)
        mae = sum(errors) / len(errors) if errors else None
        avg_pred = sum(pred_deltas) / len(pred_deltas)
        avg_actual = sum(actual_deltas) / len(actual_deltas)

        lines = [f"## Your Prediction Track Record (last {len(reviewed)} reviewed adoptions)"]
        lines.append(f"- Directional accuracy: {hits}/{len(reviewed)} ({directional_acc:.0%})")
        if mae is not None:
            lines.append(f"- Magnitude MAE: {mae:.4f} Sharpe units")
        if avg_actual != 0:
            bias_ratio = avg_pred / avg_actual
            if bias_ratio > 1.3:
                lines.append(
                    f"- Bias: you're consistently {bias_ratio:.1f}× too OPTIMISTIC "
                    f"(predicted avg {avg_pred:+.3f}, realized avg {avg_actual:+.3f}). "
                    f"Shrink your predicted_delta_sharpe_7d estimates accordingly."
                )
            elif bias_ratio < 0.7:
                lines.append(
                    f"- Bias: you're consistently too pessimistic "
                    f"(predicted avg {avg_pred:+.3f}, realized avg {avg_actual:+.3f})."
                )
        lines.append(
            "Use this to calibrate predicted_delta_sharpe_7d and confidence_interval widths. "
            "If directional accuracy < 60%, widen your confidence intervals."
        )
        return "\n".join(lines)

    def format_decay_analysis(self) -> str:
        """Show adoption decay statistics — are we overfitting to recent noise?"""
        records = [r for r in self._load() if r.get("decay_computed")]
        if len(records) < 3:
            return ""

        statuses = [r.get("decay_status", "UNKNOWN") for r in records[-10:]]
        decayed = statuses.count("DECAYED")
        persisted = statuses.count("PERSISTED")
        partial = statuses.count("PARTIAL")
        reversed_ = statuses.count("REVERSED")
        n = len(statuses)

        retentions = [float(r["decay_retention_14d"]) for r in records[-10:]
                      if r.get("decay_retention_14d") is not None]
        avg_retention = sum(retentions) / len(retentions) if retentions else None

        lines = [f"## Adoption Decay Analysis (last {n} reviewed adoptions)"]
        if avg_retention is not None:
            lines.append(f"Average 14d Sharpe retention: {avg_retention:.0%} "
                         f"(1.0 = change fully persisted, 0.0 = fully reversed)")
        lines.append(f"- Persisted (>80% retained at 14d): {persisted}/{n}")
        lines.append(f"- Partial (50-80% retained): {partial}/{n}")
        lines.append(f"- Decayed (<50% retained): {decayed}/{n}")
        lines.append(f"- Reversed (negative delta by 14d): {reversed_}/{n}")

        if decayed + reversed_ >= n * 0.5:
            lines.append(
                "WARNING: >50% of adoptions decay within 14 days. "
                "This strongly suggests overfitting to recent noise. "
                "Require higher z-scores and cite stronger evidence before proposing changes. "
                "Empty changes list may be the correct answer."
            )
        elif avg_retention is not None and avg_retention < 0.6:
            lines.append(
                "Caution: average retention is below 60% — "
                "changes are partially overfitting. Be more selective."
            )

        # Show per-adoption decay status for last 5
        lines.append("\nRecent adoption decay history:")
        for rec in records[-5:]:
            date = rec.get("date", "?")[:10]
            status = rec.get("decay_status", "?")
            ret = rec.get("decay_retention_14d")
            ret_str = f" (ret={ret:.0%})" if ret is not None else ""
            changes = rec.get("changes", {})
            change_str = ", ".join(f"{k}: {v[0]}->{v[1]}" for k, v in list(changes.items())[:2])
            lines.append(f"  {date} {status}{ret_str} — {change_str}")

        return "\n".join(lines)

    def format_for_claude(self) -> str:
        """Format track record + prediction accuracy as a compact string for the Claude prompt."""
        records = self._load()
        if not records:
            return ""

        # Prediction accuracy header (Feature 1)
        accuracy_section = self.format_prediction_accuracy()

        # Per-adoption track record
        lines = ["## Pipeline Track Record (past adoption outcomes)"]
        for rec in records[-10:]:
            date = rec.get("date", "?")[:10]
            version = rec.get("version", "?")
            source = rec.get("source", "?")
            baseline = rec.get("baseline_sharpe", 0)
            predicted = rec.get("predicted_sharpe", 0)
            decay_tag = f" [{rec['decay_status']}]" if rec.get("decay_status") else ""

            line = f"- {date} {version} ({source}): predicted Sharpe {baseline:.3f}->{predicted:.3f}{decay_tag}"

            r7 = rec.get("review_7d")
            if r7:
                pred_str = ""
                if r7.get("predicted_delta") is not None:
                    hit = "✓" if r7.get("prediction_hit") else "✗"
                    pred_str = f" [pred Δ={r7['predicted_delta']:+.3f} {hit}]"
                line += (f"  |  7d actual: Sharpe={r7['sharpe']:.3f} "
                         f"Δ={r7['delta_sharpe']:+.3f} WR={r7['win_rate']:.0%} "
                         f"n={r7['trades']}{pred_str}")
            else:
                line += "  |  7d: pending"

            r30 = rec.get("review_30d")
            if r30:
                line += f"  |  30d: Sharpe={r30['sharpe']:.3f}"
            elif r7:
                line += "  |  30d: pending"

            changes = rec.get("changes", {})
            if changes:
                change_strs = [f"{k}: {v[0]}->{v[1]}" for k, v in list(changes.items())[:4]]
                line += f"\n  Changes: {', '.join(change_strs)}"

            lines.append(line)

        # Summary stats
        reviewed = [r for r in records if r.get("review_7d")]
        if reviewed:
            hit = sum(1 for r in reviewed
                      if r["review_7d"]["sharpe"] > r.get("baseline_sharpe", 0))
            lines.append(f"\nHit rate: {hit}/{len(reviewed)} adoptions improved 7d Sharpe vs baseline")

        track_record = "\n".join(lines)
        if accuracy_section:
            return accuracy_section + "\n\n" + track_record
        return track_record
