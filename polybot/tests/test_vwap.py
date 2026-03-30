import pytest
import numpy as np
from polybot.indicators.vwap import compute_vwap, compute_vwap_signal

def test_vwap_basic():
    assert compute_vwap(np.array([101.0,102.0,103.0]), np.array([99.0,98.0,97.0]),
                        np.array([100.0]*3), np.array([10.0]*3)) > 0

def test_vwap_signal_bullish_below_vwap():
    h = np.array([100.0]*10 + [90.0]*5)
    l = np.array([99.0]*10 + [89.0]*5)
    c = np.array([100.0]*10 + [89.5]*5)
    v = np.array([100.0]*15)
    assert compute_vwap_signal(h, l, c, v)["score"] > 0

def test_vwap_signal_bearish_above_vwap():
    h = np.array([100.0]*10 + [111.0]*5)
    l = np.array([99.0]*10 + [110.0]*5)
    c = np.array([100.0]*10 + [110.5]*5)
    v = np.array([100.0]*15)
    assert compute_vwap_signal(h, l, c, v)["score"] < 0

def test_vwap_insufficient_data():
    assert compute_vwap_signal(np.array([1.0]), np.array([1.0]), np.array([1.0]), np.array([1.0]))["score"] == 0.0
