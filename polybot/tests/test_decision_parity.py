"""Decision parity: identical recorded streams through PaperTrader and
LiveTrader must produce bit-identical decisions.

Replays real 60s-era windows (fixtures/parity_windows.json.gz — actual
Chainlink raw/sixty/Binance reports, CLOB BBO changes and prints, extracted
from the VPS recordings by scripts/research/parity_fixture_gen.py) through the
production feed ingestion, `_compute_strike`, `_evaluate_signal_and_enter`,
and the maker ladder, once per trader. Every decision surface — gate skips,
signal evaluations, sizing, GTC/FOK order intents, cancel decisions, retire
reasons, booked fills, end-of-window bankroll — is traced at the shared
boundaries and asserted equal element-by-element.

What is EXCLUDED, by design (execution realism, owned by the fill-realism and
latency-drift checks, not decision parity):
- latency sleeps (frozen clock; sims no-op'd for speed — timing parity is
  measured, not asserted, see the ops watch),
- paper's simulated network-fail RNG (forced to 0),
- live's post-fill audits (+8s chain audit disabled on the stub),
- live's 1Hz GTC fill-poll cadence: the stub polls every maintain tick, so
  fill observation lands the same tick as paper's print matcher. Production
  live observes fills up to ~1s later; that lag affects only WHEN accrued
  fills are seen, never what rests, cancels, or books.
Live's wire is stubbed at the py-clob client; its fills are served by a
print-through oracle that mirrors the paper matcher's rule, so a rule change
in either place fails this suite loudly.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import time as _time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import polybot.main as main
from polybot.core.signal_engine import SignalEngine
from polybot.db.models import Database
from polybot.execution.base import compute_buy_vwap
from polybot.execution.maker_bid import AT_PRICE_QUEUE_SH, MakerBidManager
from polybot.execution.paper_trader import PaperTrader
from polybot.feeds.chainlink_feed import ChainlinkFeed
from polybot.feeds.market_scanner import BTCMarketScanner

FIXTURE = Path(__file__).parent / "fixtures" / "parity_windows.json.gz"

# Production-shaped config for the replay (values from settings.yaml; the
# taker variant relaxes tiers/edges so the dormant FOK path gets exercised).
LW_BASE = {
    "trading_enabled": True, "twap_zone_s": 30.0, "twap_k_min_s": 6.0,
    "sniper_min_edge": 0.05, "sniper_max_edge": 0.30, "sniper_fok_slip": 0.01,
    "require_max_tier": True, "taker_enabled": False,
}
MAKER_CFG = {
    "maker_bid_enabled": True, "maker_k_place_min": 6.0, "maker_k_place_max": 25.0,
    "maker_bankroll_frac": 0.15, "post_close_hold_s": 60.0,
    "maker_ladder": [[0.80, 0.20, 1.0], [0.65, 0.20, 1.0], [0.50, 0.20, 1.0],
                     [0.35, 0.20, 1.0], [0.20, 0.20, 1.0]],
}
VARIANTS = {
    "ladder": dict(LW_BASE),
    "taker": dict(LW_BASE, taker_enabled=True, require_max_tier=False,
                  sniper_min_edge=0.02, sniper_max_edge=0.90),
}
START_BANKROLL = 150.0


def _load_windows() -> list[dict]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as f:
        return json.load(f)["windows"]


class FrozenClock:
    def __init__(self, t0: float) -> None:
        self.now = t0

    def __call__(self) -> float:
        return self.now


class FakeClobWS:
    """Book/BBA state from the recorded BBO stream, with deterministic
    synthetic depth (micro-tape records best prices only — identical inputs
    for both traders is what parity needs, not historical L2)."""

    connected = True
    feed_delay_ms = 42.0
    last_print_gap_ts = 0.0

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock
        self.best_bid_ask: dict[str, dict] = {}
        self._books: dict[str, dict] = {}
        self._events: dict[str, asyncio.Event] = {}

    @staticmethod
    def _levels(p0: float, step: float) -> list[dict]:
        out = []
        for i in range(3):
            p = round(p0 + step * i, 4)
            if 0.0 < p < 1.0:
                out.append({"price": str(p), "size": str(150.0 + 100.0 * i)})
        return out

    def set_bba(self, token: str, bid, ask, rx: float) -> None:
        try:
            b = float(bid or 0.0)
            a = float(ask or 0.0)
        except (TypeError, ValueError):
            return
        self.best_bid_ask[token] = {"best_bid": b, "best_ask": a, "ts": rx}
        book = {"ts": rx}
        book["asks"] = self._levels(a, +0.01) if a > 0 else []
        book["bids"] = self._levels(b, -0.01) if b > 0 else []
        self._books[token] = book

    def get_book(self, token: str) -> dict:
        return self._books.get(token, {})

    def book_fresh(self, token: str, max_age_s: float = 3.0) -> bool:
        b = self._books.get(token)
        return bool(b) and (self._clock() - b["ts"]) <= max_age_s

    def both_books_fresh(self, a: str, b: str, max_age_s: float = 3.0) -> bool:
        return self.book_fresh(a, max_age_s) and self.book_fresh(b, max_age_s)

    def trade_event_for(self, token: str) -> asyncio.Event:
        return self._events.setdefault(token, asyncio.Event())


class FillOracle:
    """Print-through replica serving the live stub's get_order — the same
    strictly-below / at-price-beyond-queue rule MakerBidManager.on_print
    applies for paper, keyed by order id."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}

    def register(self, oid: str, token: str, px: float, sh: float) -> None:
        self.orders[oid] = {"token": token, "px": px, "sh": sh,
                            "filled": 0.0, "at": 0.0}

    def on_print(self, token: str, px: float, sz: float) -> None:
        for o in self.orders.values():
            if o["token"] != token:
                continue
            if px < o["px"] - 1e-9:
                o["filled"] = o["sh"]
            elif abs(px - o["px"]) <= 1e-9:
                o["at"] += sz
                credit = min(o["sh"], max(0.0, o["at"] - AT_PRICE_QUEUE_SH))
                if credit > o["filled"]:
                    o["filled"] = credit

    def matched(self, oid: str) -> float:
        return self.orders.get(oid, {}).get("filled", 0.0)


