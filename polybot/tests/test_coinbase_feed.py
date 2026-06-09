from polybot.feeds.coinbase_feed import CoinbaseFeed, CoinbaseState


class TestCoinbaseState:
    def test_initial_state(self):
        s = CoinbaseState()
        assert s.price == 0.0
        assert s.age_seconds == float("inf")

    def test_with_data(self):
        import time
        s = CoinbaseState(price=71000.0, updated_at=time.time(), best_bid=70999, best_ask=71001)
        assert s.price == 71000.0
        assert s.age_seconds < 1.0
        assert s.best_ask - s.best_bid == 2.0


class TestCoinbaseFeed:
    def test_default_url(self):
        f = CoinbaseFeed()
        assert "coinbase" in f.ws_url
        assert f.product_id == "BTC-USD"

    def test_handle_ticker(self):
        f = CoinbaseFeed()
        f._handle_message({
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "71500.25",
            "best_bid": "71500.00",
            "best_ask": "71500.50",
        })
        assert f.state.price == 71500.25
        assert f.state.best_bid == 71500.0
        assert f.state.best_ask == 71500.5

    def test_drops_non_finite_price(self):
        # float("NaN")/("Infinity") parse successfully; they must not reach
        # state.price (an inf would flow into L1's z = (btc-strike)/vol).
        for bad in ("NaN", "Infinity", "-Infinity"):
            f = CoinbaseFeed()
            f._handle_message({"type": "ticker", "product_id": "BTC-USD", "price": bad})
            assert f.state.price == 0.0

    def test_covers_requires_contiguous_window(self):
        """A reconnect clears the trade buffer, so window reads must report cold
        (covers False) until the buffer spans the window again."""
        import time
        f = CoinbaseFeed()
        assert f.covers(60.0) is False           # never connected
        f._window_start = time.time() - 30.0
        assert f.covers(60.0) is False           # reconnected 30s ago
        f._window_start = time.time() - 61.0
        assert f.covers(60.0) is True

    def test_cvd_acceleration_zero_until_window_covered(self):
        """Post-reconnect, a truncated baseline would overstate acceleration —
        the gate stays idle (0.0) until the 15+45s window is spanned."""
        import time
        f = CoinbaseFeed()
        now = time.time()
        f._window_start = now - 20.0             # only 20s of contiguous buffer
        for i in range(15):
            f._trades.append((now - i * 0.5, 1.0))
        assert f.get_cvd_acceleration() == 0.0
        f._window_start = now - 61.0
        assert f.get_cvd_acceleration() != 0.0

    def test_ignores_wrong_product(self):
        f = CoinbaseFeed()
        f._handle_message({"type": "ticker", "product_id": "ETH-USD", "price": "3000"})
        assert f.state.price == 0.0

    def test_ignores_non_ticker(self):
        f = CoinbaseFeed()
        f._handle_message({"type": "subscriptions", "channels": []})
        assert f.state.price == 0.0
