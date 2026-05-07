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


def test_should_adopt_rejects_negative_candidate(optimizer):
    adopt, _reason, z = optimizer.should_adopt(0.20, -0.10, n_trades=200)
    assert adopt is False
    assert z == 0.0  # short-circuit before z is computed
