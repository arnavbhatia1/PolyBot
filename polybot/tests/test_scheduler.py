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


def test_counterfactual_index_selects_scalp_record(tmp_path, monkeypatch):
    """P2-13: a position_id can have BOTH a scalp CF (context_at_scalp) and a hold
    CF (context_at_worst_moment). The index must deterministically select the scalp
    record — the exit-threshold replay reads context_at_scalp.holding_edge, so a
    hold CF shadowing it (by glob order) would silently skip the override."""
    import json as _json
    from polybot.agents import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "COUNTERFACTUALS_DIR", tmp_path)
    pid = 12345
    (tmp_path / "z_hold.json").write_text(_json.dumps({
        "position_id": pid, "counterfactual": {"gain_pct": -0.5},
        "context_at_worst_moment": {"holding_edge": -0.2},
    }))
    (tmp_path / "a_scalp.json").write_text(_json.dumps({
        "position_id": pid, "counterfactual": {"gain_pct": 0.8},
        "context_at_scalp": {"holding_edge": -0.05},
    }))
    idx = _bare_scheduler()._load_counterfactual_index()
    assert pid in idx
    assert "context_at_scalp" in idx[pid]
    assert idx[pid]["counterfactual"]["gain_pct"] == 0.8


def _cold_feed_outcome(flow_val):
    """A resolved outcome whose flow signals carry `flow_val` (None = cold feed,
    post Pass-1 telemetry fix). coinbase_cvd_60s=None forces the replay's
    else-branch read of the stored spot_flow_signal."""
    return {
        "side": "Up", "correct": True, "gain_pct": 0.1, "exit_reason": "resolution",
        "timestamp": "2026-04-10T12:00:00Z",
        "indicator_snapshot": {"trade_context": {
            "model_probability_raw": 0.62,
            "market_price_up": 0.55, "market_price_down": 0.45,
            "btc_price": 66420.0, "strike_price": 66400.0,
            "atr": 80.0, "seconds_remaining": 180,
            "regime_autocorr": 0.05, "regime_direction": 1.0,
            "prev_resolution_margin": 0.0,
            "flow_score": flow_val, "spot_flow_signal": flow_val,
            "coinbase_cvd_60s": None, "coinbase_taker_60s": None, "coinbase_taker_n": 0,
        }},
    }


def test_replay_coerces_cold_feed_none_flow_to_zero():
    """P2-1 regression: cold-feed flow_score/spot_flow_signal are stored as None
    (Pass-1 fix). The replay must coerce them to 0.0 like live — NOT leave the
    present None (dict.get default doesn't apply to a present None), which crashed
    combine_flow_family on None*weight and aborted the whole optimizer stage."""
    import math
    sched = _bare_scheduler()
    kwargs = dict(
        recommended_weights={"rsi": 0.2, "macd": 0.2, "stochastic": 0.2, "obv": 0.2, "vwap": 0.2},
        momentum_weight=0.05, atr_sigma_ratio=1.3, student_t_df=5, min_edge=0.04,
        calibrator=None, kelly_fraction=0.15, min_kelly=0.01, min_prob=0.56,
    )
    # Must not raise, and a cold (None) feed must score identically to a real 0.0.
    r_none, _ = sched._kelly_bankroll_returns(outcomes=[_cold_feed_outcome(None)], **kwargs)
    r_zero, _ = sched._kelly_bankroll_returns(outcomes=[_cold_feed_outcome(0.0)], **kwargs)
    assert r_none == r_zero
    assert all(math.isfinite(r) for r in r_none)

def _scalp_outcome(gain=0.02, pid=777):
    o = _cold_feed_outcome(0.0)
    o["exit_reason"] = "scalp"
    o["gain_pct"] = gain
    o["position_id"] = pid
    # Clear the entry gates decisively (raw L1 prob ≈ 0.59 vs price 0.50 →
    # edge ≈ 0.09 ≥ min_edge) so the replay emits exactly one return.
    o["indicator_snapshot"]["trade_context"]["market_price_up"] = 0.50
    o["indicator_snapshot"]["trade_context"]["market_price_down"] = 0.50
    return o


