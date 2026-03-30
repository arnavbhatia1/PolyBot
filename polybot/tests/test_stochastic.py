import pytest
import numpy as np
from polybot.indicators.stochastic import compute_stochastic, compute_stochastic_signal

def test_stochastic_range():
    h = np.array([float(50 + i % 5) for i in range(20)])
    l = np.array([float(45 + i % 5) for i in range(20)])
    c = np.array([float(47 + i % 5) for i in range(20)])
    k, d = compute_stochastic(h, l, c, k_period=14, d_smoothing=3)
    assert 0 <= k <= 100 and 0 <= d <= 100

def test_stochastic_overbought_signal():
    h = np.array([float(i) for i in range(50, 70)])
    l = np.array([float(i - 1) for i in range(50, 70)])
    c = np.array([float(i - 0.1) for i in range(50, 70)])
    assert compute_stochastic_signal(h, l, c, k_period=14, d_smoothing=3, overbought=80, oversold=20)["k"] > 80

def test_stochastic_signal_bearish_when_overbought():
    h = np.array([float(i) for i in range(50, 70)])
    l = np.array([float(i - 1) for i in range(50, 70)])
    c = np.array([float(i - 0.1) for i in range(50, 70)])
    assert compute_stochastic_signal(h, l, c, k_period=14, d_smoothing=3, overbought=80, oversold=20)["score"] < 0

def test_stochastic_insufficient_data():
    assert compute_stochastic_signal(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                                      k_period=14, d_smoothing=3, overbought=80, oversold=20)["score"] == 0.0
