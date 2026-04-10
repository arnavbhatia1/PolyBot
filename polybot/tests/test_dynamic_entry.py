import pytest
from polybot.main import compute_entry_phase

class TestEntryPhase:
    def test_observe_phase(self):
        result = compute_entry_phase(seconds_remaining=280.0)
        assert not result["allowed"]
        assert result["phase"] == "observe"

    def test_normal_phase(self):
        result = compute_entry_phase(seconds_remaining=200.0)
        assert result["allowed"]
        assert result["kelly_multiplier"] == 1.0
        assert result["phase"] == "normal"

    def test_late_phase(self):
        result = compute_entry_phase(seconds_remaining=100.0)
        assert result["allowed"]
        assert result["kelly_multiplier"] == 0.7
        assert result["phase"] == "late"

    def test_final_phase_requires_high_prob(self):
        result = compute_entry_phase(seconds_remaining=40.0)
        assert result["allowed"]
        assert result["min_prob_override"] == 0.90
        assert result["phase"] == "final"

    def test_boundary_observe_to_normal(self):
        # Exactly 60s elapsed = 240s remaining = end of observe, start of normal
        result = compute_entry_phase(seconds_remaining=240.0)
        assert result["allowed"]
        assert result["phase"] == "normal"

    def test_zero_remaining(self):
        result = compute_entry_phase(seconds_remaining=0.0)
        assert result["phase"] == "final"
