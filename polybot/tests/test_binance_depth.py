import pytest
from polybot.core.binance_depth import compute_spot_imbalance, compute_wall_pressure, compute_depth_usd

class TestSpotImbalance:
    def test_balanced_book(self):
        bids = [["73000.00", "1.0"], ["72999.00", "1.0"]]
        asks = [["73001.00", "1.0"], ["73002.00", "1.0"]]
        assert compute_spot_imbalance(bids, asks) == pytest.approx(0.0, abs=0.01)

    def test_bid_heavy(self):
        bids = [["73000.00", "5.0"], ["72999.00", "5.0"]]
        asks = [["73001.00", "1.0"]]
        result = compute_spot_imbalance(bids, asks)
        assert result > 0.5

    def test_ask_heavy(self):
        bids = [["73000.00", "1.0"]]
        asks = [["73001.00", "5.0"], ["73002.00", "5.0"]]
        result = compute_spot_imbalance(bids, asks)
        assert result < -0.5

    def test_empty_book(self):
        assert compute_spot_imbalance([], []) == 0.0

class TestWallPressure:
    def test_sell_wall_above_strike(self):
        asks = [["73025.00", "50.0"], ["73030.00", "20.0"], ["73050.00", "5.0"]]
        bids = [["73015.00", "1.0"], ["73010.00", "1.0"]]
        result = compute_wall_pressure(bids, asks, strike=73000.0, btc_price=73020.0, pct_range=0.001)
        assert result > 0

    def test_no_wall(self):
        asks = [["73025.00", "1.0"]]
        bids = [["73015.00", "1.0"]]
        result = compute_wall_pressure(bids, asks, strike=73000.0, btc_price=73020.0, pct_range=0.001)
        assert abs(result) < 1.0

    def test_support_wall_below_strike(self):
        bids = [["72975.00", "50.0"], ["72960.00", "20.0"]]
        asks = [["72985.00", "1.0"]]
        result = compute_wall_pressure(bids, asks, strike=73000.0, btc_price=72980.0, pct_range=0.001)
        assert result < 0

class TestDepthUsd:
    def test_computes_total_depth(self):
        bids = [["73000.00", "1.0"], ["72999.00", "2.0"]]
        asks = [["73001.00", "1.5"], ["73002.00", "0.5"]]
        result = compute_depth_usd(bids, asks, levels=2)
        assert result > 300000
