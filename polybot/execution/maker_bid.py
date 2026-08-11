"""Lock-informed maker LADDER (signal_leg="maker_bid") — the resting twin of
the lock-dip taker.

Once the final-30s projection locks a side at the never-breached tier, a
ladder of GTC bids rests on that side. The measured dip-depth CDF (233 locked
windows, 08-07) says panic goes DEEP when it comes at all — P(touch|locked):
0.96 → 5.6%, 0.93 → 4.7%, 0.86 → 3.9% — so a single shallow bid starves
(0/45 placements) while rungs spread across the depth capture the near-full
move at real prices. The panic seller of the winner and the momentum buyer of
the loser are the same flow on a binary book; the ladder serves both with
queue priority and zero reaction latency.

Lifecycle (one ladder, one window at a time): place all qualifying rungs at
max-tier lock (deepest rungs demand extra displacement headroom — deep fills
happen in violent windows where breach risk is conditionally elevated) →
cancel everything the moment the lock weakens below p99.5, the projection
goes cold, or the window runs out → book ALL accumulated fills as ONE
position at the blended price (holds to resolution). PAPER fills are
print-through conservative: only prints STRICTLY BELOW a rung count.

Rung prices auto-recalibrate nightly from the trailing tape's dip CDF
(memory/state/maker_ladder.json, hard-clamped to [0.85, 0.975]) — the engine
gets better as data accrues; the fractions and headroom rules stay frozen.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from polybot.core.signal_engine import TWAP_MARGIN_P995, twap_margin
from polybot.paths import MAKER_LADDER_PATH

logger = logging.getLogger("polybot")

MIN_NOTIONAL_USD = 1.0          # CLOB floor — below this nothing books
LADDER_PRICE_MIN = 0.92         # hard clamps on the nightly recalibration — no data
LADDER_PRICE_MAX = 0.95         # artifact may quote outside these. Measured 08-10:
                                # rungs at 0.85-0.90 filled 0 times in 285 placements
                                # (nothing trades there once a window is locked), and
                                # the old floor let a self-defeating loop run — rare
                                # deep dips -> deep quantiles -> deeper rungs -> never
                                # touched. The ceiling is 0.95, not 0.955: the p99.5
                                # cap is 0.955 and the tick is 0.01 here, so 0.95 is
                                # the highest tick-legal price inside the edge floor.


PC_VERIFY_GRACE_S = 5.0         # how long the post-close phase waits for the
                                # closing boundary report before giving up. It
                                # lands p50 1.71s / p90 2.2s / p99 2.9s after the
                                # boundary, so anything shorter retires before
                                # the outcome can possibly be known.


class MakerBidManager:
    def __init__(self, trader: Any, chainlink_feed: Any, cfg: dict,
                 paper: bool) -> None:
        self.trader = trader
        self.chainlink = chainlink_feed
        self.cfg = cfg
        self.paper = paper
        self.active: dict | None = None
        self._last_poll = 0.0
        self._ladder_cache: tuple[float, list] | None = None  # (mtime, ladder)

    # -- queries ----------------------------------------------------------

    def resting_on(self, window_ts: int) -> bool:
        return self.active is not None and self.active["window_ts"] == window_ts

    def holding_token(self) -> str | None:
        """Token this ladder still has orders on, so the WS keeps it subscribed.

        The post-close phase rests past the window's end, but rotation used to
        unsubscribe the closed window's tokens the moment the next contract was
        discovered — leaving a live bid on a token we no longer listen to. Paper
        then cannot see a fill at all (0 of 1,015 prints for a closed window
        reached us), and live loses its print stream.
        """
        return self.active["token_id"] if self.active else None

    def ladder(self) -> list:
        """[[price, budget_frac, min_headroom_mult], ...] — the nightly
        recalibration file wins when present (clamped); config is the seed."""
        seed = self.cfg.get("maker_ladder") or [[0.96, 0.40, 1.0],
                                                [0.92, 0.35, 1.0],
                                                [0.87, 0.25, 1.5]]
        try:
            mtime = MAKER_LADDER_PATH.stat().st_mtime
            if self._ladder_cache and self._ladder_cache[0] == mtime:
                return self._ladder_cache[1]
            data = json.loads(MAKER_LADDER_PATH.read_text())
            rungs = []
            for (px, frac, hm), seed_r in zip(data.get("ladder", []), seed):
                px = min(LADDER_PRICE_MAX, max(LADDER_PRICE_MIN, float(px)))
                # fractions + headroom stay FROZEN — only prices recalibrate
                rungs.append([px, seed_r[1], seed_r[2]])
            if len(rungs) == len(seed):
                self._ladder_cache = (mtime, rungs)
                return rungs
        except (OSError, ValueError, KeyError):
            pass
        return seed

    # -- placement (called from the fire path at max-tier lock) -------------

    async def consider_placement(self, window_ts: int, market_id: str,
                                 question: str, side: str, token_id: str,
                                 budget_usd: float, headroom_mult: float,
                                 snapshot: dict) -> None:
        """headroom_mult = |disp| / max-tier margin at placement time — deep
        rungs only arm when the lock has real slack (deep fills concentrate
        in violent windows)."""
        if self.active is not None or budget_usd < MIN_NOTIONAL_USD:
            return
        rungs = []
        for px, frac, need in self.ladder():
            if headroom_mult < need:
                continue
            usd = round(budget_usd * frac, 2)
            if usd < MIN_NOTIONAL_USD or not (0.0 < px < 1.0):
                continue
            shares = round(usd / px, 2)
            order_id = await self.trader.place_gtc_bid(token_id, px, shares)
            if order_id:
                rungs.append({"price": px, "shares": shares,
                              "order_id": order_id, "filled": 0.0})
        if not rungs:
            return
        self.active = {
            "window_ts": window_ts, "market_id": market_id,
            "question": question, "side": side, "token_id": token_id,
            "rungs": rungs, "placed": time.time(), "snapshot": snapshot,
        }
        logger.info("MAKER LADDER %s — %s resting on the locked side (%.0fs left)",
                    side, "/".join(f"${r['shares'] * r['price']:.0f}@{r['price']:.2f}"
                                   for r in rungs),
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
        if not (0.0 < px and sz > 0):
            return
        # STRICTLY below a rung = the market traded through that level; a
        # print AT the rung may have gone entirely to earlier queue.
        for r in a["rungs"]:
            if px < r["price"]:
                r["filled"] = min(r["shares"], r["filled"] + sz)

    # -- lifecycle (every main-loop tick; cheap float math off the hot path) --

    def certain_winner(self, window_ts: int) -> str | None:
        """The window's SETTLED winner from the two official TWAP boundary
        captures — or None if either is missing or untrusted.

        This is not a projection. `final >= strike` (tie → Up) is the exact rule
        Polymarket resolves on, and both operands are the same boundary values
        our strike capture already verifies bit-exact. Fail closed: 5-14
        boundaries/day never arrive, and a fabricated winner would rest a bid on
        a token that pays $0.
        """
        cl, close = self.chainlink, window_ts + 300
        for b in (window_ts, close):
            if not (cl.boundary_captured(b) and cl.strike_reliable(b)):
                return None
        strike, final = cl.get_strike(window_ts), cl.get_strike(close)
        if strike is None or final is None:
            return None
        return "Up" if final >= strike else "Down"

    async def _place_post_close_rung(self, a: dict) -> None:
        """One extra bid, armed only once the winner is SETTLED FACT.

        Pre-close this price is illegal — 0.999 − 0.995 is far inside the 4¢ edge
        floor. Post-close the floor does not apply: the tier probability was a
        tail bound on an unfinished average, and this is a finished one, so the
        0.5¢ is certain rather than expected. Makers pay no fee, so it is gross.
        """
        px = float(self.cfg.get("post_close_price", 0.995))
        frac = float(self.cfg.get("post_close_budget_frac", 0.40))
        budget = sum(r["shares"] * r["price"] for r in a["rungs"])
        usd = round(budget * frac, 2)
        a["pc_placed"] = True          # set first: one attempt per window, ever
        if usd < MIN_NOTIONAL_USD or not (0.0 < px < 1.0):
            return
        shares = round(usd / px, 2)
        order_id = await self.trader.place_gtc_bid(a["token_id"], px, shares)
        if not order_id:
            return
        a["rungs"].append({"price": px, "shares": shares,
                           "order_id": order_id, "filled": 0.0})
        logger.info("MAKER POST-CLOSE %s — $%.0f@%.3f on the settled winner",
                    a["side"], shares * px, px)

    async def maintain(self) -> None:
        a = self.active
        if a is None:
            return
        now = time.time()
        close = a["window_ts"] + 300
        k = close - now
        reason = None
        pc_on = bool(self.cfg.get("post_close_enabled"))
        if now >= close:
            # POST-CLOSE CERTAINTY PHASE. The market keeps accepting orders for
            # minutes after the close (verified live at close+143s), and this is
            # the only part of the window where makers win every day — the
            # outcome is settled while sellers who haven't read it yet dump the
            # winner. The projection is retired here; the boundary captures rule.
            if not pc_on:
                reason = "window closing"
            elif now - close > float(self.cfg.get("post_close_s", 120.0)):
                reason = "post-close window over"
            else:
                winner = self.certain_winner(a["window_ts"])
                if winner is None:
                    # The closing boundary report lands ~1.7s after the boundary
                    # (p90 2.2s, p99 2.9s), so "not yet" is the normal state for
                    # the first seconds — keep resting on the max-tier locked
                    # side and wait. Only give up once the grace is spent, which
                    # is the real delivery hole (5-14 boundaries/day).
                    if now - close > PC_VERIFY_GRACE_S:
                        reason = "outcome unverified"      # fail closed
                elif winner != a["side"]:
                    # The lock was wrong. Never observed at max tier in 718
                    # locked windows, but a resting bid on a $0 token is the one
                    # unbounded loss this leg has, so it is checked every tick.
                    reason = "lock missed the winner"
                elif not a.get("pc_placed"):
                    await self._place_post_close_rung(a)
        elif k <= self.cfg["maker_k_cancel_s"]:
            # Hold through the close only when the post-close phase will take
            # over; otherwise this is still the cancel point.
            if not pc_on:
                reason = "window closing"
        else:
            proj = self.chainlink.projected_final_twap(close)
            if proj is None:
                # Fail CLOSED: a cold projection means the lock is
                # unverifiable — resting orders must never sit blind.
                reason = "projection cold"
            else:
                disp = proj - a["snapshot"]["strike_price"]
                held = disp >= twap_margin(TWAP_MARGIN_P995, k) if a["side"] == "Up" \
                    else -disp >= twap_margin(TWAP_MARGIN_P995, k)
                if not held:
                    reason = "lock weakened"
        if not self.paper and reason is None and now - self._last_poll >= 1.0:
            self._last_poll = now
            for r in a["rungs"]:
                matched = await self.trader.poll_gtc_fill(r["order_id"])
                if matched is not None and matched > r["filled"]:
                    r["filled"] = min(r["shares"], matched)
        # Every rung fully filled -> book now; the position needs to be on
        # the books (it holds to resolution), not parked in dead order slots.
        if reason is None and all(r["filled"] >= r["shares"] - 1e-9
                                  for r in a["rungs"]):
            reason = "filled"
        if reason is not None:
            await self._retire(reason)

    async def _retire(self, reason: str) -> None:
        a, self.active = self.active, None
        if a is None:
            return
        for r in a["rungs"]:
            try:
                await self.trader.cancel_gtc(r["order_id"])
            except Exception as e:
                logger.warning("MAKER CANCEL failed (%s) — exchange closes "
                               "resting orders at market close", e)
        filled = sum(r["filled"] for r in a["rungs"])
        notional = sum(r["filled"] * r["price"] for r in a["rungs"])
        if filled > 0 and notional >= MIN_NOTIONAL_USD:
            vwap = round(notional / filled, 4)
            try:
                booked = await self.trader.book_maker_fill(
                    market_id=a["market_id"], question=a["question"],
                    side=a["side"], price=vwap, shares_gross=filled,
                    token_id=a["token_id"], indicator_snapshot=a["snapshot"])
                if booked:
                    logger.info("MAKER FILLED %s — %.1f sh at %.3f blended (%s)",
                                a["side"], filled, vwap, reason)
                    return
            except Exception:
                logger.exception("MAKER fill booking failed — reconcile manually")
            logger.warning("MAKER UNBOOKED %s — %.1f sh at %.3f could not book "
                           "(see the CRITICAL above; reconcile manually)",
                           a["side"], filled, vwap)
            return
        logger.info("MAKER DONE %s — %s, %.1f sh filled (below the $1 floor "
                    "books nothing)", a["side"], reason, filled)
