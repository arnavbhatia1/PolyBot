"""MakerBidManager — the lock-informed resting LADDER (execution/maker_bid.py).

Locks the money-path invariants: one ladder at a time, deep rungs demand
displacement headroom, cancel-all the moment the lock weakens / projection
goes cold / window closes, paper fills ONLY on prints strictly below a rung
(live measured invisible size ahead of us at every shared price level), all
fills book as ONE blended position at/above the $1 floor, and the nightly
price file is clamped and never touches the frozen fractions.
"""
import asyncio
import json
import time

import pytest

from polybot.execution import maker_bid as mb
from polybot.execution.maker_bid import MakerBidManager

CFG = {"maker_bid_enabled": True,
       "maker_ladder": [[0.96, 0.40, 1.0], [0.92, 0.35, 1.0], [0.87, 0.25, 1.5]],
       "maker_k_place_max": 25.0, "maker_k_place_min": 6.0,
       "maker_k_cancel_s": 1.0}


class FakeTrader:
    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.booked = []

    async def place_gtc_bid(self, token_id, price, shares):
        self.placed.append((token_id, price, shares))
        return f"o{len(self.placed)}"

    async def cancel_gtc(self, order_id):
        self.cancelled.append(order_id)

    async def poll_gtc_fill(self, order_id):
        return None

    async def book_maker_fill(self, **kw):
        self.booked.append(kw)
        return True


class FakeChainlink:
    def __init__(self, proj):
        self.proj = proj

    def projected_final_twap(self, close_ts, now=None):
        return self.proj


def _mgr(proj=None, paper=True):
    return MakerBidManager(FakeTrader(), FakeChainlink(proj), CFG, paper=paper)


def _place(mgr, window_ts, side="Up", budget=30.0, headroom=2.0):
    asyncio.run(mgr.consider_placement(
        window_ts, "btc-updown-5m-%d" % window_ts, "q", side, "tokU",
        budget, headroom,
        {"trade_context": {"signal_leg": "maker_bid", "strike_price": 64000.0},
         "strike_price": 64000.0}))


def test_full_ladder_places_with_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    w = time.time() - 150.0
    _place(mgr, w, budget=30.0, headroom=2.0)
    assert mgr.resting_on(w)
    assert [p for _, p, _ in mgr.trader.placed] == [0.96, 0.92, 0.87]
    # budget split by frozen fractions
    assert mgr.trader.placed[0][2] == pytest.approx(30.0 * 0.40 / 0.96, abs=0.01)
    _place(mgr, w)                       # second ladder is a no-op
    assert len(mgr.trader.placed) == 3


def test_thin_headroom_drops_deep_rung(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 150.0, headroom=1.2)   # < 1.5 -> deepest rung off
    assert [p for _, p, _ in mgr.trader.placed] == [0.96, 0.92]


def test_sub_dollar_budget_never_places(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 150.0, budget=0.90)
    assert mgr.active is None


def test_at_price_prints_never_fill(tmp_path, monkeypatch):
    """Live truth: 102 placements, zero fills — at any shared price level we sit
    behind size no book snapshot shows. Only a print strictly BELOW a rung
    proves the book walked through our level, and then the whole rung filled."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 150.0, budget=30.0)
    mgr.on_print("tokU", {"price": "0.96", "size": "500"})   # AT top rung: nothing
    assert all(r["filled"] == 0.0 for r in mgr.active["rungs"])
    mgr.on_print("tokU", {"price": "0.93", "size": "1"})     # below 0.96 only
    fills = {r["price"]: r["filled"] for r in mgr.active["rungs"]}
    assert fills[0.96] == pytest.approx(mgr.active["rungs"][0]["shares"])
    assert fills[0.92] == 0.0 and fills[0.87] == 0.0
    mgr.on_print("tokU", {"price": "0.86", "size": "1"})     # through everything
    assert all(r["filled"] == pytest.approx(r["shares"])
               for r in mgr.active["rungs"])


def test_price_clamped_to_the_exchange_range(tmp_path, monkeypatch):
    """LIVE rejected 0.992: "invalid price (0.992), min: 0.01 - max: 0.99". The
    valid range is [tick, 1-tick]."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()

    async def tick_01(_t): return "0.01"
    async def tick_001(_t): return "0.001"

    mgr.tick_fn = tick_01
    assert asyncio.run(mgr.legal_price("tokU", 0.992)) == pytest.approx(0.99)
    mgr.tick_fn = tick_001
    assert asyncio.run(mgr.legal_price("tokU", 0.992)) == pytest.approx(0.992)
    mgr.tick_fn = None
    assert asyncio.run(mgr.legal_price("tokU", 0.992)) == pytest.approx(0.992)


