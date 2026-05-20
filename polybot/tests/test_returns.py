import pytest
from polybot.core.returns import log_return


def test_log_return_basic():
    assert log_return(entry_price=0.55, exit_price=0.68) == pytest.approx(0.2119, abs=0.001)


def test_log_return_loss():
    assert log_return(entry_price=0.55, exit_price=0.46) == pytest.approx(-0.1784, abs=0.001)


def test_log_return_breakeven():
    assert log_return(entry_price=0.55, exit_price=0.55) == pytest.approx(0.0, abs=0.0001)


def test_log_return_total_loss_returns_sentinel():
    assert log_return(entry_price=0.55, exit_price=0.0) == -10.0
