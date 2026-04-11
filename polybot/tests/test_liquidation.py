import pytest
from polybot.core.liquidation import compute_liquidation_pressure, LiquidationTracker


class TestLiquidationPressure:
    def test_oi_drop_with_price_drop_is_bearish(self):
        pressure = compute_liquidation_pressure(
            oi_current=500_000_000, oi_previous=550_000_000,
            price_current=73000, price_previous=73200)
        assert pressure < 0

    def test_oi_drop_with_price_rise_is_bullish(self):
        pressure = compute_liquidation_pressure(
            oi_current=500_000_000, oi_previous=550_000_000,
            price_current=73200, price_previous=73000)
        assert pressure > 0

    def test_stable_oi_is_neutral(self):
        pressure = compute_liquidation_pressure(
            oi_current=500_000_000, oi_previous=500_000_000,
            price_current=73000, price_previous=73000)
        assert abs(pressure) < 0.1

    def test_oi_increase_is_neutral(self):
        pressure = compute_liquidation_pressure(
            oi_current=550_000_000, oi_previous=500_000_000,
            price_current=73000, price_previous=73100)
        assert pressure == 0.0

    def test_clamped_range(self):
        pressure = compute_liquidation_pressure(
            oi_current=100_000_000, oi_previous=600_000_000,
            price_current=70000, price_previous=73000)
        assert -1.0 <= pressure <= 1.0

    def test_tracker_update_and_get(self):
        tracker = LiquidationTracker()
        tracker.update(oi=550_000_000, price=73200, ts=1000)
        tracker.update(oi=500_000_000, price=73000, ts=1060)
        pressure = tracker.get_pressure()
        assert pressure < 0  # OI dropped + price dropped = bearish
