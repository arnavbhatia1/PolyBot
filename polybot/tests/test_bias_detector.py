import pytest
from polybot.agents.bias_detector import BiasDetector
from polybot.agents.pipeline_analytics import ghost_gain_pct

@pytest.fixture
def detector():
    return BiasDetector()

def _make_outcome(ind_scores, correct, trade_context=None):
    snap = {
        "rsi": {"score": ind_scores.get("rsi", 0)},
        "macd": {"score": ind_scores.get("macd", 0)},
        "stochastic": {"score": ind_scores.get("stochastic", 0)},
        "obv": {"score": ind_scores.get("obv", 0)},
        "vwap": {"score": ind_scores.get("vwap", 0)},
    }
    if trade_context:
        snap["trade_context"] = trade_context
    return {
        "correct": correct,
        "side": trade_context.get("side", "up") if trade_context else "up",
        "log_return": 0.05 if correct else -0.10,
        "indicator_snapshot": snap,
    }

def test_analyze_ghosts_segments_by_phase_and_flip(detector):
    """P2-6: ghosts carry entry_phase/flip_count/is_flip (stamped at gate-fire);
    analyze_ghosts must surface by_entry_phase / by_flip breakdowns, not just by_gate."""
    def _ghost(phase, flip_count, correct):
        return {
            "gate_name": "edge_decay", "resolved": True, "ghost_correct": correct,
            "side": "up", "ghost_gain_pct": 0.1 if correct else -1.0,
            "indicator_snapshot": {"trade_context": {
                "market_price_up": 0.55, "size": 10.0,
                "entry_phase": phase, "flip_count": flip_count, "is_flip": flip_count > 0,
            }},
        }
    ghosts = [
        _ghost("normal", 0, True), _ghost("normal", 0, False),
        _ghost("late", 0, True), _ghost("normal", 2, True),
    ]
    res = detector.analyze_ghosts(ghosts)
    assert "by_entry_phase" in res and "by_flip" in res
    assert set(res["by_entry_phase"]) == {"normal", "late"}
    assert res["by_entry_phase"]["normal"]["count"] == 3
    assert res["by_flip"]["flip"]["count"] == 1
    assert res["by_flip"]["initial"]["count"] == 3

def test_analyze_ghosts_prices_fee_aware(detector):
    """Ghost gain/simulated_pnl must net the entry fee via ghost_gain_pct so
    ghost analysis and the optimizer pool price ghosts identically."""
    ghosts = [
        {
            "gate_name": "edge_cap", "resolved": True, "ghost_correct": True,
            "side": "up", "ghost_gain_pct": 0.8182,
            "indicator_snapshot": {"trade_context": {"market_price_up": 0.55, "size": 10.0}},
        },
        {
            "gate_name": "edge_cap", "resolved": True, "ghost_correct": False,
            "side": "down", "ghost_gain_pct": -1.0,
            "indicator_snapshot": {"trade_context": {"market_price_down": 0.40, "size": 10.0}},
        },
    ]
    res = detector.analyze_ghosts(ghosts)
    g_win = ghost_gain_pct(0.55, True)
    g_loss = ghost_gain_pct(0.40, False)
    gate = res["by_gate"]["edge_cap"]
    assert gate["avg_gain_pct"] == pytest.approx(round((g_win + g_loss) / 2, 4), abs=1e-4)
    assert gate["simulated_pnl"] == pytest.approx(round(10.0 * g_win + 10.0 * g_loss, 2), abs=0.01)

def test_adverse_rate_zero_not_coerced_to_half(detector):
    """A genuine 0.0 adverse rate buckets as low; None/missing default to 0.5 (medium)."""
    def _o(ctx):
        return {"correct": True, "gain_pct": 0.1,
                "indicator_snapshot": {"trade_context": ctx}}
    res = detector._analyze_by_adverse_selection([
        _o({"adverse_rate_at_30s": 0.0}),
        _o({"adverse_selection_30s": 0.0}),
        _o({"adverse_rate_at_30s": None}),
        _o({}),
    ])
    assert res["low"]["n"] == 2
    assert res["medium"]["n"] == 2

