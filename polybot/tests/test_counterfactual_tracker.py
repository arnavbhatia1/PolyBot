import json
import time
import pytest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from polybot.agents.counterfactual_tracker import CounterfactualTracker

_ET = ZoneInfo("America/New_York")


@pytest.fixture
def memory_dir(tmp_path):
    return tmp_path / "memory"


@pytest.fixture
def tracker(memory_dir):
    return CounterfactualTracker(memory_dir=str(memory_dir))


def _make_pos(market_id="btc-updown-5m-1000000", side="Up", entry_price=0.45, size=100.0):
    return {
        "id": 1, "market_id": market_id, "side": side,
        "entry_price": entry_price, "size": size,
        "shares_held": size / entry_price,
        "fee_rate": 0.018, "indicator_snapshot": "{}",
    }


def _make_scalp_ctx(**overrides):
    base = {
        "exit_fill": 0.52, "pnl": 5.0, "gain_pct": 0.05, "holding_edge": -0.02,
        "model_prob": 0.55, "market_price": 0.57, "seconds_remaining": 45,
        "exit_threshold": -0.10, "strike_price": 42500.0, "btc_price": 42501.0,
    }
    base.update(overrides)
    return base


def _expired_market_id() -> str:
    """A market_id whose 5-min window expired at least 30s ago."""
    past_ts = int(time.time()) - 400
    return f"btc-updown-5m-{past_ts}"


# --- watch() ---

def test_watch_adds_to_watchlist(tracker):
    tracker.watch(_make_pos(), _make_scalp_ctx())
    assert tracker.watching_count == 1


def test_watch_ignores_empty_market_id(tracker):
    tracker.watch(_make_pos(market_id=""), _make_scalp_ctx())
    assert tracker.watching_count == 0


def test_watch_captures_context(tracker):
    pos = _make_pos(side="Down", entry_price=0.60, size=200.0)
    tracker.watch(pos, _make_scalp_ctx(exit_fill=0.65, holding_edge=-0.05, strike_price=50000.0))
    wl = tracker._watchlist[pos["id"]]
    assert wl["side"] == "Down"
    assert wl["scalp_exit_price"] == 0.65
    assert wl["holding_edge_at_scalp"] == -0.05
    assert wl["strike_price"] == 50000.0


# --- check_resolutions() ---

def test_unexpired_contracts_are_kept(tracker):
    future_ts = int(time.time()) + 600
    tracker.watch(_make_pos(market_id=f"btc-updown-5m-{future_ts}"), _make_scalp_ctx())
    assert tracker.check_resolutions() == []
    assert tracker.watching_count == 1


def test_resolves_with_chainlink_metadata(tracker):
    market_id = _expired_market_id()
    tracker.watch(_make_pos(market_id=market_id, side="Up"),
                  _make_scalp_ctx(strike_price=42500.0))
    metadata = {market_id: {"price_to_beat": 42500.0, "final_price": 42400.0}}
    resolved = tracker.check_resolutions(event_metadata=metadata)
    # Chainlink says Up lost (42400 < 42500).
    assert len(resolved) == 1
    assert resolved[0]["counterfactual"]["resolution_price"] == 0.0


def test_no_metadata_keeps_waiting(tracker):
    """No Chainlink metadata yet → tracker holds the position until it arrives or expires."""
    market_id = _expired_market_id()
    tracker.watch(_make_pos(market_id=market_id, side="Up"),
                  _make_scalp_ctx(strike_price=42500.0))
    assert tracker.check_resolutions(event_metadata={}) == []
    assert tracker.watching_count == 1


def test_resolves_down_side(tracker):
    market_id = _expired_market_id()
    tracker.watch(_make_pos(market_id=market_id, side="Down", entry_price=0.40, size=100.0),
                  _make_scalp_ctx(strike_price=42500.0))
    metadata = {market_id: {"price_to_beat": 42500.0, "final_price": 42400.0}}
    resolved = tracker.check_resolutions(event_metadata=metadata)
    assert len(resolved) == 1
    assert resolved[0]["counterfactual"]["resolution_price"] == 1.0
    assert resolved[0]["side"] == "Down"


