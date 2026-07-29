"""Wald SPRT — the pre-registered sequential gate design (frozen 2026-07-19).

Governs turning things ON for every validation after the 07-19 go-live gate;
the post-live kill rule (realized ledger, trailing-4d / 8-day t) is unchanged
and governs turning things OFF. Design constants live with each pre-registered
application; this module is only the arithmetic.

Test definition (normal day-means, known σ):
  Λ += (x·μ₁ − μ₁²/2) / σ²   per scored day-observation x (¢/sh units)
  accept H1 (deploy/graduate) at Λ ≥ log((1−β)/α)
  accept H0 (kill/park)      at Λ ≤ log(β/(1−α))
  no decision before MIN_DECISION_DAYS regardless of Λ; hard stop at
  TRUNCATE_DAYS → fall back to the fixed-horizon read (mean/t/p10 legs).
σ is FROZEN before scoring starts; if the realized day-sd over the test runs
> SIGMA_VOID_RATIO × the frozen σ, the test is VOID (regime changed under it)
— restart with a re-estimated σ, never patch mid-test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_DECISION_DAYS = 3
TRUNCATE_DAYS = 16
SIGMA_VOID_RATIO = 1.5


@dataclass
class SprtResult:
    state: str                    # "continue" | "accept_h1" | "accept_h0" | "truncated" | "void"
    lam: float                    # final log-likelihood ratio Λ
    n_days: int                   # scored day-observations consumed
    upper: float                  # accept-H1 boundary log((1−β)/α)
    lower: float                  # accept-H0 boundary log(β/(1−α))
    day_lambdas: list[float] = field(default_factory=list)  # Λ after each scored day


def run_sprt(day_means: list[float], mu1: float, sigma: float,
             alpha: float = 0.05, beta: float = 0.23,
             min_days: int = MIN_DECISION_DAYS,
             max_days: int = TRUNCATE_DAYS) -> SprtResult:
    """Score day-mean observations in order; stop at the first decision.

    Returns the state after consuming as many observations as the design
    allows: a decision ends the test immediately (later observations are
    ignored — Wald tests are stop-on-boundary, not score-everything).
    """
    upper = math.log((1.0 - beta) / alpha)
    lower = math.log(beta / (1.0 - alpha))
    if sigma <= 0 or mu1 == 0:
        return SprtResult("void", 0.0, 0, upper, lower)

    lam = 0.0
    lambdas: list[float] = []
    state = "continue"
    for i, x in enumerate(day_means, 1):
        lam += (x * mu1 - mu1 * mu1 / 2.0) / (sigma * sigma)
        lambdas.append(lam)
        if i >= min_days:
            if lam >= upper:
                state = "accept_h1"
                break
            if lam <= lower:
                state = "accept_h0"
                break
        if i >= max_days:
            state = "truncated"
            break

    # σ-blowup void check over exactly the observations the test CONSUMED —
    # a test whose variance regime changed under it must not decide on the
    # corrupted likelihood, but observations after the stopping point were
    # never "under" the test, so they can neither re-open nor retro-void a
    # decision already reached (stop-on-boundary; a decided test replays
    # deterministically forever).
    consumed = day_means[:len(lambdas)]
    if len(consumed) >= 2:
        m = sum(consumed) / len(consumed)
        sd = math.sqrt(sum((x - m) ** 2 for x in consumed) / (len(consumed) - 1))
        if sd > SIGMA_VOID_RATIO * sigma:
            return SprtResult("void", lam, len(consumed), upper, lower, lambdas)
    return SprtResult(state, lam, len(consumed), upper, lower, lambdas)


def format_status(name: str, r: SprtResult) -> str:
    """One-line human status for the nightly Discord report."""
    tag = {
        "continue": "accruing",
        "accept_h1": "✅ ACCEPT H1",
        "accept_h0": "❌ ACCEPT H0",
        "truncated": "⏱ truncated → fixed-horizon read",
        "void": "⚠️ VOID (σ regime changed / unset)",
    }[r.state]
    return (f"SPRT[{name}]: {tag} — Λ {r.lam:+.2f} "
            f"(accept ≥{r.upper:+.2f} / kill ≤{r.lower:+.2f}) over {r.n_days} scored day(s)")
