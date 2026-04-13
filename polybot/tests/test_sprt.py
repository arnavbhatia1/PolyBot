import pytest
import math
from polybot.core.sprt import SPRTAccumulator

class TestSPRT:
    def test_strong_signal_enters_quickly(self):
        sprt = SPRTAccumulator(alpha=0.05, beta=0.10, min_interval_s=0.0)
        for _ in range(7):
            result = sprt.update(prob_up=0.80)
        assert result == "ENTER"

    def test_weak_signal_stays_accumulating(self):
        sprt = SPRTAccumulator(alpha=0.05, beta=0.10, min_interval_s=0.0)
        for _ in range(5):
            result = sprt.update(prob_up=0.55)
        assert result == "ACCUMULATING"

    def test_coin_flip_never_enters(self):
        sprt = SPRTAccumulator(alpha=0.05, beta=0.10, min_interval_s=0.0)
        for _ in range(100):
            result = sprt.update(prob_up=0.50)
        assert result == "ACCUMULATING"

    def test_down_signal_also_enters(self):
        sprt = SPRTAccumulator(alpha=0.05, beta=0.10, min_interval_s=0.0)
        for _ in range(7):
            result = sprt.update(prob_up=0.20)
        assert result == "ENTER"

    def test_reset_clears_state(self):
        sprt = SPRTAccumulator(min_interval_s=0.0)
        sprt.update(0.90)
        sprt.update(0.90)
        sprt.reset()
        assert sprt.llr == 0.0
        assert sprt.update(0.55) == "ACCUMULATING"

    def test_mixed_signals_slow_accumulation(self):
        sprt = SPRTAccumulator(alpha=0.05, beta=0.10, min_interval_s=0.0)
        for _ in range(3):
            sprt.update(0.85)
            sprt.update(0.45)
        assert sprt.get_status() == "ACCUMULATING"

    def test_get_confidence(self):
        sprt = SPRTAccumulator(min_interval_s=0.0)
        sprt.update(0.80)
        c1 = sprt.get_confidence()
        sprt.update(0.80)
        c2 = sprt.get_confidence()
        assert c2 > c1

    def test_favored_side(self):
        sprt = SPRTAccumulator(min_interval_s=0.0)
        sprt.update(0.80)
        sprt.update(0.80)
        assert sprt.favored_side() == "Up"
        sprt2 = SPRTAccumulator(min_interval_s=0.0)
        sprt2.update(0.20)
        sprt2.update(0.20)
        assert sprt2.favored_side() == "Down"

    def test_observation_downsampling(self):
        """Rapid updates within min_interval_s are skipped — evidence doesn't accumulate."""
        sprt = SPRTAccumulator(alpha=0.05, beta=0.10, min_interval_s=60.0)
        # First update goes through (last_update_ts starts at 0)
        sprt.update(0.80)
        llr_after_first = sprt.llr
        assert llr_after_first > 0.0
        # Subsequent rapid updates are skipped — LLR stays the same
        for _ in range(10):
            sprt.update(0.80)
        assert sprt.llr == llr_after_first

    def test_reset_clears_last_update_ts(self):
        """After reset, the next update should always go through."""
        sprt = SPRTAccumulator(min_interval_s=60.0)
        sprt.update(0.80)
        llr1 = sprt.llr
        sprt.reset()
        assert sprt._last_update_ts == 0.0
        # First update after reset goes through
        sprt.update(0.80)
        assert sprt.llr == llr1  # same increment as the first time
