"""Paper trader: simulates FOK fills with latency, VWAP book-walk, slippage, and a network-fail rate."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from polybot.execution.base import BaseTrader, FillResult, DEFAULT_FEE_RATE, exit_fee_usdc


class PaperTrader(BaseTrader):

    # Phase 1 passive exits: paper can honor a maker fill faithfully (the fill is
    # validated against the real CLOB tape by the conservative prints-through rule
    # in main, never simulated optimistically). Live stays FOK until a real GTC
    # order subsystem exists.
    supports_passive_exit = True

    def __init__(self, db: Any, **kwargs: Any) -> None:
        super().__init__(
            db=db,
            max_bankroll_deployed=kwargs.get("max_bankroll_deployed", 0.80),
            max_concurrent_positions=kwargs.get("max_concurrent_positions", 1),
        )
        # Realism knobs (all overridable via settings.yaml -> execution.*; kwarg
        # defaults apply only when settings omit the keys). Calibrated to the operator's
        # MEASURED warm POST RTT to the Polymarket CLOB through the IRELAND VPN
        # (TTFB ~0.118-0.138s warm, ~0.35s cold); latency_floor_s is the fastest measured
        # warm RTT and the 4% heavy tail in _simulate_latency carries occasional stalls.
        self.latency_mean_s: float = kwargs.get("paper_latency_mean_s", 0.77)
        self.latency_jitter_s: float = kwargs.get("paper_latency_jitter_s", 0.40)
        self.latency_floor_s: float = kwargs.get("paper_latency_floor_s", 0.118)
        # Fallback fail rate when the book is unavailable; the i.i.d. baseline
        # otherwise — _compute_fail_rate adds state-dependent terms on top.
        self.network_fail_rate: float = kwargs.get("paper_network_fail_rate", 0.02)
        # Warm-SELL bookkeeping — mirrors LiveTrader's _sell_warmups (same TTL +
        # drift thresholds), so paper saves the ~150ms ECDSA-sign cost iff live
        # would have, rather than at a hardcoded probability.
        self._sell_warmups: dict[str, dict[str, float]] = {}

    # Match live's _MAX_RETRIES + _RETRY_BASE_DELAY so paper P&L stays honest:
    # in live a transient "ask moved up" often clears within 50-100ms and the
    # 2nd attempt fills.
    _PAPER_MAX_RETRIES: int = 3
    _PAPER_RETRY_BASE_DELAY: float = 0.03

    # Warm-SELL parameters mirror LiveTrader exactly; speedup ~150ms = the
    # ECDSA-sign cost live skips when a valid pre-signed order exists.
    _SELL_WARMUP_TTL_S: float = 5.0
    _SELL_WARMUP_SPEEDUP_S: float = 0.15

    async def _execute_buy(
        self, token_id: str, price: float, size: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Simulate a FOK market buy with realistic latency + VWAP + rejects."""
        del fee_rate  # paper applies fee math in base.py via DEFAULT_FEE_RATE
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
        # Consume a valid warm-SELL signature (≈150ms speedup, matching live);
        # otherwise pay full simulated latency just as live would.
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

        Records (shares, expected_price, ts); a valid warmup lets _execute_sell
        skip ~150ms of simulated latency (the ECDSA-sign work live saves).
        Idempotent within 1.5s when params haven't drifted — matches live.
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
        """True iff a valid warmup exists for these SELL params. Mirrors live's
        acceptance criteria (TTL <= 5s, price drift < 1¢, size drift < 5%);
        always pops the entry to prevent stale reuse — main.py re-arms next tick.
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

    # State-dependent FOK fail rate (live rejects cluster around thin top-of-book
    # + wide spread). Coefficients are estimates pending fill_stats.json cause
    # buckets; two safety properties hold regardless:
    #   1) Max combined rate <= ~2× the i.i.d. baseline (caps over-rejection if
    #      the state proxies are wrong).
    #   2) Book unavailable -> constant network_fail_rate (deterministic tests
    #      and degraded-feed startup).
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
        """Mirror live's FOK pre-check: reject before sleeping if the book walk
        would clearly exceed the limit. Returns False (abstains, like live) when
        the book is missing, stale (>5s), or empty — _walk_book handles those.
        """
        if self._clob_ws is None or not hasattr(self._clob_ws, "get_book"):
            return False
        book = self._clob_ws.get_book(token_id) or {}
        book_ts = float(book.get("ts", 0) or 0)
        if book_ts <= 0 or (time.time() - book_ts) > 5.0:
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
        """Run _walk_book up to _PAPER_MAX_RETRIES times with exponential backoff,
        re-reading the book each pass — like live, the 2nd attempt sees a
        post-recoil snapshot."""
        last: FillResult | None = None
        for attempt in range(1, self._PAPER_MAX_RETRIES + 1):
            last = self._walk_book(token_id, side=side, requested_price=requested_price,
                                   size_usd=size_usd)
            if last.filled:
                return last
            # Only retry the rejection class live retries: FOK "price moved".
            # Depth exhaustion / book-empty won't recover in 30ms.
            if "price moved" not in (last.reason or "").lower():
                return last
            if attempt < self._PAPER_MAX_RETRIES:
                # Exponential backoff with jitter, same shape as live's _retry_sleep.
                base = self._PAPER_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(base * random.uniform(0.8, 1.2))
        return last or FillResult(filled=False, reason="retry exhausted")

    def _scalp_residual_credit(self, residual_shares: float, fill_price: float,
                               fee_rate: float) -> float:
        """Simulated residual sweep: the held-back shares sell at the same fill
        price (live's sweep fills within seconds of the main leg), net of fee."""
        if residual_shares <= 0 or fill_price <= 0:
            return 0.0
        return residual_shares * fill_price - exit_fee_usdc(residual_shares, fill_price, fee_rate)

    def _maker_rebate_credit(self, rebate_usd: float) -> float:
        """Simulate Polymarket's daily maker-rebate pUSD credit: paper bankroll is
        delta-only, so credit the rebate here (live gets the real credit via the
        absolute balance sync, so its base-class override returns 0)."""
        return rebate_usd

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
        """Gaussian-jittered sleep, floored at latency_floor_s (the fastest measured warm
        POST RTT to the CLOB — Ireland VPN ~0.118s), plus a 4% heavy-tail spike (uniform
        2-4s, the occasional stall/retry p99). ``speedup_s`` subtracts saved work
        (warm-SELL signature) but the floor holds.
        """
        floor = self.latency_floor_s
        if random.random() < 0.04:
            latency = random.uniform(2.0, 4.0)
        else:
            latency = max(floor, random.gauss(self.latency_mean_s, self.latency_jitter_s))
        if speedup_s > 0:
            latency = max(floor, latency - speedup_s)
        await asyncio.sleep(latency)

    def _walk_book(self, token_id: str, side: str, requested_price: float,
                   size_usd: float) -> FillResult:
        """Walk book levels to a VWAP fill. Buy walks asks ascending; sell walks
        bids descending. Strict FOK: rejects when the post-latency VWAP lands on
        the wrong side of `requested_price`. Stale (>30s) or empty book rejects,
        matching live's insufficient-liquidity. Only the `clob_ws is None`
        branch fills synthetically — exercised solely by unit-test fixtures.
        """
        if self._clob_ws is None:
            return FillResult(filled=True, fill_price=requested_price, fill_size=size_usd)
        # `or {}`: a None book (never subscribed / WS reset) takes the stale-book
        # rejection below — unfilled, never an exception.
        book = (self._clob_ws.get_book(token_id) or {}) if hasattr(self._clob_ws, "get_book") else {}
        book_age = time.time() - float(book.get("ts", 0) or 0)
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
                reason=f"Insufficient book depth (remaining={remaining:.2f})",
            )

        # Strict FOK: BUY rejects if vwap > requested_price, SELL if vwap <
        # requested_price (book moved between calc and fill).
        if side == "buy":
            if vwap > requested_price:
                return FillResult(filled=False, reason="Price moved before fill (simulated)")
        else:
            if vwap < requested_price:
                return FillResult(filled=False, reason="Price moved before fill (simulated)")
        return FillResult(filled=True, fill_price=vwap, fill_size=spent)
