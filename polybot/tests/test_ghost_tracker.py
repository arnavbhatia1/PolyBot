import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from polybot.agents.ghost_tracker import GhostTracker

_ET = ZoneInfo("America/New_York")


@pytest.fixture
def tracker(tmp_path):
    return GhostTracker(memory_dir=str(tmp_path))


def _write_ghost(ghost_dir, market_id, ts_iso, resolved=True, name=None):
    record = {
        "market_id": market_id, "gate_name": "edge_cap", "side": "Up",
        "recorded_at": time.time(), "resolved": resolved,
        "ghost_correct": True, "ghost_gain_pct": 0.8,
        "timestamp": ts_iso,
    }
    fname = name or f"{market_id}_edge_cap_{ts_iso[:10]}.json"
    (ghost_dir / fname).write_text(json.dumps(record))


def test_rollup_skips_current_et_day(tracker, tmp_path):
    ghost_dir = tmp_path / "ghost_outcomes"
    yesterday_noon_et = (datetime.now(_ET) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    y_iso = yesterday_noon_et.astimezone(timezone.utc).isoformat()
    t_iso = datetime.now(timezone.utc).isoformat()
    _write_ghost(ghost_dir, "btc-updown-5m-1000", y_iso)
    _write_ghost(ghost_dir, "btc-updown-5m-2000", t_iso)

    rolled = tracker.rollup_old_ghosts()

    assert rolled == 1
    names = {p.name for p in ghost_dir.glob("*.json")}
    assert f"rollup_{yesterday_noon_et.strftime('%Y-%m-%d')}.json" in names
    assert any("btc-updown-5m-2000" in n for n in names)  # today's file untouched
    assert not any("btc-updown-5m-1000" in n for n in names)  # yesterday's rolled


def test_rollup_skips_unresolved(tracker, tmp_path):
    ghost_dir = tmp_path / "ghost_outcomes"
    yesterday_noon_et = (datetime.now(_ET) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    y_iso = yesterday_noon_et.astimezone(timezone.utc).isoformat()
    _write_ghost(ghost_dir, "btc-updown-5m-3000", y_iso, resolved=False)

    assert tracker.rollup_old_ghosts() == 0
    assert any("btc-updown-5m-3000" in p.name for p in ghost_dir.glob("*.json"))


def test_load_all_reads_individual_and_rollup(tracker, tmp_path):
    ghost_dir = tmp_path / "ghost_outcomes"
    yesterday_noon_et = (datetime.now(_ET) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    y_iso = yesterday_noon_et.astimezone(timezone.utc).isoformat()
    t_iso = datetime.now(timezone.utc).isoformat()
    _write_ghost(ghost_dir, "btc-updown-5m-1000", y_iso)
    _write_ghost(ghost_dir, "btc-updown-5m-2000", t_iso)
    tracker.rollup_old_ghosts()

    records = tracker.load_all()
    assert {r["market_id"] for r in records} == {"btc-updown-5m-1000", "btc-updown-5m-2000"}
