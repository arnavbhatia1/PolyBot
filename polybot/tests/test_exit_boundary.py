import pytest
from polybot.core.exit_boundary import ExitBoundary


class TestExitBoundary:
    def test_early_window_more_patient(self):
        """With lots of time left, threshold should be more negative (patient)."""
        eb = ExitBoundary()
        early = eb.compute_exit_threshold(240, entry_price=0.60)
        late = eb.compute_exit_threshold(30, entry_price=0.60)
        assert early < late  # early is more negative = more patient

    def test_at_expiry_tight(self):
        """Near expiry, threshold should be close to -fee_cost."""
        eb = ExitBoundary()
        threshold = eb.compute_exit_threshold(5, entry_price=0.60)
        assert threshold > -0.05  # very tight near expiry

    def test_midway_moderate(self):
        eb = ExitBoundary()
        threshold = eb.compute_exit_threshold(150, entry_price=0.60)
        assert -0.15 < threshold < -0.03

    def test_should_exit_model_disagrees(self):
        eb = ExitBoundary()
        # Market at 40 cents, model says only 5% chance of winning.
        # Exit value (~37 cents) >> hold value (~5 cents + tiny time value)
        should, boundary = eb.should_exit(
            60, market_price=0.40, entry_price=0.60, model_prob=0.05)
        assert should is True

    def test_should_hold_model_agrees(self):
        eb = ExitBoundary()
        # Market at 40 cents, model says 60% chance of winning.
        # Hold value (60 cents) >> exit value (~37 cents)
        should, boundary = eb.should_exit(
            60, market_price=0.40, entry_price=0.60, model_prob=0.60)
        assert should is False

    def test_should_hold_good_position(self):
        eb = ExitBoundary()
        should, boundary = eb.should_exit(120, market_price=0.75, entry_price=0.60)
        assert should is False  # winning position, hold

    def test_threshold_always_negative(self):
        eb = ExitBoundary()
        for secs in [10, 30, 60, 120, 180, 240, 300]:
            t = eb.compute_exit_threshold(secs, entry_price=0.60)
            assert t < 0  # always negative (some adverse edge tolerated)
