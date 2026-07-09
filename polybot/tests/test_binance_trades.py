import pytest
import time
from polybot.feeds.binance_trades import BinanceTradeAccumulator, BinanceTradesFeed


class TestNonFiniteGuard:
    def test_drops_non_finite_aggtrade(self):
        # float("NaN")/("Infinity") parse fine; they must not poison the CVD.
        acc = BinanceTradeAccumulator()
        feed = BinanceTradesFeed(acc)
        for bad in ("NaN", "Infinity"):
            feed._handle_message({"e": "aggTrade", "p": bad, "q": "1.0", "m": False})
            feed._handle_message({"e": "aggTrade", "p": "73000", "q": bad, "m": False})
        assert len(acc._trades) == 0


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
        # Bypass the default 20-trade noise floor: this test asserts the math, not the gate.
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



class TestCovers:
    def test_covers_false_until_window_spans(self):
        acc = BinanceTradeAccumulator()
        assert acc.covers(10.0) is False           # no trades yet
        acc.add_trade(60000.0, 1.0, False, time.time())
        assert acc.covers(10.0) is False           # window started just now

    def test_covers_true_once_spanning(self, monkeypatch):
        acc = BinanceTradeAccumulator()
        acc.add_trade(60000.0, 1.0, False, time.time())
        acc._window_start = time.time() - 31.0     # window has run 31s
        assert acc.covers(30.0) is True
        assert acc.covers(60.0) is False

    def test_clear_resets_coverage(self):
        """A reconnect clears the accumulator — a truncated window must read as
        not-covering so consumers stamp None, not an understated CVD."""
        acc = BinanceTradeAccumulator()
        acc.add_trade(60000.0, 1.0, False, time.time())
        acc._window_start = time.time() - 31.0
        assert acc.covers(30.0) is True
        acc.clear()
        assert acc.covers(30.0) is False
