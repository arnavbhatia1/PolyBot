"""Track pipeline recommendation outcomes — did past changes actually help?

Logs each adoption with predicted Sharpe delta. On subsequent runs, fills in
actual 7d/30d Sharpe from real outcomes so Claude can see its own track record.
"""
from __future__ import annotations

import json
import logging
import math
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
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
                        reason: str = "") -> None:
        """Log a new adoption event."""
        records = self._load()
        records.append({
            "date": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "version": version,
            "baseline_sharpe": round(baseline_sharpe, 4),
            "predicted_sharpe": round(predicted_sharpe, 4),
            "changes": {k: [old, new] for k, (old, new) in changes.items()},
            "reason": reason,
            "review_7d": None,
            "review_30d": None,
        })
        self._save(records)

    def review_past_adoptions(self, outcomes: list[dict[str, Any]]) -> None:
        """Fill in actual Sharpe for adoptions that are now old enough to evaluate."""
        records = self._load()
        if not records or not outcomes:
            return

        now = datetime.now(timezone.utc)
        changed = False

        for rec in records:
            try:
                adopt_dt = datetime.fromisoformat(rec["date"])
            except (ValueError, KeyError):
                continue

            version = rec.get("version", "")
            age_days = (now - adopt_dt).total_seconds() / 86400

            # 7-day review
            if rec.get("review_7d") is None and age_days >= 7:
                window_start = adopt_dt
                window_end = adopt_dt + timedelta(days=7)
                rets = self._returns_in_window(outcomes, version, window_start, window_end)
                if len(rets) >= 10:
                    rec["review_7d"] = {
                        "sharpe": round(_sharpe(rets), 4),
                        "trades": len(rets),
                        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
                    }
                    changed = True
                    logger.info(f"Pipeline review: {version} 7d Sharpe={rec['review_7d']['sharpe']:.3f} "
                               f"({len(rets)} trades)")

            # 30-day review
            if rec.get("review_30d") is None and age_days >= 30:
                window_start = adopt_dt
                window_end = adopt_dt + timedelta(days=30)
                rets = self._returns_in_window(outcomes, version, window_start, window_end)
                if len(rets) >= 30:
                    rec["review_30d"] = {
                        "sharpe": round(_sharpe(rets), 4),
                        "trades": len(rets),
                        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
                    }
                    changed = True
                    logger.info(f"Pipeline review: {version} 30d Sharpe={rec['review_30d']['sharpe']:.3f} "
                               f"({len(rets)} trades)")

        if changed:
            self._save(records)

    @staticmethod
    def _returns_in_window(outcomes: list[dict[str, Any]], version: str,
                           start: datetime, end: datetime) -> list[float]:
        """Get gain_pcts for outcomes matching version within the time window."""
        rets = []
        for o in outcomes:
            if o.get("weight_version") != version:
                continue
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

    def days_since_last_adoption(self) -> float | None:
        """Return days since the most recent adoption, or None if no adoptions."""
        records = self._load()
        if not records:
            return None
        last = records[-1]
        try:
            adopt_dt = datetime.fromisoformat(last["date"])
            return (datetime.now(timezone.utc) - adopt_dt).total_seconds() / 86400
        except (ValueError, KeyError):
            return None

    def get_track_record(self) -> list[dict[str, Any]]:
        """Return adoption history for Claude context."""
        return self._load()

    def format_for_claude(self) -> str:
        """Format track record as a compact string for the Claude prompt."""
        records = self._load()
        if not records:
            return ""

        lines = ["## Pipeline Track Record (past adoption outcomes)"]
        for rec in records[-10:]:  # last 10 adoptions
            date = rec.get("date", "?")[:10]
            version = rec.get("version", "?")
            source = rec.get("source", "?")
            baseline = rec.get("baseline_sharpe", 0)
            predicted = rec.get("predicted_sharpe", 0)

            line = f"- {date} {version} ({source}): predicted Sharpe {baseline:.3f}->{predicted:.3f}"

            r7 = rec.get("review_7d")
            if r7:
                line += f"  |  7d actual: Sharpe={r7['sharpe']:.3f} WR={r7['win_rate']:.0%} n={r7['trades']}"
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

        return "\n".join(lines)
