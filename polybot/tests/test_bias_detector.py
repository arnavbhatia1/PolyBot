import json
import pytest
from pathlib import Path
from polybot.agents.bias_detector import BiasDetector

@pytest.fixture
def biases_path(tmp_path):
    return tmp_path / "biases.json"

@pytest.fixture
def detector(biases_path):
    return BiasDetector(biases_path=str(biases_path))

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

def test_save_writes_file(detector, biases_path):
    detector.save({"per_indicator": {"rsi": {"accuracy": 0.65}}, "overall": {}})
    assert biases_path.exists()
    saved = json.loads(biases_path.read_text())
    assert saved["per_indicator"]["rsi"]["accuracy"] == 0.65

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
    assert "10-20%" in cal
    assert cal["10-20%"]["count"] == 2

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
