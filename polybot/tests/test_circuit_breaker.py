"""Tests for the tiered-floor circuit breaker."""

import pytest
from polybot.execution.circuit_breaker import CircuitBreaker


# ------------------------------------------------------------------
# Construction & defaults
# ------------------------------------------------------------------

class TestConstruction:
    def test_default_values(self):
        cb = CircuitBreaker(initial_bankroll=100.0)
        assert cb.current_bankroll == 100.0
        assert cb.peak_bankroll == 100.0
        assert cb.locked_tier == 100.0
        assert cb.floor == pytest.approx(85.0)
        assert cb.min_multiplier == 0.40
        assert cb.kelly_multiplier == 1.0

    def test_streak_alert_params_accepted(self):
        """losses_to_reduce / wins_to_restore are stored for streak alerts (not sizing)."""
        cb = CircuitBreaker(losses_to_reduce=5, wins_to_restore=3)
        assert cb.losses_to_reduce == 5
        assert cb.wins_to_restore == 3

    def test_tier_locking_on_init(self):
        """locked_tier is the highest tier at or below initial bankroll."""
        assert CircuitBreaker(initial_bankroll=100.0).locked_tier == 100.0
        assert CircuitBreaker(initial_bankroll=149.0).locked_tier == 100.0
        assert CircuitBreaker(initial_bankroll=150.0).locked_tier == 150.0
        assert CircuitBreaker(initial_bankroll=1000.0).locked_tier == 1000.0
        assert CircuitBreaker(initial_bankroll=1200.0).locked_tier == 1000.0


# ------------------------------------------------------------------
# Drawdown calculation
# ------------------------------------------------------------------

class TestDrawdown:
    def test_drawdown_after_loss(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        assert cb.drawdown_pct == pytest.approx(0.10)

    def test_drawdown_at_floor(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(850.0)
        assert cb.drawdown_pct == pytest.approx(0.15)

    def test_drawdown_beyond_floor(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(700.0)
        assert cb.drawdown_pct == pytest.approx(0.30)

    def test_drawdown_no_divide_by_zero(self):
        """locked_tier is always >= first tier ($100), so division is always safe."""
        cb = CircuitBreaker(initial_bankroll=0.0)
        # locked_tier=$100 (first tier), bankroll=0 → 100% drawdown, no crash
        assert cb.drawdown_pct == pytest.approx(1.0)
        assert cb.kelly_multiplier == pytest.approx(cb.min_multiplier)

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

    def test_min_kelly_at_or_below_floor(self):
        """Kelly bottoms at min_multiplier once bankroll falls to the floor — no halt."""
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)
        cb.update_bankroll(850.0)  # exactly 15% drawdown
        assert cb.kelly_multiplier == pytest.approx(0.25)
        cb.update_bankroll(700.0)  # 30% drawdown — still floored, never halts
        assert cb.kelly_multiplier == pytest.approx(0.25)
        assert cb.kelly_multiplier > 0

    def test_kelly_recovers_as_bankroll_climbs(self):
        import math
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25, floor_pct=0.85)
        # At floor: clamped to min
        cb.update_bankroll(cb.floor)
        assert cb.kelly_multiplier == pytest.approx(0.25)
        # Midway: concave sqrt curve
        midpoint = (cb.floor + cb.locked_tier) / 2.0
        cb.update_bankroll(midpoint)
        expected_mid = 0.25 + (1.0 - 0.25) * math.sqrt(0.5)
        assert cb.kelly_multiplier == pytest.approx(expected_mid)
        # At tier: full Kelly
        cb.update_bankroll(cb.locked_tier)
        assert cb.kelly_multiplier == 1.0

    def test_kelly_resets_at_new_high(self):
        cb = CircuitBreaker(initial_bankroll=1000.0)
        cb.update_bankroll(900.0)
        assert cb.kelly_multiplier < 1.0
        cb.update_bankroll(1050.0)
        assert cb.kelly_multiplier == 1.0


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
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)
        # locked_tier=1000, floor=850
        cb.update_bankroll(1050.0)   # still tier=1000 (1050 < 1500)
        assert cb.peak_bankroll == 1050.0
        assert cb.kelly_multiplier == 1.0
        # Drop to exactly the tier — still full Kelly
        cb.update_bankroll(1000.0)
        assert cb.drawdown_pct == 0.0
        assert cb.kelly_multiplier == 1.0
        # Drop below tier into scaling zone
        cb.update_bankroll(950.0)
        assert cb.drawdown_pct == pytest.approx((1000.0 - 950.0) / 1000.0)
        # Drop more
        cb.update_bankroll(900.0)
        assert cb.drawdown_pct == pytest.approx((1000.0 - 900.0) / 1000.0)
        # Recover past tier — drawdown gone
        cb.update_bankroll(1100.0)
        assert cb.peak_bankroll == 1100.0
        assert cb.drawdown_pct == 0.0
        assert cb.kelly_multiplier == 1.0


# ------------------------------------------------------------------
# Tier ratcheting
# ------------------------------------------------------------------

