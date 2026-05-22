import pytest
import time
from polybot.feeds.binance_trades import BinanceTradeAccumulator

class TestCVD:
    def test_net_buying_positive_cvd(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(price=73000, qty=1.0, is_buyer_maker=False, ts=now)
        acc.add_trade(price=73010, qty=0.5, is_buyer_maker=False, ts=now)
        acc.add_trade(price=72990, qty=0.3, is_buyer_maker=True, ts=now)
        cvd = acc.get_cvd(window_s=120)
        assert cvd > 0

    def test_net_selling_negative_cvd(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(price=73000, qty=0.2, is_buyer_maker=False, ts=now)
        acc.add_trade(price=72990, qty=2.0, is_buyer_maker=True, ts=now)
        cvd = acc.get_cvd(window_s=120)
        assert cvd < 0

    def test_expired_trades_excluded(self):
        acc = BinanceTradeAccumulator()
        old = time.time() - 300
        now = time.time()
        acc.add_trade(price=73000, qty=10.0, is_buyer_maker=False, ts=old)
        acc.add_trade(price=73000, qty=0.1, is_buyer_maker=True, ts=now)
        cvd = acc.get_cvd(window_s=120)
        assert cvd < 0

class TestTakerRatio:
    def test_all_buys(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 1.0, False, now)
        acc.add_trade(73000, 1.0, False, now)
        # Bypass the 5-trade noise floor: this test is asserting the math, not the gate.
        assert acc.get_taker_ratio(window_s=120, min_trades=2) == pytest.approx(1.0)

    def test_balanced(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 1.0, False, now)
        acc.add_trade(73000, 1.0, True, now)
        assert acc.get_taker_ratio(window_s=120, min_trades=2) == pytest.approx(0.5)

    def test_below_min_trades_returns_neutral(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 1.0, False, now)
        assert acc.get_taker_ratio(window_s=120) == pytest.approx(0.5)

