"""Integration tests for the L2-L5 signal layers."""
import numpy as np
from polybot.core.signal_engine import SignalEngine


def _closes() -> np.ndarray:
    return np.array([73000.0 + i for i in range(25)])


def test_bullish_spot_flow_increases_prob():
    engine = SignalEngine(spot_flow_weight=0.04)
    base = engine.compute_probability(
        btc_price=73020, strike_price=73000, seconds_remaining=120,
        atr=25, closes=_closes(), spot_flow_signal=0.0,
    )
    bullish = engine.compute_probability(
        btc_price=73020, strike_price=73000, seconds_remaining=120,
        atr=25, closes=_closes(), spot_flow_signal=0.8,
    )
    assert bullish > base


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


def test_prev_margin_carries_momentum():
    engine = SignalEngine(prev_margin_weight=0.02)
    base = engine.compute_probability(
        btc_price=73020, strike_price=73000, seconds_remaining=120,
        atr=25, closes=_closes(), prev_resolution_margin=0.0,
    )
    carry = engine.compute_probability(
        btc_price=73020, strike_price=73000, seconds_remaining=120,
        atr=25, closes=_closes(), prev_resolution_margin=80.0,
    )
    assert carry > base


def test_evaluate_runs_end_to_end():
    engine = SignalEngine()
    indicators = {"atr": {"atr": 25, "passes": True, "reason": "ok"}}
    signal = engine.evaluate(
        indicators, has_position=False, in_entry_window=True,
        btc_price=73050, strike_price=73000,
        seconds_remaining=120, market_price_up=0.55,
        market_price_down=0.45, closes=_closes(),
    )
    assert signal.action in ("BUY_YES", "BUY_NO", "SKIP")
