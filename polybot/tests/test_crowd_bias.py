import pytest
from polybot.core.crowd_bias import CrowdBiasTracker


class TestFLB:
    def test_favorite_undervalued(self):
        t = CrowdBiasTracker()
        adj = t.compute_flb_adjustment(0.85, 0.14)
        assert adj > 0  # favorite (Up at 85%) is undervalued

    def test_longshot_overvalued(self):
        t = CrowdBiasTracker()
        adj = t.compute_flb_adjustment(0.15, 0.84)
        assert adj < 0  # longshot (Up at 15%) is overvalued

    def test_neutral_at_50(self):
        t = CrowdBiasTracker()
        adj = t.compute_flb_adjustment(0.50, 0.49)
        assert adj == 0.0

    def test_clamped_to_range(self):
        t = CrowdBiasTracker()
        adj = t.compute_flb_adjustment(0.99, 0.01)
        assert -0.03 <= adj <= 0.03

    def test_boundary_at_65(self):
        t = CrowdBiasTracker()
        adj = t.compute_flb_adjustment(0.65, 0.34)
        assert adj > 0

    def test_boundary_at_35(self):
        t = CrowdBiasTracker()
        adj = t.compute_flb_adjustment(0.35, 0.64)
        assert adj < 0

    def test_dead_zone(self):
        """Between 35-65%, FLB should be zero."""
        t = CrowdBiasTracker()
        assert t.compute_flb_adjustment(0.50, 0.49) == 0.0
        assert t.compute_flb_adjustment(0.45, 0.54) == 0.0
        assert t.compute_flb_adjustment(0.55, 0.44) == 0.0


class TestRecencyFade:
    def test_no_streak(self):
        t = CrowdBiasTracker()
        t.record_resolution("Up")
        t.record_resolution("Down")
        t.record_resolution("Up")
        assert t.compute_recency_fade() == 0.0

    def test_fade_up_streak(self):
        t = CrowdBiasTracker()
        for _ in range(4):
            t.record_resolution("Up")
        fade = t.compute_recency_fade()
        assert fade < 0  # should fade Up streak -> bearish

    def test_fade_down_streak(self):
        t = CrowdBiasTracker()
        for _ in range(3):
            t.record_resolution("Down")
        fade = t.compute_recency_fade()
        assert fade > 0  # should fade Down streak -> bullish

    def test_longer_streak_stronger(self):
        t3 = CrowdBiasTracker()
        for _ in range(3): t3.record_resolution("Up")
        t5 = CrowdBiasTracker()
        for _ in range(5): t5.record_resolution("Up")
        assert abs(t5.compute_recency_fade()) > abs(t3.compute_recency_fade())

    def test_too_few_resolutions(self):
        t = CrowdBiasTracker()
        t.record_resolution("Up")
        t.record_resolution("Up")
        assert t.compute_recency_fade() == 0.0

    def test_streak_broken(self):
        t = CrowdBiasTracker()
        for _ in range(5):
            t.record_resolution("Up")
        t.record_resolution("Down")
        # Streak is broken — only 1 Down
        assert t.compute_recency_fade() == 0.0

    def test_streak_strength_values(self):
        # streak=3 -> strength=0.3
        t = CrowdBiasTracker()
        for _ in range(3): t.record_resolution("Down")
        assert t.compute_recency_fade() == pytest.approx(0.3)

        # streak=4 -> strength=0.6
        t = CrowdBiasTracker()
        for _ in range(4): t.record_resolution("Down")
        assert t.compute_recency_fade() == pytest.approx(0.6)

        # streak=5 -> strength=0.9
        t = CrowdBiasTracker()
        for _ in range(5): t.record_resolution("Down")
        assert t.compute_recency_fade() == pytest.approx(0.9)

        # streak=6+ -> strength=1.0 (capped)
        t = CrowdBiasTracker()
        for _ in range(7): t.record_resolution("Down")
        assert t.compute_recency_fade() == pytest.approx(1.0)

    def test_max_history(self):
        t = CrowdBiasTracker(max_history=5)
        for _ in range(10):
            t.record_resolution("Up")
        assert len(t._resolution_history) == 5


class TestRoundNumber:
    def test_near_round_thousand(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(70000) < 1.0
        assert t.compute_round_number_signal(71000) < 1.0

    def test_far_from_round(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(70500) == 1.0

    def test_exact_round(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(80000) == 0.92

    def test_near_round_within_50(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(70025) == 0.92
        assert t.compute_round_number_signal(69975) == 0.92

    def test_near_round_within_100(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(70075) == 0.96
        assert t.compute_round_number_signal(69925) == 0.96

    def test_zero_strike(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(0) == 1.0

    def test_negative_strike(self):
        t = CrowdBiasTracker()
        assert t.compute_round_number_signal(-100) == 1.0


class TestComposite:
    def test_composite_keys(self):
        t = CrowdBiasTracker()
        result = t.compute_composite(0.85, 0.14, 70500)
        assert "flb" in result
        assert "recency_fade" in result
        assert "round_number_dampening" in result
        assert "composite_logit_adjustment" in result

    def test_composite_with_all_signals(self):
        t = CrowdBiasTracker()
        for _ in range(4):
            t.record_resolution("Up")
        result = t.compute_composite(0.85, 0.14, 70000)
        # FLB > 0 (favorite Up), recency < 0 (fade Up streak), round < 1.0
        assert result["flb"] > 0
        assert result["recency_fade"] < 0
        assert result["round_number_dampening"] < 1.0

    def test_composite_neutral(self):
        t = CrowdBiasTracker()
        result = t.compute_composite(0.50, 0.49, 70500)
        assert result["flb"] == 0.0
        assert result["recency_fade"] == 0.0
        assert result["round_number_dampening"] == 1.0
        assert result["composite_logit_adjustment"] == 0.0
