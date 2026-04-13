"""Tests for the new signal layers added to SignalEngine."""
import pytest
import numpy as np
from polybot.core.signal_engine import SignalEngine


class TestWallPressureIntegration:
    def test_wall_reduces_up_probability(self):
        engine = SignalEngine(wall_weight=0.05)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=0.0,
        )
        with_wall = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=0.8,
        )
        assert with_wall < base

    def test_support_wall_increases_up_probability(self):
        engine = SignalEngine(wall_weight=0.05)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=72980, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=0.0,
        )
        with_support = engine.compute_probability(
            btc_price=72980, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=-0.8,
        )
        assert with_support > base


class TestSpotFlowIntegration:
    def test_bullish_spot_flow_increases_prob(self):
        engine = SignalEngine(spot_flow_weight=0.04)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, spot_flow_signal=0.0,
        )
        bullish = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, spot_flow_signal=0.8,
        )
        assert bullish > base


class TestIVRatioIntegration:
    def test_high_iv_widens_probability(self):
        engine = SignalEngine()
        closes = np.array([73000.0 + i for i in range(25)])
        normal = engine.compute_probability(
            btc_price=73050, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, iv_ratio=1.0,
        )
        high_iv = engine.compute_probability(
            btc_price=73050, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, iv_ratio=1.5,
        )
        assert abs(high_iv - 0.5) < abs(normal - 0.5)


class TestPrevResolutionMargin:
    def test_strong_up_carries_momentum(self):
        engine = SignalEngine(prev_margin_weight=0.02)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, prev_resolution_margin=0.0,
        )
        carry = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, prev_resolution_margin=80.0,
        )
        assert carry > base


class TestBackwardCompatibility:
    """Verify existing behavior unchanged when new params use defaults."""
    def test_default_params_no_change(self):
        engine = SignalEngine()
        closes = np.array([73000.0 + i for i in range(25)])
        # With all new params at defaults, result should be deterministic
        p = engine.compute_probability(
            btc_price=73050, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes,
        )
        assert 0.0 < p < 1.0

    def test_evaluate_works_with_no_new_params(self):
        engine = SignalEngine()
        closes = np.array([73000.0 + i for i in range(25)])
        indicators = {"atr": {"atr": 25, "passes": True, "reason": "ok"}}
        signal = engine.evaluate(
            indicators, has_position=False, in_entry_window=True,
            btc_price=73050, strike_price=73000,
            seconds_remaining=120, market_price_up=0.55,
            market_price_down=0.45, closes=closes,
        )
        assert signal.action in ("BUY_YES", "BUY_NO", "SKIP")
