"""Correlation-aware multiplier for concurrent-position sizing.

Adjacent 5-min Polymarket BTC windows share regime and microstructure, so
same-side concurrent bets are highly correlated (ρ ≈ 0.7–0.9) and opposite-side
bets are naturally hedged (ρ ≈ -0.2) — a flat discount that treats them as
independent misprices both.

Two ρ=0.8 half-Kelly bets carry ~0.9× the variance of a single full-Kelly bet
(a flat 0.5× discount under-sizes risk); two anti-correlated half-Kelly bets
carry ~0.35× variance, leaving headroom for larger sizing.
"""
from __future__ import annotations

from typing import Any, Iterable

_CORR_SAME_SIDE = 0.75
_CORR_OPPOSITE_SIDE = -0.25

def estimate_correlation(new_side: str, new_market_id: str,
                         open_position: dict[str, Any]) -> float | None:
    """Estimate correlation between a candidate trade and an open position.

    Returns +0.75 (same side) or -0.25 (opposite). Returns None for same-market
    positions (flip — handled by flip-trading logic, not by this multiplier)
    and for unrecognized sides.
    """
    if open_position.get("market_id") == new_market_id:
        return None
    open_side = (open_position.get("side") or "").lower()
    new = new_side.lower()
    if not open_side or new not in ("up", "down"):
        return None
    return _CORR_SAME_SIDE if open_side == new else _CORR_OPPOSITE_SIDE


def concurrent_multiplier(new_side: str, new_market_id: str,
                          open_positions: Iterable[dict[str, Any]]) -> float:
    """Kelly multiplier for a new position given the current book of opens.

    Picks the worst (highest) correlation among open positions and maps it to a
    sizing multiplier.

    Returns 1.0 when no open positions (or none comparable — e.g., only a same-market flip).
    """
    correlations = [
        rho for p in open_positions
        if (rho := estimate_correlation(new_side, new_market_id, p)) is not None
    ]
    if not correlations:
        return 1.0
    worst_rho = max(correlations)
    if worst_rho > 0.6:
        return 0.35
    if worst_rho > 0.3:
        return 0.55
    if worst_rho > -0.2:
        return 0.70
    return 0.90
