import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from polybot.agents.counterfactual_tracker import CounterfactualTracker


@pytest.fixture
def memory_dir(tmp_path):
    return tmp_path / "memory"


@pytest.fixture
def tracker(memory_dir):
    return CounterfactualTracker(memory_dir=str(memory_dir))


def _make_pos(market_id="btc-updown-5m-1000000", side="Up", entry_price=0.45, size=100.0):
    return {
        "id": 1,
        "market_id": market_id,
        "side": side,
        "entry_price": entry_price,
        "size": size,
        "shares_held": size / entry_price,
        "fee_rate": 0.018,
        "weight_version": "weights_v001",
        "indicator_snapshot": "{}",
    }


def _make_scalp_ctx(exit_fill=0.52, pnl=5.0, gain_pct=0.05, holding_edge=-0.02,
                    model_prob=0.55, market_price=0.57, seconds_remaining=45,
                    exit_threshold=-0.10, strike_price=42500.0, btc_price=42501.0):
    return {
        "exit_fill": exit_fill, "pnl": pnl, "gain_pct": gain_pct,
        "holding_edge": holding_edge, "model_prob": model_prob,
        "market_price": market_price, "seconds_remaining": seconds_remaining,
        "exit_threshold": exit_threshold, "strike_price": strike_price,
        "btc_price": btc_price,
    }


# --- watch() tests ---

def test_watch_adds_to_watchlist(tracker):
    pos = _make_pos()
    tracker.watch(pos, _make_scalp_ctx())
    assert tracker.watching_count == 1


def test_watch_ignores_empty_market_id(tracker):
    pos = _make_pos(market_id="")
    tracker.watch(pos, _make_scalp_ctx())
    assert tracker.watching_count == 0


def test_watch_captures_all_context(tracker):
    pos = _make_pos(side="Down", entry_price=0.60, size=200.0)
    ctx = _make_scalp_ctx(exit_fill=0.65, pnl=10.0, holding_edge=-0.05, strike_price=50000.0)
    tracker.watch(pos, ctx)
    wl = tracker._watchlist[pos["market_id"]]
    assert wl["side"] == "Down"
    assert wl["scalp_exit_price"] == 0.65
    assert wl["holding_edge_at_scalp"] == -0.05
    assert wl["strike_price"] == 50000.0


# --- check_resolutions() tests ---

def _make_binance_feed():
    return MagicMock()


def test_check_resolutions_ignores_unexpired_contracts(tracker):
    """Contracts still in-window should not be resolved."""
    # window_ts far in the future
    future_ts = int(time.time()) + 600
    pos = _make_pos(market_id=f"btc-updown-5m-{future_ts}")
    tracker.watch(pos, _make_scalp_ctx())

    feed = _make_binance_feed()
    resolved = tracker.check_resolutions(feed, lambda f, m: 42500.0)
    assert resolved == []
    assert tracker.watching_count == 1  # still watching


def test_check_resolutions_resolves_expired_contract(tracker):
    """Expired contract should be resolved and removed from watchlist."""
    # window_ts 10 minutes ago (well past expiry + 30s buffer)
    past_ts = int(time.time()) - 600 + 300  # still within 10 min cleanup window
    # Actually let's make it recent enough to not get cleaned up
    past_ts = int(time.time()) - 100  # 100s ago, expiry was 100-300 = 200s ago... no
    # window expires at window_ts + 300. We want now > window_ts + 300 + 30 (buffer)
    # So window_ts = now - 400 means expiry was 100s ago, > 30s buffer
    past_ts = int(time.time()) - 400
    pos = _make_pos(market_id=f"btc-updown-5m-{past_ts}", side="Up")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0))

    feed = _make_binance_feed()
    # BTC at expiry = 42600 > strike 42500, so Up won → resolution_price = 1.0
    btc_fn = lambda f, m: 42600.0
    resolved = tracker.check_resolutions(feed, btc_fn)

    assert len(resolved) == 1
    assert resolved[0]["counterfactual"]["resolution_price"] == 1.0
    assert resolved[0]["scalp_was_optimal"] is False  # scalp pnl=5 < hold pnl (big win)
    assert tracker.watching_count == 0


