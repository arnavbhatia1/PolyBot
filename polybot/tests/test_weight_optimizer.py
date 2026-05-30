import pytest
from polybot.agents.weight_optimizer import WeightOptimizer


@pytest.fixture
def optimizer():
    return WeightOptimizer()


def test_should_adopt_passes_with_clear_improvement(optimizer):
    # Big delta with enough trades clears the z=0.5 floor easily.
    adopt, reason, z = optimizer.should_adopt(0.20, 0.50, n_trades=200)
    assert adopt is True
    assert z > 0.5
    assert "z=" in reason


def test_should_adopt_rejects_tiny_improvement(optimizer):
    # 0.02 absolute delta at n=200 is well within noise.
    adopt, _reason, z = optimizer.should_adopt(0.20, 0.22, n_trades=200)
    assert adopt is False
    assert z < 0.5


def test_should_adopt_rejects_low_sample(optimizer):
    adopt, reason, _z = optimizer.should_adopt(0.20, 0.50, n_trades=50)
    assert adopt is False
    assert "need 100" in reason


def test_should_adopt_rejects_below_abs_floor(optimizer):
    # Healthy baseline + collapsed candidate → blocked by abs floor.
    adopt, reason, _z = optimizer.should_adopt(0.20, -0.10, n_trades=200)
    assert adopt is False
    assert "abs floor" in reason


def test_should_adopt_allows_recovery_from_negative_baseline(optimizer):
    # Baseline negative, candidate less negative AND clears z — recovery path.
    adopt, reason, z = optimizer.should_adopt(-0.05, 0.10, n_trades=200)
    assert adopt is True
    assert z > 0.3


def test_should_adopt_rejects_non_finite_candidate(optimizer):
    # P2-2: a NaN/Inf candidate Sharpe (e.g. from a malformed weight set) must be
    # rejected — NaN comparisons are all False and would otherwise read as "adopt".
    for bad in (float("nan"), float("inf"), float("-inf")):
        adopt, reason, z = optimizer.should_adopt(0.20, bad, n_trades=200)
        assert adopt is False
        assert "non-finite" in reason
        assert z == 0.0
