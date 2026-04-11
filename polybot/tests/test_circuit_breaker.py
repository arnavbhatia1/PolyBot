"""Tests for drawdown-based circuit breaker."""

import pytest
from polybot.execution.circuit_breaker import CircuitBreaker


# ------------------------------------------------------------------
# Construction & defaults
# ------------------------------------------------------------------

class TestConstruction:
    def test_default_values(self):
        cb = CircuitBreaker()
        assert cb.peak_bankroll == 1000.0
        assert cb.current_bankroll == 1000.0
        assert cb.max_drawdown_pct == 0.15
        assert cb.min_multiplier == 0.25
        assert cb.kelly_multiplier == 1.0

    def test_custom_initial_bankroll(self):
        cb = CircuitBreaker(initial_bankroll=500.0)
        assert cb.peak_bankroll == 500.0
        assert cb.current_bankroll == 500.0
        assert cb.kelly_multiplier == 1.0

    def test_legacy_params_accepted(self):
        """Old-style params don't crash — just stored for streak alerts."""
        cb = CircuitBreaker(losses_to_reduce=5, wins_to_restore=3)
        assert cb.losses_to_reduce == 5
        assert cb.wins_to_restore == 3


# ------------------------------------------------------------------
# Drawdown calculation
# ------------------------------------------------------------------

