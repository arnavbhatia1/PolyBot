import pytest
from polybot.core.returns import log_return, gain_pct


def test_log_return_basic():
    assert log_return(entry_price=0.55, exit_price=0.68) == pytest.approx(0.2119, abs=0.001)


def test_log_return_loss():
    assert log_return(entry_price=0.55, exit_price=0.46) == pytest.approx(-0.1784, abs=0.001)


def test_log_return_breakeven():
    assert log_return(entry_price=0.55, exit_price=0.55) == pytest.approx(0.0, abs=0.0001)


def test_log_return_total_loss_returns_sentinel():
    """exit_price=0 → -10.0 sentinel (avoids log(0) = -inf)."""
    assert log_return(entry_price=0.55, exit_price=0.0) == -10.0


def test_gain_pct_basic():
    assert gain_pct(0.50, 0.65) == pytest.approx(0.30, abs=1e-6)


def test_gain_pct_total_loss_bounded():
    """Binary loss → -1.0, never -inf."""
    assert gain_pct(0.50, 0.0) == -1.0


def test_gain_pct_zero_entry_returns_zero():
    assert gain_pct(0.0, 0.50) == 0.0
