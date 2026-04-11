import pytest
import time
from polybot.core.binance_trades import BinanceTradeAccumulator

class TestCVDAcceleration:
    def test_accelerating_buying(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        for i in range(10):
            acc.add_trade(73000, 0.1, False, now - 50 + i)
        for i in range(10):
            acc.add_trade(73000, 0.5, False, now - 10 + i)
        accel = acc.get_cvd_acceleration(recent_s=15, baseline_s=45)
        assert accel > 0

    def test_decelerating_buying(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        for i in range(10):
            acc.add_trade(73000, 0.5, False, now - 50 + i)
        for i in range(10):
            acc.add_trade(73000, 0.1, False, now - 10 + i)
        accel = acc.get_cvd_acceleration(recent_s=15, baseline_s=45)
        assert accel < 0

    def test_no_trades_returns_zero(self):
        acc = BinanceTradeAccumulator()
        assert acc.get_cvd_acceleration() == 0.0