class TestDrawdown:
    def test_no_drawdown_at_peak(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        assert cb.drawdown_pct == 0.0

    def test_drawdown_after_loss(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        assert cb.drawdown_pct == pytest.approx(0.10)

    def test_drawdown_at_max(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15)
        cb.update_bankroll(850.0)
        assert cb.drawdown_pct == pytest.approx(0.15)

    def test_drawdown_beyond_max(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15)
        cb.update_bankroll(700.0)
        assert cb.drawdown_pct == pytest.approx(0.30)

    def test_drawdown_zero_peak(self):
        """Edge case: peak is 0 (shouldn't happen, but don't divide by zero)."""
        cb = CircuitBreaker(initial_bankroll=0.0)
        assert cb.drawdown_pct == 0.0

    def test_drawdown_recovers(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        assert cb.drawdown_pct == pytest.approx(0.10)
        cb.update_bankroll(950.0)
        assert cb.drawdown_pct == pytest.approx(0.05)

    def test_drawdown_resets_at_new_high(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        cb.update_bankroll(1050.0)
        assert cb.drawdown_pct == 0.0
        assert cb.peak_bankroll == 1050.0


# ------------------------------------------------------------------
# Kelly multiplier scaling
# ------------------------------------------------------------------

class TestKellyMultiplier:
    def test_full_kelly_at_peak(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        assert cb.kelly_multiplier == 1.0

    def test_full_kelly_above_peak(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(1100.0)
        assert cb.kelly_multiplier == 1.0

    def test_min_kelly_at_max_drawdown(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(850.0)  # exactly 15% drawdown
        assert cb.kelly_multiplier == pytest.approx(0.25)

    def test_min_kelly_beyond_max_drawdown(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(700.0)  # 30% drawdown — deeper than max
        assert cb.kelly_multiplier == pytest.approx(0.25)

    def test_linear_scaling_midpoint(self):
        """At half of max_drawdown (7.5%), multiplier should be halfway between 1.0 and 0.25."""
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(925.0)  # 7.5% drawdown = half of 15%
        expected = 1.0 - (1.0 - 0.25) * (0.075 / 0.15)  # 0.625
        assert cb.kelly_multiplier == pytest.approx(expected)

    def test_linear_scaling_quarter(self):
        """At 25% of max drawdown (3.75%), multiplier should be 25% of the way down."""
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(962.5)  # 3.75% drawdown = quarter of 15%
        expected = 1.0 - (1.0 - 0.25) * (0.0375 / 0.15)  # 0.8125
        assert cb.kelly_multiplier == pytest.approx(expected)

    def test_kelly_recovers_as_bankroll_climbs(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(850.0)
        assert cb.kelly_multiplier == pytest.approx(0.25)
        cb.update_bankroll(925.0)  # 7.5% drawdown
        assert cb.kelly_multiplier == pytest.approx(0.625)
        cb.update_bankroll(1000.0)
        assert cb.kelly_multiplier == 1.0

    def test_kelly_resets_at_new_high(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        assert cb.kelly_multiplier < 1.0
        cb.update_bankroll(1050.0)
        assert cb.kelly_multiplier == 1.0

    def test_custom_min_multiplier(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.20, min_multiplier=0.10)
        cb.update_bankroll(800.0)  # 20% drawdown
        assert cb.kelly_multiplier == pytest.approx(0.10)

    def test_never_halts_trading(self):
        """Even at extreme drawdown, kelly_multiplier > 0."""
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)
        cb.update_bankroll(100.0)  # 90% drawdown
        assert cb.kelly_multiplier == 0.25
        assert cb.kelly_multiplier > 0


# ------------------------------------------------------------------
# update_bankroll
# ------------------------------------------------------------------

class TestUpdateBankroll:
    def test_updates_current(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(950.0)
        assert cb.current_bankroll == 950.0

    def test_raises_peak(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(1100.0)
        assert cb.peak_bankroll == 1100.0

    def test_does_not_lower_peak(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(800.0)
        assert cb.peak_bankroll == 1000.0

    def test_sequence_of_updates(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        # Win some — still above initial, no drawdown
        cb.update_bankroll(1050.0)
        assert cb.peak_bankroll == 1050.0
        assert cb.kelly_multiplier == 1.0
        # Lose some — still above initial ($1000), no drawdown
        cb.update_bankroll(1000.0)
        assert cb.drawdown_pct == 0.0
        assert cb.kelly_multiplier == 1.0
        # Lose below initial — NOW drawdown kicks in
        cb.update_bankroll(950.0)
        dd = (1000.0 - 950.0) / 1000.0  # 5% below initial
        assert cb.drawdown_pct == pytest.approx(dd)
        # Lose more
        cb.update_bankroll(900.0)
        dd2 = (1000.0 - 900.0) / 1000.0  # 10% below initial
        assert cb.drawdown_pct == pytest.approx(dd2)
        # Recover past initial — drawdown gone
        cb.update_bankroll(1100.0)
        assert cb.peak_bankroll == 1100.0
        assert cb.drawdown_pct == 0.0
        assert cb.kelly_multiplier == 1.0


# ------------------------------------------------------------------
# Streak tracking (logging only, no sizing impact)
# ------------------------------------------------------------------

class TestStreakTracking:
    def test_record_win_resets_losses(self):
        cb = CircuitBreaker()
        cb.consecutive_losses = 5
        cb.record_win()
        assert cb.consecutive_losses == 0

    def test_record_loss_resets_wins(self):
        cb = CircuitBreaker()
        cb.consecutive_wins = 3
        cb.record_loss()
        assert cb.consecutive_wins == 0

    def test_record_win_increments(self):
        cb = CircuitBreaker()
        cb.record_win()
        assert cb.consecutive_wins == 1
        cb.record_win()
        assert cb.consecutive_wins == 2

    def test_record_loss_increments(self):
        cb = CircuitBreaker()
        cb.record_loss()
        assert cb.consecutive_losses == 1
        cb.record_loss()
        assert cb.consecutive_losses == 2

    def test_loss_streak_event(self):
        cb = CircuitBreaker(losses_to_reduce=3)
        assert cb.record_loss() is None
        assert cb.record_loss() is None
        event = cb.record_loss()
        assert event == "streak_losses"

    def test_loss_streak_continues_firing(self):
        cb = CircuitBreaker(losses_to_reduce=3)
        for _ in range(3):
            cb.record_loss()
        # 4th loss still fires because consecutive_losses >= threshold
        event = cb.record_loss()
        assert event == "streak_losses"

    def test_win_streak_event(self):
        cb = CircuitBreaker(wins_to_restore=2)
        assert cb.record_win() is None
        event = cb.record_win()
        assert event == "streak_wins"

    def test_win_streak_continues_firing(self):
        cb = CircuitBreaker(wins_to_restore=2)
        cb.record_win()
        cb.record_win()
        event = cb.record_win()
        assert event == "streak_wins"

    def test_streaks_dont_affect_kelly(self):
        """Kelly is ONLY driven by drawdown, never by streaks."""
        cb = CircuitBreaker(initial_bankroll=1000.0)
        # 5 consecutive losses shouldn't change kelly — bankroll unchanged
        for _ in range(5):
            cb.record_loss()
        assert cb.kelly_multiplier == 1.0  # No bankroll change = no drawdown

    def test_record_win_returns_none_before_threshold(self):
        cb = CircuitBreaker(wins_to_restore=3)
        assert cb.record_win() is None
        assert cb.record_win() is None

    def test_record_loss_returns_none_before_threshold(self):
        cb = CircuitBreaker(losses_to_reduce=3)
        assert cb.record_loss() is None
        assert cb.record_loss() is None


# ------------------------------------------------------------------
# Reset
# ------------------------------------------------------------------

class TestReset:
    def test_reset_clears_streaks(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.consecutive_losses = 5
        cb.consecutive_wins = 3
        cb.reset()
        assert cb.consecutive_losses == 0
        assert cb.consecutive_wins == 0

    def test_reset_preserves_bankroll(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        cb.reset()
        assert cb.current_bankroll == 900.0
        assert cb.peak_bankroll == 1000.0
        assert cb.drawdown_pct == pytest.approx(0.10)

    def test_reset_preserves_kelly_scaling(self):
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(925.0)  # 7.5% drawdown
        mult_before = cb.kelly_multiplier
        cb.reset()
        assert cb.kelly_multiplier == mult_before


# ------------------------------------------------------------------
# Integration scenario: full trading day
# ------------------------------------------------------------------

class TestTradingDayScenario:
    def test_gradual_drawdown_and_recovery(self):
        """Simulate a day: lose some, kelly scales down, win back, kelly recovers."""
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)

        # Start of day: full Kelly
        assert cb.kelly_multiplier == 1.0

        # Lose trade 1: bankroll drops to 950 (5% dd)
        cb.update_bankroll(950.0)
        cb.record_loss()
        assert cb.drawdown_pct == pytest.approx(0.05)
        mult_after_1 = cb.kelly_multiplier
        assert 0.25 < mult_after_1 < 1.0

        # Lose trade 2: bankroll drops to 900 (10% dd)
        cb.update_bankroll(900.0)
        cb.record_loss()
        assert cb.drawdown_pct == pytest.approx(0.10)
        mult_after_2 = cb.kelly_multiplier
        assert mult_after_2 < mult_after_1  # Kelly decreased

        # Lose trade 3: bankroll drops to 860 (14% dd)
        cb.update_bankroll(860.0)
        event = cb.record_loss()
        assert event == "streak_losses"  # 3rd consecutive loss
        assert cb.drawdown_pct == pytest.approx(0.14)
        mult_after_3 = cb.kelly_multiplier
        assert mult_after_3 < mult_after_2

        # Win trade 4: bankroll recovers to 910 (9% dd)
        cb.update_bankroll(910.0)
        cb.record_win()
        assert cb.drawdown_pct == pytest.approx(0.09)
        mult_after_4 = cb.kelly_multiplier
        assert mult_after_4 > mult_after_3  # Kelly recovered

        # Win trade 5: new high at 1020
        cb.update_bankroll(1020.0)
        cb.record_win()
        assert cb.drawdown_pct == 0.0
        assert cb.kelly_multiplier == 1.0
        assert cb.peak_bankroll == 1020.0

    def test_deep_drawdown_bottoms_at_min(self):
        """Even heavy losses don't kill trading — just reduce size."""
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)

        # Catastrophic day: 25% loss
        cb.update_bankroll(750.0)
        assert cb.kelly_multiplier == 0.25  # Bottomed at min
        assert cb.kelly_multiplier > 0  # Still trading!

        # Slow recovery
        cb.update_bankroll(800.0)
        assert cb.kelly_multiplier == 0.25  # Still beyond max_drawdown

        cb.update_bankroll(900.0)
        assert cb.drawdown_pct == pytest.approx(0.10)
        assert cb.kelly_multiplier > 0.25  # Started recovering

    def test_day_reset_keeps_drawdown(self):
        """New trading day resets streaks but NOT bankroll/drawdown."""
        cb = CircuitBreaker(initial_bankroll=1000.0, max_drawdown_pct=0.15, min_multiplier=0.25)
        cb.update_bankroll(900.0)
        cb.record_loss()
        cb.record_loss()
        cb.record_loss()

        mult_before_reset = cb.kelly_multiplier
        cb.reset()

        # Streaks gone
        assert cb.consecutive_losses == 0
        # But drawdown persists
        assert cb.kelly_multiplier == mult_before_reset
        assert cb.drawdown_pct == pytest.approx(0.10)
