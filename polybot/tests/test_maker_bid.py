"""MakerBidManager — the lock-informed resting bid (execution/maker_bid.py).

Locks the money-path invariants: one order at a time, cancel the moment the
lock weakens or the window closes, paper fills ONLY on prints strictly below
the bid (queue position is unknowable — at-price fills would flatter the
shadow), fills at/above the $1 floor book through the trader, and a full fill
books immediately.
"""
import asyncio
import time

import pytest

from polybot.core.signal_engine import TWAP_MARGIN_P995, twap_margin
from polybot.execution.maker_bid import MakerBidManager

CFG = {"maker_bid_enabled": True, "maker_bid_discount": 0.02,
       "maker_k_place_max": 25.0, "maker_k_place_min": 3.0,
       "maker_k_cancel_s": 1.0}


class FakeTrader:
    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.booked = []
        self.matched = None

    async def place_gtc_bid(self, token_id, price, shares):
        self.placed.append((token_id, price, shares))
        return f"o{len(self.placed)}"

    async def cancel_gtc(self, order_id):
        self.cancelled.append(order_id)

    async def poll_gtc_fill(self, order_id):
        return self.matched

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


def _place(mgr, window_ts, side="Up", bid=0.935, size=20.0):
    asyncio.run(mgr.consider_placement(
        window_ts, "btc-updown-5m-%d" % window_ts, "q", side, "tokU", bid, size,
        {"trade_context": {"signal_leg": "maker_bid", "strike_price": 64000.0},
         "strike_price": 64000.0}))


def test_places_once_and_reports_resting():
    mgr = _mgr()
    w = int(time.time() // 300) * 300
    _place(mgr, w)
    assert mgr.resting_on(w)
    assert len(mgr.trader.placed) == 1
    _place(mgr, w)                       # second placement is a no-op
    assert len(mgr.trader.placed) == 1


def test_sub_dollar_size_never_places():
    mgr = _mgr()
    _place(mgr, int(time.time() // 300) * 300, size=0.90)
    assert mgr.active is None


def test_print_through_is_strictly_conservative():
    mgr = _mgr()
    w = int(time.time() // 300) * 300
    _place(mgr, w, bid=0.935)
    mgr.on_print("tokU", {"price": "0.935", "size": "50"})   # AT the bid — queue unknown
    assert mgr.active["filled_shares"] == 0.0
    mgr.on_print("wrong-token", {"price": "0.90", "size": "50"})
    assert mgr.active["filled_shares"] == 0.0
    mgr.on_print("tokU", {"price": "0.93", "size": "8"})     # traded THROUGH us
    assert mgr.active["filled_shares"] == pytest.approx(8.0)
    mgr.on_print("tokU", {"price": "0.90", "size": "999"})   # capped at our size
    assert mgr.active["filled_shares"] == pytest.approx(mgr.active["shares"])


def test_cancel_on_lock_weaken_books_nothing_unfilled():
    # Projection collapses to the strike — the lock is gone, the order must die.
    # window_ts is a raw timestamp here: the manager only derives close = ts+300,
    # so mid-window k values can be pinned deterministically.
    w = time.time() - 150.0              # k = 150s: cancel can only be the lock
    mgr = _mgr(proj=64000.5)
    _place(mgr, w)
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert mgr.trader.cancelled == ["o1"]
    assert mgr.trader.booked == []


def test_full_fill_books_immediately_via_trader():
    w = time.time() - 150.0
    mgr = _mgr(proj=64100.0)             # comfortably locked Up
    _place(mgr, w, bid=0.935, size=18.7)  # 20 shares
    mgr.on_print("tokU", {"price": "0.92", "size": "20"})
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert len(mgr.trader.booked) == 1
    b = mgr.trader.booked[0]
    assert b["price"] == 0.935 and b["shares_gross"] == pytest.approx(20.0)
    assert b["indicator_snapshot"]["trade_context"]["signal_leg"] == "maker_bid"


def test_partial_fill_books_at_window_close():
    w = time.time() - 299.1              # k ≈ 0.9s — the closing cancel path
    mgr = _mgr(proj=64100.0)
    _place(mgr, w, bid=0.935, size=18.7)
    mgr.on_print("tokU", {"price": "0.93", "size": "5"})     # $4.7 partial ≥ $1 floor
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert len(mgr.trader.booked) == 1
    assert mgr.trader.booked[0]["shares_gross"] == pytest.approx(5.0)


def test_lock_held_keeps_resting():
    w = time.time() - 150.0
    mgr = _mgr(proj=64200.0)             # far past every margin
    _place(mgr, w)
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)
    assert mgr.trader.cancelled == []
