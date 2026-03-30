import pytest
import numpy as np
from polybot.indicators.ema import compute_ema, compute_ema_signal

def test_ema_length_matches_input():
    closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    assert len(compute_ema(closes, period=3)) == len(closes)

def test_ema_responds_to_recent_prices():
    closes = np.array([10.0] * 10 + [20.0])
    assert compute_ema(closes, period=5)[-1] > 10.0

def test_ema_fast_above_slow_bullish():
    closes = np.array([float(i) for i in range(1, 30)])
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "bullish"

def test_ema_fast_below_slow_bearish():
    closes = np.array([float(30 - i) for i in range(30)])
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "bearish"

def test_ema_chop_detection():
    closes = np.array([100.0] * 30)
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "chop"

def test_ema_needs_enough_data():
    closes = np.array([1.0, 2.0])
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "insufficient_data"
