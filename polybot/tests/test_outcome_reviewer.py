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
    reviewer.record_outcome(position_id=1, market_id="market_123", question="Will X happen?",
        side="YES", predicted_probability=0.72, actual_outcome=True, entry_price=0.55,
        exit_price=0.68, log_return=0.212, prompt_version="v001", category="politics")
    files = list(Path(outcomes_dir).glob("*.json"))
    assert len(files) == 1

def test_record_outcome_content(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="market_123", question="Will X happen?",
        side="YES", predicted_probability=0.72, actual_outcome=True, entry_price=0.55,
        exit_price=0.68, log_return=0.212, prompt_version="v001", category="politics")
    files = list(Path(outcomes_dir).glob("*.json"))
    data = json.loads(files[0].read_text())
    assert data["predicted_probability"] == 0.72
    assert data["actual_outcome"] is True
    assert data["correct"] is True
    assert data["error"] == pytest.approx(0.28, abs=0.01)

def test_correct_when_predicted_high_and_resolved_yes(reviewer):
    record = reviewer._evaluate(predicted_probability=0.72, actual_outcome=True)
    assert record["correct"] is True

def test_incorrect_when_predicted_high_and_resolved_no(reviewer):
    record = reviewer._evaluate(predicted_probability=0.72, actual_outcome=False)
    assert record["correct"] is False

def test_error_calculation(reviewer):
    record = reviewer._evaluate(predicted_probability=0.72, actual_outcome=True)
    assert record["error"] == pytest.approx(0.28, abs=0.01)

def test_load_all_outcomes(reviewer, outcomes_dir):
    for i in range(3):
        reviewer.record_outcome(position_id=i, market_id=f"market_{i}", question="Q?",
            side="YES", predicted_probability=0.7, actual_outcome=True, entry_price=0.55,
            exit_price=0.68, log_return=0.2, prompt_version="v001", category="politics")
    outcomes = reviewer.load_all_outcomes()
    assert len(outcomes) == 3
