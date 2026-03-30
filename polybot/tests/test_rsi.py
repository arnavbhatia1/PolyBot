import pytest
import numpy as np
from polybot.indicators.rsi import compute_rsi, compute_rsi_signal

def test_rsi_range():
    closes = np.array([44.0, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
                       46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    assert 0 <= compute_rsi(closes, period=14) <= 100

def test_rsi_overbought():
    assert compute_rsi(np.array([float(i) for i in range(50)]), period=14) > 70

def test_rsi_oversold():
    assert compute_rsi(np.array([float(50 - i) for i in range(50)]), period=14) < 30

def test_rsi_signal_score_bearish_when_overbought():
    signal = compute_rsi_signal(np.array([float(i) for i in range(50)]), period=14, overbought=70, oversold=30)
    assert signal["score"] < 0

def test_rsi_signal_score_bullish_when_oversold():
    signal = compute_rsi_signal(np.array([float(50 - i) for i in range(50)]), period=14, overbought=70, oversold=30)
    assert signal["score"] > 0

def test_rsi_insufficient_data():
    assert compute_rsi_signal(np.array([1.0, 2.0]), period=14, overbought=70, oversold=30)["score"] == 0.0