def _mk_scanner() -> BTCMarketScanner:
    sc = BTCMarketScanner.__new__(BTCMarketScanner)
    sc.min_book_depth_usd = 50.0

    async def fee_rate(token, http_client=None):
        return 0.07

    async def tick_size(token, http_client=None):
        return "0.01"

    sc.fetch_fee_rate = fee_rate
    sc.fetch_tick_size = tick_size
    return sc


async def _mk_paper(db: Database, monkeypatch) -> PaperTrader:
    trader = PaperTrader(db=db, paper_network_fail_rate=0.0)
    monkeypatch.setattr(PaperTrader, "_compute_fail_rate",
                        lambda self, token_id, side: 0.0)

    async def _no_sleep(self, speedup_s: float = 0.0):
        return None

    monkeypatch.setattr(PaperTrader, "_simulate_latency", _no_sleep)

    async def _no_gtc_sleep(self):
        return None

    monkeypatch.setattr(PaperTrader, "_simulate_gtc_latency", _no_gtc_sleep)
    monkeypatch.setattr(PaperTrader, "_record_stats",
                        staticmethod(lambda filled, side, reason="": None))
    return trader


async def _mk_live(db: Database, fakews: FakeClobWS, oracle: FillOracle,
                   monkeypatch, wire: list | None = None):
    import polybot.execution.live_trader as lt

    client = MagicMock()
    creds = {"apiKey": "k", "secret": "s", "passphrase": "p"}
    client.derive_api_key.return_value = creds
    seq = {"n": 0}

    # Wire capture happens at post_order — what the exchange actually
    # receives. Capturing at the sign step double-counts warm pre-signs that
    # are never posted (every post-fill tick warm-signs, then open_trade
    # rejects on duplicate market). The decision trace records intents at the
    # manager→trader boundary — a live-side transformation BELOW that
    # boundary (rounding, unit slip) is invisible to it, so the test also
    # asserts wire == intents.
    def post_order(signed, order_type):
        tag, args = signed
        if tag == "signed":     # GTC (create_order)
            if wire is not None:
                wire.append(("gtc", args.token_id[-8:],
                             round(args.price, 10), round(args.size, 6)))
            seq["n"] += 1
            return {"orderID": f"live-{seq['n']}", "success": True}
        if wire is not None:    # FOK (create_market_order)
            wire.append(("fok", args.token_id[-8:],
                         round(args.price, 10), round(args.amount, 2)))
        return {"success": True, "status": "matched", "orderID": "fok-1"}

    client.post_order.side_effect = post_order
    client.create_order.side_effect = lambda args: ("signed", args)
    client.create_market_order.side_effect = lambda args: ("signed-mo", args)
    client.get_order.side_effect = lambda oid: {"size_matched": oracle.matched(oid)}
    client.cancel_orders.side_effect = lambda oids: {"canceled": oids}

    with patch.dict("os.environ", {
        "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
        "POLYMARKET_FUNDER": "0x0000000000000000000000000000000000000001",
    }):
        with patch.object(lt, "_create_clob_client", return_value=client):
            trader = lt.LiveTrader(db=db)

    monkeypatch.setattr(lt, "_record_submit_latency",
                        lambda total, sign, post: None)
    monkeypatch.setattr(lt, "_update_fill_stats",
                        lambda filled, side, reason="": None)

    async def _balance(token_id):
        return 0.0

    trader._get_token_balance = _balance

    async def _settle(ev):
        return None

    trader._await_buy_settle = _settle
    trader._ws_vwap_since = (
        lambda token, since, expected, amount:
        compute_buy_vwap(fakews.get_book(token), amount))

    async def _noop():
        return None

    trader._maybe_recheck_allowance = _noop

    async def _noop_token(token_id):
        return None

    trader._cache_post_buy_balance = _noop_token
    # Post-fill chain audit is execution realism, not a decision — off.
    trader._schedule_fill_audit = None
    return trader


