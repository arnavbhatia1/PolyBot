import pytest
import numpy as np
from polybot.core.regime import RegimeDetector, RegimeState


class TestRegimeDetector:
    def test_trending_up_detected(self):
        det = RegimeDetector(lookback=20)
        closes = np.array([100.0 + i * 0.5 for i in range(25)])
        state = det.classify(closes=closes, atr=25.0, atr_history=[20, 22, 24, 25, 26], cvd=5.0)
        assert state.name == "trending_up"
        assert state.skip is False

    def test_trending_down_detected(self):
        closes = np.array([100.0 - i * 0.5 for i in range(25)])
        det = RegimeDetector(lookback=20)
        state = det.classify(closes=closes, atr=25.0, atr_history=[20, 22, 24, 25, 26], cvd=-5.0)
        assert state.name == "trending_down"
        assert state.skip is False

    def test_mean_reverting_detected(self):
        closes = np.array([100.0 + (1 if i % 2 == 0 else -1) * 2 for i in range(25)])
        det = RegimeDetector(lookback=20)
        state = det.classify(closes=closes, atr=25.0, atr_history=[15, 20, 25, 30, 35], cvd=0.0)
        assert state.name == "mean_reverting"
        assert state.skip is False

    def test_volatile_detected(self):
        det = RegimeDetector(lookback=20)
        closes = np.array([100.0 + i * 0.1 for i in range(25)])
        state = det.classify(closes=closes, atr=80.0, atr_history=[20, 25, 30, 25, 20], cvd=0.5)
        assert state.name == "volatile"
        assert state.skip is False

    def test_quiet_detected(self):
        det = RegimeDetector(lookback=20)
        closes = np.array([100.0 + i * 0.01 for i in range(25)])
        state = det.classify(closes=closes, atr=3.0, atr_history=[20, 25, 30, 25, 20], cvd=0.0)
        assert state.name == "quiet"
        assert state.skip is True

    def test_default_when_insufficient_data(self):
        det = RegimeDetector(lookback=20)
        state = det.classify(closes=np.array([100.0]), atr=25.0, atr_history=[], cvd=0.0)
        assert state.name == "unknown"
        assert state.skip is False

    def test_default_lookback_is_50(self):
        """Default lookback is 50 — needs 52+ closes to produce non-unknown result."""
        det = RegimeDetector()  # default lookback=50
        closes = np.array([100.0 + i * 0.5 for i in range(55)])
        state = det.classify(closes=closes, atr=25.0, atr_history=[20, 25, 30], cvd=5.0)
        assert state.name != "unknown"
