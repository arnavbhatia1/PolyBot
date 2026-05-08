import pytest
from polybot.main import compute_time_multiplier


class TestTimeMultiplier:
    def test_normal_window_full_kelly(self):
        mult, phase = compute_time_multiplier(prob=0.60, seconds_remaining=200.0)
        assert mult == 1.0
        assert phase == "normal"

    def test_high_conviction_late_barely_penalized(self):
        mult, _ = compute_time_multiplier(prob=0.92, seconds_remaining=45.0)
        assert mult > 0.80

    def test_atm_late_heavily_penalized(self):
        mult, _ = compute_time_multiplier(prob=0.60, seconds_remaining=45.0)
        assert mult < 0.75

    def test_final_phase_label(self):
        _, phase = compute_time_multiplier(prob=0.85, seconds_remaining=15.0)
        assert phase == "final"

    def test_multiplier_never_below_floor(self):
        mult, _ = compute_time_multiplier(prob=0.50, seconds_remaining=1.0)
        assert mult >= 0.40

    def test_conviction_scales_penalty(self):
        low, _ = compute_time_multiplier(prob=0.60, seconds_remaining=60.0)
        high, _ = compute_time_multiplier(prob=0.95, seconds_remaining=60.0)
        assert high > low
