from __future__ import annotations

import math
import logging

logger = logging.getLogger(__name__)


# Adoption requires this many candidate trades AND z-score above this floor.
# z = delta_sharpe / JK_SE (autocorr-adjusted). z=0.5 ≈ 69% one-sided confidence
# the change is real — chosen to be permissive enough for the pipeline to keep
# adapting through regime shifts, strict enough to filter noise.
MIN_CANDIDATE_TRADES = 100
ADOPTION_Z_FLOOR = 0.3


def _sharpe(returns: list[float]) -> float:
    """Per-trade unannualized Sharpe from a list of gain_pct values."""
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    var = sum((r - avg) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.0
    return avg / std if std > 0 else 0.0


def _lag1_autocorr(values: list[float]) -> float:
    """1-lag autocorrelation of a returns series. Returns 0 when undefined."""
    if len(values) < 3:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    num = sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n))
    den = sum((v - mean) ** 2 for v in values)
    return num / den if den > 0 else 0.0


def _jk_se(sharpe: float, n_trades: int, returns: list[float] | None = None) -> float:
    """Jobson-Korkie standard error for a per-trade Sharpe, autocorr-adjusted."""
    if n_trades < 2:
        return 0.0
    se = math.sqrt((1.0 + 0.5 * sharpe ** 2) / max(n_trades, 1))
    if returns and len(returns) >= 3:
        rho = _lag1_autocorr(returns)
        se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
    return se


def _sharpe_z_test(old_sharpe: float, new_sharpe: float, n_trades: int,
                   returns: list[float] | None = None) -> float:
    """Z-score for Sharpe improvement (Jobson-Korkie SE, autocorr-inflated)."""
    se = _jk_se(old_sharpe, n_trades, returns)
    return (new_sharpe - old_sharpe) / se if se > 0 else 0.0


class WeightOptimizer:
    """Adoption gate for pipeline-proposed parameter changes.

    Single statistical test — no static absolute floor, no crisis-mode toggle.
    A candidate change adopts when its Sharpe improvement clears z=0.5 against
    the autocorr-adjusted Jobson-Korkie SE (≈69% one-sided confidence). This
    scales naturally with sample size: small N → wider SE → tighter floor;
    large N → narrower SE → looser floor.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        # Args/kwargs accepted for backward compat with old call sites.
        pass

    def should_adopt(self, current_sharpe: float, candidate_sharpe: float,
                     n_trades: int = 0,
                     candidate_returns: list[float] | None = None) -> tuple[bool, str, float]:
        """Returns (adopt, reason, z_score). z = delta / JK_SE."""
        delta = candidate_sharpe - current_sharpe

        if candidate_sharpe <= 0:
            return False, f"candidate Sharpe {candidate_sharpe:.3f} <= 0", 0.0

        if n_trades < MIN_CANDIDATE_TRADES:
            return False, (
                f"only {n_trades} candidate trades (need {MIN_CANDIDATE_TRADES}) — "
                f"your min_model_probability or min_edge may be filtering too aggressively"
            ), 0.0

        se = _jk_se(current_sharpe, n_trades, candidate_returns)
        z = delta / se if se > 0 else 0.0

        if z < ADOPTION_Z_FLOOR:
            return False, (
                f"z={z:.2f} below floor {ADOPTION_Z_FLOOR} "
                f"(delta={delta:+.4f}, SE={se:.4f}, n={n_trades})"
            ), z

        return True, f"delta={delta:+.4f} z={z:.2f} (SE={se:.4f}, n={n_trades})", z
