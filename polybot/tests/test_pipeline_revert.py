"""Auto-revert old-value capture (§11 stage 1 — symmetric adopt/revert).

The recording of an adopted change MUST store the *pre-adoption* old value so the
auto-revert can restore it. The recording runs after the mutation loop, so re-reading
`getattr(signal_engine, param)` would capture the NEW value (revert → no-op) and `None`
for L6 weights (which live in `derived_weights`, not as attributes → revert skipped).
"""
from unittest.mock import MagicMock

from polybot.agents.scheduler import AgentScheduler


def _bare_scheduler():
    return AgentScheduler(
        outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock())


def test_record_run_adoption_uses_pre_adoption_old_values():
    sched = _bare_scheduler()
    captured = {}
    tracker = MagicMock()
    tracker.record_adoption.side_effect = lambda **kw: captured.update(kw)
    sched.pipeline_tracker = tracker
    # signal_engine is already MUTATED to the new values by the time recording runs —
    # proving the fix must NOT re-read it.
    sched.signal_engine = MagicMock()
    sched.signal_engine.atr_sigma_ratio = 1.6
    sched.signal_engine.derived_weights = {"log_atr_ratio": 0.03}

    info = {"per_change": [
        {"param": "atr_sigma_ratio", "decision": "adopted", "old_value": 1.3,
         "candidate_sharpe": 0.22, "predicted_delta_sharpe_7d": 0.01},
        {"param": "derived_log_atr_ratio_weight", "decision": "adopted", "old_value": 0.0,
         "candidate_sharpe": 0.22, "predicted_delta_sharpe_7d": 0.01},
        {"param": "min_edge", "decision": "rejected", "old_value": 0.04},
    ]}
    adopted = [{"param": "atr_sigma_ratio", "value": 1.6},
               {"param": "derived_log_atr_ratio_weight", "value": 0.03}]

    sched._record_run_adoption(adopted, info, current_sharpe=0.1, pipeline_source="test")

    chg = captured["changes"]
    assert chg["atr_sigma_ratio"] == (1.3, 1.6)              # pre-adoption old, not mutated 1.6
    assert chg["derived_log_atr_ratio_weight"] == (0.0, 0.03)  # L6: 0.0, not None
    assert "min_edge" not in chg                             # rejected → not recorded


def test_apply_revert_restores_l6_and_regular_params():
    """Given a flagged record carrying correct pre-adoption old values, the auto-revert
    restores both a regular attribute and an L6 weight (in derived_weights)."""
    sched = _bare_scheduler()
    se = MagicMock()
    se.atr_sigma_ratio = 1.6
    se.derived_weights = {"log_atr_ratio": 0.03}
    sched.signal_engine = se
    sched._config = None                       # skip the settings.yaml persistence branch
    sched._exit_edge_threshold = None
    sched._invalidate_baseline_cache = lambda: None

    tracker = MagicMock()
    tracker._load.return_value = [{
        "version": "params",
        "changes": {"atr_sigma_ratio": [1.3, 1.6],
                    "derived_log_atr_ratio_weight": [0.0, 0.03]},
        "rollback_recommended": True, "reverted": False,
    }]
    sched.pipeline_tracker = tracker

    sched._apply_revert_adoptions()

    assert se.atr_sigma_ratio == 1.3                       # regular attr restored
    assert se.derived_weights["log_atr_ratio"] == 0.0      # L6 weight restored