def _cf_index(he, secs=120, mp=0.5, loss_cut=False, cf_gain=-1.0, pid=777):
    return {pid: {"counterfactual": {"gain_pct": cf_gain},
                  "context_at_scalp": {"holding_edge": he, "seconds_remaining": secs,
                                       "market_price": mp, "fee_rate": 0.07,
                                       "loss_cut": loss_cut}}}


_REPLAY_KWARGS = dict(
    recommended_weights={"rsi": 0.2, "macd": 0.2, "stochastic": 0.2, "obv": 0.2, "vwap": 0.2},
    momentum_weight=0.05, atr_sigma_ratio=1.3, student_t_df=5, min_edge=0.04,
    calibrator=None, kelly_fraction=0.15, min_kelly=0.01, min_prob=0.56,
)


def test_exit_replay_scores_against_blended_threshold_not_raw():
    """At ATM with 120s left the blended fire criterion is the boundary curve
    (≈ -0.057), well above a raw -0.10 candidate. A scalp at holding_edge -0.08
    would STILL fire live under the candidate, so it must keep its scalp gain —
    the raw-threshold comparison (-0.08 > -0.10) would wrongly reprice it."""
    sched = _bare_scheduler()
    no_cf, _ = sched._kelly_bankroll_returns(
        outcomes=[_scalp_outcome()], **_REPLAY_KWARGS)
    with_cf, _ = sched._kelly_bankroll_returns(
        outcomes=[_scalp_outcome()], exit_threshold_override=-0.10,
        counterfactual_index=_cf_index(he=-0.08), **_REPLAY_KWARGS)
    assert len(no_cf) == 1
    assert with_cf == no_cf  # scalp outcome kept, counterfactual NOT substituted


def test_exit_replay_reprices_when_candidate_would_not_fire():
    """A scalp at holding_edge -0.02 sits above the blended criterion — under the
    candidate live would have held, so the hold-to-resolution gain substitutes."""
    sched = _bare_scheduler()
    no_cf, _ = sched._kelly_bankroll_returns(
        outcomes=[_scalp_outcome()], **_REPLAY_KWARGS)
    with_cf, _ = sched._kelly_bankroll_returns(
        outcomes=[_scalp_outcome()], exit_threshold_override=-0.10,
        counterfactual_index=_cf_index(he=-0.02), **_REPLAY_KWARGS)
    assert len(with_cf) == 1
    assert with_cf != no_cf  # counterfactual gain substituted


def test_exit_replay_never_reprices_loss_cuts():
    """Loss-cut closes fire independently of exit_edge_threshold — even a
    holding_edge far above the criterion must keep its actual scalp outcome."""
    sched = _bare_scheduler()
    no_cf, _ = sched._kelly_bankroll_returns(
        outcomes=[_scalp_outcome()], **_REPLAY_KWARGS)
    with_cf, _ = sched._kelly_bankroll_returns(
        outcomes=[_scalp_outcome()], exit_threshold_override=-0.10,
        counterfactual_index=_cf_index(he=0.05, loss_cut=True), **_REPLAY_KWARGS)
    assert with_cf == no_cf


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
    """Epoch-float resolved_at must normalize to ISO so ghosts aren't stamped 0.0 and
    dropped by the 60-day window."""
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
    """A dataset entirely inside the holdout window must still learn: the holdout
    disables and the full pool reaches the analysis builder and evolver (not an empty
    opt pool, which would zero learning)."""
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
    """Ghosts reach the optimizer's backtest pool (§3) but are excluded from the
    real-performance bias analysis."""
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


# ---------------------------------------------------------------------------
# Live-vs-replay parity: the replay must reproduce compute_probability from a
# production-shaped trade_context (§17 shared-math invariant), numerically.
# ---------------------------------------------------------------------------