def test_rungs_below_the_exchange_min_size_are_skipped(tmp_path, monkeypatch):
    """LIVE rejected a 2.49-share rung: "Size (2.49) lower than the minimum: 5".
    $1 of notional is not enough on its own."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 150.0, budget=6.0)     # 0.96 rung -> 2.5 sh
    assert all(s >= mb.MIN_SHARES for _, _, s in mgr.trader.placed)


def test_gtc_placement_pays_the_measured_post_rtt(tmp_path, monkeypatch):
    """A resting bid does not exist until its POST lands, so paper must wait too."""
    from polybot.execution.paper_trader import PaperTrader
    pt = PaperTrader.__new__(PaperTrader)
    pt.latency_scale = 1.0
    pt.latency_floor_s = 0.32
    t0 = time.time()
    oid = asyncio.run(pt.place_gtc_bid("tokU", 0.90, 50.0))
    took = time.time() - t0
    # Measured GTC round trip (0.049-0.170), not the taker table: a resting bid
    # never crosses, so it never pays the 250ms itode hold.
    assert oid and 0.045 <= took <= 0.40
    t1 = time.time()
    asyncio.run(pt.cancel_gtc(oid))
    assert 0.045 <= time.time() - t1 <= 0.40     # cancels round-trip too


def test_cancel_all_on_lock_weaken_books_blended(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64000.5)             # lock gone
    w = time.time() - 150.0
    _place(mgr, w, budget=30.0)
    mgr.on_print("tokU", {"price": "0.91", "size": "1"})    # through rungs 1+2
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert len(mgr.trader.cancelled) == 3                    # every rung pulled
    assert len(mgr.trader.booked) == 1
    b = mgr.trader.booked[0]
    # both walked-through rungs fill in FULL and book at the blend
    sh96 = round(30.0 * 0.40 / 0.96, 2)
    sh92 = round(30.0 * 0.35 / 0.92, 2)
    assert b["shares_gross"] == pytest.approx(sh96 + sh92)
    assert b["price"] == pytest.approx(
        (sh96 * 0.96 + sh92 * 0.92) / (sh96 + sh92), abs=1e-4)


def test_projection_cold_cancels_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=None)
    _place(mgr, time.time() - 150.0)
    asyncio.run(mgr.maintain())
    assert mgr.active is None and len(mgr.trader.cancelled) == 3
    assert mgr.trader.booked == []


def test_nightly_file_moves_prices_only_and_clamps(tmp_path, monkeypatch):
    lp = tmp_path / "maker_ladder.json"
    lp.write_text(json.dumps({"ladder": [[0.999, 0.9, 9.0], [0.94, 0.9, 9.0],
                                          [0.10, 0.9, 9.0]]}))
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", lp)
    mgr = _mgr()
    rungs = mgr.ladder()
    # Prices clamped to [0.15, 0.95]; fractions + headroom stay the SEED's.
    # Deep is allowed on purpose — break-even win rate equals the price paid, so
    # a 0.20 rung needs 20% while 0.95 needs 95%. The ceiling stays 0.95: above
    # it a resting buy is inside the 4c edge floor and measured negative.
    assert [r[0] for r in rungs] == [0.95, 0.94, 0.15]
    assert [r[1] for r in rungs] == [0.40, 0.35, 0.25]
    assert [r[2] for r in rungs] == [1.0, 1.0, 1.5]


def test_lock_held_keeps_resting(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0)
    w = time.time() - 150.0
    _place(mgr, w)
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)
    assert mgr.trader.cancelled == []


def test_cancels_at_the_close(tmp_path, monkeypatch):
    """Every rung dies at maker_k_cancel_s — nothing rests through the close."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 299.5              # k = 0.5s < maker_k_cancel_s
    mgr = _mgr(proj=64200.0)
    _place(mgr, w, side="Up", budget=30.0, headroom=2.0)
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)
    assert len(mgr.trader.cancelled) == 3