def _wire_traces(trader, mgr, engine, trace, monkeypatch, oracle=None):
    orig_place = trader.place_gtc_bid

    async def place(token, px, sh):
        oid = await orig_place(token, px, sh)
        trace.append(("gtc_place", token[-8:], round(px, 10), round(sh, 6),
                      oid is not None))
        if oracle is not None and oid:
            oracle.register(oid, token, px, sh)
        return oid

    trader.place_gtc_bid = place
    orig_cancel = trader.cancel_gtc

    async def cancel(oid):
        # Order ids differ per mode; the sequence position is the identity.
        trace.append(("gtc_cancel",))
        return await orig_cancel(oid)

    trader.cancel_gtc = cancel
    orig_book = trader.book_maker_fill

    async def book(**kw):
        trace.append(("book_maker", kw["side"], round(kw["price"], 10),
                      round(kw["shares_gross"], 8)))
        return await orig_book(**kw)

    trader.book_maker_fill = book
    orig_buy = trader._execute_buy

    async def buy(token, price, size, fee_rate=0.07):
        trace.append(("fok_buy", token[-8:], round(price, 10),
                      round(size, 2), round(fee_rate, 6)))
        res = await orig_buy(token, price, size, fee_rate=fee_rate)
        trace.append(("fok_result", bool(res.filled),
                      round(res.fill_price, 10), round(res.fill_size, 6)))
        return res

    trader._execute_buy = buy
    orig_retire = mgr._retire

    async def retire(reason):
        trace.append(("retire", reason))
        await orig_retire(reason)

    mgr._retire = retire
    orig_eval = engine.evaluate_twap_lock

    def ev(*a, **kw):
        sig = orig_eval(*a, **kw)
        trace.append(("signal", round(a[2], 3), sig.action, round(sig.prob, 6),
                      round(sig.edge, 9), round(sig.kelly_size, 12), sig.side,
                      sig.reason))
        return sig

    engine.evaluate_twap_lock = ev


