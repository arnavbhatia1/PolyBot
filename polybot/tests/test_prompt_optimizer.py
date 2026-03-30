import json
import pytest
from pathlib import Path
from polybot.agents.prompt_optimizer import PromptOptimizer

@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "v001.txt").write_text("You are an analyst.")
    (d / "v002.txt").write_text("You are an expert analyst.")
    return d

@pytest.fixture
def scores_path(tmp_path):
    path = tmp_path / "prompt_scores.json"
    path.write_text(json.dumps({"v001": {"accuracy": 0.55, "total": 30}, "v002": {"accuracy": 0.62, "total": 20}}))
    return path

@pytest.fixture
def optimizer(prompts_dir, scores_path):
    return PromptOptimizer(prompts_dir=str(prompts_dir), scores_path=str(scores_path), min_improvement=0.03)

def test_get_version_scores(optimizer):
    scores = optimizer.get_version_scores()
    assert scores["v001"]["accuracy"] == 0.55

def test_get_best_version(optimizer):
    assert optimizer.get_best_version() == "v002"

def test_record_score(optimizer, scores_path):
    optimizer.record_score("v003", accuracy=0.70, total=10)
    scores = json.loads(scores_path.read_text())
    assert "v003" in scores

def test_get_next_version(optimizer):
    assert optimizer.get_next_version() == "v003"

def test_save_new_prompt(optimizer, prompts_dir):
    optimizer.save_prompt("v003", "New improved prompt.")
    assert (prompts_dir / "v003.txt").exists()

def test_should_adopt_when_improvement_above_threshold(optimizer):
    assert optimizer.should_adopt(current_accuracy=0.62, candidate_accuracy=0.66) is True

def test_should_not_adopt_when_improvement_below_threshold(optimizer):
    assert optimizer.should_adopt(current_accuracy=0.62, candidate_accuracy=0.63) is False