_PARITY_PARAMS = dict(
    regime_weight=0.08, flow_weight=0.06, spot_flow_weight=0.07,
    prev_margin_weight=0.03, momentum_weight=0.06, atr_sigma_ratio=1.5,
    student_t_df=4, logit_scale=3.5, min_atr=10.0,
    regime_momentum_threshold=0.12, final_logit_clamp=3.5,
    l5_regime_damp_cap=0.6, atr_regime_shift_threshold=0.5,
)
_PARITY_L4 = {"rsi": 0.25, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.15}
_PARITY_L6 = {"log_atr_ratio": 0.0, "autocorr_signed_mag": 0.01, "flow_disagreement": 0.01}
_PARITY_IND = {"rsi": {"score": 0.4}, "macd": {"score": -0.3}, "stochastic": {"score": 0.2},
               "obv": {"score": 0.1}, "vwap": {"score": -0.2}}


def _parity_live_and_ctx(btc_price: float, strike: float):
    """Run a fresh live engine once and stamp a trade_context exactly the way
    main.py does (incl. its 4dp/6dp rounding). Returns (p_up_live, raw_up, ctx);
    model_probability_raw is stamped per side by _parity_replayed_prob."""
    import math
    import numpy as np
    from polybot.core.signal_engine import SignalEngine

    closes = np.array([66000 + 30 * math.sin(i * 0.7) + i * 3 for i in range(40)], dtype=float)
    atr, secs, fs, sf, pm = 30.0, 180.0, 0.3, 0.2, 12.0
    eng = SignalEngine(weights=dict(_PARITY_L4), derived_weights=dict(_PARITY_L6),
                       **_PARITY_PARAMS)
    p_up = eng.compute_probability(btc_price, strike, secs, atr, _PARITY_IND,
                                   closes=closes, flow_signal=fs, spot_flow_signal=sf,
                                   prev_resolution_margin=pm)
    ctx = {
        "btc_price": btc_price, "strike_price": strike, "seconds_remaining": secs,
        "market_price_up": 0.30, "market_price_down": 0.30,
        "closes_tail": [float(closes[-2]), float(closes[-1])],
        "atr": atr,
        "atr_rolling_20": round(eng.last_atr_rolling_20, 6),
        "atr_long_term_mean": round(eng.last_atr_long_term_mean, 6),
        "prev_resolution_margin": pm,
        "flow_score": fs, "spot_flow_signal": sf,
        "regime_autocorr": round(eng.last_regime_autocorr, 4),
        "regime_direction": round(eng.last_regime_direction, 4),
    }
    return p_up, eng.last_raw_prob_up, ctx


def _parity_replayed_prob(ctx: dict, side: str, raw_up: float) -> float:
    """Replay one stamped outcome and invert the Kelly sizing back to the
    probability the replay used (gain_pct=1, kelly_fraction=1 → return = raw
    Kelly; p = (r·net_b + 1) / (net_b + 1))."""
    from polybot.execution.base import DEFAULT_FEE_RATE
    sched = _bare_scheduler()
    side_ctx = dict(ctx)
    # main.py stamps the CHOSEN SIDE's raw probability.
    side_ctx["model_probability_raw"] = raw_up if side == "Up" else 1.0 - raw_up
    o = {"side": side, "gain_pct": 1.0,
         "exit_timestamp": datetime.now(timezone.utc).isoformat(),
         "indicator_snapshot": {**_PARITY_IND, "trade_context": side_ctx}}
    returns, _ = sched._kelly_bankroll_returns(
        outcomes=[o], recommended_weights=dict(_PARITY_L4),
        min_edge=-1.0, calibrator=None, kelly_fraction=1.0, min_kelly=0.0, min_prob=0.0,
        derived_weights=dict(_PARITY_L6), **_PARITY_PARAMS)
    assert len(returns) == 1, "parity row was filtered out of the replay"
    mp = 0.30
    net_b = ((1.0 - mp) / mp) * (1.0 - DEFAULT_FEE_RATE)
    return (returns[0] * net_b + 1.0) / (net_b + 1.0)


