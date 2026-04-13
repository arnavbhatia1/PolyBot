import pytest
from polybot.core.kraken_feed import KrakenFeed, KrakenState


class TestKrakenState:
    def test_initial_state(self):
        s = KrakenState()
        assert s.price == 0.0
        assert s.age_seconds == float("inf")

    def test_with_data(self):
        import time
        s = KrakenState(price=71000, bid=70999, ask=71001, updated_at=time.time())
        assert s.spread == 2.0
        assert s.age_seconds < 1.0


class TestKrakenFeed:
    def test_default_url(self):
        f = KrakenFeed()
        assert "kraken" in f.ws_url

    def test_handle_ticker(self):
        f = KrakenFeed()
        # Kraken format: [channelID, {data}, "ticker", "XBT/USD"]
        f._handle_message([
            42,
            {
                "a": ["71500.50", "1", "1.000"],   # ask
                "b": ["71500.00", "2", "2.000"],   # bid
                "c": ["71500.25", "0.123"],         # close/last
                "v": ["1234.5", "5678.9"],          # volume
                "p": ["71400.0", "71300.0"],        # vwap
            },
            "ticker",
            "XBT/USD"
        ])
        assert f.state.price == 71500.25
        assert f.state.bid == 71500.0
        assert f.state.ask == 71500.5

    def test_ignores_wrong_pair(self):
        f = KrakenFeed()
        f._handle_message([42, {}, "ticker", "ETH/USD"])
        assert f.state.price == 0.0

    def test_ignores_non_list(self):
        f = KrakenFeed()
        f._handle_message({"event": "systemStatus"})
        assert f.state.price == 0.0
