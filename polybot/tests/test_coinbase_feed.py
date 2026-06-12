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

    def test_ignores_wrong_product(self):
        f = CoinbaseFeed()
        f._handle_message({"type": "ticker", "product_id": "ETH-USD", "price": "3000"})
        assert f.state.price == 0.0

    def test_ignores_non_ticker(self):
        f = CoinbaseFeed()
        f._handle_message({"type": "subscriptions", "channels": []})
        assert f.state.price == 0.0


class TestRealizedVol:
    def test_zero_below_three_samples(self):
        import time
        f = CoinbaseFeed()
        now = time.time()
        f._prices.extend([(now - 5, 71000.0), (now - 4, 71010.0)])
        assert f.realized_vol(60.0) == 0.0

    def test_flat_prices_zero_vol(self):
        import time
        f = CoinbaseFeed()
        now = time.time()
        f._prices.extend([(now - i, 71000.0) for i in range(10, 0, -1)])
        assert f.realized_vol(60.0) == 0.0

    def test_matches_sample_stdev_of_log_returns(self):
        import math
        import time
        f = CoinbaseFeed()
        now = time.time()
        prices = [71000.0, 71071.0, 70929.0, 71000.0]
        f._prices.extend([(now - (len(prices) - i), p) for i, p in enumerate(prices)])
        rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
        m = sum(rets) / len(rets)
        expected = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
        assert abs(f.realized_vol(60.0) - expected) < 1e-12

    def test_window_excludes_old_samples(self):
        import time
        f = CoinbaseFeed()
        now = time.time()
        f._prices.extend([(now - 120, 50000.0), (now - 115, 90000.0)])  # outside 60s
        f._prices.extend([(now - 3, 71000.0), (now - 2, 71000.0), (now - 1, 71000.0)])
        assert f.realized_vol(60.0) == 0.0  # only the flat in-window samples count

    def test_ticker_samples_prices_at_1s_buckets(self):
        f = CoinbaseFeed()
        msg = {"type": "ticker", "product_id": "BTC-USD", "price": "71500.25"}
        f._handle_message(msg)
        f._handle_message(msg)  # same second: not resampled
        assert len(f._prices) == 1
        f._last_price_sample -= 1.5  # pretend the last sample is older
        f._handle_message(msg)
        assert len(f._prices) == 2
