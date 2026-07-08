"""Integration tests for the L1 signal engine."""
import numpy as np
from polybot.core.signal_engine import SignalEngine


def _closes() -> np.ndarray:
    return np.array([73000.0 + i for i in range(25)])


def test_higher_atr_widens_probability():
    """Higher ATR (more vol) produces probability closer to 0.5 (wider uncertainty)."""
    engine = SignalEngine()
    tight = engine.compute_probability(
        btc_price=73050, strike_price=73000, seconds_remaining=120,
        atr=10, closes=_closes(),
    )
    wide = engine.compute_probability(
        btc_price=73050, strike_price=73000, seconds_remaining=120,
        atr=100, closes=_closes(),
    )
    assert abs(wide - 0.5) < abs(tight - 0.5)
