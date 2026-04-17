"""Correlation-aware multiplier for concurrent-position sizing.

Replaces the flat ``concurrent_position_discount`` that treated two concurrent
Polymarket BTC windows as if they were independent. They're not — adjacent 5-min
windows share regime and microstructure, so same-side concurrent bets are highly
correlated (ρ ≈ 0.7–0.9) and opposite-side bets are naturally hedged (ρ ≈ -0.2).

Two ρ=0.8 half-Kelly bets carry ~0.9× the variance of a single full-Kelly bet —
i.e., the flat 0.5× discount under-sizes risk. Two anti-correlated half-Kelly
bets carry ~0.35× variance, leaving headroom for larger sizing.
"""
from __future__ import annotations

from typing import Any, Iterable

# Heuristic correlations between a new BTC-5min position and an open one. Tightened
# from the full taxonomy in the critique: we don't have stored regime tags on open
# positions, but all concurrent BTC 5-min positions share a macro regime by construction
# (consecutive windows, same instrument), so side agreement is the dominant signal.
_CORR_SAME_SIDE = 0.75
_CORR_OPPOSITE_SIDE = -0.25


def estimate_correlation(new_side: str, new_market_id: str,
                         open_position: dict[str, Any],
                         max_single_usd: float = 0.0) -> float | None:
    """Estimate correlation between a candidate trade and an open position.

    The base rho (±0.75 / ±0.25) is scaled by ``position_size / max_single_usd`` so a
    tiny $0.50 leftover position contributes ~0.04 of correlation, not the full 0.75.
    A full-sized position contributes the full base rho. Returns None for same-market
    positions (flip — handled by flip-trading logic, not by this multiplier).
    """
    if open_position.get("market_id") == new_market_id:
        return None
    open_side = (open_position.get("side") or "").lower()
    new = new_side.lower()
    if not open_side or new not in ("up", "down"):
        return None
    base_rho = _CORR_SAME_SIDE if open_side == new else _CORR_OPPOSITE_SIDE
    if max_single_usd > 0:
        pos_size = float(open_position.get("size", 0) or 0)
        size_weight = max(0.0, min(1.0, pos_size / max_single_usd))
        return base_rho * size_weight
    return base_rho


def concurrent_multiplier(new_side: str, new_market_id: str,
                          open_positions: Iterable[dict[str, Any]],
                          max_single_usd: float = 0.0) -> float:
    """Kelly multiplier for a new position given the current book of opens.

    Picks the worst (highest) correlation among open positions and maps it to a
    sizing multiplier. When ``max_single_usd`` is supplied, correlations are
    size-weighted so tiny residual positions don't penalize the new entry the
    same as a full-size concurrent would.

    Returns 1.0 when no open positions (or none comparable — e.g., only a same-market flip).
    """
    correlations = [
        rho for p in open_positions
        if (rho := estimate_correlation(new_side, new_market_id, p, max_single_usd)) is not None
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
