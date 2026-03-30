import json
import pytest
from pathlib import Path
from polybot.agents.bias_detector import BiasDetector

@pytest.fixture
def outcomes():
    return [
        {"category": "politics", "predicted_probability": 0.80, "actual_outcome": True, "error": 0.20, "correct": True},
        {"category": "politics", "predicted_probability": 0.75, "actual_outcome": False, "error": 0.75, "correct": False},
        {"category": "politics", "predicted_probability": 0.85, "actual_outcome": True, "error": 0.15, "correct": True},
        {"category": "politics", "predicted_probability": 0.70, "actual_outcome": False, "error": 0.70, "correct": False},
        {"category": "crypto", "predicted_probability": 0.60, "actual_outcome": True, "error": 0.40, "correct": True},
        {"category": "crypto", "predicted_probability": 0.55, "actual_outcome": True, "error": 0.45, "correct": True},
        {"category": "crypto", "predicted_probability": 0.50, "actual_outcome": True, "error": 0.50, "correct": True},
    ]

@pytest.fixture
def biases_path(tmp_path):
    return tmp_path / "biases.json"

@pytest.fixture
def detector(biases_path):
    return BiasDetector(biases_path=str(biases_path))

def test_detect_biases_returns_dict(detector, outcomes):
    biases = detector.detect(outcomes)
    assert isinstance(biases, dict)

def test_detect_politics_overestimation(detector, outcomes):
    biases = detector.detect(outcomes)
    assert "politics" in biases
    assert biases["politics"] < 0

def test_detect_crypto_underestimation(detector, outcomes):
    biases = detector.detect(outcomes)
    assert "crypto" in biases
    assert biases["crypto"] > 0

def test_save_biases_writes_file(detector, outcomes, biases_path):
    biases = detector.detect(outcomes)
    detector.save(biases)
    assert biases_path.exists()
    saved = json.loads(biases_path.read_text())
    assert "politics" in saved

def test_detect_skips_categories_with_few_samples(detector):
    outcomes = [{"category": "sports", "predicted_probability": 0.80, "actual_outcome": True, "error": 0.20, "correct": True}]
    biases = detector.detect(outcomes, min_samples=3)
    assert "sports" not in biases
