import pytest
from polybot.agents.ta_evolver import TAEvolver

@pytest.fixture
def evolver(tmp_path):
    return TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"))

def test_analyze_computes_stats(evolver):
    outcomes = [
        {"correct": True, "log_return": 0.05, "indicator_snapshot": {"rsi": {"score": 0.8}, "macd": {"score": 0.7},
         "stochastic": {"score": 0.5}, "obv": {"score": 0.3}, "vwap": {"score": 0.2}}},
        {"correct": False, "log_return": -0.1, "indicator_snapshot": {"rsi": {"score": 0.3}, "macd": {"score": 0.2},
         "stochastic": {"score": 0.1}, "obv": {"score": -0.1}, "vwap": {"score": 0.0}}}]
    analysis = evolver.analyze(outcomes)
    assert analysis["total_trades"] == 2 and "win_rate" in analysis

def test_recommend_weights(evolver):
    outcomes = [{"correct": True, "log_return": 0.05,
                 "indicator_snapshot": {"rsi": {"score": 0.8}, "macd": {"score": 0.9},
                  "stochastic": {"score": 0.5}, "obv": {"score": 0.1}, "vwap": {"score": 0.3}}} for _ in range(10)]
    recs = evolver.recommend_weight_adjustments(outcomes, {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20})
    assert isinstance(recs, dict) and "rsi" in recs

def test_save_log(evolver, tmp_path):
    evolver.save_log({"win_rate": 0.65, "total_trades": 15}, {"rsi": 0.22})
    assert (tmp_path / "strategy_log.md").exists()
