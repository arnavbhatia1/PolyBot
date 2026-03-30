import json
import pytest
from pathlib import Path
from polybot.agents.strategy_evolver import StrategyEvolver, StrategyRecommendation

def test_analyze_outcomes_detects_low_win_rate():
    outcomes = [{"correct": False, "log_return": -0.1} for _ in range(8)] + [{"correct": True, "log_return": 0.05} for _ in range(2)]
    evolver = StrategyEvolver(strategy_log_path="/tmp/test_log.md")
    analysis = evolver.analyze_local(outcomes, current_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})
    assert analysis["win_rate"] == pytest.approx(0.20, abs=0.01)

def test_analyze_outcomes_detects_high_win_rate():
    outcomes = [{"correct": True, "log_return": 0.05} for _ in range(9)] + [{"correct": False, "log_return": -0.1} for _ in range(1)]
    evolver = StrategyEvolver(strategy_log_path="/tmp/test_log.md")
    analysis = evolver.analyze_local(outcomes, current_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})
    assert analysis["win_rate"] == pytest.approx(0.90, abs=0.01)

def test_generate_recommendations_low_win_rate():
    evolver = StrategyEvolver(strategy_log_path="/tmp/test_log.md")
    analysis = {"win_rate": 0.30, "avg_log_return": -0.05, "total_trades": 10}
    recs = evolver.generate_recommendations(analysis, current_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24})
    assert len(recs) > 0
    params = [r.param for r in recs]
    assert "ev_threshold" in params

def test_recommendation_dataclass():
    rec = StrategyRecommendation(param="ev_threshold", current_value=0.05, recommended_value=0.08,
        reason="Win rate below 50%, raising EV threshold")
    assert rec.param == "ev_threshold"
    assert rec.recommended_value == 0.08

def test_save_log(tmp_path):
    log_path = tmp_path / "strategy_log.md"
    evolver = StrategyEvolver(strategy_log_path=str(log_path))
    recs = [StrategyRecommendation("ev_threshold", 0.05, 0.08, "Low win rate")]
    evolver.save_log(recs, analysis={"win_rate": 0.30, "total_trades": 10})
    assert log_path.exists()
    content = log_path.read_text()
    assert "ev_threshold" in content
