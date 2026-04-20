import pytest
import time
from polybot.feeds.bybit_feed import BybitState, compute_perp_lead, compute_funding_signal


class TestPerpLead:
    def test_perp_above_spot(self):
        lead = compute_perp_lead(perp_price=73050.0, spot_price=73000.0)
        assert lead > 0

    def test_perp_below_spot(self):
        lead = compute_perp_lead(perp_price=72950.0, spot_price=73000.0)
        assert lead < 0

    def test_no_lead(self):
        lead = compute_perp_lead(perp_price=73000.0, spot_price=73000.0)
        assert lead == pytest.approx(0.0)

    def test_normalized_range(self):
        lead = compute_perp_lead(perp_price=73100.0, spot_price=73000.0)
        assert -1.0 <= lead <= 1.0

    def test_zero_prices(self):
        assert compute_perp_lead(0, 73000) == 0.0
        assert compute_perp_lead(73000, 0) == 0.0


class TestFundingSignal:
    def test_positive_funding_bearish(self):
        signal = compute_funding_signal(funding_rate=0.0005)
        assert signal < 0

    def test_negative_funding_bullish(self):
        signal = compute_funding_signal(funding_rate=-0.0003)
        assert signal > 0

    def test_neutral_funding(self):
        signal = compute_funding_signal(funding_rate=0.0001)
        assert abs(signal) < 0.3


class TestBybitState:
    def test_staleness_detection(self):
        state = BybitState()
        state.perp_price = 73050.0
        state.perp_updated = time.time()
        assert state.is_stale(spot_price=73000.0, spot_updated=time.time() - 5.0, threshold_usd=20.0)

    def test_not_stale_when_close(self):
        state = BybitState()
        state.perp_price = 73005.0
        state.perp_updated = time.time()
        assert not state.is_stale(spot_price=73000.0, spot_updated=time.time(), threshold_usd=20.0)

    def test_get_lead_delegates(self):
        state = BybitState()
        state.perp_price = 73050.0
        assert state.get_lead(73000.0) > 0

    def test_get_funding_signal_delegates(self):
        state = BybitState()
        state.funding_rate = 0.0005
        assert state.get_funding_signal() < 0
