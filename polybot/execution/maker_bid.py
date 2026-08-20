"""Projection-side deep maker LADDER (signal_leg="deep_proj").

In the final minute the book prices off spot while the resolution is a 60s
average that is mostly already written. The ladder rests GTC bids on the
projection-favored side at prices where being wrong is priced in (break-even
win rate equals the price paid), and panic crossing the spread fills them.

Rungs carry per-rung sign-quality requirements (`need` = multiple of the
p99.5 projection error at the current k). The floor's only job is refusing
photo-finishes — under the 60s-rule tables the sign named the winner in
873/873 armed windows at need 0.5 (engine-true 08-14..17); the rung price
carries the risk (see WALLETS.md / RESEARCH.md for provenance).

Lifecycle, one ladder per window: place when the projection clears the
ladder's minimum need with k in [place_min, place_max] → cancel everything
the moment the floor breaks or the projection goes cold → after the close,
keep resting for post_close_hold_s ONLY while the boundary-verified winner
equals our side → book all accumulated fills as ONE blended position.

Paper fills are print-through: a rung fills only when the tape prints STRICTLY
below its price — live proved that at any shared price level we are behind
size the book snapshot cannot see (102 live placements, zero fills against a
~290k-share wall), so at-price prints never count. Place and cancel both pay
the measured GTC round trip.

Rung prices live in settings.yaml, set by break-even economics rather than dip
frequency; the nightly job only reports the dip CDF. An operator
memory/state/maker_ladder.json may override prices, clamped and only when its
rung count matches the seed.
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
MIN_SHARES = 5.0                # exchange minimum order size; a 2.49-share rung
                                # is rejected outright ("lower than the minimum: 5")
AT_PRICE_QUEUE_SH = 135.0       # measured median resting size per deep level
                                # (live book watcher, 49 windows / 30k levels,
                                # 08-17 — up 2.5x from 08-14's 55 as the deep-
                                # buyer cohort grew). Paper credits an AT-price
                                # print only beyond this much typical queue
                                # ahead of us — a live-measured constant, never
                                # a book snapshot (snapshot queue models are
                                # BANNED; they built the 77-fills/day fantasy).
LADDER_PRICE_MIN = 0.15         # clamps on an operator price file. The band is
LADDER_PRICE_MAX = 0.95         # deep on purpose: break-even win rate equals the
                                # price paid, so a 0.20 rung needs 20% against a
                                # measured 77-96%, while 0.95 needs 95%.

PC_VERIFY_GRACE_S = 5.0         # wait this long for the closing boundary report
                                # (lands p50 1.71s / p99 2.9s after the boundary)
                                # before failing closed post-close.


class MakerBidManager:
    def __init__(self, trader: Any, chainlink_feed: Any, cfg: dict,
                 paper: bool) -> None:
        self.trader = trader
        self.chainlink = chainlink_feed
        self.cfg = cfg
        self.paper = paper
        self.active: dict | None = None
        # Set by main: (active, shares, vwap, reason) -> None. A maker fill IS an
        # entry, so it gets the same banner + Discord ping a taker entry does.
        self.on_fill: Any = None
        # Set by main to market_scanner.fetch_tick_size.
        self.tick_fn: Any = None
        # Set by main to the CLOB feed — paper's whole fill mechanism is the
        # print stream, so a reconnect while we rested poisons the sample.
        self.clob_ws: Any = None
        self._last_poll = 0.0
        self._ladder_cache: tuple[float, list] | None = None  # (mtime, ladder)

    async def legal_price(self, token_id: str, px: float) -> float:
        """Round DOWN to the tick and clamp to the exchange's valid range.

        The valid range is [tick, 1 - tick], so a 0.01 tick caps bids at 0.99
        and rejects 0.992 outright (measured live). Rounding DOWN can only
        improve margin.
        """
        if self.tick_fn is None:
            return px
        try:
            tick = float(await self.tick_fn(token_id))
        except Exception:
            return px
        if tick <= 0:
            return px
        snapped = round(int(round(px / tick, 6)) * tick, 10)
        return max(tick, min(snapped, round(1.0 - tick, 10)))

    # -- queries ----------------------------------------------------------

    def resting_on(self, window_ts: int) -> bool:
        return self.active is not None and self.active["window_ts"] == window_ts

    def holding_tokens(self) -> set[str]:
        """Tokens the WS must stay subscribed to, so rotation cannot unsubscribe a
        token we still have a bid on — that blinds the paper fill matcher."""
        return {self.active["token_id"]} if self.active else set()

    def ladder(self) -> list:
        """[[price, budget_frac, min_headroom_mult], ...] — the nightly
        recalibration file wins when present (clamped); config is the seed."""
        seed = self.cfg.get("maker_ladder") or [[0.80, 0.20, 2.0],
                                                [0.65, 0.20, 2.0],
                                                [0.50, 0.20, 2.0],
                                                [0.35, 0.20, 2.0],
                                                [0.20, 0.20, 2.0]]
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

    # -- placement (called from the fire path in the late zone) -------------

    async def consider_placement(self, window_ts: int, market_id: str,
                                 question: str, side: str, token_id: str,
                                 budget_usd: float, headroom_mult: float,
                                 snapshot: dict) -> None:
        """headroom_mult = |disp| / p99.5-error at placement time — a rung
        arms only when the sign clears its `need` multiple of that error."""
        if self.active is not None or budget_usd < MIN_NOTIONAL_USD:
            return
        rungs = []
        for px, frac, need in self.ladder():
            if headroom_mult < need:
                continue
            usd = round(budget_usd * frac, 2)
            if usd < MIN_NOTIONAL_USD or not (0.0 < px < 1.0):
                continue
            px = await self.legal_price(token_id, px)
            shares = round(usd / px, 2)
            if shares < MIN_SHARES:
                # Loud: a starved rung is silent lost geometry — the breaker
                # or a thin bankroll can strip the top rung and nothing else
                # would ever say so.
                logger.info("MAKER RUNG SKIPPED at %.2f — %.2f sh is under the "
                            "%.0f-share exchange minimum (budget $%.2f)",
                            px, shares, MIN_SHARES, usd)
                continue
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
        logger.info("MAKER LADDER %s — resting $%.0f on the locked side with "
                    "%.0fs left (%s)", side,
                    sum(r["shares"] * r["price"] for r in rungs),
                    window_ts + 300 - time.time(),
                    " · ".join(f"${r['shares'] * r['price']:.0f} at {r['price']:.2f}"
                               for r in rungs))

    # -- paper fill matcher (clob_ws print hook; sync, must not raise) ------

    def on_print(self, asset_id: str, trade: dict) -> None:
        """A print STRICTLY below a rung fills it in full: the seller walked
        through our level, so on the exchange our order filled before that
        lower price could print. A print AT a rung's price fills only the
        volume beyond AT_PRICE_QUEUE_SH — the live-measured typical queue
        ahead of a fresh joiner at a deep level — accumulated across the
        window's at-price prints. Both flows are tracked separately
        (filled/filled_at_px) so live fills can recalibrate the constant."""
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
        for r in a["rungs"]:
            if px < r["price"] - 1e-9:
                r["filled"] = r["shares"]
            elif abs(px - r["price"]) <= 1e-9:
                seen = r.get("at_px_vol", 0.0) + sz
                r["at_px_vol"] = seen
                credit = min(r["shares"], max(0.0, seen - AT_PRICE_QUEUE_SH))
                if credit > r["filled"]:
                    r["filled"] = credit
                    r["filled_at_px"] = True

    # -- lifecycle (every main-loop tick; cheap float math off the hot path) --

    def min_need(self) -> float:
        """The ladder's noise floor: the smallest per-rung `need` (multiple of
        the p99.5 projection error at the current k) — below it the sign is
        inside its own error and picks nothing. Photo-finish windows cannot
        clear any fraction of the error scale, so the floor refuses exactly
        the windows that pay nobody."""
        return min((float(r[2]) for r in self.ladder()), default=0.5)

    def certain_winner(self, window_ts: int) -> str | None:
        """The window's SETTLED winner from the two official TWAP boundary
        captures — or None if either is missing or untrusted. Fail closed: 5-14
        boundaries/day never arrive, and a fabricated winner would keep a bid
        resting on a token that pays $0."""
        cl, close = self.chainlink, window_ts + 300
        for b in (window_ts, close):
            if not (cl.boundary_captured(b) and cl.strike_reliable(b)):
                return None
        strike, final = cl.get_strike(window_ts), cl.get_strike(close)
        if strike is None or final is None:
            return None
        return "Up" if final >= strike else "Down"

    async def maintain(self) -> None:
        a = self.active
        if a is None:
            return
        now = time.time()
        close = a["window_ts"] + 300
        k = close - now
        reason = None
        if now >= close:
            # POST-CLOSE HOLD: 23% of the reference wallet's pnl lands just
            # after the close, at deep prices where no queue walls exist. The
            # projection is retired here — the boundary-verified winner rules,
            # re-checked every tick, and anything unverified fails closed.
            if now - close > float(self.cfg.get("post_close_hold_s", 0.0)):
                reason = "post-close hold over"
            else:
                winner = self.certain_winner(a["window_ts"])
                if winner is None:
                    # The closing report lands ~1.7s late (p99 2.9s): "not yet"
                    # is normal at first; a real delivery hole fails closed.
                    if now - close > PC_VERIFY_GRACE_S:
                        reason = "outcome unverified"
                elif winner != a["side"]:
                    reason = "lock missed the winner"
        else:
            proj = self.chainlink.projected_final_twap(close, bridged=True)
            if proj is None:
                # Fail CLOSED: a cold projection means the sign is
                # unverifiable — resting orders must never sit blind.
                reason = "projection cold"
            else:
                disp = proj - a["snapshot"]["strike_price"]
                signed = disp if a["side"] == "Up" else -disp
                if signed < self.min_need() * twap_margin(TWAP_MARGIN_P995, k):
                    # Flipped, or inside the sign's own noise — either way the
                    # side is no longer picked by anything. Nothing holds:
                    # the same floor that gates placement kills the rest.
                    reason = ("projection flipped" if signed <= 0.0
                              else "sign inside noise")
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
        a = self.active
        if a is None:
            return
        try:
            try:
                for r in a["rungs"]:
                    try:
                        await self.trader.cancel_gtc(r["order_id"])
                    except Exception as e:
                        logger.warning("MAKER CANCEL failed (%s) — exchange closes "
                                       "resting orders at market close", e)
                if not self.paper:
                    # A live fill can land inside the 1Hz poll gap or the cancel
                    # round trip itself — re-read each order's final matched size
                    # so the booking can never run short of what the wallet holds.
                    for r in a["rungs"]:
                        try:
                            matched = await self.trader.poll_gtc_fill(r["order_id"])
                        except Exception as e:
                            logger.warning("MAKER POLL failed (%s) — booking what "
                                           "is already known filled", e)
                            continue
                        if matched is not None and matched > r["filled"]:
                            r["filled"] = min(r["shares"], matched)
            finally:
                # Shutdown cancels this mid-round-trip: shares already filled are
                # real on the exchange and must reach the DB before we unwind.
                await self._book(a, reason)
        finally:
            self.active = None

    async def _book(self, a: dict, reason: str) -> None:
        filled = sum(r["filled"] for r in a["rungs"])
        notional = sum(r["filled"] * r["price"] for r in a["rungs"])
        if filled > 0 and notional >= MIN_NOTIONAL_USD:
            # Unrounded: the booking re-derives the debit as shares x price, so
            # a 4dp vwap makes it disagree with what the rungs actually cost.
            vwap = notional / filled
            # Mark the sample when the print stream had a hole under us.
            gap_ts = getattr(self.clob_ws, "last_print_gap_ts", None)
            if isinstance(a["snapshot"], dict):
                a["snapshot"]["print_gap"] = (None if gap_ts is None
                                              else int(gap_ts >= a["placed"]))
            try:
                booked = await self.trader.book_maker_fill(
                    market_id=a["market_id"], question=a["question"],
                    side=a["side"], price=vwap, shares_gross=filled,
                    token_id=a["token_id"], indicator_snapshot=a["snapshot"])
                if booked:
                    if self.on_fill is not None:
                        try:
                            self.on_fill(a, filled, vwap, reason)
                        except Exception:
                            logger.exception("maker fill banner failed (fill IS booked)")
                    else:
                        logger.info("MAKER FILLED %s — %.1f sh at %.3f blended (%s)",
                                    a["side"], filled, vwap, reason)
                    return
            except Exception:
                logger.exception("MAKER fill booking failed — reconcile manually")
            logger.warning("MAKER UNBOOKED %s — %.1f sh at %.3f could not book "
                           "(see the CRITICAL above; reconcile manually)",
                           a["side"], filled, vwap)
            return
        logger.info("MAKER OFF %s — nobody sold into the bid (%s)",
                    a["side"], reason)
