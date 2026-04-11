import pytest
from polybot.core.signal_engine import compute_signal_consensus

class TestSignalConsensus:
    def test_full_agreement_boosts(self):
        signals = {"flow": 0.3, "spot_flow": 0.2, "wall": -0.4, "perp": 0.1, "cvd_accel": 0.15}
        result = compute_signal_consensus(signals, side="Up")
        assert result > 1.0

    def test_disagreement_reduces(self):
        signals = {"flow": 0.3, "spot_flow": -0.3, "wall": 0.4, "perp": -0.2, "cvd_accel": -0.1}
        result = compute_signal_consensus(signals, side="Up")
        assert result < 1.0

    def test_no_signals_neutral(self):
        signals = {"flow": 0.0, "spot_flow": 0.0, "wall": 0.0}
        result = compute_signal_consensus(signals, side="Up")
        assert result == 1.0

    def test_down_side_flips_direction(self):
        signals = {"flow": -0.3, "spot_flow": -0.2, "perp": -0.4}
        result = compute_signal_consensus(signals, side="Down")
        assert result > 1.0

    def test_all_in_dead_zone(self):
        signals = {"flow": 0.01, "spot_flow": -0.02, "perp": 0.03}
        result = compute_signal_consensus(signals, side="Up")
        assert result == 1.0

    def test_wall_inverted(self):
        # Wall pressure: positive = resistance above = bearish for Up
        # So for an Up trade, negative wall = AGREES (support below)
        signals = {"wall": -0.5}  # negative wall = bullish = agrees with Up
        result = compute_signal_consensus(signals, side="Up")
        assert result > 1.0
