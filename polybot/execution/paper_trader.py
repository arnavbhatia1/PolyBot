"""Paper trader — simulated fills that approximate live CLOB mechanics:
gaussian-jittered submission latency, post-latency book re-check, VWAP across
consumed levels, FOK rejection on `max_slippage` violation, and a configurable
network-failure rate to exercise the retry path.

When no CLOB WebSocket is attached (e.g. unit tests, first-tick) the trader
fills at the requested price.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from polybot.execution.base import BaseTrader, FillResult, DEFAULT_FEE_RATE, exit_fee_usdc

logger = logging.getLogger(__name__)


class PaperTrader(BaseTrader):

    def __init__(self, db: Any, **kwargs: Any) -> None:
        super().__init__(
            db=db,
            max_slippage=kwargs.get("max_slippage", 0.02),
            max_bankroll_deployed=kwargs.get("max_bankroll_deployed", 0.80),
            max_concurrent_positions=kwargs.get("max_concurrent_positions", 1),
        )
        # Realism knobs (all overridable via settings.yaml -> execution.*)
        self.latency_mean_s: float = kwargs.get("paper_latency_mean_s", 0.4)
        self.latency_jitter_s: float = kwargs.get("paper_latency_jitter_s", 0.15)
        self.network_fail_rate: float = kwargs.get("paper_network_fail_rate", 0.02)
        self._clob_ws: Any = None

    def set_clob_ws(self, clob_ws: Any) -> None:
        """Attach the CLOB WebSocket so fills can re-check the book post-latency."""
        self._clob_ws = clob_ws

    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Simulate a FOK market buy with realistic latency + VWAP + rejects."""
        await self._simulate_latency()
        if random.random() < self.network_fail_rate:
            return FillResult(filled=False, reason="simulated network error")
        return self._walk_book(token_id, side="buy", requested_price=price, size_usd=size)

    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """Simulate a FOK market sell for `shares` shares at realistic VWAP."""
        await self._simulate_latency()
        if random.random() < self.network_fail_rate:
            return FillResult(filled=False, reason="simulated network error")
        size_usd = shares * price
        return self._walk_book(token_id, side="sell", requested_price=price, size_usd=size_usd)

    async def _resolve_bankroll(self, position: dict[str, Any], exit_price: float) -> float:
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE
        fee_usdc = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - fee_usdc
        bankroll = await self.db.get_bankroll()
        return bankroll + revenue

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _simulate_latency(self) -> None:
        """Gaussian-jittered sleep approximating Polymarket match latency (~250-600ms typical)."""
        latency = max(0.25, random.gauss(self.latency_mean_s, self.latency_jitter_s))
        await asyncio.sleep(latency)

    def _walk_book(self, token_id: str, side: str, requested_price: float,
                   size_usd: float) -> FillResult:
        """Walk book levels to compute fill-size-weighted average price (VWAP).

        Buy walks asks ascending; sell walks bids descending. Rejects when
        post-latency VWAP violates `max_slippage` vs `requested_price`. Falls
        back to instant fill at `requested_price` when no book is available
        or when the snapshot is older than 30s (CLOB book deltas only update
        BBA, not the full ladder).
        """
        if self._clob_ws is None:
            return FillResult(filled=True, fill_price=requested_price, fill_size=size_usd)
        book = self._clob_ws.get_book(token_id) if hasattr(self._clob_ws, "get_book") else {}
        import time as _time
        book_age = _time.time() - float(book.get("ts", 0) or 0)
        if book_age > 30:
            return FillResult(filled=True, fill_price=requested_price, fill_size=size_usd)
        levels_raw = book.get("asks" if side == "buy" else "bids", [])
        if not levels_raw:
            return FillResult(filled=True, fill_price=requested_price, fill_size=size_usd)

        levels = []
        for lvl in levels_raw:
            try:
                levels.append((float(lvl.get("price", 0)), float(lvl.get("size", 0))))
            except (TypeError, ValueError):
                continue
        levels = [(p, s) for p, s in levels if p > 0 and s > 0]
        if not levels:
            return FillResult(filled=True, fill_price=requested_price, fill_size=size_usd)
        levels.sort(key=lambda ps: ps[0], reverse=(side == "sell"))

        # Walk levels up to size_usd (buy) or size_shares (sell), compute VWAP.
        remaining = size_usd if side == "buy" else (size_usd / requested_price)
        spent = 0.0
        consumed = 0.0
        for lvl_price, lvl_shares in levels:
            if remaining <= 0:
                break
            level_usd = lvl_price * lvl_shares
            if side == "buy":
                take_usd = min(remaining, level_usd)
                take_shares = take_usd / lvl_price
                spent += take_usd
                consumed += take_shares
                remaining -= take_usd
            else:
                take_shares = min(remaining, lvl_shares)
                take_usd = lvl_price * take_shares
                spent += take_usd
                consumed += take_shares
                remaining -= take_shares

        if consumed <= 0:
            return FillResult(filled=False, reason="book empty during fill")
        vwap = spent / consumed

        # Detect insufficient depth (book couldn't absorb the full order from direct levels).
        if remaining > 1e-6:
            return FillResult(
                filled=False,
                reason=f"insufficient book depth (remaining={remaining:.2f})",
            )

        # negRisk cross-matching: the live CLOB will execute at the better of the direct
        # book VWAP or the cross-matched /price API price (requested_price). For sells,
        # better = higher; for buys, better = lower. Without this, paper sells of losing
        # tokens fill at near-zero direct bids while live would fill at the cross-matched
        # price (and paper buys could fill at expensive direct asks while live cross-match
        # via complementary-token "merge" matching is cheaper).
        if side == "sell":
            fill_price = max(vwap, requested_price)
        else:
            fill_price = min(vwap, requested_price)
            # Simulate FOK price-moved rejection: if the book has moved more than
            # max_slippage above requested_price, live would reject the order outright.
            if vwap > requested_price * (1 + self.max_slippage):
                return FillResult(filled=False, reason="price moved before fill (simulated FOK rejection)")

        fill_size = spent if side == "buy" else 0.0
        return FillResult(filled=True, fill_price=fill_price, fill_size=fill_size)
