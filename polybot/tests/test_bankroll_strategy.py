import pytest
from polybot.core.bankroll_strategy import compute_kelly_tier, _wilson_lower, compute_uncertainty_discount


class TestWilsonLower:
    def test_high_wr_large_n(self):
        """At 500 trades with 65% WR, lower bound should be well above 55%."""
        lb = _wilson_lower(0.65, 500)
        assert lb > 0.60

    def test_moderate_wr_small_n(self):
        """At 100 trades with 60% WR, lower bound should be ~50-53% (high uncertainty)."""
        lb = _wilson_lower(0.60, 100)
        assert 0.49 < lb < 0.55

    def test_zero_trades(self):
        assert _wilson_lower(0.60, 0) == 0.0

    def test_perfect_wr(self):
        """Even at 100% WR with 50 trades, lower bound is below 100%."""
        lb = _wilson_lower(1.0, 50)
        assert lb < 1.0
        assert lb > 0.90


class TestKellyTier:
    def test_baseline_under_200_trades(self):
        """Below min trades threshold, always returns base kelly."""
        assert compute_kelly_tier(trade_count=150, win_rate=0.65, base_kelly=0.15) == 0.15

    def test_tier1_needs_convincing_wr(self):
        """200 trades at 56% WR: Wilson lower ~0.49 < 0.55, stays at base."""
        assert compute_kelly_tier(trade_count=200, win_rate=0.56, base_kelly=0.15) == 0.15

    def test_tier1_at_200_with_strong_wr(self):
        """200 trades at 63% WR: Wilson lower ~0.56 >= 0.55, ratchets to 0.18."""
        assert compute_kelly_tier(trade_count=200, win_rate=0.63, base_kelly=0.15) == 0.18

    def test_tier2_at_400_trades(self):
        """400 trades at 63% WR: Wilson lower ~0.58 >= 0.56, ratchets to 0.22."""
        assert compute_kelly_tier(trade_count=400, win_rate=0.63, base_kelly=0.15) == 0.22

    def test_tier3_at_750_trades(self):
        """750 trades at 62% WR: Wilson lower ~0.585 >= 0.57, ratchets to 0.25."""
        assert compute_kelly_tier(trade_count=750, win_rate=0.62, base_kelly=0.15) == 0.25

    def test_drops_back_if_wr_falls(self):
        """Even at 750 trades, if WR drops to 53%, Wilson lower < 0.55."""
        assert compute_kelly_tier(trade_count=750, win_rate=0.53, base_kelly=0.15) == 0.15

    def test_zero_trades(self):
        assert compute_kelly_tier(trade_count=0, win_rate=0.0, base_kelly=0.15) == 0.15


class TestUncertaintyDiscount:
    def test_few_trades_hits_floor(self):
        """At 50 trades with 6% edge, raw would be 0.31 but floor is 0.50."""
        d = compute_uncertainty_discount(50, 0.06)
        assert d == 0.50  # floor prevents over-discounting

    def test_many_trades_light_discount(self):
        """At 1000 trades with 6% edge, discount should be near 1.0."""
        d = compute_uncertainty_discount(1000, 0.06)
        assert d > 0.90

    def test_zero_trades(self):
        d = compute_uncertainty_discount(0, 0.06)
        assert d == 0.50  # minimum floor

    def test_zero_edge(self):
        d = compute_uncertainty_discount(100, 0.0)
        assert d == 0.50  # minimum floor

    def test_large_edge_less_discount(self):
        """Larger edge means less relative uncertainty."""
        d_small = compute_uncertainty_discount(200, 0.04)
        d_large = compute_uncertainty_discount(200, 0.10)
        assert d_large > d_small