def _reset_main_globals():
    main._strike_trusted.clear()
    main._gamma_strikes.clear()
    main._strike_logged.clear()
    main._last_skip_log.clear()
    main._last_gate_skip_state.clear()
    main._pending_eval_ctx.clear()
    main._last_snipe_log.clear()
    main._current_window_id = None
    main._last_logged_action = None
    main._invalidate_open_positions_cache()
    main._open_positions_cache = []


async def _replay(window: dict, variant: str, mode: str,
                  monkeypatch) -> tuple[list, float, list]:
    """Run one window's stream through one trader. Returns (trace, bankroll)."""
    clock = FrozenClock(window["events"][0][0] - 1.0)
    monkeypatch.setattr(_time, "time", clock)

    _reset_main_globals()
    trace: list = []
    monkeypatch.setattr(main, "_record_skip", lambda gate: trace.append(("skip", gate)))
    monkeypatch.setattr(main, "_emit_gate_skip",
                        lambda cid, key, reason, quiet=False:
                        trace.append(("gateskip", key, reason)))

    db = Database(":memory:")
    await db.initialize()
    await db.set_bankroll(START_BANKROLL)
    fakews = FakeClobWS(clock)
    feed = ChainlinkFeed()
    engine = SignalEngine(min_edge=0.04, kelly_fraction=0.08)
    scanner = _mk_scanner()
    breaker = SimpleNamespace(kelly_multiplier=1.0)

    oracle = None
    wire: list = []
    if mode == "paper":
        trader = await _mk_paper(db, monkeypatch)
    else:
        oracle = FillOracle()
        trader = await _mk_live(db, fakews, oracle, monkeypatch, wire=wire)
    trader.set_clob_ws(fakews)

    mgr = MakerBidManager(trader, feed, MAKER_CFG, paper=(mode == "paper"))
    mgr.clob_ws = fakews
    mgr.tick_fn = scanner.fetch_tick_size
    main._MAKER_MGR = mgr
    _wire_traces(trader, mgr, engine, trace, monkeypatch, oracle=oracle)

    w_ts = window["window_ts"]
    cid = window["cid"]
    close = w_ts + 300
    token_up, token_down = window["token_up"], window["token_down"]
    meta = ({"price_to_beat": window["label"]["price_to_beat"]}
            if window["serve_ptb"] else {})
    cfg = {"late_window": VARIANTS[variant], "maker": MAKER_CFG,
           "execution": {"max_book_fill_pct": 0.50, "slippage_impact_pct": 0.03}}
    window_strikes: dict[int, float] = {}
    lel = 0

    async def tick():
        nonlocal window_strikes, lel
        contract = {
            "market_id": cid, "slug": cid, "question": f"parity {cid}",
            "seconds_remaining": close - clock.now,
            "token_id_up": token_up, "token_id_down": token_down,
            "event_metadata": meta,
        }
        # Fixed marks: the lat stamps must serialize identically in both runs.
        main._loop_marks = {"wake": 0.0, "pre_eval": 0.0}
        strike, window_strikes, lel = main._compute_strike(
            cid, window_strikes, 0, lel, feed, contract)
        if strike is None:
            trace.append(("no_strike", round(close - clock.now, 3)))
        else:
            trace.append(("strike", round(strike, 6),
                          bool(main._strike_trusted.get(w_ts, False))))
            book_up = fakews.get_book(token_up)
            book_down = fakews.get_book(token_down)
            ask_up, depth_up = scanner.clob_best_ask(book_up)
            ask_down, depth_down = scanner.clob_best_ask(book_down)
            price_up = ask_up if ask_up > 0 else 0.0
            price_down = ask_down if ask_down > 0 else 0.0
            if price_up > 0 and price_down > 0:
                await main._evaluate_signal_and_enter(
                    contract, cid, engine, scanner, None, fakews, trader,
                    None, db, cfg, breaker, price_up, price_down,
                    book_up, book_down,
                    depth_up * ask_up, depth_down * ask_down,
                    strike, 0, lel, token_up, token_down,
                    max_bankroll_pct=0.80,
                    bankroll=await db.get_bankroll(),
                    chainlink_feed=feed, ghost_tracker=None)
        if mode == "live":
            mgr._last_poll = 0.0   # poll every tick — see module docstring
        await mgr.maintain()

    eval_from = w_ts + 180.0
    hold_until = close + MAKER_CFG["post_close_hold_s"] + 15.0
    for ev in window["events"]:
        rx = ev[0]
        if rx > hold_until:
            break
        clock.now = rx
        kind = ev[1]
        if kind == "l":
            feed.ingest_raw(ev[2], float(ev[3]), None, rx)
            if rx >= eval_from:
                await tick()
        elif kind == "t":
            feed.ingest_sixty(ev[2], float(ev[3]), None, rx)
        elif kind == "s":
            feed.ingest_binance(ev[2], float(ev[3]), rx)
        elif kind == "b":
            fakews.set_bba(ev[2], ev[3], ev[4], rx)
        elif kind == "p":
            try:
                px, sz = float(ev[3]), float(ev[4])
            except (TypeError, ValueError):
                continue
            mgr.on_print(ev[2], {"price": px, "size": sz})
            if oracle is not None:
                oracle.on_print(ev[2], px, sz)
    if mgr.active is not None:
        clock.now = hold_until + 1.0
        await tick()

    bankroll = await db.get_bankroll()
    await db.close()
    return trace, bankroll, wire


