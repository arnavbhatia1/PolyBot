import pytest
import numpy as np
from polybot.indicators.obv import compute_obv, compute_obv_signal

def test_obv_increases_on_up_close():
    obv = compute_obv(np.array([10.0, 11.0, 12.0, 13.0, 14.0]), np.array([100.0]*5))
    assert obv[-1] > obv[0]

def test_obv_decreases_on_down_close():
    obv = compute_obv(np.array([14.0, 13.0, 12.0, 11.0, 10.0]), np.array([100.0]*5))
    assert obv[-1] < obv[0]

def test_obv_signal_bullish_when_price_up_obv_up():
    assert compute_obv_signal(np.array([float(10+i) for i in range(20)]), np.array([100.0]*20), slope_period=5)["score"] > 0

def test_obv_signal_bearish_when_price_down_obv_down():
    assert compute_obv_signal(np.array([float(30-i) for i in range(20)]), np.array([100.0]*20), slope_period=5)["score"] < 0

def test_obv_insufficient_data():
    assert compute_obv_signal(np.array([1.0]), np.array([1.0]), slope_period=5)["score"] == 0.0
