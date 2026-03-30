import json
import pytest
from pathlib import Path
from polybot.brain.prompt_builder import PromptBuilder


@pytest.fixture
def prompt_dir(tmp_path):
    v1 = tmp_path / "v001.txt"
    v1.write_text("You are a prediction market analyst. Estimate the probability of YES.")
    v2 = tmp_path / "v002.txt"
    v2.write_text("You are an expert analyst. Consider base rates carefully.")
    return tmp_path


@pytest.fixture
def biases_file(tmp_path):
    biases = {"politics": -0.14, "crypto": 0.05}
    path = tmp_path / "biases.json"
    path.write_text(json.dumps(biases))
    return path


@pytest.fixture
def lessons_file(tmp_path):
    lessons = {"overconfidence": "Claude tends to be overconfident on short-expiry markets",
               "volume_signal": "High volume spikes often precede resolution"}
    path = tmp_path / "lessons.json"
    path.write_text(json.dumps(lessons))
    return path


def test_load_base_prompt(prompt_dir):
    builder = PromptBuilder(prompts_dir=str(prompt_dir))
    prompt = builder.load_base_prompt("v001")
    assert "prediction market analyst" in prompt


def test_load_nonexistent_version_raises(prompt_dir):
    builder = PromptBuilder(prompts_dir=str(prompt_dir))
    with pytest.raises(FileNotFoundError):
        builder.load_base_prompt("v999")


def test_build_prompt_includes_base(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(prompts_dir=str(prompt_dir), biases_path=str(biases_file), lessons_path=str(lessons_file))
    prompt = builder.build(version="v001", category="politics")
    assert "prediction market analyst" in prompt


def test_build_prompt_includes_bias_correction(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(prompts_dir=str(prompt_dir), biases_path=str(biases_file), lessons_path=str(lessons_file))
    prompt = builder.build(version="v001", category="politics")
    assert "-14" in prompt or "14%" in prompt


def test_build_prompt_includes_lessons(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(prompts_dir=str(prompt_dir), biases_path=str(biases_file), lessons_path=str(lessons_file))
    prompt = builder.build(version="v001", category="politics")
    assert "overconfident" in prompt


def test_build_prompt_no_bias_for_unknown_category(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(prompts_dir=str(prompt_dir), biases_path=str(biases_file), lessons_path=str(lessons_file))
    prompt = builder.build(version="v001", category="sports")
    assert "bias correction" not in prompt.lower() or "no known bias" in prompt.lower()


def test_build_prompt_handles_missing_files(prompt_dir):
    builder = PromptBuilder(prompts_dir=str(prompt_dir), biases_path="/nonexistent/biases.json", lessons_path="/nonexistent/lessons.json")
    prompt = builder.build(version="v001", category="politics")
    assert "prediction market analyst" in prompt
