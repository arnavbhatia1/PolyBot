import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from polybot.agents.scheduler import AgentScheduler


def _bare_scheduler():
    return AgentScheduler(
        outcome_reviewer=MagicMock(), bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock())


def test_directional_old_value_resolves_exit_edge_threshold():
    """exit_edge_threshold is scheduler-owned (not a signal_engine attr); its
    directional old_value must come through, not log as null."""
    sched = _bare_scheduler()
    sched.signal_engine = MagicMock()
    sched.signal_engine.derived_weights = {"flow_disagreement": 0.005}
    sched._exit_edge_threshold = -0.08
    assert sched._directional_old_value("exit_edge_threshold") == -0.08


def test_directional_old_value_exit_edge_falls_back_to_default():
    """When the scheduler hasn't been handed a live value yet, fall back to the
    config default rather than None — the directional row still gets an old_value."""
    sched = _bare_scheduler()
    sched.signal_engine = MagicMock()
    sched.signal_engine.derived_weights = {}
    sched._exit_edge_threshold = None
    assert sched._directional_old_value("exit_edge_threshold") is not None


def test_directional_old_value_resolves_l6_weight_from_dict():
    sched = _bare_scheduler()
    sched.signal_engine = MagicMock()
    sched.signal_engine.derived_weights = {"flow_disagreement": 0.005, "log_atr_ratio": 0.0}
    assert sched._directional_old_value("derived_flow_disagreement_weight") == 0.005
    assert sched._directional_old_value("derived_log_atr_ratio_weight") == 0.0


def test_build_current_config_includes_l6_derived_weights():
    """The recommender dedups proposals against current_config; without the L6
    weights it can't tell an already-on feature (flow_disagreement seeded at
    0.005) from one still at zero, so it re-proposes a no-op probe."""
    sched = _bare_scheduler()
    sched._config = {}
    sched.indicator_engine = MagicMock()
    sched.indicator_engine.get_weights.return_value = {
        "rsi": 0.2, "macd": 0.2, "stochastic": 0.2, "obv": 0.2, "vwap": 0.2}
    sched.signal_engine = MagicMock()
    sched.signal_engine.derived_weights = {
        "log_atr_ratio": 0.0, "autocorr_signed_mag": 0.0,
        "flow_disagreement": 0.005}
    cfg = sched._build_current_config()
    assert cfg["derived_flow_disagreement_weight"] == 0.005
    assert cfg["derived_log_atr_ratio_weight"] == 0.0
    assert cfg["derived_autocorr_signed_mag_weight"] == 0.0


def test_fit_calibrator_on_below_min_returns_none():
    """Too-thin pool → identity (None), so the gate falls back to identity."""
    sched = _bare_scheduler()
    pool = [{"indicator_snapshot": {"trade_context": {"model_probability_raw": 0.6}},
             "correct": True} for _ in range(10)]
    assert sched._fit_calibrator_on(pool, min_samples=75) is None


def test_weight_backtest_scores_through_gate_calibrator_not_live():
    """Two-calibrator split: weight backtests must use the OOS gate-reference
    calibrator, never the live signal_engine.calibrator (which now fits the
    freshest data and would leak the holdout into the gate)."""
    sched = _bare_scheduler()
    sched.signal_engine = MagicMock()
    sched.signal_engine.calibrator = "LIVE"   # must NOT reach the backtest
    sched._gate_calibrator = "GATE"           # must be the one used
    captured = {}

    def _spy(**kwargs):
        captured["calibrator"] = kwargs.get("calibrator")
        return [], []

    sched._kelly_bankroll_returns = _spy
    _keys = ("weights", "momentum_weight", "atr_sigma_ratio", "student_t_df", "min_edge",
             "kelly_fraction", "min_kelly", "min_model_probability", "regime_weight",
             "flow_weight", "spot_flow_weight", "prev_margin_weight",
             "logit_scale", "min_atr", "regime_momentum_threshold", "final_logit_clamp",
             "l5_regime_damp_cap", "atr_regime_shift_threshold", "derived_weights")
    sched._config_for_helper = lambda *a, **k: {key: 0 for key in _keys}
    sched._backtest_recommendations({}, [{"dummy": 1}])
    assert captured["calibrator"] == "GATE"


def test_gate_calibrator_defaults_to_none():
    """Before a cycle sets it, the gate calibrator is None (identity) — backtests
    then run at identity, matching pre-split behavior on an empty window."""
    assert _bare_scheduler()._gate_calibrator is None

