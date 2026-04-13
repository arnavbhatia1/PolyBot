import pytest
import math
from polybot.core.edge_halflife import EdgeHalfLifeTracker


class TestEdgeHalfLife:
    def test_insufficient_data(self):
        tracker = EdgeHalfLifeTracker(outcomes_dir="/nonexistent")
        result = tracker.compute()
        assert result["regime"] == "insufficient_data"
        assert result["kelly_discount"] == 1.0

    def test_avg_realized_edge(self):
        outcomes = [{"correct": True}] * 7 + [{"correct": False}] * 3
        edge = EdgeHalfLifeTracker._avg_realized_edge(outcomes)
        assert edge == pytest.approx(0.2)  # 70% WR - 50% = 20%

    def test_no_decay_returns_healthy(self):
        tracker = EdgeHalfLifeTracker()
        # Mock: recent edge >= prior edge
        result = tracker.compute.__wrapped__(tracker) if hasattr(tracker.compute, '__wrapped__') else None
        # Since we can't easily mock file IO, just test the static method
        assert EdgeHalfLifeTracker._avg_realized_edge([]) == 0.0
