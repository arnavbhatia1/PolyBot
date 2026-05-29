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

    def test_ignores_wrong_product(self):
        f = CoinbaseFeed()
        f._handle_message({"type": "ticker", "product_id": "ETH-USD", "price": "3000"})
        assert f.state.price == 0.0

    def test_ignores_non_ticker(self):
        f = CoinbaseFeed()
        f._handle_message({"type": "subscriptions", "channels": []})
        assert f.state.price == 0.0
