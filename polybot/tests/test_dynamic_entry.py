import pytest
from polybot.main import compute_time_multiplier


class TestTimeMultiplier:
    def test_normal_window_full_kelly(self):
        """In the first 60% of window, multiplier is 1.0 regardless of prob."""
        result = compute_time_multiplier(prob=0.60, seconds_remaining=200.0)
        assert result["kelly_multiplier"] == 1.0
        assert result["phase"] == "normal"

    def test_always_allowed(self):
        """No hard observe block — SPRT owns observation."""
        result = compute_time_multiplier(prob=0.70, seconds_remaining=280.0)
        assert result["allowed"] is True

    def test_high_conviction_late_barely_penalized(self):
        """92% prob at T-45s should have high multiplier (~0.88)."""
        result = compute_time_multiplier(prob=0.92, seconds_remaining=45.0)
        assert result["kelly_multiplier"] > 0.80

    def test_atm_late_heavily_penalized(self):
        """60% prob at T-45s should be noticeably penalized vs normal."""
        result = compute_time_multiplier(prob=0.60, seconds_remaining=45.0)
        assert result["kelly_multiplier"] < 0.75  # penalized from 1.0

    def test_final_requires_high_prob(self):
        """Last 30s requires >90% confidence."""
        result = compute_time_multiplier(prob=0.85, seconds_remaining=15.0)
        assert result["min_prob_override"] == 0.90
        assert result["phase"] == "final"

    def test_final_no_override_outside_30s(self):
        result = compute_time_multiplier(prob=0.70, seconds_remaining=45.0)
        assert result["min_prob_override"] is None

    def test_multiplier_never_below_floor(self):
        """Worst case: ATM (50%) at expiry, still >= 0.40."""
        result = compute_time_multiplier(prob=0.50, seconds_remaining=1.0)
        assert result["kelly_multiplier"] >= 0.40

    def test_conviction_scales_penalty(self):
        """Higher conviction = less penalty at same time."""
        low = compute_time_multiplier(prob=0.60, seconds_remaining=60.0)
        high = compute_time_multiplier(prob=0.95, seconds_remaining=60.0)
        assert high["kelly_multiplier"] > low["kelly_multiplier"]
