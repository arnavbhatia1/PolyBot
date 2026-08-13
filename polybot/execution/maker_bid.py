"""Lock-informed maker LADDER (signal_leg="maker_bid") — the resting twin of
the lock-dip taker.

Once the final-30s projection locks a side at the never-breached tier, a ladder
of GTC bids rests on that side and holds to resolution. Break-even win rate for
such a bid is the price paid, so deep rungs need 20-40% where shallow ones need
92-95% — hence most of the budget sits deep, where a dollar also buys 3-5x the
shares.

Lifecycle, one ladder per window: place qualifying rungs at max-tier lock (deep
rungs demand extra displacement headroom) → cancel everything when the lock
weakens below p99.5, the projection goes cold, or the window runs out → book all
accumulated fills as ONE blended position.

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
LADDER_PRICE_MIN = 0.15         # clamps on an operator price file. The band is
LADDER_PRICE_MAX = 0.95         # deep on purpose: break-even win rate equals the
                                # price paid, so a 0.20 rung needs 20% against a
                                # measured 77-96%, while 0.95 needs 95%.

DEEP_HOLD_MAX_PX = 0.85         # rungs below this survive a transient lock
                                # weakening; the wick that fills them IS the move
                                # that dips the projection, so cancelling would
                                # run away exactly when the trade appears. A
                                # flipped or cold projection still kills all.


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
            px = await self.legal_price(token_id, px)
            shares = round(usd / px, 2)
            if shares < MIN_SHARES:
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
        """A rung fills only on a print STRICTLY below its price: the seller
        walked through our level, so on the exchange our order filled before
        that lower price could print. At-price prints never count — live
        measured invisible size ahead of us at every shared level (102
        placements, zero fills), so a snapshot queue model overcounts.
        Conservative by construction; the paper bar can only be harder than
        live, never easier."""
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
            if r.get("cancelled") or px >= r["price"] - 1e-9:
                continue            # at/above our price: the queue ate it
            r["filled"] = r["shares"]

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
