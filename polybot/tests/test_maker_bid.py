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
    # Prices clamped to [0.92, 0.95]; fractions + headroom stay the SEED's.
    # The band is deliberately narrow: above 0.95 breaches the 4c edge floor
    # (the p99.5 cap is 0.955 and the tick is 0.01), and below 0.92 nothing
    # trades once a window is locked — rungs at 0.85-0.90 filled 0 times in
    # 285 placements while a deep-quantile feedback loop drove them lower.
    assert [r[0] for r in rungs] == [0.95, 0.94, 0.92]
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


# ── post-close certainty phase ────────────────────────────────────────────────
PC_CFG = dict(CFG, post_close_enabled=True, post_close_s=120.0,
              post_close_price=0.995, post_close_budget_frac=0.40)


class FakeSettledChainlink(FakeChainlink):
    """Boundary captures for the open and close of one window."""

    def __init__(self, strike=None, final=None, trusted=True, proj=None):
        super().__init__(proj)
        self._b = {}
        if strike is not None:
            self._b["open"] = strike
        if final is not None:
            self._b["close"] = final
        self.trusted = trusted
        self.window_ts = None

    def _key(self, b):
        return "open" if b == self.window_ts else "close"

    def boundary_captured(self, b):
        return self._key(b) in self._b

    def strike_reliable(self, b):
        return self.trusted and self.boundary_captured(b)

    def get_strike(self, b):
        return self._b.get(self._key(b))


def _pc_mgr(strike, final, trusted=True):
    cl = FakeSettledChainlink(strike, final, trusted)
    return MakerBidManager(FakeTrader(), cl, PC_CFG, paper=True)


def _pc_place(mgr, window_ts, side="Up"):
    mgr.chainlink.window_ts = window_ts
    _place(mgr, window_ts, side=side, budget=30.0, headroom=2.0)


def test_post_close_arms_on_the_settled_winner(tmp_path, monkeypatch):
    """Once both boundaries are captured, final >= strike settles the winner and
    a 0.995 rung arms — a price the pre-close edge floor forbids, legal here
    because the average is finished rather than projected."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0                      # window already closed
    mgr = _pc_mgr(strike=64000.0, final=64010.0)  # final > strike -> Up won
    _pc_place(mgr, w, side="Up")
    n_before = len(mgr.trader.placed)
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)                     # NOT cancelled at the close
    assert len(mgr.trader.placed) == n_before + 1
    assert mgr.trader.placed[-1][1] == 0.995
    assert mgr.trader.cancelled == []
    asyncio.run(mgr.maintain())                  # never places twice
    assert len(mgr.trader.placed) == n_before + 1


def test_post_close_pulls_everything_when_the_lock_missed(tmp_path, monkeypatch):
    """A bid resting on a $0 token is this leg's only unbounded loss, so the
    settled winner is re-checked every tick, not just at the transition."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=63990.0)  # final < strike -> DOWN won
    _pc_place(mgr, w, side="Up")                  # we are resting on Up
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)
    assert len(mgr.trader.cancelled) == 3
    assert 0.995 not in [p for _, p, _ in mgr.trader.placed]


def test_post_close_fails_closed_without_trusted_boundaries(tmp_path, monkeypatch):
    """5-14 boundaries/day never arrive. Once the grace for the closing report
    is spent, no capture and no trust means no bid."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 300.0 - mb.PC_VERIFY_GRACE_S - 1.0
    for kwargs in ({"strike": 64000.0, "final": None},
                   {"strike": None, "final": 64010.0},
                   {"strike": 64000.0, "final": 64010.0, "trusted": False}):
        mgr = _pc_mgr(**kwargs)
        _pc_place(mgr, w, side="Up")
        asyncio.run(mgr.maintain())
        assert not mgr.resting_on(w), kwargs
        assert 0.995 not in [p for _, p, _ in mgr.trader.placed], kwargs


def test_post_close_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0 - 120.0              # past the post-close budget
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    _pc_place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)


def test_disabled_still_cancels_at_the_close(tmp_path, monkeypatch):
    """The old behaviour must survive the flag being off."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    cl = FakeSettledChainlink(64000.0, 64010.0)
    mgr = MakerBidManager(FakeTrader(), cl, CFG, paper=True)   # post_close absent
    cl.window_ts = w
    _place(mgr, w, side="Up", budget=30.0, headroom=2.0)
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)
    assert 0.995 not in [p for _, p, _ in mgr.trader.placed]


def test_post_close_rung_fills_only_strictly_below(tmp_path, monkeypatch):
    """Same conservative convention as every other rung — queue position is
    unknowable, so a print AT 0.995 may have gone entirely to earlier queue."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    _pc_place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    pc = mgr.active["rungs"][-1]
    mgr.on_print("tokU", {"price": 0.995, "size": 5.0})
    assert pc["filled"] == 0.0
    mgr.on_print("tokU", {"price": 0.99, "size": 5.0})
    assert pc["filled"] == 5.0


def test_post_close_waits_for_the_closing_boundary_report(tmp_path, monkeypatch):
    """The closing boundary lands ~1.7s after the boundary (p99 2.9s), so an
    unverified outcome in the first seconds is NORMAL and must not retire —
    checking at close+0s made the whole phase a no-op in production."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 300.5                       # only 0.5s past the close
    mgr = _pc_mgr(strike=64000.0, final=None)     # closing report not in yet
    _pc_place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)                      # still resting, still waiting
    assert mgr.trader.cancelled == []
    # the report arrives, and the rung arms on the next tick
    mgr.chainlink._b["close"] = 64010.0
    asyncio.run(mgr.maintain())
    assert mgr.trader.placed[-1][1] == 0.995


def test_post_close_gives_up_after_the_grace(tmp_path, monkeypatch):
    """A genuine delivery hole still fails closed."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 300.0 - mb.PC_VERIFY_GRACE_S - 1.0
    mgr = _pc_mgr(strike=64000.0, final=None)
    _pc_place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)
    assert 0.995 not in [p for _, p, _ in mgr.trader.placed]
