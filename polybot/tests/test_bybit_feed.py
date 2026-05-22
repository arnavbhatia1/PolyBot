import time
from polybot.feeds.bybit_feed import BybitState


class TestBybitState:
    def test_staleness_detection(self):
        state = BybitState()
        state.perp_price = 73050.0
        state.perp_updated = time.time()
        assert state.is_stale(spot_price=73000.0, spot_updated=time.time() - 5.0, threshold_usd=20.0)

    def test_not_stale_when_close(self):
        state = BybitState()
        state.perp_price = 73005.0
        state.perp_updated = time.time()
        assert not state.is_stale(spot_price=73000.0, spot_updated=time.time(), threshold_usd=20.0)