def test_check_resolutions_up_loses(tracker):
    """If Up side loses, resolution_price = 0.0."""
    past_ts = int(time.time()) - 400
    pos = _make_pos(market_id=f"btc-updown-5m-{past_ts}", side="Up")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0, pnl=-20.0, gain_pct=-0.20))

    # BTC at expiry = 42400 < strike 42500, so Up lost
    resolved = tracker.check_resolutions(_make_binance_feed(), lambda f, m: 42400.0)

    assert len(resolved) == 1
    assert resolved[0]["counterfactual"]["resolution_price"] == 0.0
    # Scalp lost $20, holding would have lost $100 (entire size). Scalp was optimal.
    assert resolved[0]["scalp_was_optimal"] is True


def test_check_resolutions_down_side(tracker):
    """Down side should resolve correctly."""
    past_ts = int(time.time()) - 400
    pos = _make_pos(market_id=f"btc-updown-5m-{past_ts}", side="Down", entry_price=0.40, size=100.0)
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0, pnl=5.0))

    # BTC at expiry = 42400 < strike, so Down won → resolution_price = 1.0
    resolved = tracker.check_resolutions(_make_binance_feed(), lambda f, m: 42400.0)

    assert len(resolved) == 1
    assert resolved[0]["counterfactual"]["resolution_price"] == 1.0
    assert resolved[0]["side"] == "Down"


def test_check_resolutions_writes_json(tracker, memory_dir):
    """Resolved counterfactuals should be written to disk."""
    past_ts = int(time.time()) - 400
    pos = _make_pos(market_id=f"btc-updown-5m-{past_ts}")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0))

    tracker.check_resolutions(_make_binance_feed(), lambda f, m: 42600.0)

    cf_dir = memory_dir / "counterfactuals"
    files = list(cf_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert "actual" in data
    assert "counterfactual" in data
    assert "context_at_scalp" in data


def test_check_resolutions_skips_zero_btc(tracker):
    """If btc_at_expiry returns 0, skip and try again next tick."""
    past_ts = int(time.time()) - 400
    pos = _make_pos(market_id=f"btc-updown-5m-{past_ts}")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0))

    resolved = tracker.check_resolutions(_make_binance_feed(), lambda f, m: 0.0)
    assert resolved == []
    assert tracker.watching_count == 1  # still watching


def test_stale_entries_removed(tracker):
    """Entries watched for > 10 min past expiry should be removed without a record."""
    very_old_ts = int(time.time()) - 2000  # way past 10 min window
    pos = _make_pos(market_id=f"btc-updown-5m-{very_old_ts}")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0))

    resolved = tracker.check_resolutions(_make_binance_feed(), lambda f, m: 42600.0)
    assert resolved == []
    assert tracker.watching_count == 0  # removed as stale


# --- load_all() tests ---

def test_load_all_returns_sorted(tracker, memory_dir):
    """Records should be sorted by timestamp."""
    cf_dir = memory_dir / "counterfactuals"
    cf_dir.mkdir(parents=True, exist_ok=True)

    for i, ts in enumerate(["2026-04-09T14:00:00", "2026-04-09T13:00:00", "2026-04-09T15:00:00"]):
        record = {"position_id": i, "timestamp": ts, "scalp_was_optimal": True}
        (cf_dir / f"{i}_test_{i}.json").write_text(json.dumps(record))

    results = tracker.load_all()
    assert len(results) == 3
    assert results[0]["timestamp"] == "2026-04-09T13:00:00"
    assert results[2]["timestamp"] == "2026-04-09T15:00:00"


def test_load_all_empty(tracker):
    assert tracker.load_all() == []


def test_load_all_skips_malformed(tracker, memory_dir):
    cf_dir = memory_dir / "counterfactuals"
    cf_dir.mkdir(parents=True, exist_ok=True)
    (cf_dir / "bad.json").write_text("not valid json{{{")
    (cf_dir / "good.json").write_text(json.dumps({"timestamp": "2026-01-01", "ok": True}))

    results = tracker.load_all()
    assert len(results) == 1


