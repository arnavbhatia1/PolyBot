import json
import pytest
from pathlib import Path
from polybot.agents.outcome_reviewer import OutcomeReviewer

@pytest.fixture
def outcomes_dir(tmp_path):
    return tmp_path / "outcomes"

@pytest.fixture
def reviewer(outcomes_dir):
    return OutcomeReviewer(outcomes_dir=str(outcomes_dir))

def test_record_outcome_creates_file(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="market_123", question="BTC Up?",
        side="Up", signal_score=0.72, profitable=True, entry_price=0.55,
        exit_price=0.68, log_return=0.212, weight_version="weights_v001", category="crypto-5min")
    files = list(Path(outcomes_dir).glob("*.json"))
    assert len(files) == 1

def test_record_outcome_content(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="market_123", question="BTC Up?",
        side="Up", signal_score=0.72, profitable=True, entry_price=0.55,
        exit_price=0.68, log_return=0.212, weight_version="weights_v001", category="crypto-5min",
        indicator_snapshot={"rsi": {"score": 0.3}, "macd": {"score": 0.5}})
    files = list(Path(outcomes_dir).glob("*.json"))
    data = json.loads(files[0].read_text())
    assert data["signal_score"] == 0.72
    assert data["correct"] is True
    assert data["weight_version"] == "weights_v001"
    assert data["indicator_snapshot"]["macd"]["score"] == 0.5

def test_profitable_trade_marked_correct(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="m1", question="Q?",
        side="Up", signal_score=0.5, profitable=True, entry_price=0.55,
        exit_price=0.68, log_return=0.2, weight_version="v001")
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["correct"] is True

def test_losing_trade_marked_incorrect(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="m1", question="Q?",
        side="Up", signal_score=0.5, profitable=False, entry_price=0.55,
        exit_price=0.40, log_return=-0.3, weight_version="v001")
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["correct"] is False

def test_load_all_outcomes(reviewer, outcomes_dir):
    for i in range(3):
        reviewer.record_outcome(position_id=i, market_id=f"market_{i}", question="Q?",
            side="Up", signal_score=0.7, profitable=True, entry_price=0.55,
            exit_price=0.68, log_return=0.2, weight_version="weights_v001", category="crypto-5min")
    outcomes = reviewer.load_all_outcomes()
    assert len(outcomes) == 3

def test_exit_reason_recorded(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="m1", question="Q?",
        side="Up", signal_score=0.7, profitable=True, entry_price=0.50,
        exit_price=0.80, log_return=0.47, weight_version="v001", exit_reason="scalp")
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["exit_reason"] == "scalp"

def test_exit_reason_defaults_to_resolution(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=2, market_id="m2", question="Q?",
        side="Down", signal_score=0.6, profitable=False, entry_price=0.55,
        exit_price=0.0, log_return=-10.0, weight_version="v001")
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["exit_reason"] == "resolution"
