from __future__ import annotations

import math
import logging
from polybot.agents.pipeline_analytics import sharpe as _sharpe

logger = logging.getLogger(__name__)


# Adoption requires this many candidate trades AND z-score above this floor.
# z = delta_sharpe / JK_SE (autocorr-adjusted). z=0.3 ≈ 62% one-sided confidence
# the change is real — chosen to be permissive enough for the pipeline to keep
# adapting through regime shifts, strict enough to filter noise.
MIN_CANDIDATE_TRADES = 100
ADOPTION_Z_FLOOR = 0.3


def _lag_autocorr(values: list[float], lag: int) -> float:
    """k-lag autocorrelation of a returns series. Returns 0 when undefined."""
    if len(values) <= lag + 1:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    num = sum((values[i] - mean) * (values[i - lag] - mean) for i in range(lag, n))
    den = sum((v - mean) ** 2 for v in values)
    return num / den if den > 0 else 0.0


def _lag1_autocorr(values: list[float]) -> float:
    return _lag_autocorr(values, 1)


def _newey_west_factor(returns: list[float]) -> float:
    """sqrt(1 + 2·Σ wₖ·ρₖ) with Bartlett weights wₖ = 1 − k/(L+1). Floors at 1 (IID baseline).

    Data-adaptive lag selection per Newey & West (1994): L = floor(4·(n/100)^(2/9)).
    Scales naturally with sample size — small N gets short lag (low variance on
    the autocorr estimate); large N gets longer lag (catches slow-decaying
    autocorrelation in regime-clustered trade returns).
    """
    n = len(returns)
    if n < 4:
        return 1.0
    nw_lag = max(1, int(4 * (n / 100.0) ** (2.0 / 9.0)))
    eff_lag = max(1, min(nw_lag, n - 2))
    weighted_sum = 0.0
    for k in range(1, eff_lag + 1):
        rho = _lag_autocorr(returns, k)
        bartlett = 1.0 - k / (eff_lag + 1.0)
        weighted_sum += bartlett * rho
    return math.sqrt(max(1.0, 1.0 + 2.0 * weighted_sum))


def _jk_se(sharpe: float, n_trades: int, returns: list[float] | None = None) -> float:
    """Jobson-Korkie SE for per-trade Sharpe, Newey-West autocorr-adjusted (up to 5 lags)."""
    if n_trades < 2:
        return 0.0
    se = math.sqrt((1.0 + 0.5 * sharpe ** 2) / max(n_trades, 1))
    if returns and len(returns) >= 3:
        se *= _newey_west_factor(returns)
    return se


class WeightOptimizer:
    """Adoption gate for pipeline-proposed parameter changes.

    Single statistical test — no static absolute floor, no crisis-mode toggle.
    A candidate change adopts when its Sharpe improvement clears z=0.3 against
    the autocorr-adjusted Jobson-Korkie SE (≈62% one-sided confidence). This
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
