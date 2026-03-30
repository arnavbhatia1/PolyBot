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

def _make_outcome(ind_scores, correct):
    return {
        "correct": correct,
        "indicator_snapshot": {
            "rsi": {"score": ind_scores.get("rsi", 0)},
            "macd": {"score": ind_scores.get("macd", 0)},
            "stochastic": {"score": ind_scores.get("stochastic", 0)},
            "obv": {"score": ind_scores.get("obv", 0)},
            "vwap": {"score": ind_scores.get("vwap", 0)},
        }
    }

def test_detect_returns_dict(detector):
    outcomes = [
        _make_outcome({"rsi": 0.5, "macd": 0.5}, correct=True),
        _make_outcome({"rsi": 0.5, "macd": 0.5}, correct=True),
        _make_outcome({"rsi": 0.5, "macd": 0.5}, correct=False),
    ]
    biases = detector.detect(outcomes)
    assert isinstance(biases, dict)

def test_useful_indicator_gets_positive_bias(detector):
    outcomes = [_make_outcome({"rsi": 0.8}, correct=True) for _ in range(5)]
    biases = detector.detect(outcomes)
    assert "rsi" in biases
    assert biases["rsi"] > 0

def test_misleading_indicator_gets_negative_bias(detector):
    outcomes = [_make_outcome({"macd": 0.8}, correct=False) for _ in range(5)]
    biases = detector.detect(outcomes)
    assert "macd" in biases
    assert biases["macd"] < 0

def test_skips_with_few_samples(detector):
    outcomes = [_make_outcome({"rsi": 0.5}, correct=True)]
    biases = detector.detect(outcomes, min_samples=3)
    assert len(biases) == 0

def test_save_biases_writes_file(detector, biases_path):
    detector.save({"rsi": 0.15, "macd": -0.1})
    assert biases_path.exists()
    saved = json.loads(biases_path.read_text())
    assert saved["rsi"] == 0.15
