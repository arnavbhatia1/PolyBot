import pytest
import time
from polybot.feeds.chainlink_feed import ChainlinkFeed


class TestChainlinkFeed:
    def test_initial_state(self):
        f = ChainlinkFeed()
        assert f.price == 0.0
        assert f.age_seconds == float("inf")

    def test_get_strike_no_data(self):
        f = ChainlinkFeed()
        assert f.get_strike(1776000000) is None

    def test_price_update(self):
        f = ChainlinkFeed()
        f._price = 71500.0
        f._last_update = time.time()
        assert f.price == 71500.0
        assert f.age_seconds < 1.0

    def test_boundary_capture(self):
        f = ChainlinkFeed()
        boundary_ts = (int(time.time()) // 300) * 300
        f._price = 71234.56
        f._record_boundary(boundary_ts)
        assert f.get_strike(boundary_ts + 300) == 71234.56

    def test_boundary_last_update_wins(self):
        """The last observation before the next boundary defines the strike,
        matching Polymarket's on-chain latestRoundData() at the boundary block."""
        f = ChainlinkFeed()
        boundary_ts = (int(time.time()) // 300) * 300
        # Three observations within the 5-min window leading up to next_boundary.
        f._price = 71000.0
        f._record_boundary(boundary_ts + 60)
        f._price = 72000.0
        f._record_boundary(boundary_ts + 180)
        f._price = 73000.0
        f._record_boundary(boundary_ts + 290)
        # The latest update before next_boundary defines the strike.
        assert f.get_strike(boundary_ts + 300) == 73000.0
