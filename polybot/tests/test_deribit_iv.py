import pytest
from polybot.core.deribit_iv import compute_iv_ratio, IVState


class TestIVRatio:
    def test_iv_above_historical(self):
        ratio = compute_iv_ratio(current_iv=0.80, historical_iv=0.60)
        assert ratio > 1.0

    def test_iv_below_historical(self):
        ratio = compute_iv_ratio(current_iv=0.40, historical_iv=0.60)
        assert ratio < 1.0

    def test_equal_iv(self):
        ratio = compute_iv_ratio(current_iv=0.60, historical_iv=0.60)
        assert ratio == pytest.approx(1.0)

    def test_clamped_range(self):
        ratio = compute_iv_ratio(current_iv=2.0, historical_iv=0.30)
        assert ratio <= 3.0  # default cap raised from 2.0 to 3.0

    def test_custom_clamp_bounds(self):
        ratio = compute_iv_ratio(current_iv=2.0, historical_iv=0.30, iv_max=2.0)
        assert ratio <= 2.0

    def test_zero_historical(self):
        ratio = compute_iv_ratio(current_iv=0.50, historical_iv=0.0)
        assert ratio == 1.0


class TestIVState:
    def test_get_iv_ratio_with_atr(self):
        state = IVState()
        state.btc_iv = 0.80  # 80% annualized
        # ATR=25 on BTC ~73000 -> annualized ~ (25/73000)*sqrt(525600) ~ 0.248
        ratio = state.get_iv_ratio(atr=25.0, btc_price=73000.0)
        assert ratio > 1.0  # 0.80 / 0.248 > 1

    def test_returns_1_when_no_data(self):
        state = IVState()
        assert state.get_iv_ratio(atr=25.0, btc_price=73000.0) == 1.0
