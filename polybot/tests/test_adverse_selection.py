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

    def test_all_adverse_shrunk(self, tmp_path):
        # 20 adverse fills under prior(n=10, rate=0.5) → 25/30 ≈ 0.833
        m = _isolated_monitor(tmp_path)
        for i in range(20):
            fill = FillEvent(
                timestamp=time.time() - 120,
                side="Up", fill_price=0.60, token_id="t1",
                midprice_at_fill=0.60,
                midprice_30s=0.50,
                resolved=True,
            )
            m._fills.append(fill)
        assert m.get_adverse_rate(30.0) == pytest.approx(25 / 30)

    def test_no_adverse_shrunk(self, tmp_path):
        # 20 favorable fills → 5/30 ≈ 0.167
        m = _isolated_monitor(tmp_path)
        for i in range(20):
            fill = FillEvent(
                timestamp=time.time() - 120,
                side="Up", fill_price=0.60, token_id="t1",
                midprice_at_fill=0.60,
                midprice_30s=0.70,
                resolved=True,
            )
            m._fills.append(fill)
        assert m.get_adverse_rate(30.0) == pytest.approx(5 / 30)

    def test_down_side_adverse_shrunk(self, tmp_path):
        m = _isolated_monitor(tmp_path)
        for i in range(20):
            fill = FillEvent(
                timestamp=time.time() - 120,
                side="Down", fill_price=0.60, token_id="t1",
                midprice_at_fill=0.60,
                midprice_30s=0.70,
                resolved=True,
            )
            m._fills.append(fill)
        assert m.get_adverse_rate(30.0) == pytest.approx(25 / 30)

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

    def test_decay_signed_for_up_side(self, tmp_path):
        """Up trade: post-fill mid > fill mid means market moved IN FAVOR (positive delta)."""
        m = _isolated_monitor(tmp_path)
        m.record_fill("Up", 0.60, "tA", midprice=0.60, position_id=42)
        # Drive checkpoints by hand — patch the fill's timestamp into the past so
        # update_prices fires all windows.
        m._fills[-1].timestamp = time.time() - 65.0
        prices = {"tA": 0.63}
        m.update_prices(lambda tid: prices.get(tid, 0))
        snap = m.get_decay_for_position(42)
        assert snap is not None
        assert snap["resolved_windows"] == 5
        # All deltas should be +0.03 (post 0.63 - fill 0.60 = +0.03, Up sign +1)
        for k in ("5s", "10s", "15s", "30s", "60s"):
            assert snap["deltas"][k] == pytest.approx(0.03)

    def test_decay_signed_for_down_side(self, tmp_path):
        """Down trade: post-fill mid < fill mid means market moved IN FAVOR (positive delta after sign-flip)."""
        m = _isolated_monitor(tmp_path)
        m.record_fill("Down", 0.40, "tB", midprice=0.40, position_id=99)
        m._fills[-1].timestamp = time.time() - 65.0
        prices = {"tB": 0.37}
        m.update_prices(lambda tid: prices.get(tid, 0))
        snap = m.get_decay_for_position(99)
        # (0.37 - 0.40) * (-1) = +0.03
        for k in ("5s", "10s", "15s", "30s", "60s"):
            assert snap["deltas"][k] == pytest.approx(0.03)

    def test_decay_partial_windows_when_closed_early(self, tmp_path):
        """Trade that closes at 12s has 5s/10s resolved but 15s/30s/60s still None."""
        m = _isolated_monitor(tmp_path)
        m.record_fill("Up", 0.55, "tC", midprice=0.55, position_id=7)
        m._fills[-1].timestamp = time.time() - 12.0
        m.update_prices(lambda tid: 0.58)
        snap = m.get_decay_for_position(7)
        assert snap["deltas"]["5s"] is not None
        assert snap["deltas"]["10s"] is not None
        assert snap["deltas"]["15s"] is None
        assert snap["deltas"]["30s"] is None
        assert snap["deltas"]["60s"] is None
        assert snap["resolved_windows"] == 2

    def test_decay_returns_none_for_unknown_position(self, tmp_path):
        m = _isolated_monitor(tmp_path)
        m.record_fill("Up", 0.60, "tA", midprice=0.60, position_id=1)
        assert m.get_decay_for_position(999) is None