# --- BiasDetector.analyze_counterfactuals() integration ---

def test_bias_detector_counterfactual_analysis():
    from polybot.agents.bias_detector import BiasDetector
    detector = BiasDetector(biases_path="/tmp/fake_biases.json")

    counterfactuals = [
        {
            "scalp_was_optimal": True,
            "delta_pnl": 0,
            "actual": {"gain_pct": 0.05},
            "counterfactual": {"gain_pct": -1.0},
            "context_at_scalp": {"holding_edge": -0.15, "seconds_remaining": 20},
        },
        {
            "scalp_was_optimal": False,
            "delta_pnl": 50.0,
            "actual": {"gain_pct": -0.10},
            "counterfactual": {"gain_pct": 0.90},
            "context_at_scalp": {"holding_edge": -0.02, "seconds_remaining": 60},
        },
        {
            "scalp_was_optimal": False,
            "delta_pnl": 30.0,
            "actual": {"gain_pct": 0.02},
            "counterfactual": {"gain_pct": 0.80},
            "context_at_scalp": {"holding_edge": -0.05, "seconds_remaining": 100},
        },
    ]

    result = detector.analyze_counterfactuals(counterfactuals)
    assert result["total_scalps_tracked"] == 3
    assert result["optimal_scalps"] == 1
    assert result["suboptimal_scalps"] == 2
    # 1/3 optimal
    assert abs(result["scalp_accuracy"] - 0.3333) < 0.01
    # Avg missed PnL for suboptimal: (50 + 30) / 2 = 40
    assert abs(result["avg_missed_pnl"] - 40.0) < 0.01


def test_bias_detector_counterfactual_empty():
    from polybot.agents.bias_detector import BiasDetector
    detector = BiasDetector(biases_path="/tmp/fake_biases.json")
    assert detector.analyze_counterfactuals([]) == {}


def test_check_resolutions_uses_event_metadata(tracker):
    """When event_metadata is provided, use Chainlink prices instead of Binance."""
    past_ts = int(time.time()) - 400
    market_id = f"btc-updown-5m-{past_ts}"
    pos = _make_pos(market_id=market_id, side="Up")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0, pnl=5.0))

    # Chainlink says: priceToBeat=42500, finalPrice=42400 → Down won
    # But Binance says BTC=42600 (would say Up won — wrong)
    event_metadata = {
        market_id: {"price_to_beat": 42500.0, "final_price": 42400.0}
    }
    resolved = tracker.check_resolutions(
        _make_binance_feed(), lambda f, m: 42600.0, event_metadata=event_metadata
    )

    assert len(resolved) == 1
    # Chainlink says Down won, so Up side loses → resolution_price = 0.0
    assert resolved[0]["counterfactual"]["resolution_price"] == 0.0


def test_check_resolutions_falls_back_to_binance(tracker):
    """Without event_metadata, falls back to Binance (existing behavior)."""
    past_ts = int(time.time()) - 400
    pos = _make_pos(market_id=f"btc-updown-5m-{past_ts}", side="Up")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0))

    resolved = tracker.check_resolutions(
        _make_binance_feed(), lambda f, m: 42600.0, event_metadata={}
    )

    assert len(resolved) == 1
    assert resolved[0]["counterfactual"]["resolution_price"] == 1.0  # Binance: 42600 > 42500


def test_check_resolutions_metadata_records_chainlink_prices(tracker):
    """Counterfactual record should include Chainlink prices when available."""
    past_ts = int(time.time()) - 400
    market_id = f"btc-updown-5m-{past_ts}"
    pos = _make_pos(market_id=market_id, side="Up")
    tracker.watch(pos, _make_scalp_ctx(strike_price=42500.0))

    event_metadata = {
        market_id: {"price_to_beat": 72304.13, "final_price": 72129.75}
    }
    resolved = tracker.check_resolutions(
        _make_binance_feed(), lambda f, m: 42600.0, event_metadata=event_metadata
    )

    assert resolved[0]["context_at_scalp"]["chainlink_price_to_beat"] == 72304.13
    assert resolved[0]["context_at_scalp"]["chainlink_final_price"] == 72129.75
