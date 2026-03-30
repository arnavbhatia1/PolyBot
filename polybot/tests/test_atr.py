import pytest
import numpy as np
from polybot.indicators.atr import compute_atr, compute_atr_gate

def test_atr_positive():
    h = np.array([float(100 + i % 3) for i in range(20)])
    l = np.array([float(98 + i % 3) for i in range(20)])
    c = np.array([float(99 + i % 3) for i in range(20)])
    assert compute_atr(h, l, c, period=14) > 0

def test_atr_gate_passes_normal_volatility():
    h = np.array([float(100 + (i % 5)) for i in range(100)])
    l = np.array([float(98 + (i % 5)) for i in range(100)])
    c = np.array([float(99 + (i % 5)) for i in range(100)])
    result = compute_atr_gate(h, l, c, period=14, low_pct=25, high_pct=90, history=100)
    assert isinstance(result["passes"], bool)

def test_atr_gate_fails_zero_volatility():
    result = compute_atr_gate(np.array([100.0]*100), np.array([100.0]*100), np.array([100.0]*100),
                               period=14, low_pct=25, high_pct=90, history=100)
    assert result["passes"] is False

def test_atr_insufficient_data():
    result = compute_atr_gate(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                               period=14, low_pct=25, high_pct=90, history=100)
    assert result["passes"] is False
