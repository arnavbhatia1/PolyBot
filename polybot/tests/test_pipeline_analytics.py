import pytest
from polybot.agents.pipeline_analytics import ghost_gain_pct
from polybot.execution.base import DEFAULT_FEE_RATE


def test_win_nets_entry_fee():
    p = 0.5
    expected = (1 - p) / p - DEFAULT_FEE_RATE * (1 - p)
    assert ghost_gain_pct(p, True) == pytest.approx(expected)
    assert ghost_gain_pct(p, True) < (1 - p) / p  # strictly below gross payoff


def test_loss_nets_entry_fee():
    p = 0.55
    expected = -1.0 - DEFAULT_FEE_RATE * (1 - p)
    assert ghost_gain_pct(p, False) == pytest.approx(expected)
    assert ghost_gain_pct(p, False) < -1.0  # fee makes a loss worse than -100%


def test_zero_fee_matches_gross_binary_payoff():
    assert ghost_gain_pct(0.55, True, fee_rate=0.0) == pytest.approx(0.45 / 0.55)
    assert ghost_gain_pct(0.55, False, fee_rate=0.0) == -1.0


def test_custom_fee_rate():
    p = 0.6
    assert ghost_gain_pct(p, True, fee_rate=0.10) == pytest.approx((1 - p) / p - 0.10 * (1 - p))


def test_degenerate_prices_return_zero():
    assert ghost_gain_pct(0.0, True) == 0.0
    assert ghost_gain_pct(-0.1, True) == 0.0
    assert ghost_gain_pct(1.0, True) == 0.0
    assert ghost_gain_pct(1.2, False) == 0.0


def test_fee_vanishes_at_high_price():
    # fee per $1 of size = fee_rate * (1 - p) -> 0 as p -> 1
    assert ghost_gain_pct(0.99, True) == pytest.approx(0.01 / 0.99 - DEFAULT_FEE_RATE * 0.01)
