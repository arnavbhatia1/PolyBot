import json
import pytest
from polybot.agents.weight_optimizer import WeightOptimizer

@pytest.fixture
def weights_dir(tmp_path):
    d = tmp_path / "weights"
    d.mkdir()
    (d / "weights_v001.json").write_text(json.dumps({"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20,
                                                     "entry_threshold": 0.60, "version": "weights_v001"}))
    return d

@pytest.fixture
def scores_path(tmp_path):
    path = tmp_path / "weight_scores.json"
    path.write_text(json.dumps({"weights_v001": {"sharpe": 1.2, "total_trades": 50, "win_rate": 0.65}}))
    return path

@pytest.fixture
def optimizer(weights_dir, scores_path):
    return WeightOptimizer(weights_dir=str(weights_dir), scores_path=str(scores_path), min_improvement=0.03)

def test_get_scores(optimizer):
    assert "weights_v001" in optimizer.get_scores()

def test_get_best_version(optimizer):
    assert optimizer.get_best_version() == "weights_v001"

def test_save_weights(optimizer, weights_dir):
    optimizer.save_weights("weights_v002", {"rsi": 0.22})
    assert (weights_dir / "weights_v002.json").exists()

def test_record_score(optimizer, scores_path):
    optimizer.record_score("weights_v002", sharpe=1.5, total_trades=30, win_rate=0.70)
    assert "weights_v002" in json.loads(scores_path.read_text())

def test_should_adopt(optimizer):
    assert optimizer.should_adopt(1.2, 1.3) is True
    assert optimizer.should_adopt(1.2, 1.21) is False

def test_get_next_version(optimizer):
    assert optimizer.get_next_version() == "weights_v002"
