"""Lock-informed maker LADDER (signal_leg="maker_bid") — the resting twin of
the lock-dip taker.

Once the final-30s projection locks a side at the never-breached tier, a
DEEP-WEIGHTED ladder of GTC bids rests on that side and holds to resolution.
Break-even win rate for such a bid is exactly the price paid, so the edge
widens the deeper the rung sits: the market's most profitable formable maker
(31% ROC, t 3.19, 5/5 positive days) realizes +61¢/sh at 0.09-0.21 and +41¢/sh
at 0.37 while LOSING 5.9¢/sh at 0.95, and its 124-of-814 window count is simply
the ~15% rate at which a deep bid gets wicked, not a filter. So most of the
budget sits deep, where each dollar also buys 3-5x the shares; shallow rungs
stay small because they fill often for a thin-to-negative margin. The panic
seller of the winner and the momentum buyer of the loser are the same flow on a
binary book; the ladder serves both with queue priority and zero latency.

Lifecycle (one ladder, one window at a time): place all qualifying rungs at
max-tier lock (deepest rungs demand extra displacement headroom — deep fills
happen in violent windows where breach risk is conditionally elevated) →
cancel everything the moment the lock weakens below p99.5, the projection
goes cold, or the window runs out → book ALL accumulated fills as ONE
position at the blended price (holds to resolution). PAPER fills are
print-through conservative: only prints STRICTLY BELOW a rung count.

Rung prices live in settings.yaml and are set by BREAK-EVEN economics (see
LADDER_PRICE_MIN below), not by dip frequency. The nightly job only REPORTS the
trailing dip CDF; nothing overwrites the geometry. An operator-supplied
memory/state/maker_ladder.json still overrides prices if present, clamped to
[0.15, 0.95] and only when its rung count matches the config seed.

After the close the ladder does NOT die: the post-close certainty phase holds it
for post_close_s, verifies the winner from the two official TWAP boundary
captures (not the projection), and arms post_close_ladder on the settled side.
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
LADDER_PRICE_MIN = 0.15         # hard clamps on the nightly recalibration — no data
LADDER_PRICE_MAX = 0.95         # artifact may quote outside these.
                                #
                                # The band is DEEP on purpose. Break-even win
                                # rate for a resting buy held to resolution is
                                # exactly the price paid, so the edge widens as
                                # the rung goes deeper: the market's most
                                # profitable formable maker (31% ROC, t 3.19,
                                # 5/5 days) realizes +61c/sh at 0.09-0.21 and
                                # +41c/sh at 0.37, while LOSING 5.9c/sh at 0.95.
                                # Its 124-of-814 window count is not selectivity
                                # — it is the 15% rate at which a deep bid gets
                                # wicked. A shallow ladder needs 92-95% just to
                                # break even and collects pennies when it wins;
                                # a deep one needs 20-40% and collects most of
                                # the dollar. Deep rungs still demand extra lock
                                # headroom, and every rung dies the moment the
                                # lock weakens below p99.5.


DEEP_HOLD_MAX_PX = 0.85         # rungs BELOW this price survive a transient lock
                                # weakening; at/above it they pull. The wick that
                                # fills a deep rung IS the spot move that pushes
                                # the projection under the p99.5 margin, so a
                                # blanket cancel runs away at exactly the moment
                                # the trade appears. A deep rung is paid for that
                                # risk (break-even 20-35% against a measured
                                # 77-96% win rate) and wicks revert; a 0.90 rung
                                # needs 90% and is not. Everything still dies if
                                # the projection FLIPS side or goes cold.

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
        self.pending: dict | None = None   # post-close intent for a closing window
        self._last_poll = 0.0
        self._ladder_cache: tuple[float, list] | None = None  # (mtime, ladder)

    # -- queries ----------------------------------------------------------

    def resting_on(self, window_ts: int) -> bool:
        return self.active is not None and self.active["window_ts"] == window_ts

    def holding_tokens(self) -> set[str]:
        """Tokens the WS must stay subscribed to.

        The post-close phase rests past the window's end, but rotation used to
        unsubscribe the closed window's tokens the moment the next contract was
        discovered — leaving a live bid on a token we no longer listen to. Paper
        then cannot see a fill at all (0 of 1,015 prints for a closed window
        reached us), and live loses its print stream.

        BOTH sides of a pending window are kept: the winner is unknown until the
        closing boundary lands ~2s later, and going deaf in that gap would break
        paper/live parity silently — live still polls its fills over REST.
        """
        out: set[str] = set()
        if self.active:
            out.add(self.active["token_id"])
        if self.pending:
            out.update(t for t in (self.pending["token_up"],
                                   self.pending["token_down"]) if t)
        return out

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
                                 snapshot: dict, pc_budget: float = 0.0) -> None:
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
            # Post-close is sized off BANKROLL, not off this ladder's Kelly
            # budget — a settled outcome is not a probabilistic bet, and
            # inheriting a fraction of fractional Kelly made every fill ~$2.
            "pc_budget": pc_budget,
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
            if not r.get("cancelled") and px < r["price"]:
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

    async def _place_post_close_ladder(self, a: dict) -> None:
        """Bids on the SETTLED winner, armed only once the outcome is fact.

        Pre-close these prices are illegal — all of them sit inside the 4¢ edge
        floor. Post-close the floor does not apply: the tier probability was a
        tail bound on an unfinished average and this is a finished one, so the
        margin is certain rather than expected. Makers pay no fee, so it is gross.

        Geometry measured 08-11 over 150 windows / 1,364 post-close sales of the
        winner. Sellers hit 0.990 (p05 through p50 all 0.990) and supply is
        ~$475/window — far more than the bankroll can absorb — so the top rung
        takes MARGIN over queue position: resting 0.995 wins the race and halves
        the 1¢. The deep rungs are the fat tail; 8 of the 1,364 sales printed at
        or below 0.95 and returned 22% against 1.01%, and a resting bid that
        never fills costs nothing.
        """
        rungs = self.cfg.get("post_close_ladder") or [[0.99, 1.0]]
        budget = a.get("pc_budget") or 0.0     # bankroll-sized, set at arm time
        if budget <= 0.0:                      # fallback: fraction of the ladder
            budget = (sum(r["shares"] * r["price"] for r in a["rungs"])
                      * float(self.cfg.get("post_close_budget_frac", 0.40)))
        a["pc_placed"] = True          # set first: one attempt per window, ever
        placed = []
        for px, frac in rungs:
            px = float(px)
            usd = round(budget * float(frac), 2)
            if usd < MIN_NOTIONAL_USD or not (0.0 < px < 1.0):
                continue
            shares = round(usd / px, 2)
            order_id = await self.trader.place_gtc_bid(a["token_id"], px, shares)
            if not order_id:
                continue
            a["rungs"].append({"price": px, "shares": shares,
                               "order_id": order_id, "filled": 0.0})
            placed.append((px, shares))
        if placed:
            logger.info("MAKER POST-CLOSE %s — %s on the settled winner",
                        a["side"], "/".join(f"${s * p:.0f}@{p:.3f}"
                                            for p, s in placed))

    def arm_post_close(self, window_ts: int, market_id: str, question: str,
                       token_up: str, token_down: str, budget_usd: float,
                       snapshot: dict) -> None:
        """Remember a closing window so post-close can arm WITHOUT a pre-close
        ladder.

        The outcome is settled fact in every window, but the pre-close ladder
        only rests on the few that lock at max tier with k in [3,25]s — so tying
        post-close to it threw away all but a handful of windows a day. Called
        every tick near the close; dedupes by window_ts.
        """
        if self.active is not None or budget_usd < MIN_NOTIONAL_USD:
            return
        if self.pending is not None and self.pending["window_ts"] >= window_ts:
            return
        self.pending = {"window_ts": window_ts, "market_id": market_id,
                        "question": question, "token_up": token_up,
                        "token_down": token_down, "budget": budget_usd,
                        "snapshot": snapshot}

    async def _promote_pending(self) -> None:
        """Turn a pending intent into an active post-close-only ladder, once the
        settled winner is known. Fails closed exactly like the ladder path."""
        p = self.pending
        if p is None or not self.cfg.get("post_close_enabled"):
            return
        now, close = time.time(), p["window_ts"] + 300
        if now < close:
            return
        if now - close > float(self.cfg.get("post_close_s", 90.0)):
            self.pending = None                    # the phase already expired
            return
        winner = self.certain_winner(p["window_ts"])
        if winner is None:
            # Normal for the first ~2s (the closing report lands p90 2.2s late);
            # a real delivery hole gives up rather than guess a $0 token.
            if now - close > PC_VERIFY_GRACE_S:
                self.pending = None
            return
        self.pending = None
        self.active = {
            "window_ts": p["window_ts"], "market_id": p["market_id"],
            "question": p["question"], "side": winner,
            "token_id": p["token_up"] if winner == "Up" else p["token_down"],
            "rungs": [], "placed": now, "snapshot": p["snapshot"],
            "pc_budget": p["budget"],
        }

    async def maintain(self) -> None:
        if self.active is None:
            await self._promote_pending()
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
                    await self._place_post_close_ladder(a)
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
                signed = disp if a["side"] == "Up" else -disp
                if signed <= 0.0:
                    # Projection now points at the OTHER side — nothing is worth
                    # holding, at any depth.
                    reason = "projection flipped"
                elif signed < twap_margin(TWAP_MARGIN_P995, k):
                    # Weakened but still ours: shallow rungs pull, deep rungs stay.
                    if await self._prune_shallow(a):
                        reason = "lock weakened"
        if not self.paper and reason is None and now - self._last_poll >= 1.0:
            self._last_poll = now
            for r in a["rungs"]:
                matched = await self.trader.poll_gtc_fill(r["order_id"])
                if matched is not None and matched > r["filled"]:
                    r["filled"] = min(r["shares"], matched)
        # Every rung fully filled -> book now; the position needs to be on
        # the books (it holds to resolution), not parked in dead order slots.
        live = [r for r in a["rungs"] if not r.get("cancelled")]
        if reason is None and live and all(r["filled"] >= r["shares"] - 1e-9
                                          for r in live):
            reason = "filled"
        if reason is not None:
            await self._retire(reason)

    async def _prune_shallow(self, a: dict) -> bool:
        """Cancel rungs at/above DEEP_HOLD_MAX_PX; True when none are left live.

        Accumulated fills are preserved and still book at retire — only the
        resting remainder is pulled.
        """
        for r in a["rungs"]:
            if r.get("cancelled") or r["price"] < DEEP_HOLD_MAX_PX:
                continue
            r["cancelled"] = True
            try:
                await self.trader.cancel_gtc(r["order_id"])
            except Exception as e:
                logger.warning("MAKER CANCEL failed (%s)", e)
        return all(r.get("cancelled") for r in a["rungs"])

    async def _retire(self, reason: str) -> None:
        a, self.active = self.active, None
        if a is None:
            return
        for r in a["rungs"]:
            if r.get("cancelled"):
                continue
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
