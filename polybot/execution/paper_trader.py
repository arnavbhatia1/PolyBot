"""Paper trader: simulates FOK fills with latency, VWAP book-walk, slippage, and a network-fail rate."""
from __future__ import annotations

import asyncio
import logging
import random
import time
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
        # Defaults tuned to live latency_stats.json (p50≈770ms, p99≈2.3s).
        self.latency_mean_s: float = kwargs.get("paper_latency_mean_s", 0.77)
        self.latency_jitter_s: float = kwargs.get("paper_latency_jitter_s", 0.40)
        # Treated as the fallback fail rate when the book is unavailable — and
        # as the i.i.d. baseline (genuine network/protocol errors) when book
        # state is readable. Real live FOK rejects cluster around adverse moves
        # (thin top-of-book, wide spread); _compute_fail_rate adds state-dependent
        # terms on top of this floor.
        self.network_fail_rate: float = kwargs.get("paper_network_fail_rate", 0.02)
        # Warm-SELL bookkeeping — mirrors LiveTrader's _sell_warmups dict. Lets paper
        # see the same ~150ms ECDSA-sign saving live realizes during the scalp danger
        # zone (main.py calls warm_sell_signature on every hold tick where
        # -0.05 < holding_edge < -0.005). Same TTL and drift thresholds as live, so
        # the realization rate emerges from identical conditions rather than a
        # hardcoded probability — paper saves the speedup iff live would have.
        self._sell_warmups: dict[str, dict[str, float]] = {}

    # Match live's _MAX_RETRIES + _RETRY_BASE_DELAY semantics so paper trades
    # the same FOK behaviour the live bot does (one shot per attempt with a
    # short backoff between retries). Keeps paper P&L distribution honest:
    # in live a transient "ask moved up" often clears within 50-100ms and the
    # 2nd attempt fills — paper used to give up immediately and skip those.
    _PAPER_MAX_RETRIES: int = 3
    _PAPER_RETRY_BASE_DELAY: float = 0.03

    # Warm-SELL parameters mirror LiveTrader exactly so paper and live realize
    # the speedup on the same set of trades. Speedup ~150ms = the ECDSA-sign
    # cost live skips when a valid pre-signed order exists.
    _SELL_WARMUP_TTL_S: float = 5.0
    _SELL_WARMUP_SPEEDUP_S: float = 0.15

    async def _execute_buy(
        self, token_id: str, price: float, size: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Simulate a FOK market buy with realistic latency + VWAP + rejects."""
        del fee_rate  # paper applies fee math in base.py via the same DEFAULT_FEE_RATE
        if self._precheck_rejects(token_id, side="buy", requested_price=price, size_usd=size):
            return FillResult(filled=False, reason="pre-check: book walk would exceed limit (matches live)")
        await self._simulate_latency()
        if random.random() < self._compute_fail_rate(token_id, side="buy"):
            return FillResult(filled=False, reason="simulated network error")
        return await self._retry_walk(token_id, side="buy", requested_price=price, size_usd=size)

    async def _execute_sell(
        self, token_id: str, shares: float, price: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Simulate a FOK market sell for `shares` shares at realistic VWAP."""
        del fee_rate
        size_usd = shares * price
        if self._precheck_rejects(token_id, side="sell", requested_price=price, size_usd=size_usd):
            return FillResult(filled=False, reason="pre-check: book walk would exceed limit (matches live)")
        # Consume a warm-SELL signature if one is valid for these params — the
        # speedup matches the ~150ms ECDSA-sign cost live skips. If no valid
        # warmup, pay full simulated latency just as live would.
        warmup_speedup = self._SELL_WARMUP_SPEEDUP_S if self._take_sell_warmup(
            token_id, shares, price
        ) else 0.0
        await self._simulate_latency(speedup_s=warmup_speedup)
        if random.random() < self._compute_fail_rate(token_id, side="sell"):
            return FillResult(filled=False, reason="simulated network error")
        return await self._retry_walk(token_id, side="sell", requested_price=price, size_usd=size_usd)

    async def warm_sell_signature(self, token_id: str, shares: float,
                                  expected_price: float,
                                  fee_rate: float = DEFAULT_FEE_RATE) -> None:
        """Paper analogue of LiveTrader.warm_sell_signature — bookkeeping only.

        Records (shares, expected_price, ts) so a subsequent _execute_sell can
        check whether the warmup is still valid (within TTL and parameter drift
        thresholds identical to live). When valid, _execute_sell skips ~150ms of
        simulated latency to mirror the ECDSA-sign work live actually saves.

        Idempotent within TTL when params haven't drifted — matches live's
        early-return at ``live_trader.py:715-721``.
        """
        del fee_rate  # paper has no signing work; param kept for API parity
        if not token_id or shares <= 0 or expected_price <= 0:
            return
        existing = self._sell_warmups.get(token_id)
        if existing is not None:
            age = time.time() - existing["ts"]
            price_drift = abs(existing["price"] - expected_price)
            size_drift = abs(existing["amount"] - shares) / max(shares, 1e-6)
            if age < 1.5 and price_drift < 0.005 and size_drift < 0.02:
                return  # still good — don't reset the clock
        self._sell_warmups[token_id] = {
            "amount": shares,
            "price": expected_price,
            "ts": time.time(),
        }

    def _take_sell_warmup(self, token_id: str, shares: float,
                          expected_price: float) -> bool:
        """Return True iff a valid warmup exists for these SELL params.

        Mirrors LiveTrader._take_sell_warmup acceptance criteria: TTL <= 5s,
        price drift < 1¢, size drift < 5%. Always pops the entry to prevent
        stale reuse — main.py will re-arm on the next hold tick if needed.
        """
        entry = self._sell_warmups.pop(token_id, None)
        if entry is None:
            return False
        age = time.time() - entry["ts"]
        if age > self._SELL_WARMUP_TTL_S:
            return False
        if abs(entry["price"] - expected_price) > 0.01:
            return False
        if abs(entry["amount"] - shares) / max(shares, 1e-6) > 0.05:
            return False
        return True

    # State-dependent FOK fail rate. Live FOK rejects cluster around adverse
    # moves: thin top-of-book + wide spread = match engine more likely to
    # reject before the limit can clear. Coefficients here are estimates;
    # refine them once fill_stats.json buckets failures by cause. Until then
    # the formula must satisfy two safety properties:
    #   1) Max combined rate must not exceed ~2× the i.i.d. baseline (caps
    #      regime-specific over-rejection if our state proxies are wrong).
    #   2) When book is unavailable, fall back to the constant network_fail_rate
    #      so tests and degraded-feed startup behave deterministically.
    _STATE_FAIL_RATE_BASE: float = 0.005
    _STATE_FAIL_RATE_WIDE_SPREAD: float = 0.010   # additive when spread > 5%
    _STATE_FAIL_RATE_THIN_TOP_DEPTH: float = 0.010  # additive when top-of-book < $50
    _STATE_FAIL_RATE_CAP: float = 0.030  # absolute ceiling

    def _compute_fail_rate(self, token_id: str, side: str) -> float:
        if self._clob_ws is None or not hasattr(self._clob_ws, "get_book"):
            return self.network_fail_rate
        book = self._clob_ws.get_book(token_id) or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return self.network_fail_rate
        try:
            best_bid = max(float(b["price"]) for b in bids if b.get("price"))
            best_ask = min(float(a["price"]) for a in asks if a.get("price"))
        except (TypeError, ValueError, KeyError):
            return self.network_fail_rate
        mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
        if mid <= 0:
            return self.network_fail_rate
        spread_pct = (best_ask - best_bid) / mid
        # Top-of-book depth on the side we're hitting (asks for buy, bids for sell).
        side_levels = asks if side == "buy" else bids
        try:
            top = side_levels[0]
            top_depth_usd = float(top["price"]) * float(top["size"])
        except (TypeError, ValueError, KeyError, IndexError):
            return self.network_fail_rate
        rate = self._STATE_FAIL_RATE_BASE
        if spread_pct > 0.05:
            rate += self._STATE_FAIL_RATE_WIDE_SPREAD
        if top_depth_usd < 50.0:
            rate += self._STATE_FAIL_RATE_THIN_TOP_DEPTH
        return min(rate, self._STATE_FAIL_RATE_CAP)

    def _precheck_rejects(self, token_id: str, side: str, requested_price: float,
                          size_usd: float) -> bool:
        """Mirror live's FOK pre-check: walk the current book against the limit and
        reject before sleeping if the walk would clearly exceed limit. Returns False
        when the book is missing, stale (>5s), or empty — let _walk_book handle those
        same as live's pre-check abstains in those cases.
        """
        if self._clob_ws is None or not hasattr(self._clob_ws, "get_book"):
            return False
        book = self._clob_ws.get_book(token_id) or {}
        import time as _time
        book_ts = float(book.get("ts", 0) or 0)
        if book_ts <= 0 or (_time.time() - book_ts) > 5.0:
            return False
        levels_raw = book.get("asks" if side == "buy" else "bids") or []
        if not levels_raw:
            return False
        try:
            parsed = [(float(l["price"]), float(l["size"])) for l in levels_raw
                      if l.get("price") and l.get("size")]
        except (TypeError, ValueError, KeyError):
            return False
        if not parsed:
            return False
        parsed.sort(key=lambda ps: ps[0], reverse=(side == "sell"))

        spent = 0.0
        consumed = 0.0
        if side == "buy":
            remaining = size_usd
            for px, sz in parsed:
                if remaining <= 0:
                    break
                level_usd = px * sz
                take_usd = min(remaining, level_usd)
                spent += take_usd
                consumed += take_usd / px
                remaining -= take_usd
        else:
            remaining = size_usd / requested_price  # shares
            for px, sz in parsed:
                if remaining <= 0:
                    break
                take_shares = min(remaining, sz)
                spent += px * take_shares
                consumed += take_shares
                remaining -= take_shares
        # Insufficient depth → abstain (matches live's `return None` path).
        if remaining > 1e-6 or consumed <= 0:
            return False
        vwap = spent / consumed
        return vwap > requested_price if side == "buy" else vwap < requested_price

    async def _retry_walk(self, token_id: str, side: str, requested_price: float,
                          size_usd: float) -> FillResult:
        """Run _walk_book up to _PAPER_MAX_RETRIES times with exponential backoff
        between attempts. Re-reads the book each pass so any intervening WS update
        is reflected, matching live's behavior where the second attempt sees a
        post-recoil snapshot of the order book."""
        last: FillResult | None = None
        for attempt in range(1, self._PAPER_MAX_RETRIES + 1):
            last = self._walk_book(token_id, side=side, requested_price=requested_price,
                                   size_usd=size_usd)
            if last.filled:
                return last
            # Only retry on the same class of rejection live retries on:
            # the FOK-rejection ("price moved before fill"). Don't retry depth
            # exhaustion or book-empty; those won't recover in 30ms.
            if "price moved" not in (last.reason or ""):
                return last
            if attempt < self._PAPER_MAX_RETRIES:
                # Exponential backoff with jitter, same shape as live's _retry_sleep.
                base = self._PAPER_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(base * random.uniform(0.8, 1.2))
        return last or FillResult(filled=False, reason="retry exhausted")

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

    async def _simulate_latency(self, speedup_s: float = 0.0) -> None:
        """Gaussian-jittered sleep, plus a 4% chance of a heavy-tail spike to mirror
        live's p99/max (2.3-4.8s outliers from network jitter or REST congestion).
        Floor at 0.35s — live's fastest observed sign+post.

        ``speedup_s`` subtracts from the sampled latency to mirror cases where live
        skips work (e.g. warm-SELL signature). Floor is preserved so even a saved
        sign-cost still respects live's fastest observed round-trip.
        """
        if random.random() < 0.04:
            # Heavy tail: uniform [2.0s, 4.0s] roughly matches p95-p99 from live.
            latency = random.uniform(2.0, 4.0)
        else:
            latency = max(0.35, random.gauss(self.latency_mean_s, self.latency_jitter_s))
        if speedup_s > 0:
            latency = max(0.35, latency - speedup_s)
        await asyncio.sleep(latency)

    def _walk_book(self, token_id: str, side: str, requested_price: float,
                   size_usd: float) -> FillResult:
        """Walk book levels to compute fill-size-weighted average price (VWAP).

        Buy walks asks ascending; sell walks bids descending. Rejects when
        post-latency VWAP violates `max_slippage` vs `requested_price`. When
        the book is stale (>30s) or empty, paper now matches live: live's
        FOK would have nothing to walk against either and Polymarket's match
        engine returns insufficient-liquidity. Only the `clob_ws is None`
        branch falls back to a synthetic fill — that path is exercised solely
        by unit-test fixtures that pass no clob_ws.
        """
        if self._clob_ws is None:
            return FillResult(filled=True, fill_price=requested_price, fill_size=size_usd)
        book = self._clob_ws.get_book(token_id) if hasattr(self._clob_ws, "get_book") else {}
        import time as _time
        book_age = _time.time() - float(book.get("ts", 0) or 0)
        if book_age > 30:
            return FillResult(filled=False, reason="book snapshot stale (>30s)")
        levels_raw = book.get("asks" if side == "buy" else "bids", [])
        if not levels_raw:
            return FillResult(filled=False, reason="book empty on requested side")

        levels = []
        for lvl in levels_raw:
            try:
                levels.append((float(lvl.get("price", 0)), float(lvl.get("size", 0))))
            except (TypeError, ValueError):
                continue
        levels = [(p, s) for p, s in levels if p > 0 and s > 0]
        if not levels:
            return FillResult(filled=False, reason="book empty after parse")
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

        # Strict FOK semantics — must match live exactly:
        #   BUY: rejects if vwap > requested_price (book ask moved up between calc and fill)
        #   SELL: rejects if vwap < requested_price (book bid moved down between calc and fill)
        if side == "buy":
            if vwap > requested_price:
                return FillResult(filled=False, reason="price moved before fill (simulated FOK rejection)")
        else:
            if vwap < requested_price:
                return FillResult(filled=False, reason="price moved before fill (simulated FOK rejection)")
        return FillResult(filled=True, fill_price=vwap, fill_size=spent)
