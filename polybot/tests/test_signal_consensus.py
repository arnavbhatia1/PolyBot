from polybot.core.signal_engine import compute_signal_consensus


def test_full_agreement_boosts():
    signals = {"flow": 0.3, "spot_flow": 0.2, "perp": 0.1, "cvd_accel": 0.15}
    assert compute_signal_consensus(signals, side="Up") > 1.0


def test_disagreement_reduces():
    signals = {"flow": 0.3, "spot_flow": -0.3, "perp": -0.2, "cvd_accel": -0.1}
    assert compute_signal_consensus(signals, side="Up") < 1.0


def test_no_signals_neutral():
    signals = {"flow": 0.0, "spot_flow": 0.0}
    assert compute_signal_consensus(signals, side="Up") == 1.0


def test_down_side_flips_direction():
    signals = {"flow": -0.3, "spot_flow": -0.2, "perp": -0.4}
    assert compute_signal_consensus(signals, side="Down") > 1.0


def test_all_in_dead_zone():
    signals = {"flow": 0.01, "spot_flow": -0.02, "perp": 0.03}
    assert compute_signal_consensus(signals, side="Up") == 1.0
