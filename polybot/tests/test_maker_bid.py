"""MakerBidManager — the lock-informed resting LADDER (execution/maker_bid.py).

Locks the money-path invariants: one ladder at a time, deep rungs demand
displacement headroom, cancel-all the moment the lock weakens / projection
goes cold / window closes, paper fills ONLY on prints strictly below a rung,
all fills book as ONE blended position at/above the $1 floor, and the nightly
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
       "maker_k_place_max": 25.0, "maker_k_place_min": 3.0,
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


def test_print_through_fills_per_rung_strictly_below(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 150.0, budget=30.0)
    mgr.on_print("tokU", {"price": "0.96", "size": "99"})   # AT top rung — no fill
    assert all(r["filled"] == 0.0 for r in mgr.active["rungs"])
    mgr.on_print("tokU", {"price": "0.93", "size": "5"})    # through rung 1 only
    fills = [r["filled"] for r in mgr.active["rungs"]]
    assert fills[0] == pytest.approx(5.0) and fills[1] == 0.0 and fills[2] == 0.0
    mgr.on_print("tokU", {"price": "0.86", "size": "999"})  # through all, capped
    assert all(r["filled"] == pytest.approx(r["shares"]) for r in mgr.active["rungs"])


def test_cancel_all_on_lock_weaken_books_blended(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64000.5)             # lock gone
    w = time.time() - 150.0
    _place(mgr, w, budget=30.0)
    mgr.on_print("tokU", {"price": "0.91", "size": "4"})    # fills rungs 1+2 partially
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert len(mgr.trader.cancelled) == 3                    # every rung pulled
    assert len(mgr.trader.booked) == 1
    b = mgr.trader.booked[0]
    # 4 sh @0.96 + 4 sh @0.92 -> blended 0.94
    assert b["shares_gross"] == pytest.approx(8.0)
    assert b["price"] == pytest.approx(0.94)


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
    # prices clamped to [0.85, 0.975]; fractions + headroom stay the SEED's
    assert [r[0] for r in rungs] == [0.975, 0.94, 0.85]
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
