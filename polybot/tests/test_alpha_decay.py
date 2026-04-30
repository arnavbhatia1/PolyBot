import time
from polybot.core.alpha_decay import AlphaDecayTracker


def test_rising_edge_positive_rate():
    tracker = AlphaDecayTracker()
    now = time.time()
    tracker.add_observation(now - 20, 0.60)
    tracker.add_observation(now - 10, 0.70)
    tracker.add_observation(now, 0.80)
    assert tracker.get_decay_rate() > 0


def test_falling_edge_negative_rate():
    tracker = AlphaDecayTracker()
    now = time.time()
    tracker.add_observation(now - 20, 0.85)
    tracker.add_observation(now - 10, 0.75)
    tracker.add_observation(now, 0.65)
    assert tracker.get_decay_rate() < 0


def test_stable_edge_near_zero():
    tracker = AlphaDecayTracker()
    now = time.time()
    for _ in range(3):
        tracker.add_observation(now, 0.75)
        now += 10
    assert abs(tracker.get_decay_rate()) < 0.001


def test_insufficient_data_returns_zero():
    tracker = AlphaDecayTracker()
    tracker.add_observation(time.time(), 0.75)
    assert tracker.get_decay_rate() == 0.0


def test_reset_clears():
    tracker = AlphaDecayTracker()
    now = time.time()
    tracker.add_observation(now, 0.90)
    tracker.add_observation(now + 1, 0.80)
    tracker.reset()
    assert tracker.get_decay_rate() == 0.0
