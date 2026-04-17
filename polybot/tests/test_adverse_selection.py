import pytest
import time
from polybot.core.adverse_selection import AdverseSelectionMonitor, FillEvent


def _isolated_monitor(tmp_path):
    """Build a monitor whose persistence file is scoped to a tmpdir so the test
    doesn't pick up live state from polybot/memory/ or stomp on the real snapshot."""
    return AdverseSelectionMonitor(state_path=tmp_path / "adverse_state.json")


class TestAdverseSelectionMonitor:
    def test_no_data_returns_neutral(self, tmp_path):
        m = _isolated_monitor(tmp_path)
        assert m.get_adverse_rate() == 0.5

    def test_all_adverse_returns_1(self, tmp_path):
        m = _isolated_monitor(tmp_path)
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

    def test_no_adverse_returns_0(self, tmp_path):
        m = _isolated_monitor(tmp_path)
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

    def test_down_side_adverse(self, tmp_path):
        m = _isolated_monitor(tmp_path)
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

    def test_record_fill(self, tmp_path):
        m = _isolated_monitor(tmp_path)
        m.record_fill("Up", 0.60, "token1", 0.60)
        assert len(m._fills) == 1
        assert m._fills[0].side == "Up"
        assert m._fills[0].midprice_10s is None

    def test_stats(self, tmp_path):
        m = _isolated_monitor(tmp_path)
        stats = m.get_stats()
        assert stats["total_tracked"] == 0
        assert stats["adverse_rate_30s"] == 0.5

    def test_persistence_round_trip(self, tmp_path):
        """Fills persist across monitor instances via on-disk state snapshot."""
        path = tmp_path / "adverse_state.json"
        m1 = AdverseSelectionMonitor(state_path=path)
        m1.record_fill("Up", 0.60, "token1", 0.60)
        m1.record_fill("Down", 0.40, "token2", 0.40)
        m2 = AdverseSelectionMonitor(state_path=path)
        assert len(m2._fills) == 2
        assert m2._fills[0].side == "Up"
        assert m2._fills[1].side == "Down"
