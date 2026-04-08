import math
import numpy as np


def log_return(entry_price: float, exit_price: float) -> float:
    if exit_price <= 0 or entry_price <= 0:
        return -10.0  # Total loss in binary market (avoids math.log(0))
    return math.log(exit_price / entry_price)


def gain_pct(entry_price: float, exit_price: float) -> float:
    """Arithmetic return for binary outcomes: (exit - entry) / entry.

    Bounded [-1, +inf) — correct metric for binary options where
    log returns are undefined at exit_price=0.
    """
    if entry_price <= 0:
        return 0.0
    return (exit_price - entry_price) / entry_price


def total_log_return(returns: list[float]) -> float:
    return sum(returns)


def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    mean_return = arr.mean()
    std_return = arr.std(ddof=1)
    if std_return == 0:
        return 0.0
    return float((mean_return - risk_free_rate) / std_return)
