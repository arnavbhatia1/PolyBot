import pytest
import numpy as np
from polybot.indicators.macd import compute_macd, compute_macd_signal

def test_macd_returns_three_components():
    closes = np.array([float(i) for i in range(50)])
    m, s, h = compute_macd(closes, fast=12, slow=26, signal=9)
    assert isinstance(m, float) and isinstance(s, float) and isinstance(h, float)

def test_macd_signal_score_positive_when_bullish():
    assert compute_macd_signal(np.array([float(i) for i in range(50)]), fast=12, slow=26, signal_period=9)["score"] > 0

def test_macd_signal_score_negative_when_bearish():
    assert compute_macd_signal(np.array([float(50 - i) for i in range(50)]), fast=12, slow=26, signal_period=9)["score"] < 0

def test_macd_insufficient_data():
    assert compute_macd_signal(np.array([1.0, 2.0]), fast=12, slow=26, signal_period=9)["score"] == 0.0