def test_holding_tokens_tracks_the_active_ladder(tmp_path, monkeypatch):
    """Rotation must not unsubscribe a token the ladder still rests on — that
    blinds the paper fill matcher."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0)
    assert mgr.holding_tokens() == set()
    w = time.time() - 150.0
    _place(mgr, w, side="Up")
    assert mgr.holding_tokens() == {"tokU"}
    asyncio.run(mgr._retire("test"))
    assert mgr.holding_tokens() == set()


# ── deep rungs survive a transient lock weakening ─────────────────────────────
DEEP_CFG = dict(CFG, maker_ladder=[[0.90, 0.15, 1.0], [0.60, 0.20, 1.0],
                                   [0.35, 0.30, 1.5], [0.20, 0.35, 1.5]])


def _deep(proj):
    return MakerBidManager(FakeTrader(), FakeChainlink(proj), DEEP_CFG, paper=True)


def test_weakened_lock_pulls_shallow_but_holds_deep(tmp_path, monkeypatch):
    """The spot wick that fills a deep rung is the same move that drops the
    projection under the p99.5 margin. Cancelling everything there runs away at
    exactly the moment the trade appears — so only rungs at/above 0.85 pull."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _deep(proj=64000.5)                  # still Up, but inside the margin
    w = time.time() - 150.0
    _place(mgr, w, budget=40.0, headroom=2.0)
    assert [p for _, p, _ in mgr.trader.placed] == [0.90, 0.60, 0.35, 0.20]
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)                   # ladder survives
    assert len(mgr.trader.cancelled) == 1      # only the 0.90 rung
    live = [r["price"] for r in mgr.active["rungs"] if not r.get("cancelled")]
    assert live == [0.60, 0.35, 0.20]
    # a pruned rung stops accumulating paper fills
    mgr.on_print("tokU", {"price": 0.88, "size": 5.0})
    assert mgr.active["rungs"][0]["filled"] == 0.0


def test_projection_flip_pulls_every_depth(tmp_path, monkeypatch):
    """Still-ours-but-weak holds deep; pointing at the OTHER side does not."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _deep(proj=63999.0)                  # below strike -> Down favoured
    w = time.time() - 150.0
    _place(mgr, w, budget=40.0, headroom=2.0)
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert len(mgr.trader.cancelled) == 4


def test_deep_fill_during_weakness_still_books(tmp_path, monkeypatch):
    """The whole point: a deep rung filled while the lock was weak must book."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _deep(proj=64000.5)
    w = time.time() - 150.0
    _place(mgr, w, budget=40.0, headroom=2.0)
    asyncio.run(mgr.maintain())                # prunes 0.90, holds the rest
    # A wick to 0.19 trades THROUGH every live rung, so all three fill in full
    # and book as one position at the blend.
    mgr.on_print("tokU", {"price": 0.19, "size": 3.0})
    asyncio.run(mgr._retire("test"))
    assert len(mgr.trader.booked) == 1
    sh60 = round(40.0 * 0.20 / 0.60, 2)
    sh35 = round(40.0 * 0.30 / 0.35, 2)
    sh20 = round(40.0 * 0.35 / 0.20, 2)
    assert mgr.trader.booked[0]["shares_gross"] == pytest.approx(sh60 + sh35 + sh20)
    assert mgr.trader.booked[0]["price"] == pytest.approx(
        (sh60 * 0.60 + sh35 * 0.35 + sh20 * 0.20) / (sh60 + sh35 + sh20), abs=1e-4)
