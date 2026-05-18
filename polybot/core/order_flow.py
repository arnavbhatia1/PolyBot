"""Order flow signal computation from Polymarket CLOB data.

Combines two independent signals:
1. Book imbalance — bid depth vs ask depth reveals directional pressure
2. Trade flow — net buy vs sell volume from recent trades reveals informed activity

The composite signal is passed to SignalEngine as flow_signal (-1 to +1),
where positive = bullish (favors Up) and negative = bearish (favors Down).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, TypedDict

logger = logging.getLogger(__name__)
_BOOK_DEPTH_LEVELS = 5
_TRADE_FLOW_HALF_LIFE_S = 30.0

class FlowData(TypedDict):
    flow_score: float
    book_imbalance: float
    trade_flow: float
    trade_count: int


def _sum_top_levels(orders: list[dict[str, Any]], best_first: str, n: int) -> float:
    """Sum size over the top-N price levels.

    best_first = "high" → highest prices first (use for bids: best bid = highest)
    best_first = "low"  → lowest prices first  (use for asks: best ask = lowest)

    Far-out resting orders are dropped — they don't reflect tradeable
    intent on the timescale this signal operates over.
    """
    try:
        sorted_orders = sorted(
            orders,
            key=lambda o: float(o.get("price", 0)),
            reverse=(best_first == "high"),
        )
    except (ValueError, TypeError):
        sorted_orders = orders
    return sum(float(o.get("size", 0)) for o in sorted_orders[:n])


def book_imbalance(book_up: dict[str, Any], book_down: dict[str, Any],
                   depth_levels: int = _BOOK_DEPTH_LEVELS) -> float:
    """Compute bid/ask imbalance across both sides of the binary market.

    Returns float from -1 (bearish / Down pressure) to +1 (bullish / Up pressure).

    Only the top `depth_levels` levels on each side are counted. A 10k-share
    resting bid 20¢ from the market is non-informative noise — the previous
    full-book sum gave it equal weight to a 100-share top-of-book bid, which
    distorted the imbalance.

    Logic:
    - If Up bids >> Up asks -> buyers accumulating Up -> bullish
    - If Down bids >> Down asks -> buyers accumulating Down -> bearish
    """
    bid_up = _sum_top_levels(book_up.get("bids", []), "high", depth_levels)
    ask_up = _sum_top_levels(book_up.get("asks", []), "low", depth_levels)
    bid_down = _sum_top_levels(book_down.get("bids", []), "high", depth_levels)
    ask_down = _sum_top_levels(book_down.get("asks", []), "low", depth_levels)

    # Net buying pressure: bid-heavy on Up OR ask-heavy on Down = bullish
    up_pressure = bid_up - ask_up    # positive = buying Up
    down_pressure = bid_down - ask_down  # positive = buying Down

    total = bid_up + ask_up + bid_down + ask_down
    if total == 0:
        return 0.0

    net = (up_pressure - down_pressure) / total
    return max(-1.0, min(1.0, net))


def trade_flow(trades_up: list[dict[str, Any]], trades_down: list[dict[str, Any]], lookback_seconds: float = 120.0, half_life_s: float = _TRADE_FLOW_HALF_LIFE_S) -> float:
    """Compute net trade flow direction from recent trade history. Returns float from -1 (net selling/Down buying) to +1 (net buying Up).
    
    Trades are recency-weighted by an exponential decay (`half_life_s` = 30s
    by default). Polymarket CLOB sizes are in shares. Bullish-Up activity = buying Up OR
    selling Down (each share represents the same $1 binary payoff).
    """
    now = time.time()
    cutoff = now - lookback_seconds
    decay_k = math.log(2) / max(half_life_s, 1.0)

    def _accum(trades: list[dict[str, Any]]) -> tuple[float, float]:
        buy_v = 0.0
        sell_v = 0.0
        for t in trades:
            ts = t.get("timestamp", 0)
            if ts < cutoff:
                continue
            age = max(0.0, now - ts)  # future-dated timestamps clamped to "fresh"
            w = math.exp(-decay_k * age)
            sz = float(t.get("size", 0)) * w
            side = t.get("side", "").upper()
            if side == "BUY":
                buy_v += sz
            elif side == "SELL":
                sell_v += sz
        return buy_v, sell_v

    buy_up, sell_up = _accum(trades_up)
    buy_down, sell_down = _accum(trades_down)

    net_up = (buy_up + sell_down) - (buy_down + sell_up)
    total = buy_up + sell_up + buy_down + sell_down
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, net_up / total))


def compute_flow_signal(book_up: dict[str, Any], book_down: dict[str, Any],
                        trades_up: list[dict[str, Any]], trades_down: list[dict[str, Any]],
                        book_weight: float = 0.6,
                        trade_weight: float = 0.4,
                        lookback_seconds: float = 120.0) -> FlowData:
    """Compute composite order flow signal.

    Args:
        book_up: CLOB order book for Up token
        book_down: CLOB order book for Down token
        trades_up: Recent trade history for Up token
        trades_down: Recent trade history for Down token
        book_weight: Weight for book imbalance component
        trade_weight: Weight for trade flow component
        lookback_seconds: Only count trades within this window

    Returns:
        dict with:
            flow_score: float -1 to +1 (positive = bullish)
            book_imbalance: float -1 to +1
            trade_flow: float -1 to +1
            trade_count: int (total trades considered)
    """
    bi = book_imbalance(book_up, book_down)
    tf = trade_flow(trades_up, trades_down, lookback_seconds)

    score = bi * book_weight + tf * trade_weight
    score = max(-1.0, min(1.0, score))

    trade_count = len(trades_up) + len(trades_down)

    return {
        "flow_score": round(score, 4),
        "book_imbalance": round(bi, 4),
        "trade_flow": round(tf, 4),
        "trade_count": trade_count,
    }
