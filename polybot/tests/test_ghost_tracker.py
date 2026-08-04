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


def _record(tracker, market_id, gate, secs=200.0):
    tracker.record_rejection(
        gate_name=gate, side="Up", signal_prob=0.7, signal_edge=0.05,
        market_id=market_id, seconds_remaining=secs, indicator_snapshot={},
    )


def test_per_gate_dedup_sniper_veto_coexists_with_base_ghost(tracker):
    # An early base-path ghost must NOT swallow a later sniper-path veto in the
    # same window — per-market dedup zeroed the sniper evidence stream live.
    mid = f"btc-updown-5m-{int(time.time() // 300) * 300}"
    _record(tracker, mid, "sniper_only", secs=280.0)
    _record(tracker, mid, "min_size", secs=30.0)
    _record(tracker, mid, "min_size", secs=25.0)  # refire: first-wins per gate
    assert len(tracker._pending) == 2
    assert {g for (_, g) in tracker._pending} == {"sniper_only", "min_size"}


def test_watched_markets_exposes_pending_ghost_windows(tracker):
    # The resolution loop fetches Gamma metadata for these after window close —
    # ghosts in untraded windows died unresolved without this surface.
    ts = int(time.time() // 300) * 300
    _record(tracker, f"btc-updown-5m-{ts}", "min_size")
    _record(tracker, f"btc-updown-5m-{ts - 300}", "thin_book_depth")
    assert set(tracker.watched_markets) == {
        f"btc-updown-5m-{ts}", f"btc-updown-5m-{ts - 300}"}


def test_ghosts_resolve_and_persist_per_gate(tracker, tmp_path):
    ts = int(time.time()) - 400  # window expired >30s ago, within 20-min hold
    win = ts - (ts % 300)
    mid = f"btc-updown-5m-{win}"
    _record(tracker, mid, "sniper_only")
    _record(tracker, mid, "min_size")
    meta = {mid: {"final_price": 63500.0, "price_to_beat": 63400.0}}
    resolved = tracker.check_resolutions(event_metadata=meta)
    assert {r["gate_name"] for r in resolved} == {"sniper_only", "min_size"}
    assert all(r["ghost_correct"] for r in resolved)  # Up side, Up won
    assert len(list((tmp_path / "ghost_outcomes").glob("*.json"))) == 2