def test_replay_matches_live_probability_up_side():
    """Full-stack numeric parity, Up side: L1 t-CDF + L2-L5 + active L6 + clamp +
    sigmoid must round-trip through the stamped context within stamping precision
    (regime is stamped at 4dp — anything beyond ~1e-3 is a real divergence)."""
    p_live, raw_up, ctx = _parity_live_and_ctx(btc_price=66480.0, strike=66400.0)
    p_replay = _parity_replayed_prob(ctx, "Up", raw_up)
    assert abs(p_replay - p_live) < 1e-3, f"live {p_live:.6f} vs replay {p_replay:.6f}"
    assert abs(p_live - 0.5) > 0.05, "test inputs must produce a non-trivial probability"


def test_replay_matches_live_probability_down_side():
    """Down side: the replay must produce P(down) = 1 - P(up), not misread the
    stored side-probability as P(up)."""
    p_live_up, raw_up, ctx = _parity_live_and_ctx(btc_price=66320.0, strike=66400.0)
    p_replay_down = _parity_replayed_prob(ctx, "Down", raw_up)
    assert abs(p_replay_down - (1.0 - p_live_up)) < 1e-3, (
        f"live P(down) {1.0 - p_live_up:.6f} vs replay {p_replay_down:.6f}")


def test_replay_skips_rows_without_l1_inputs_or_atr():
    """Rows missing btc/strike/seconds or with a dead ATR can't be replayed for a
    candidate (and live structurally never trades an ATR-dead tick) — they must be
    skipped in both arms, never approximated from the stored side-probability."""
    _, _, ctx = _parity_live_and_ctx(btc_price=66480.0, strike=66400.0)
    sched = _bare_scheduler()
    for missing in ("btc_price", "strike_price", "seconds_remaining", "atr"):
        broken = dict(ctx)
        broken[missing] = 0
        broken["model_probability_raw"] = 0.62
        o = {"side": "Up", "gain_pct": 1.0,
             "exit_timestamp": datetime.now(timezone.utc).isoformat(),
             "indicator_snapshot": {**_PARITY_IND, "trade_context": broken}}
        returns, _ = sched._kelly_bankroll_returns(
            outcomes=[o], recommended_weights=dict(_PARITY_L4),
            min_edge=-1.0, calibrator=None, kelly_fraction=1.0, min_kelly=0.0, min_prob=0.0,
            derived_weights=dict(_PARITY_L6), **_PARITY_PARAMS)
        assert returns == [], f"row with {missing}=0 must be skipped, not replayed"


@pytest.mark.asyncio
async def test_combined_holdout_check_includes_exit_threshold_override():
    """When the adopted batch contains exit_edge_threshold, the combined-holdout
    backtest must carry the counterfactual override — otherwise the 'joint set'
    silently excludes that change's entire effect."""
    sched = _bare_scheduler()
    sched.weight_optimizer.should_adopt.return_value = (True, "ok", 1.0)
    sched.pipeline_tracker = None
    sched._check_regime_adoption = lambda *a, **k: (True, "regime ok")
    sched._load_counterfactual_index = lambda: {1: {"counterfactual": {"gain_pct": 0.5},
                                                    "context_at_scalp": {}}}
    base_returns = ([0.010, -0.009] * 30, [1.0] * 60)   # sharpe ≈ 0.05
    cand_returns = ([0.050, 0.040] * 30, [1.0] * 60)    # sharpe ≈ 9
    sched._backtest_recommendations = lambda recs, outcomes: base_returns
    sched._backtest_single_change = lambda change, outcomes: {
        "returns": cand_returns[0], "weights": cand_returns[1],
        "sharpe": 9.0, "candidate_trades": 60}
    combined_calls = []
    def _spy(**kwargs):
        combined_calls.append(kwargs)
        return cand_returns
    sched._kelly_bankroll_returns = _spy
    recs = {"changes": [{"param": "exit_edge_threshold", "value": -0.05},
                        {"param": "min_edge", "value": 0.05}]}
    outcomes = _make_outcomes(200)
    info = await sched._run_weight_optimizer(recs, outcomes,
                                             holdout_outcomes=_make_outcomes(60))
    assert info["decision"] == "adopted", info
    assert len(combined_calls) == 1, "combined-holdout backtest must run once"
    assert combined_calls[0]["exit_threshold_override"] == -0.05
    assert combined_calls[0]["counterfactual_index"] is not None


