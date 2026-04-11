import pytest
import time
from polybot.core.alpha_decay import AlphaDecayTracker

class TestAlphaDecay:
    def test_rising_edge_positive_rate(self):
        tracker = AlphaDecayTracker()
        now = time.time()
        tracker.add_observation(now - 20, 0.60)
        tracker.add_observation(now - 10, 0.70)
        tracker.add_observation(now, 0.80)
        rate = tracker.get_decay_rate()
        assert rate > 0

    def test_falling_edge_negative_rate(self):
        tracker = AlphaDecayTracker()
        now = time.time()
        tracker.add_observation(now - 20, 0.85)
        tracker.add_observation(now - 10, 0.75)
        tracker.add_observation(now, 0.65)
        rate = tracker.get_decay_rate()
        assert rate < 0

    def test_stable_edge_near_zero(self):
        tracker = AlphaDecayTracker()
        now = time.time()
        tracker.add_observation(now - 20, 0.75)
        tracker.add_observation(now - 10, 0.75)
        tracker.add_observation(now, 0.75)
        rate = tracker.get_decay_rate()
        assert abs(rate) < 0.001

    def test_should_enter_now_on_decay(self):
        tracker = AlphaDecayTracker()
        now = time.time()
        tracker.add_observation(now - 20, 0.90)
        tracker.add_observation(now - 10, 0.80)
        tracker.add_observation(now, 0.70)
        assert tracker.should_enter_now()

    def test_should_wait_on_growth(self):
        tracker = AlphaDecayTracker()
        now = time.time()
        tracker.add_observation(now - 20, 0.60)
        tracker.add_observation(now - 10, 0.70)
        tracker.add_observation(now, 0.80)
        assert not tracker.should_enter_now()

    def test_insufficient_data(self):
        tracker = AlphaDecayTracker()
        tracker.add_observation(time.time(), 0.75)
        assert tracker.get_decay_rate() == 0.0
        assert not tracker.should_enter_now()

    def test_reset_clears(self):
        tracker = AlphaDecayTracker()
        now = time.time()
        tracker.add_observation(now, 0.90)
        tracker.add_observation(now + 1, 0.80)
        tracker.reset()
        assert tracker.get_decay_rate() == 0.0