def _make_outcomes(n):
    """Helper: generate n fake outcome dicts with sequential timestamps."""
    return [
        {"timestamp": f"2026-04-{(i % 28) + 1:02d}T12:00:00Z", "correct": True, "gain_pct": 0.1,
         "log_return": 0.1, "indicator_snapshot": {}}
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
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
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
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()
    # BiasDetector still runs, but TAEvolver and WeightOptimizer are skipped
    assert call_order == ["bias"]


def test_ghost_resolved_at_epoch_normalizes_to_parseable_iso():
    """Regression: ghost `resolved_at` is a Unix epoch float, but the pipeline parses
    exit_timestamp as ISO-8601. If left as a stringified epoch, fromisoformat() raises,
    the record sorts/windows as 0.0, and the 60-day cutoff drops EVERY resolved ghost —
    silently killing the entire ghost backtest population (entry-gate tuning signal)."""
    sched = _bare_scheduler()
    g = {
        "resolved": True, "side": "up", "ghost_correct": True,
        "resolved_at": 1779951671.6652386,                       # epoch float, as stored
        "timestamp": "2026-05-28T07:01:11.665252+00:00",
        "indicator_snapshot": {"trade_context": {"market_price_up": 0.5}},
    }
    out = sched._ghost_to_outcome(g)
    assert out is not None
    # Must parse as ISO-8601 (would raise on the raw epoch string) AND survive a 60-day window.
    parsed = datetime.fromisoformat(out["exit_timestamp"].replace("Z", "+00:00"))
    assert (parsed.year, parsed.month, parsed.day) == (2026, 5, 28)
    cutoff = datetime(2026, 3, 30, tzinfo=timezone.utc)           # 60d before 2026-05-29
    assert parsed > cutoff, "normalized ghost must not fall outside the 60-day window"


@pytest.mark.asyncio
async def test_pipeline_learns_when_all_data_is_within_holdout_window():
    """Regression: a dataset younger than HOLDOUT_DAYS must still learn.

    The rolling 7-day holdout was swallowing the ENTIRE dataset of a young bot,
    leaving the pre-holdout (opt) pool empty. The analysis dict was then built on
    that empty pool, so the recommender read total_trades=0 and proposed nothing —
    the pipeline silently stopped learning every night despite hundreds of trades.
    The pre-existing tests all used April timestamps (>7d old → holdout empty →
    opt got everything), so this exact path was never exercised. Here every trade
    is timestamped within the last ~2 days, fully inside the holdout window."""
    seen: dict = {}

    async def mock_bias(outcomes=None):
        seen["bias_n"] = len(outcomes or [])
        return {"per_indicator": {}, "overall": {"total_trades": len(outcomes or [])}}

    async def spy_ta_evolver(analysis, outcomes=None):
        seen["evolver_n"] = len(outcomes or [])
        seen["analysis_total_trades"] = analysis.get("overall", {}).get("total_trades", 0)
        return {}

    async def mock_weight_optimizer(recs, outcomes=None, **kwargs):
        seen["optimizer_ran"] = True
        return {"decision": "skipped"}

    # 250 trades spaced 15 min apart → spans ~2.6 days, ALL inside the 7-day holdout
    # (matches a ~2-day-old production bot). Pre-fix this drove the opt pool to exactly 0.
    now = datetime.now(timezone.utc)
    recent = [
        {"timestamp": (now - timedelta(minutes=15 * i)).isoformat(),
         "exit_timestamp": (now - timedelta(minutes=15 * i)).isoformat(),
         "correct": True, "gain_pct": 0.1, "log_return": 0.1, "indicator_snapshot": {}}
        for i in range(250)
    ]
    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = recent
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = spy_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()

    # The whole dataset is inside the holdout window, so the holdout must auto-disable
    # and the FULL pool must reach both the analysis builder and the evolver. Before the
    # fix, bias_n / analysis_total_trades / evolver_n were all 0 and the optimizer ran
    # against an empty proposal set — i.e. no learning.
    assert seen.get("bias_n") == 250, f"analysis built on empty pool (bias_n={seen.get('bias_n')})"
    assert seen.get("analysis_total_trades") == 250, "evolver saw a zeroed analysis → would skip learning"
    assert seen.get("evolver_n") == 250
    assert seen.get("optimizer_ran") is True


@pytest.mark.asyncio
async def test_ghosts_excluded_from_bias_but_kept_for_optimizer():
    """Ghosts must feed the optimizer's backtest pool (§3) but NOT the real-performance
    analysis. A ghost is a trade we rejected; counting it in by-side WR / the Sharpe card
    conflates "how the strategy did" with "what it declined to do" — and ghosts carry a
    gain_pct but no pnl, so they drag the Sharpe negative beside positive P&L."""
    seen: dict = {}

    async def mock_bias(outcomes=None):
        seen["bias_ghosts"] = sum(1 for o in (outcomes or []) if o.get("is_ghost"))
        seen["bias_n"] = len(outcomes or [])
        return {"per_indicator": {}, "overall": {"total_trades": len(outcomes or [])}}

    async def mock_ta_evolver(analysis, outcomes=None):
        return {}

    async def mock_weight_optimizer(recs, outcomes=None, **kwargs):
        seen["opt_ghosts"] = sum(1 for o in (outcomes or []) if o.get("is_ghost"))
        seen["opt_n"] = len(outcomes or [])
        return {"decision": "skipped"}

    now = datetime.now(timezone.utc)
    def _mk(i, ghost):
        ts = (now - timedelta(minutes=15 * i)).isoformat()
        return {"timestamp": ts, "exit_timestamp": ts, "correct": True, "gain_pct": 0.1,
                "pnl": 1.0, "side": "up", "indicator_snapshot": {}, "is_ghost": ghost}
    reals = [_mk(i, False) for i in range(220)]
    ghosts = [_mk(i + 220, True) for i in range(40)]
    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = reals + ghosts
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()

    assert seen.get("bias_ghosts") == 0, "ghosts leaked into the real-performance analysis"
    assert seen.get("bias_n") == 220, "bias analysis must see exactly the real trades"
    assert seen.get("opt_ghosts") == 40, "ghosts must still reach the optimizer backtest (§3)"
    assert seen.get("opt_n") == 260, "optimizer must get the full real+ghost pool"


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
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()
    assert call_order == ["bias", "ta_evolver", "weight_optimizer"]
