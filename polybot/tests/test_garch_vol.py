import pytest
import numpy as np
from polybot.core.garch_vol import GarchPredictor


class TestGarchPredictor:
    def test_insufficient_data(self):
        g = GarchPredictor()
        assert g.forecast_5min_vol(np.array([0.01, 0.02])) == 0.0

    def test_forecast_positive(self):
        g = GarchPredictor()
        returns = np.random.normal(0, 0.001, 100)  # typical 1-min BTC returns
        vol = g.forecast_5min_vol(returns)
        assert vol > 0

    def test_higher_recent_vol_higher_forecast(self):
        g = GarchPredictor()
        calm = np.random.normal(0, 0.0005, 100)
        wild = np.random.normal(0, 0.005, 100)
        assert g.forecast_5min_vol(wild) > g.forecast_5min_vol(calm)

    def test_vol_ratio_neutral_no_iv(self):
        g = GarchPredictor()
        returns = np.random.normal(0, 0.001, 50)
        assert g.compute_vol_ratio(returns, 0.0) == 1.0

    def test_sizing_adjustment_range(self):
        g = GarchPredictor()
        returns = np.random.normal(0, 0.001, 50)
        adj = g.compute_sizing_adjustment(returns, 0.80)
        assert 0.7 <= adj <= 1.3

    def test_vol_ratio_clamped(self):
        g = GarchPredictor()
        returns = np.random.normal(0, 0.01, 50)  # very high vol
        ratio = g.compute_vol_ratio(returns, 0.10)  # very low IV
        assert ratio <= 2.0  # clamped
