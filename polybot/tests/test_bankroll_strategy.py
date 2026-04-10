import pytest
from polybot.core.bankroll_strategy import compute_kelly_tier


class TestKellyTier:
    def test_baseline_under_100_trades(self):
        assert compute_kelly_tier(trade_count=50, win_rate=0.60, base_kelly=0.15) == 0.15

    def test_tier2_at_100_trades(self):
        assert compute_kelly_tier(trade_count=150, win_rate=0.56, base_kelly=0.15) == 0.18

    def test_tier2_requires_win_rate(self):
        assert compute_kelly_tier(trade_count=150, win_rate=0.53, base_kelly=0.15) == 0.15

    def test_tier3_at_250_trades(self):
        assert compute_kelly_tier(trade_count=300, win_rate=0.57, base_kelly=0.15) == 0.22

    def test_tier4_at_500_trades(self):
        assert compute_kelly_tier(trade_count=600, win_rate=0.58, base_kelly=0.15) == 0.25

    def test_tier4_requires_high_win_rate(self):
        assert compute_kelly_tier(trade_count=600, win_rate=0.56, base_kelly=0.15) == 0.22

    def test_drops_back_if_win_rate_falls(self):
        assert compute_kelly_tier(trade_count=300, win_rate=0.53, base_kelly=0.15) == 0.15

    def test_zero_trades(self):
        assert compute_kelly_tier(trade_count=0, win_rate=0.0, base_kelly=0.15) == 0.15
