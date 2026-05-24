import json
import time
import pytest
from polybot.agents.counterfactual_tracker import CounterfactualTracker


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


# --- BiasDetector.analyze_counterfactuals integration ---

def test_bias_detector_counterfactual_analysis():
    from polybot.agents.bias_detector import BiasDetector
    detector = BiasDetector(biases_path="/tmp/fake_biases.json")
    counterfactuals = [
        {"scalp_was_optimal": True,  "delta_pnl": 0,    "actual": {"gain_pct": 0.05},
         "counterfactual": {"gain_pct": -1.0}, "context_at_scalp": {"holding_edge": -0.15, "seconds_remaining": 20}},
        {"scalp_was_optimal": False, "delta_pnl": 50.0, "actual": {"gain_pct": -0.10},
         "counterfactual": {"gain_pct": 0.90},  "context_at_scalp": {"holding_edge": -0.02, "seconds_remaining": 60}},
        {"scalp_was_optimal": False, "delta_pnl": 30.0, "actual": {"gain_pct": 0.02},
         "counterfactual": {"gain_pct": 0.80},  "context_at_scalp": {"holding_edge": -0.05, "seconds_remaining": 100}},
    ]
    result = detector.analyze_counterfactuals(counterfactuals)
    assert result["total_scalps_tracked"] == 3
    assert result["optimal_scalps"] == 1
    assert abs(result["scalp_accuracy"] - 1/3) < 0.01
    assert abs(result["avg_missed_pnl"] - 40.0) < 0.01
