"""PipelineTracker — final-verdict evidence (probe suppression) + review windows."""
from datetime import datetime, timedelta, timezone

import pytest

from polybot.agents.pipeline_tracker import PipelineTracker
from polybot.agents.recommender_base import BaseRecommender


@pytest.fixture
def tracker(tmp_path):
    return PipelineTracker(tmp_path / "pipeline_history.json")


def _record(adopt_dt, **overrides):
    rec = {
        "date": adopt_dt.isoformat(),
        "source": "test",
        "version": "v_test",
        "baseline_sharpe": 1.0,
        "predicted_sharpe": 1.1,
        "changes": {"atr_sigma_ratio": [1.3, 1.45]},
        "reason": "",
        "review_7d": None,
        "review_14d": None,
        "review_30d": None,
    }
    rec.update(overrides)
    return rec


def _outcome(dt, gain):
    return {"timestamp": dt.isoformat(), "gain_pct": gain}


# ---------------------------------------------------------------------------
# get_cumulative_failures — final verdicts only
# ---------------------------------------------------------------------------

_RUN_LOG = [{
    "date": datetime.now(timezone.utc).isoformat(),
    "source": "test",
    "baseline_sharpe": 0.1,
    "changes": [
        {"param": "exit_edge_threshold", "new_value": -0.08, "decision": "adopted",
         "backtest_delta_sharpe": 0.012},
        {"param": "exit_edge_threshold", "new_value": -0.05, "decision": "backed_out",
         "backtest_delta_sharpe": 0.003},
        {"param": "min_edge", "new_value": 0.05, "decision": "rejected",
         "backtest_delta_sharpe": -0.002},
        {"param": "kelly_fraction", "new_value": 0.10, "decision": "deferred_crisis"},
    ],
}]


def test_cumulative_failures_cover_all_final_decisions(tracker):
    tracker._save_runs(_RUN_LOG)
    result = tracker.get_cumulative_failures()
    assert any("adopted" in e for e in result["exit_edge_threshold"])
    assert any("backed_out" in e for e in result["exit_edge_threshold"])
    assert any("rejected" in e for e in result["min_edge"])
    assert "kelly_fraction" not in result  # deferred_crisis = no evidence


def test_structural_probes_suppressed_by_final_verdicts(tracker):
    tracker._save_runs(_RUN_LOG)
    rec = BaseRecommender({"cumulative_failures": tracker.get_cumulative_failures()}, {})
    rec._rule_structural_probes()
    proposed = {(p["param"], p["value"]) for p in rec.proposals}
    assert ("exit_edge_threshold", -0.08) not in proposed  # adopted = evidence
    assert ("exit_edge_threshold", -0.05) not in proposed  # backed_out = evidence
    assert ("exit_edge_threshold", -0.03) in proposed      # never tested → fires


def test_structural_probe_not_suppressed_by_deferred_crisis(tracker):
    tracker._save_runs([{
        "date": datetime.now(timezone.utc).isoformat(),
        "source": "test",
        "baseline_sharpe": 0.1,
        "changes": [
            {"param": "derived_log_atr_ratio_weight", "new_value": 0.005,
             "decision": "deferred_crisis"},
        ],
    }])
    rec = BaseRecommender({"cumulative_failures": tracker.get_cumulative_failures()}, {})
    rec._rule_structural_probes()
    proposed = {(p["param"], p["value"]) for p in rec.proposals}
    assert ("derived_log_atr_ratio_weight", 0.005) in proposed


# ---------------------------------------------------------------------------
# review_past_adoptions — window finalization + rollback at each window
# ---------------------------------------------------------------------------

def test_review_window_waits_full_duration(tracker):
    now = datetime.now(timezone.utc)
    adopt = now - timedelta(days=10)
    tracker._save([_record(adopt)])
    outcomes = (
        [_outcome(adopt + timedelta(days=1, hours=i), 0.01 if i % 2 else -0.005)
         for i in range(10)]
        + [_outcome(adopt + timedelta(days=8, hours=i), 0.02) for i in range(5)]
    )
    tracker.review_past_adoptions(outcomes)
    rec = tracker._load()[0]
    assert rec["review_7d"] is not None
    assert rec["review_7d"]["trades"] == 10   # day-8 trades excluded from the 7d window
    assert rec["review_14d"] is None          # 14 days not yet elapsed


def test_review_window_finalizes_with_thin_trade_count(tracker):
    now = datetime.now(timezone.utc)
    adopt = now - timedelta(days=8)
    tracker._save([_record(adopt)])
    outcomes = [_outcome(adopt + timedelta(days=2, hours=i), 0.01 - 0.005 * (i % 3))
                for i in range(5)]
    tracker.review_past_adoptions(outcomes)
    rec = tracker._load()[0]
    assert rec["review_7d"] is not None
    assert rec["review_7d"]["trades"] == 5
    assert not rec.get("rollback_recommended")  # n < 100 → no rollback flag


def test_rollback_fires_at_14d_when_7d_window_was_thin(tracker):
    now = datetime.now(timezone.utc)
    adopt = now - timedelta(days=15)
    tracker._save([_record(adopt)])
    outcomes = (
        [_outcome(adopt + timedelta(days=1, hours=3 * i), 0.01 if i % 2 else -0.01)
         for i in range(20)]
        + [_outcome(adopt + timedelta(days=7, minutes=30 + 90 * i),
                    -0.01 if i % 2 else -0.03)
           for i in range(100)]
    )
    tracker.review_past_adoptions(outcomes)
    rec = tracker._load()[0]
    assert rec["review_7d"]["trades"] == 20
    assert rec["review_14d"]["trades"] == 120
    assert rec["rollback_recommended"] is True
    assert rec["rollback_reason"].startswith("14d")


def test_prefilled_reviews_left_untouched(tracker):
    now = datetime.now(timezone.utc)
    adopt = now - timedelta(days=20)
    old_review = {"sharpe": 0.5, "delta_sharpe": -0.5, "trades": 12, "win_rate": 0.6}
    tracker._save([_record(adopt, review_7d=dict(old_review))])
    outcomes = [_outcome(adopt + timedelta(days=2, hours=2 * i),
                         0.01 - 0.005 * (i % 3)) for i in range(30)]
    tracker.review_past_adoptions(outcomes)
    rec = tracker._load()[0]
    assert rec["review_7d"] == old_review     # not recomputed
    assert rec["review_14d"] is not None      # newly finalized


def test_rollback_not_retriggered_when_already_flagged(tracker):
    now = datetime.now(timezone.utc)
    adopt = now - timedelta(days=15)
    tracker._save([_record(
        adopt,
        review_7d={"sharpe": -2.0, "delta_sharpe": -3.0, "trades": 120, "win_rate": 0.1},
        rollback_recommended=True,
        rollback_reason="7d original reason",
    )])
    outcomes = [_outcome(adopt + timedelta(days=7, minutes=30 + 90 * i),
                         -0.01 if i % 2 else -0.03) for i in range(100)]
    tracker.review_past_adoptions(outcomes)
    rec = tracker._load()[0]
    assert rec["rollback_reason"] == "7d original reason"
    assert rec["review_14d"] is not None
