"""Order flow signal computation from Polymarket CLOB data.

Combines two independent signals:
1. Book imbalance — bid depth vs ask depth reveals directional pressure
2. Trade flow — net buy vs sell volume from recent trades reveals informed activity

The composite signal is passed to SignalEngine as flow_signal (-1 to +1),
where positive = bullish (favors Up) and negative = bearish (favors Down).
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class FlowData(TypedDict):
    flow_score: float
    book_imbalance: float
    trade_flow: float
    trade_count: int


def book_imbalance(book_up: dict[str, Any], book_down: dict[str, Any]) -> float:
    """Compute bid/ask imbalance across both sides of the binary market.

    Returns float from -1 (bearish / Down pressure) to +1 (bullish / Up pressure).

    Logic:
    - If Up bids >> Up asks -> buyers accumulating Up -> bullish
    - If Down bids >> Down asks -> buyers accumulating Down -> bearish
    - Combines both books for a net directional signal
    """
    bid_up = sum(float(b.get("size", 0)) for b in book_up.get("bids", []))
    ask_up = sum(float(a.get("size", 0)) for a in book_up.get("asks", []))
    bid_down = sum(float(b.get("size", 0)) for b in book_down.get("bids", []))
    ask_down = sum(float(a.get("size", 0)) for a in book_down.get("asks", []))

    # Net buying pressure: bid-heavy on Up OR ask-heavy on Down = bullish
    up_pressure = bid_up - ask_up    # positive = buying Up
    down_pressure = bid_down - ask_down  # positive = buying Down

    total = bid_up + ask_up + bid_down + ask_down
    if total == 0:
        return 0.0

    net = (up_pressure - down_pressure) / total
    return max(-1.0, min(1.0, net))


def trade_flow(trades_up: list[dict[str, Any]], trades_down: list[dict[str, Any]],
               lookback_seconds: float = 120.0) -> float:
    """Compute net trade flow direction from recent trade history.

    Returns float from -1 (net selling/Down buying) to +1 (net buying Up).

    Uses trade side and size from WebSocket last_trade_price events.
    Only considers trades within lookback_seconds.
    """
    import time
    cutoff = time.time() - lookback_seconds

    buy_vol_up = 0.0
    sell_vol_up = 0.0
    buy_vol_down = 0.0
    sell_vol_down = 0.0

    for t in trades_up:
        if t.get("timestamp", 0) < cutoff:
            continue
        size = float(t.get("size", 0))
        side = t.get("side", "").upper()
        if side == "BUY":
            buy_vol_up += size
        elif side == "SELL":
            sell_vol_up += size

    for t in trades_down:
        if t.get("timestamp", 0) < cutoff:
            continue
        size = float(t.get("size", 0))
        side = t.get("side", "").upper()
        if side == "BUY":
            buy_vol_down += size
        elif side == "SELL":
            sell_vol_down += size

    # Net Up buying = buy_vol_up + sell_vol_down (buying Up = selling Down)
    # Net Down buying = buy_vol_down + sell_vol_up (buying Down = selling Up)
    net_up = (buy_vol_up + sell_vol_down) - (buy_vol_down + sell_vol_up)
    total = buy_vol_up + sell_vol_up + buy_vol_down + sell_vol_down

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
