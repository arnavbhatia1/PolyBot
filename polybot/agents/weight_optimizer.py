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


def _lag1_autocorr(values: list[float]) -> float:
    """Lag-1 autocorrelation of a returns series. Returns 0 when undefined."""
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    num = sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n))
    den = sum((v - mean) ** 2 for v in values)
    return num / den if den > 0 else 0.0


def _jk_se(sharpe: float, n_trades: int, returns: list[float] | None = None) -> float:
    """Jobson-Korkie SE for per-trade Sharpe, inflated by the lag-1
    autocorrelation of realized returns when available.

    Earlier versions ran a data-adaptive Newey-West correction with Bartlett
    weights over L≈4-5 lags. Empirically the higher-order lags (k≥2) sat inside
    the ±2/√n noise band at production sample sizes, so summing them added
    estimator variance without removing bias. Collapsed to lag-1 only:
    `se × sqrt(max(1, 1 + 2·ρ₁))`. Floor at 1 keeps the correction from
    shrinking SE when ρ₁ < 0 (an artifact of the underlying short-window
    estimator, not a real anti-autocorrelation signal we'd want to credit).

    Two scheduler-internal sites (`_precompute_baseline`, `_run_weight_optimizer`)
    compute the same SE inline using `_lag1_autocorr` directly — three call
    sites, one mathematical regime.
    """
    if n_trades < 2:
        return 0.0
    se = math.sqrt((1.0 + 0.5 * sharpe ** 2) / max(n_trades, 1))
    if returns and len(returns) >= 3:
        rho1 = _lag1_autocorr(returns)
        se *= math.sqrt(max(1.0, 1.0 + 2.0 * rho1))
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
        """Returns (adopt, reason, z_score). z = delta / JK_SE.

        Soft absolute floor: when baseline Sharpe is already negative, allow
        adoption of a less-negative candidate that clears z — this is the
        recovery path during regime shifts. The floor of −0.05 prevents
        adopting an outright collapse.
        """
        delta = candidate_sharpe - current_sharpe

        if n_trades < MIN_CANDIDATE_TRADES:
            return False, (
                f"only {n_trades} candidate trades (need {MIN_CANDIDATE_TRADES}) — "
                f"your min_model_probability or min_edge may be filtering too aggressively"
            ), 0.0

        se = _jk_se(current_sharpe, n_trades, candidate_returns)
        z = delta / se if se > 0 else 0.0

        abs_floor = min(0.0, current_sharpe) - 0.05
        if candidate_sharpe < abs_floor:
            return False, (
                f"candidate Sharpe {candidate_sharpe:.3f} below abs floor {abs_floor:.3f} "
                f"(baseline {current_sharpe:.3f})"
            ), z

        if z < ADOPTION_Z_FLOOR:
            return False, (
                f"z={z:.2f} below floor {ADOPTION_Z_FLOOR} "
                f"(delta={delta:+.4f}, SE={se:.4f}, n={n_trades})"
            ), z

        return True, f"delta={delta:+.4f} z={z:.2f} (SE={se:.4f}, n={n_trades})", z
