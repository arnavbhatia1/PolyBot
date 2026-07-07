"""Filesystem paths for everything PolyBot persists under `polybot/memory/`.

Single source of truth for the memory layout. All paths key off MEMORY_DIR so the
bot's running directory can't leak a stray `polybot/polybot/memory/` tree.
Under memory/: per-event record dirs (outcomes/, ghost_outcomes/, counterfactuals/
— append-only JSON + rollup_YYYY-MM-DD.json bundles) and state/ (rolling
single-file state + logs rewritten in place).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

POLYBOT_DIR: Path = Path(__file__).resolve().parent
MEMORY_DIR: Path = Path(os.environ.get("POLYBOT_MEMORY_DIR") or (POLYBOT_DIR / "memory"))

# ── Per-event record directories ──────────────────────────────────────────────
OUTCOMES_DIR: Path = MEMORY_DIR / "outcomes"
GHOSTS_DIR: Path = MEMORY_DIR / "ghost_outcomes"
COUNTERFACTUALS_DIR: Path = MEMORY_DIR / "counterfactuals"

# ── Rolling single-file state + logs (memory/state/) ──────────────────────────
STATE_DIR: Path = MEMORY_DIR / "state"
ADVERSE_STATE_PATH: Path = STATE_DIR / "adverse_state.json"
FEED_STALENESS_PATH: Path = STATE_DIR / "feed_staleness.json"
FILL_STATS_PATH: Path = STATE_DIR / "fill_stats.json"
LATENCY_STATS_PATH: Path = STATE_DIR / "latency_stats.json"
ORPHAN_POSITIONS_PATH: Path = STATE_DIR / "orphan_positions.json"
PREV_MARGIN_PATH: Path = STATE_DIR / "prev_resolution_margin.json"
# ET day's opening bankroll snapshot — mid-day restarts reload it instead of
# reconstructing from (bankroll − trade sum), which drifts whenever money
# settles on-chain outside recorded trades.
DAY_OPEN_PATH: Path = STATE_DIR / "day_open_bankroll.json"
# E1 recorder: out-of-band price-sum moments the [0.98, 1.02] gate skips (JSONL,
# append-only) — the cross-book-arb pool the gate otherwise censors unmeasured.
PRICE_SUM_OUTLIERS_PATH: Path = STATE_DIR / "price_sum_outliers.jsonl"

# Gate-skip stats: a lifetime accumulator + the live current-day file. Each finished
# ET day folds into the accumulator (see fold_gate_day + main._ensure_gate_stats_day_loaded).
GATE_STATS_PATH: Path = STATE_DIR / "gate_stats.json"                 # accumulator
GATE_STATS_CURRENT_PATH: Path = STATE_DIR / "gate_stats_current.json"  # today only

def trim_jsonl_by_age(path: Path, max_age_days: float) -> int:
    """Drop lines from an append-only JSONL whose `ts` is older than max_age_days,
    keeping the file bounded. Atomic (temp + replace); unparseable lines are kept
    (never silently lose data). Returns lines dropped. Best-effort: never raises.
    """
    try:
        if not path.exists():
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400.0
        kept, dropped = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                if float(json.loads(s).get("ts", 0)) >= cutoff:
                    kept.append(s)
                else:
                    dropped += 1
            except (ValueError, json.JSONDecodeError, AttributeError):
                kept.append(s)  # keep anything we can't date rather than lose it
        if dropped:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
            tmp.replace(path)
        return dropped
    except Exception:
        return 0


def fold_gate_day(acc_path: Path, counts: dict, day_key: str) -> dict | None:
    """Add one finished ET day's gate-skip counts into the lifetime accumulator at
    `acc_path` (created if absent), bumping days_accumulated and the day range.
    No-op when `counts` is empty. Returns the updated accumulator dict (or None).
    """
    if not counts:
        return None
    acc = {"counts": {}, "days_accumulated": 0, "first_day": None, "last_day": None}
    try:
        if acc_path.exists():
            d = json.loads(acc_path.read_text())
            if isinstance(d, dict) and "days_accumulated" in d:
                acc = d
                acc.setdefault("counts", {})
    except Exception:
        pass
    for k, v in counts.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            acc["counts"][str(k)] = int(acc["counts"].get(str(k), 0)) + int(v)
    acc["days_accumulated"] = int(acc.get("days_accumulated", 0)) + 1
    acc["first_day"] = acc.get("first_day") or day_key
    acc["last_day"] = day_key
    acc["total_skips"] = sum(v for v in acc["counts"].values()
                             if isinstance(v, (int, float)) and not isinstance(v, bool))
    acc["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        acc_path.parent.mkdir(parents=True, exist_ok=True)
        acc_path.write_text(json.dumps(acc, indent=2))
    except Exception:
        pass
    return acc