class TestTierRatcheting:
    def test_floor_ratchets_up_when_tier_crossed(self):
        cb = CircuitBreaker(initial_bankroll=100.0)
        assert cb.locked_tier == 100.0
        assert cb.floor == pytest.approx(85.0)
        cb.update_bankroll(155.0)    # crosses $150 tier
        assert cb.locked_tier == 150.0
        assert cb.floor == pytest.approx(127.5)

    def test_floor_never_goes_down(self):
        cb = CircuitBreaker(initial_bankroll=100.0)
        cb.update_bankroll(205.0)    # crosses $200 tier → floor=$170
        assert cb.floor == pytest.approx(170.0)
        cb.update_bankroll(140.0)    # drops back well below $200
        assert cb.locked_tier == 200.0   # tier locked in
        assert cb.floor == pytest.approx(170.0)   # floor unchanged

    def test_kelly_above_locked_tier_is_full(self):
        cb = CircuitBreaker(initial_bankroll=100.0)
        cb.update_bankroll(130.0)    # above $100 tier, below $150
        assert cb.kelly_multiplier == 1.0

    def test_kelly_at_floor_is_min(self):
        cb = CircuitBreaker(initial_bankroll=200.0, floor_pct=0.85, min_multiplier=0.40)
        # locked_tier=200, floor=170
        cb.update_bankroll(170.0)
        assert cb.kelly_multiplier == pytest.approx(0.40)

    def test_kelly_below_floor_stays_at_min(self):
        cb = CircuitBreaker(initial_bankroll=200.0, floor_pct=0.85, min_multiplier=0.40)
        cb.update_bankroll(150.0)    # below floor of 170
        assert cb.kelly_multiplier == pytest.approx(0.40)

    def test_kelly_midpoint_between_floor_and_tier(self):
        import math
        cb = CircuitBreaker(initial_bankroll=200.0, floor_pct=0.85, min_multiplier=0.40)
        # tier=200, floor=170, midpoint=185 -> concave sqrt(0.5) ≈ 0.707
        cb.update_bankroll(185.0)
        expected = 0.40 + (1.0 - 0.40) * math.sqrt(0.5)
        assert cb.kelly_multiplier == pytest.approx(expected)

    def test_multiple_tier_crossings(self):
        cb = CircuitBreaker(initial_bankroll=100.0)
        cb.update_bankroll(155.0)
        assert cb.locked_tier == 150.0
        cb.update_bankroll(205.0)
        assert cb.locked_tier == 200.0
        cb.update_bankroll(310.0)
        assert cb.locked_tier == 300.0
        assert cb.floor == pytest.approx(255.0)


# ------------------------------------------------------------------
# Restart restore (restore_from_peak)
# ------------------------------------------------------------------

class TestRestoreFromPeak:
    def test_locks_tier_from_peak_keeps_current_balance(self):
        """Restart at $700 with a $1000 historical peak: floor stays at the $1000
        tier ($850) and Kelly reflects the live $700 drawdown, not a reset to 1.0."""
        cb = CircuitBreaker(initial_bankroll=700.0)
        # Fresh boot would lock the tier at $700's tier ($600) — too low.
        assert cb.locked_tier == 600.0
        cb.restore_from_peak(1000.0, 700.0)
        assert cb.locked_tier == 1000.0
        assert cb.floor == pytest.approx(850.0)
        assert cb.current_bankroll == 700.0
        assert cb.peak_bankroll == 1000.0
        # $700 is below the $850 floor → Kelly bottoms at min_multiplier.
        assert cb.kelly_multiplier == pytest.approx(cb.min_multiplier)

    def test_current_above_floor_uses_concave_scaling(self):
        import math
        cb = CircuitBreaker(initial_bankroll=900.0)
        cb.restore_from_peak(1000.0, 900.0)
        assert cb.locked_tier == 1000.0
        assert cb.current_bankroll == 900.0
        # 900 sits between floor (850) and tier (1000): concave sqrt interpolation.
        ratio = (900.0 - 850.0) / (1000.0 - 850.0)
        expected = cb.min_multiplier + (1.0 - cb.min_multiplier) * math.sqrt(ratio)
        assert cb.kelly_multiplier == pytest.approx(expected)


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
        assert cb.record_loss() == "streak_losses"
        # Continues firing past threshold (consecutive_losses still ≥ threshold)
        assert cb.record_loss() == "streak_losses"

    def test_win_streak_event(self):
        cb = CircuitBreaker(wins_to_restore=2)
        assert cb.record_win() is None
        assert cb.record_win() == "streak_wins"

    def test_streaks_dont_affect_kelly(self):
        """Kelly is ONLY driven by drawdown, never by streaks."""
        cb = CircuitBreaker(initial_bankroll=1000.0)
        for _ in range(5):
            cb.record_loss()
        assert cb.kelly_multiplier == 1.0  # No bankroll change = no drawdown


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
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)
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
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)

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
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)

        # Catastrophic day: 25% loss
        cb.update_bankroll(750.0)
        assert cb.kelly_multiplier == 0.25  # Bottomed at min
        assert cb.kelly_multiplier > 0  # Still trading!

        # Slow recovery
        cb.update_bankroll(800.0)
        assert cb.kelly_multiplier == 0.25  # Still below the floor

        cb.update_bankroll(900.0)
        assert cb.drawdown_pct == pytest.approx(0.10)
        assert cb.kelly_multiplier > 0.25  # Started recovering

    def test_day_reset_keeps_drawdown(self):
        """New trading day resets streaks but NOT bankroll/drawdown."""
        cb = CircuitBreaker(initial_bankroll=1000.0, min_multiplier=0.25)
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
