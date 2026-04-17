import pytest
from polybot.core.bankroll_strategy import _wilson_lower, compute_uncertainty_discount, DrawdownVelocityTracker


class TestWilsonLower:
    def test_high_wr_large_n(self):
        lb = _wilson_lower(0.65, 500)
        assert lb > 0.60

    def test_moderate_wr_small_n(self):
        lb = _wilson_lower(0.60, 100)
        assert 0.49 < lb < 0.55

    def test_zero_trades(self):
        assert _wilson_lower(0.60, 0) == 0.0

    def test_perfect_wr(self):
        lb = _wilson_lower(1.0, 50)
        assert lb < 1.0
        assert lb > 0.90


class TestUncertaintyDiscount:
    def test_few_trades_hits_floor(self):
        # Smoothed curve: floor lowered to 0.40; n=50 still floor-bound at avg_edge=0.06
        d = compute_uncertainty_discount(50, 0.06)
        assert d == 0.40

    def test_many_trades_light_discount(self):
        d = compute_uncertainty_discount(1000, 0.06)
        assert d > 0.90

    def test_zero_trades(self):
        assert compute_uncertainty_discount(0, 0.06) == 0.40

    def test_zero_edge(self):
        assert compute_uncertainty_discount(100, 0.0) == 0.40

    def test_large_edge_less_discount(self):
        d_small = compute_uncertainty_discount(200, 0.04)
        d_large = compute_uncertainty_discount(200, 0.10)
        assert d_large > d_small

    def test_continuous_decay_past_prior_floor(self):
        # Previously 0.50-floor-pinned until ~140 trades at 6% edge. New curve
        # should show discount above the 0.40 floor at n=100 for avg_edge=0.06.
        d = compute_uncertainty_discount(100, 0.06)
        assert d > 0.40


class TestDrawdownVelocity:
    def test_no_breach_with_few_trades(self):
        t = DrawdownVelocityTracker()
        for _ in range(5):
            t.record_trade(-0.50)
        assert t.is_velocity_breach() is False  # < 10 trades

    def test_breach_on_heavy_losses(self):
        t = DrawdownVelocityTracker()
        for _ in range(15):
            t.record_trade(-0.10)  # 15 × -10% = -150% rolling PnL
        assert t.is_velocity_breach() is True

    def test_no_breach_on_wins(self):
        t = DrawdownVelocityTracker()
        for _ in range(15):
            t.record_trade(0.10)
        assert t.is_velocity_breach() is False