@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("w_idx", [0, 1, 2])
@pytest.mark.asyncio
async def test_paper_and_live_decide_identically(w_idx, variant, monkeypatch):
    windows = _load_windows()
    window = windows[w_idx]
    paper_trace, paper_bankroll, _ = await _replay(window, variant, "paper", monkeypatch)
    live_trace, live_bankroll, wire = await _replay(window, variant, "live", monkeypatch)
    assert len(paper_trace) == len(live_trace), (
        f"trace length {len(paper_trace)} vs {len(live_trace)}; first delta: "
        f"{next((i, a, b) for i, (a, b) in enumerate(zip(paper_trace, live_trace)) if a != b)}"
        if paper_trace and live_trace else "one trace empty")
    for i, (p, l) in enumerate(zip(paper_trace, live_trace)):
        assert p == l, f"decision divergence at trace[{i}]: paper={p} live={l}"
    assert paper_bankroll == pytest.approx(live_bankroll, abs=1e-9)
    # The wire must carry the traced intents unchanged — a live-side
    # transformation between the manager boundary and the exchange (rounding,
    # unit slip) is exactly the divergence the trace alone cannot see.
    intents_gtc = [(t[1], t[2], t[3]) for t in live_trace if t[0] == "gtc_place"]
    wire_gtc = [(w[1], w[2], w[3]) for w in wire if w[0] == "gtc"]
    assert wire_gtc == intents_gtc, f"wire != intents: {wire_gtc} vs {intents_gtc}"
    intents_fok = [(t[1], t[2], t[3]) for t in live_trace if t[0] == "fok_buy"]
    wire_fok = [(w[1], w[2], w[3]) for w in wire if w[0] == "fok"]
    assert wire_fok == intents_fok, f"wire != intents: {wire_fok} vs {intents_fok}"


@pytest.mark.parametrize("w_idx", [0, 1, 2])
@pytest.mark.asyncio
async def test_replay_exercises_the_ladder(w_idx, monkeypatch):
    """The parity suite is only meaningful if the replay actually arms — every
    fixture window was a real armed-and-filled window, so the paper replay
    must place rungs and book a fill."""
    windows = _load_windows()
    trace, _, _ = await _replay(windows[w_idx], "ladder", "paper", monkeypatch)
    kinds = {t[0] for t in trace}
    assert "gtc_place" in kinds, f"replay never placed rungs: {sorted(kinds)}"
    assert "retire" in kinds
