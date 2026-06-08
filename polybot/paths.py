"""Filesystem paths for everything PolyBot persists under `polybot/memory/`.

Single source of truth for the memory layout — open this file to see the whole
map. All paths are keyed off MEMORY_DIR so the bot's running directory can't leak
a stray `polybot/polybot/memory/` tree.

Three kinds of state live under memory/:
  - per-event RECORD dirs: outcomes/, ghost_outcomes/, counterfactuals/ (append-only
    JSON per trade/ghost/scalp, plus their rollup_YYYY-MM-DD.json bundles)
  - calibration/: the fitted isotonic calibrator
  - state/: rolling single-file state + logs — everything the bot/pipeline rewrites
    in place (adverse/crisis/feed state, fill+latency stats, the prev-margin carry,
    the counterfactual watchlist, pipeline history/run-log, the strategy log, and the
    gate-skip accumulator + current-day file).
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

# ── Calibrator ────────────────────────────────────────────────────────────────
CALIBRATION_DIR: Path = MEMORY_DIR / "calibration"
CALIBRATION_PARAMS_PATH: Path = CALIBRATION_DIR / "isotonic_params.json"

# ── Rolling single-file state + logs (memory/state/) ──────────────────────────
STATE_DIR: Path = MEMORY_DIR / "state"
ADVERSE_STATE_PATH: Path = STATE_DIR / "adverse_state.json"
CRISIS_STATE_PATH: Path = STATE_DIR / "crisis_state.json"
FEED_STALENESS_PATH: Path = STATE_DIR / "feed_staleness.json"
FILL_STATS_PATH: Path = STATE_DIR / "fill_stats.json"
LATENCY_STATS_PATH: Path = STATE_DIR / "latency_stats.json"
ORPHAN_POSITIONS_PATH: Path = STATE_DIR / "orphan_positions.json"
PREV_MARGIN_PATH: Path = STATE_DIR / "prev_resolution_margin.json"
CF_WATCHLIST_PATH: Path = STATE_DIR / "cf_watchlist.json"
PIPELINE_HISTORY_PATH: Path = STATE_DIR / "pipeline_history.json"
PIPELINE_RUN_LOG_PATH: Path = STATE_DIR / "pipeline_run_log.json"
STRATEGY_LOG_PATH: Path = STATE_DIR / "strategy_log.md"

# Gate-skip stats: a lifetime accumulator + the live current-day file. Each finished
# ET day folds into the accumulator (see fold_gate_day + main._ensure_gate_stats_day_loaded).
GATE_STATS_PATH: Path = STATE_DIR / "gate_stats.json"                 # accumulator
GATE_STATS_CURRENT_PATH: Path = STATE_DIR / "gate_stats_current.json"  # today only

# Pipeline freeze sentinel. When this file exists, the nightly pipeline still RUNS
# (analysis, directional table, "would-adopt" diagnostics, rollups) but does NOT
# mutate the live strategy: save_config() and the isotonic calibrator save become
# no-ops. This holds every tunable param + the calibrator fixed so a multi-day
# paper run is ONE stationary strategy — a clean control whose Sharpe/log-loss is
# interpretable. Freeze: create the file. Unfreeze: delete it. Git-visible on purpose.
PIPELINE_FROZEN_PATH: Path = STATE_DIR / "PIPELINE_FROZEN"


def is_pipeline_frozen() -> bool:
    """True when the freeze sentinel is present (pipeline mutations suppressed)."""
    try:
        return PIPELINE_FROZEN_PATH.exists()
    except Exception:
        return False


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
