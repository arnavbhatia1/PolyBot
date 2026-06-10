"""L3 flow signal: book imbalance + recency-weighted trade flow → flow_signal ∈ [-1, 1]."""
from __future__ import annotations

import math
import time
from typing import Any, TypedDict

_BOOK_DEPTH_LEVELS = 5
_TRADE_FLOW_HALF_LIFE_S = 30.0

class FlowData(TypedDict):
    flow_score: float
    book_imbalance: float
    trade_flow: float
    trade_count: int


def _sort_levels(orders: list[dict[str, Any]], best_first: str) -> list[dict[str, Any]]:
    try:
        return sorted(
            orders,
            key=lambda o: float(o.get("price", 0)),
            reverse=(best_first == "high"),
        )
    except (ValueError, TypeError):
        return orders


def _side_size(orders: list[dict[str, Any]], best_first: str, n: int) -> float:
    """Sum of the top-n level sizes (best price first)."""
    sorted_orders = _sort_levels(orders, best_first)
    if not sorted_orders:
        return 0.0
    return sum(float(o.get("size", 0)) for o in sorted_orders[:n])


def book_imbalance(book_up: dict[str, Any], book_down: dict[str, Any],
                   depth_levels: int = _BOOK_DEPTH_LEVELS) -> float:
    """Bid/ask imbalance ∈ [-1, 1] across top-N levels of each side."""
    bid_up = _side_size(book_up.get("bids", []), "high", depth_levels)
    ask_up = _side_size(book_up.get("asks", []), "low", depth_levels)
    bid_down = _side_size(book_down.get("bids", []), "high", depth_levels)
    ask_down = _side_size(book_down.get("asks", []), "low", depth_levels)

    up_pressure = bid_up - ask_up
    down_pressure = bid_down - ask_down
    total = bid_up + ask_up + bid_down + ask_down
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (up_pressure - down_pressure) / total))


def trade_flow(trades_up: list[dict[str, Any]], trades_down: list[dict[str, Any]],
               lookback_seconds: float = 120.0,
               half_life_s: float = _TRADE_FLOW_HALF_LIFE_S) -> float:
    """Recency-weighted net flow ∈ [-1, 1]. Buying Up == selling Down (same direction)."""
    now = time.time()
    cutoff = now - lookback_seconds
    decay_k = math.log(2) / max(half_life_s, 1.0)

    def _accum(trades: list[dict[str, Any]]) -> tuple[float, float]:
        buy_v = sell_v = 0.0
        for t in trades:
            ts = t.get("timestamp", 0)
            if ts < cutoff:
                continue
            age = max(0.0, now - ts)
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
    bi = book_imbalance(book_up, book_down)
    tf = trade_flow(trades_up, trades_down, lookback_seconds)

    score = bi * book_weight + tf * trade_weight
    score = max(-1.0, min(1.0, score))

    return {
        "flow_score": round(score, 4),
        "book_imbalance": round(bi, 4),
        "trade_flow": round(tf, 4),
        "trade_count": len(trades_up) + len(trades_down),
    }