@pytest.mark.asyncio
async def test_evolver_ghost_context_excludes_holdout_epoch_resolved_at():
    """Ghost `resolved_at` is an epoch float — the evolver-context holdout filter
    must parse it and drop holdout-period ghosts, not fail open and leak them."""
    seen: dict = {}
    now = datetime.now(timezone.utc)

    async def mock_bias(outcomes=None):
        return {"per_indicator": {}, "overall": {"total_trades": len(outcomes or [])}}
    async def mock_ta_evolver(analysis, outcomes=None):
        return {}
    async def mock_weight_optimizer(recs, outcomes=None, **kwargs):
        return {"decision": "skipped"}

    def _mk(days_ago):
        ts = (now - timedelta(days=days_ago)).isoformat()
        return {"timestamp": ts, "exit_timestamp": ts, "correct": True,
                "gain_pct": 0.1, "indicator_snapshot": {}}
    # 230 pre-holdout + 40 in-holdout → holdout active.
    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = (
        [_mk(8 + i * 0.2) for i in range(230)] + [_mk(0.1 + i * 0.05) for i in range(40)])

    ghost_tracker = MagicMock()
    old_ghost = {"resolved": True, "resolved_at": (now - timedelta(days=10)).timestamp(),
                 "gate_name": "old"}
    fresh_ghost = {"resolved": True, "resolved_at": now.timestamp(), "gate_name": "fresh"}
    ghost_tracker.load_all.return_value = [old_ghost, fresh_ghost]

    bias_detector = MagicMock()
    bias_detector.analyze_ghosts.side_effect = lambda ghosts: seen.update(
        gates=[g.get("gate_name") for g in ghosts]) or {}

    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=bias_detector,
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
    scheduler.ghost_tracker = ghost_tracker
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()

    assert seen.get("gates") == ["old"], (
        f"holdout-period ghost leaked into the evolver context: {seen.get('gates')}")


@pytest.mark.asyncio
async def test_frozen_pipeline_is_analysis_only(monkeypatch):
    """PIPELINE_FROZEN → analysis still runs, but no evolver/optimizer, no
    auto-revert, and the live calibrator object is left untouched."""
    import polybot.paths as paths_mod
    monkeypatch.setattr(paths_mod, "is_pipeline_frozen", lambda: True)
    from polybot.core.calibrator import IsotonicCalibrator

    calls = []
    async def mock_bias(outcomes=None):
        calls.append("bias")
        return {"per_indicator": {}, "overall": {}}
    async def mock_ta_evolver(analysis, outcomes=None):
        calls.append("ta_evolver")
        return {}
    async def mock_weight_optimizer(recs, outcomes=None, **kwargs):
        calls.append("weight_optimizer")
        return {"decision": "skipped"}

    outcome_reviewer = MagicMock()
    outcome_reviewer.load_all_outcomes.return_value = _make_outcomes(250)
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=MagicMock(),
        ta_evolver=MagicMock(), weight_optimizer=MagicMock(),
        outcome_interval_seconds=3600, daily_pipeline_hour=2)
    scheduler.signal_engine = MagicMock()
    sentinel_cal = IsotonicCalibrator()
    scheduler.signal_engine.calibrator = sentinel_cal
    scheduler.pipeline_tracker = MagicMock()
    scheduler._apply_revert_adoptions = lambda: calls.append("revert")
    scheduler._run_bias_detector = mock_bias
    scheduler._run_ta_evolver = mock_ta_evolver
    scheduler._run_weight_optimizer = mock_weight_optimizer
    await scheduler.run_daily_pipeline()

    assert calls == ["bias"], f"frozen cycle ran adoption stages: {calls}"
    assert scheduler.signal_engine.calibrator is sentinel_cal, "frozen cycle swapped the calibrator"


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
