import pytest
import time
from polybot.feeds.chainlink_feed import ChainlinkFeed


class TestChainlinkFeed:
    def test_initial_state(self):
        f = ChainlinkFeed()
        assert f.price == 0.0
        assert f.is_stale is True
        assert f.age_seconds == float("inf")

    def test_get_strike_no_data(self):
        f = ChainlinkFeed()
        assert f.get_strike(1776000000) is None

    def test_price_update(self):
        f = ChainlinkFeed()
        f._price = 71500.0
        f._last_update = time.time()
        assert f.price == 71500.0
        assert f.is_stale is False
        assert f.age_seconds < 1.0

    def test_stale_after_30s(self):
        f = ChainlinkFeed()
        f._price = 71500.0
        f._last_update = time.time() - 35
        assert f.is_stale is True

    def test_boundary_capture(self):
        f = ChainlinkFeed()
        f._price = 71234.56
        f._last_update = time.time()
        # Simulate boundary at a known timestamp
        boundary_ts = (int(time.time()) // 300) * 300
        f._boundary_prices[boundary_ts] = 71234.56
        assert f.get_strike(boundary_ts) == 71234.56
