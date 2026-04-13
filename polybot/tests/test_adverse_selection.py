import pytest
import time
from polybot.core.adverse_selection import AdverseSelectionMonitor, FillEvent


class TestAdverseSelectionMonitor:
    def test_no_data_returns_neutral(self):
        m = AdverseSelectionMonitor()
        assert m.get_adverse_rate() == 0.5

    def test_all_adverse_returns_1(self):
        m = AdverseSelectionMonitor()
        for i in range(20):
            fill = FillEvent(
                timestamp=time.time() - 120,
                side="Up", fill_price=0.60, token_id="t1",
                midprice_at_fill=0.60,
                midprice_30s=0.50,  # dropped -- adverse for Up
                resolved=True,
            )
            m._fills.append(fill)
        assert m.get_adverse_rate(30.0) == 1.0

    def test_no_adverse_returns_0(self):
        m = AdverseSelectionMonitor()
        for i in range(20):
            fill = FillEvent(
                timestamp=time.time() - 120,
                side="Up", fill_price=0.60, token_id="t1",
                midprice_at_fill=0.60,
                midprice_30s=0.70,  # rose -- favorable for Up
                resolved=True,
            )
            m._fills.append(fill)
        assert m.get_adverse_rate(30.0) == 0.0

    def test_down_side_adverse(self):
        m = AdverseSelectionMonitor()
        for i in range(20):
            fill = FillEvent(
                timestamp=time.time() - 120,
                side="Down", fill_price=0.60, token_id="t1",
                midprice_at_fill=0.60,
                midprice_30s=0.70,  # rose -- adverse for Down
                resolved=True,
            )
            m._fills.append(fill)
        assert m.get_adverse_rate(30.0) == 1.0

    def test_record_fill(self):
        m = AdverseSelectionMonitor()
        m.record_fill("Up", 0.60, "token1", 0.60)
        assert len(m._fills) == 1
        assert m._fills[0].side == "Up"
        assert m._fills[0].midprice_10s is None

    def test_stats(self):
        m = AdverseSelectionMonitor()
        stats = m.get_stats()
        assert stats["total_tracked"] == 0
        assert stats["adverse_rate_30s"] == 0.5
