import pytest
import numpy as np
from polybot.math_engine.returns import log_return, sharpe_ratio, total_log_return


def test_log_return_basic():
    result = log_return(entry_price=0.55, exit_price=0.68)
    assert result == pytest.approx(0.2119, abs=0.001)


def test_log_return_loss():
    result = log_return(entry_price=0.55, exit_price=0.46)
    assert result == pytest.approx(-0.1784, abs=0.001)


def test_log_return_breakeven():
    result = log_return(entry_price=0.55, exit_price=0.55)
    assert result == pytest.approx(0.0, abs=0.0001)


def test_total_log_return_sums_correctly():
    returns = [0.2, -0.1, 0.15, -0.05]
    result = total_log_return(returns)
    assert result == pytest.approx(0.2, abs=0.001)


def test_sharpe_ratio_basic():
    returns = [0.05, 0.06, 0.04, 0.07, 0.05, 0.06]
    sr = sharpe_ratio(returns, risk_free_rate=0.0)
    assert sr > 1.0


def test_sharpe_ratio_negative():
    returns = [-0.05, -0.06, -0.04, -0.07, -0.05, -0.06]
    sr = sharpe_ratio(returns, risk_free_rate=0.0)
    assert sr < 0


def test_sharpe_ratio_empty_returns():
    sr = sharpe_ratio([], risk_free_rate=0.0)
    assert sr == 0.0


def test_sharpe_ratio_single_return():
    sr = sharpe_ratio([0.05], risk_free_rate=0.0)
    assert sr == 0.0
