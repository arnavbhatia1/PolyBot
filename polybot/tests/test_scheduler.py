import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from polybot.agents.scheduler import AgentScheduler

@pytest.fixture
def scheduler():
    return AgentScheduler(outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})

def test_scheduler_has_all_agents(scheduler):
    assert scheduler.outcome_reviewer is not None
    assert scheduler.bias_detector is not None
    assert scheduler.ta_evolver is not None
    assert scheduler.weight_optimizer is not None

def test_scheduler_accepts_claude_client():
    mock_claude = MagicMock()
    mock_evolver = MagicMock()
    mock_evolver.claude_client = None
    s = AgentScheduler(outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        ta_evolver=mock_evolver, weight_optimizer=MagicMock(),
        claude_client=mock_claude)
    assert s.claude_client is mock_claude
    assert mock_evolver.claude_client is mock_claude

def _make_outcomes(n):
    """Helper: generate n fake outcome dicts with sequential timestamps."""
    return [
        {"timestamp": f"2026-04-{(i % 28) + 1:02d}T12:00:00Z", "correct": True, "gain_pct": 0.1,
         "log_return": 0.1, "weight_version": "weights_v001", "indicator_snapshot": {}}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_run_daily_pipeline_calls_agents_in_order():
    call_order = []
    async def mock_bias(outcomes=None):
        call_order.append("bias")
        return {"per_indicator": {}, "overall": {}}
    async def mock_ta_evolver(analysis, outcomes=None):
        call_order.append("ta_evolver")
        return {}
    async def mock_weight_optimizer(recs, outcomes=None, **kwargs):
        call_order.append("weight_optimizer")
        return {"decision": "skipped"}
    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = _make_outcomes(250)
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()
    assert call_order == ["bias", "ta_evolver", "weight_optimizer"]


@pytest.mark.asyncio
async def test_pipeline_skips_learning_below_200_trades():
    """TAEvolver and WeightOptimizer must not run with fewer than 200 trades."""
    call_order = []
    async def mock_bias(outcomes=None):
        call_order.append("bias")
        return {"per_indicator": {}, "overall": {}}
    async def mock_ta_evolver(analysis, outcomes=None):
        call_order.append("ta_evolver")
        return {}
    async def mock_weight_optimizer(recs, outcomes=None):
        call_order.append("weight_optimizer")
    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = _make_outcomes(30)
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={})
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()
    # BiasDetector still runs, but TAEvolver and WeightOptimizer are skipped
    assert call_order == ["bias"]


@pytest.mark.asyncio
async def test_pipeline_runs_learning_at_exactly_200_trades():
    """At exactly 200 trades the learning pipeline should run."""
    call_order = []
    async def mock_bias(outcomes=None):
        call_order.append("bias")
        return {"per_indicator": {}, "overall": {}}
    async def mock_ta_evolver(analysis, outcomes=None):
        call_order.append("ta_evolver")
        return {}
    async def mock_weight_optimizer(recs, outcomes=None, **kwargs):
        call_order.append("weight_optimizer")
        return {"decision": "skipped"}
    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = _make_outcomes(200)
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2,
        math_config={})
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()
    assert call_order == ["bias", "ta_evolver", "weight_optimizer"]
