"""Lock-informed maker bid (signal_leg="maker_bid") — the resting twin of the
lock-dip taker.

Once the final-30s projection says a side is locked, a GTC bid rests on that
side at a discount to the taker cap: the whipsaw panic that the FOK leg has to
race (~0.4s) fills a resting bid with zero latency, queue priority, no 250ms
taker hold — and earns liquidity-rewards points while it waits. The taker leg
is suppressed while a bid rests on the same window (never two entry paths into
one window); windows the maker can't serve (lock arrives too late, placement
fails) fall back to the taker.

Lifecycle (one active order, ever): place at lock (k within the placement
band, trusted strike) → cancel the moment the lock weakens below the p99.5
margin or the window runs out → book any fills through the trader's normal
position path. PAPER fills are print-through conservative: only prints
STRICTLY BELOW the bid count (the market traded through our level), never
prints at it — queue position is unknowable, so at-price fills would flatter
the shadow.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from polybot.core.signal_engine import TWAP_MARGIN_P995, twap_margin

logger = logging.getLogger("polybot")

MIN_NOTIONAL_USD = 1.0          # CLOB floor — below this nothing books


class MakerBidManager:
    def __init__(self, trader: Any, chainlink_feed: Any, cfg: dict,
                 paper: bool) -> None:
        self.trader = trader
        self.chainlink = chainlink_feed
        self.cfg = cfg
        self.paper = paper
        self.active: dict | None = None
        self._last_poll = 0.0

    # -- queries ----------------------------------------------------------

    def resting_on(self, window_ts: int) -> bool:
        return self.active is not None and self.active["window_ts"] == window_ts

    # -- placement (called from the fire path when locked but no dip) ------

    async def consider_placement(self, window_ts: int, market_id: str,
                                 question: str, side: str, token_id: str,
                                 bid: float, size_usd: float,
                                 snapshot: dict) -> None:
        if self.active is not None or size_usd < MIN_NOTIONAL_USD:
            return
        if not (0.0 < bid < 1.0):
            return
        shares = round(size_usd / bid, 2)
        order_id = await self.trader.place_gtc_bid(token_id, bid, shares)
        if not order_id:
            return
        self.active = {
            "window_ts": window_ts, "market_id": market_id,
            "question": question, "side": side, "token_id": token_id,
            "bid": bid, "shares": shares, "order_id": order_id,
            "placed": time.time(), "filled_shares": 0.0,
            "snapshot": snapshot,
        }
        logger.info("MAKER BID %s — $%.2f resting at %.3f on the locked side "
                    "(%.0fs left)", side, size_usd, bid,
                    window_ts + 300 - time.time())

    # -- paper fill matcher (clob_ws print hook; sync, must not raise) ------

    def on_print(self, asset_id: str, trade: dict) -> None:
        a = self.active
        if a is None or not self.paper or asset_id != a["token_id"]:
            return
        try:
            px = float(trade.get("price") or 0.0)
            sz = float(trade.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        # STRICTLY below the bid = the market traded through our level; a print
        # AT the bid may have gone entirely to earlier queue — never count it.
        if 0.0 < px < a["bid"] and sz > 0:
            a["filled_shares"] = min(a["shares"], a["filled_shares"] + sz)

    # -- lifecycle (every main-loop tick; cheap float math off the hot path) --

    async def maintain(self) -> None:
        a = self.active
        if a is None:
            return
        now = time.time()
        close = a["window_ts"] + 300
        k = close - now
        reason = None
        if k <= self.cfg["maker_k_cancel_s"]:
            reason = "window closing"
        else:
            proj = self.chainlink.projected_final_twap(close)
            if proj is None:
                # Fail CLOSED: a cold projection means the lock is
                # unverifiable — a resting order must never sit blind.
                reason = "projection cold"
            else:
                disp = proj - a["snapshot"]["strike_price"]
                held = disp >= twap_margin(TWAP_MARGIN_P995, k) if a["side"] == "Up" \
                    else -disp >= twap_margin(TWAP_MARGIN_P995, k)
                if not held:
                    reason = "lock weakened"
        if not self.paper and reason is None and now - self._last_poll >= 1.0:
            self._last_poll = now
            matched = await self.trader.poll_gtc_fill(a["order_id"])
            if matched is not None and matched > a["filled_shares"]:
                a["filled_shares"] = min(a["shares"], matched)
        # A complete fill books immediately — the position must be on the
        # books (it holds to resolution), not parked in a dead order slot.
        if reason is None and a["filled_shares"] >= a["shares"] - 1e-9:
            reason = "filled"
        if reason is not None:
            await self._retire(reason)

    async def _retire(self, reason: str) -> None:
        a, self.active = self.active, None
        if a is None:
            return
        try:
            await self.trader.cancel_gtc(a["order_id"])
        except Exception as e:
            logger.warning("MAKER CANCEL failed (%s) — exchange closes resting "
                           "orders at market close", e)
        filled = a["filled_shares"]
        if filled * a["bid"] >= MIN_NOTIONAL_USD:
            try:
                booked = await self.trader.book_maker_fill(
                    market_id=a["market_id"], question=a["question"],
                    side=a["side"], price=a["bid"], shares_gross=filled,
                    token_id=a["token_id"], indicator_snapshot=a["snapshot"])
                if booked:
                    logger.info("MAKER FILLED %s — %.1f sh at %.3f (%s)",
                                a["side"], filled, a["bid"], reason)
                    return
            except Exception:
                logger.exception("MAKER fill booking failed — reconcile manually")
            logger.warning("MAKER UNBOOKED %s — %.1f sh at %.3f could not book "
                           "(see the CRITICAL above; reconcile manually)",
                           a["side"], filled, a["bid"])
            return
        logger.info("MAKER DONE %s — %s, %.1f sh filled (below the $1 floor "
                    "books nothing)", a["side"], reason, filled)