def test_detect_returns_rich_dict(detector):
    outcomes = [
        _make_outcome({"rsi": 0.5, "macd": 0.5}, correct=True),
        _make_outcome({"rsi": 0.5, "macd": 0.5}, correct=True),
        _make_outcome({"rsi": 0.5, "macd": 0.5}, correct=False),
    ]
    result = detector.detect(outcomes)
    assert isinstance(result, dict)
    assert "per_indicator" in result
    assert "side_analysis" in result
    assert "edge_calibration" in result
    assert "time_patterns" in result
    assert "volatility_patterns" in result
    assert "overall" in result

def test_useful_indicator_gets_high_accuracy(detector):
    outcomes = [_make_outcome({"rsi": 0.8}, correct=True) for _ in range(5)]
    result = detector.detect(outcomes)
    assert "rsi" in result["per_indicator"]
    assert result["per_indicator"]["rsi"]["accuracy"] > 0.5

def test_misleading_indicator_gets_low_accuracy(detector):
    outcomes = [_make_outcome({"macd": 0.8}, correct=False) for _ in range(5)]
    result = detector.detect(outcomes)
    assert "macd" in result["per_indicator"]
    assert result["per_indicator"]["macd"]["accuracy"] < 0.5

def test_skips_with_few_samples(detector):
    outcomes = [_make_outcome({"rsi": 0.5}, correct=True)]
    result = detector.detect(outcomes, min_samples=3)
    assert result["per_indicator"] == {}

def test_overall_stats(detector):
    outcomes = [
        _make_outcome({"rsi": 0.5}, correct=True),
        _make_outcome({"rsi": 0.5}, correct=True),
        _make_outcome({"rsi": 0.5}, correct=False),
    ]
    result = detector.detect(outcomes)
    overall = result["overall"]
    assert overall["total_trades"] == 3
    assert 0.6 < overall["win_rate"] < 0.7  # 2/3

def test_side_analysis(detector):
    outcomes = [
        _make_outcome({"rsi": 0.3}, correct=True, trade_context={"side": "up"}),
        _make_outcome({"rsi": 0.3}, correct=False, trade_context={"side": "down"}),
        _make_outcome({"rsi": 0.3}, correct=True, trade_context={"side": "up"}),
    ]
    result = detector.detect(outcomes)
    sides = result["side_analysis"]
    assert sides["up"]["count"] == 2
    assert sides["up"]["win_rate"] == 1.0
    assert sides["down"]["count"] == 1
    assert sides["down"]["win_rate"] == 0.0

def test_edge_calibration_with_trade_context(detector):
    outcomes = [
        _make_outcome({"rsi": 0.5}, correct=True, trade_context={"edge": 0.12}),
        _make_outcome({"rsi": 0.5}, correct=True, trade_context={"edge": 0.25}),
        _make_outcome({"rsi": 0.5}, correct=False, trade_context={"edge": 0.15}),
    ]
    result = detector.detect(outcomes)
    cal = result["edge_calibration"]
    # Buckets: "4-8%", "8-12%", "12-20%", "20%+". Edge 0.12 → "12-20%", 0.15 → "12-20%", 0.25 → "20%+"
    assert "12-20%" in cal
    assert cal["12-20%"]["count"] == 2
    assert "20%+" in cal
    assert cal["20%+"]["count"] == 1

def test_time_patterns_with_trade_context(detector):
    outcomes = [
        _make_outcome({"rsi": 0.5}, correct=True, trade_context={"seconds_remaining": 240}),
        _make_outcome({"rsi": 0.5}, correct=False, trade_context={"seconds_remaining": 120}),
        _make_outcome({"rsi": 0.5}, correct=True, trade_context={"seconds_remaining": 30}),
    ]
    result = detector.detect(outcomes)
    tp = result["time_patterns"]
    assert "180-300s" in tp
    assert "60-180s" in tp
    assert "0-60s" in tp

def test_graceful_without_trade_context(detector):
    """Outcomes lacking trade_context should still produce valid per_indicator and overall."""
    outcomes = [_make_outcome({"rsi": 0.8}, correct=True) for _ in range(5)]
    result = detector.detect(outcomes)
    assert result["edge_calibration"] == {}
    assert result["time_patterns"] == {}
    assert result["volatility_patterns"] == {}
    assert result["overall"]["total_trades"] == 5
    assert "rsi" in result["per_indicator"]