def test_writes_record_to_disk(tracker, memory_dir):
    market_id = _expired_market_id()
    tracker.watch(_make_pos(market_id=market_id), _make_scalp_ctx(strike_price=42500.0))
    metadata = {market_id: {"price_to_beat": 42500.0, "final_price": 42600.0}}
    tracker.check_resolutions(event_metadata=metadata)
    files = list((memory_dir / "counterfactuals").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert "actual" in data and "counterfactual" in data and "context_at_scalp" in data


def test_stale_entries_dropped(tracker):
    very_old = int(time.time()) - 2000
    tracker.watch(_make_pos(market_id=f"btc-updown-5m-{very_old}"),
                  _make_scalp_ctx(strike_price=42500.0))
    assert tracker.check_resolutions() == []
    assert tracker.watching_count == 0


# --- hold-moment tracking across flip re-entries (mis-keying guard) ---

def _make_hold_ctx(**overrides):
    base = {
        "holding_edge": -0.05, "model_prob": 0.55, "market_price": 0.50,
        "seconds_remaining": 120, "exit_threshold": -0.10,
        "strike_price": 42500.0, "btc_price": 42490.0,
    }
    base.update(overrides)
    return base


def test_concurrent_positions_track_independently(tracker):
    """Two positions in one window (the normal entry + a late-window sniper stack)
    each track their OWN worst moment, keyed by position_id — neither clobbers the
    other (the old market_id keying silently dropped both at resolution)."""
    mid = "btc-updown-5m-1000000"
    pos1 = _make_pos(market_id=mid)                                  # id=1, Up
    pos2 = _make_pos(market_id=mid, side="Down", entry_price=0.55)
    pos2["id"] = 2
    tracker.track_hold_moment(mid, pos1, _make_hold_ctx(holding_edge=-0.30))
    tracker.track_hold_moment(mid, pos2, _make_hold_ctx(holding_edge=-0.05))
    assert tracker._hold_worst[1]["side"] == "Up"
    assert tracker._hold_worst[1]["worst_holding_edge"] == -0.30
    assert tracker._hold_worst[2]["side"] == "Down"
    assert tracker._hold_worst[2]["worst_holding_edge"] == -0.05


def test_watch_clears_scalped_positions_hold_state(tracker):
    """Scalping a position discards ITS hold-worst state (keyed by position_id); a
    concurrently-held position's state in the same window is left intact."""
    mid = "btc-updown-5m-1000000"
    pos1 = _make_pos(market_id=mid)                       # id=1
    tracker.track_hold_moment(mid, pos1, _make_hold_ctx(holding_edge=-0.30))
    tracker.watch(pos1, _make_scalp_ctx())
    assert 1 not in tracker._hold_worst                   # pos1's slot cleared on scalp

    pos2 = _make_pos(market_id=mid)
    pos2["id"] = 2
    tracker.track_hold_moment(mid, pos2, _make_hold_ctx(holding_edge=-0.10))
    tracker.watch(pos1, _make_scalp_ctx())  # stale watch for pos1 must not evict pos2
    assert tracker._hold_worst[2]["position_id"] == 2


def test_record_hold_resolution_keys_to_resolving_position(tracker, memory_dir):
    """The full flip sequence: pos1 holds (deep worst) then scalps, pos2 re-enters
    and resolves — the hold CF must carry pos2's identity, never pos1's."""
    mid = "btc-updown-5m-1000000"
    pos1 = _make_pos(market_id=mid)
    pos2 = _make_pos(market_id=mid, side="Down", entry_price=0.55, size=110.0)
    pos2["id"] = 2
    tracker.track_hold_moment(mid, pos1, _make_hold_ctx(holding_edge=-0.30))
    tracker.watch(pos1, _make_scalp_ctx())
    tracker.track_hold_moment(mid, pos2, _make_hold_ctx(holding_edge=-0.08, market_price=0.60))
    record = tracker.record_hold_resolution(mid, 1.0, 25.0, 0.227, position_id=2)
    assert record is not None
    assert record["position_id"] == 2
    assert record["side"] == "Down"
    assert record["actual"]["pnl"] == 25.0
    assert record["context_at_worst_moment"]["holding_edge"] == -0.08


def test_record_hold_resolution_drops_mismatched_position(tracker, memory_dir):
    """If the tracked moments belong to a different position (re-entry resolved
    before its first HOLD tick), no record is written — arms never mix."""
    mid = "btc-updown-5m-1000000"
    tracker.track_hold_moment(mid, _make_pos(market_id=mid), _make_hold_ctx())
    record = tracker.record_hold_resolution(mid, 1.0, 25.0, 0.227, position_id=2)
    assert record is None
    assert list((memory_dir / "counterfactuals").glob("*.json")) == []
    assert mid not in tracker._hold_worst  # consumed either way


# --- load_all() ---

def test_load_all_returns_sorted(tracker, memory_dir):
    cf_dir = memory_dir / "counterfactuals"
    cf_dir.mkdir(parents=True, exist_ok=True)
    for i, ts in enumerate(["2026-04-09T14:00:00", "2026-04-09T13:00:00", "2026-04-09T15:00:00"]):
        (cf_dir / f"{i}_test_{i}.json").write_text(
            json.dumps({"position_id": i, "timestamp": ts, "scalp_was_optimal": True})
        )
    results = tracker.load_all()
    assert [r["timestamp"] for r in results] == [
        "2026-04-09T13:00:00", "2026-04-09T14:00:00", "2026-04-09T15:00:00"
    ]


def test_load_all_skips_malformed(tracker, memory_dir):
    cf_dir = memory_dir / "counterfactuals"
    cf_dir.mkdir(parents=True, exist_ok=True)
    (cf_dir / "bad.json").write_text("not valid json{{{")
    (cf_dir / "good.json").write_text(json.dumps({"timestamp": "2026-01-01"}))
    assert len(tracker.load_all()) == 1


# --- rollup_old_counterfactuals() ---

def test_rollup_skips_current_et_day(tracker, memory_dir):
    cf_dir = memory_dir / "counterfactuals"
    cf_dir.mkdir(parents=True, exist_ok=True)
    yesterday_noon_et = (datetime.now(_ET) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    y_iso = yesterday_noon_et.astimezone(timezone.utc).isoformat()
    t_iso = datetime.now(timezone.utc).isoformat()
    (cf_dir / "old.json").write_text(json.dumps(
        {"position_id": 1, "market_id": "m_old", "timestamp": y_iso}))
    (cf_dir / "today.json").write_text(json.dumps(
        {"position_id": 2, "market_id": "m_today", "timestamp": t_iso}))

    rolled = tracker.rollup_old_counterfactuals()

    assert rolled == 1
    names = {p.name for p in cf_dir.glob("*.json")}
    assert f"rollup_{yesterday_noon_et.strftime('%Y-%m-%d')}.json" in names
    assert "today.json" in names  # today's file untouched
    assert "old.json" not in names
