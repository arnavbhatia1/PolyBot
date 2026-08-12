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


def test_print_fills_per_rung_at_or_below_with_empty_queue(tmp_path, monkeypatch):
    """With nothing resting ahead of us, a print AT our price fills — that is
    what the exchange does. The old rule refused it and was wrong."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()                                    # book_fn unset -> queue 0
    _place(mgr, time.time() - 150.0, budget=30.0)
    mgr.on_print("tokU", {"price": "0.96", "size": "5"})    # AT top rung -> fills
    fills = [r["filled"] for r in mgr.active["rungs"]]
    assert fills[0] == pytest.approx(5.0) and fills[1] == 0.0
    mgr.on_print("tokU", {"price": "0.86", "size": "999"})  # through all, capped
    assert all(r["filled"] == pytest.approx(r["shares"]) for r in mgr.active["rungs"])


def test_gtc_placement_pays_the_measured_post_rtt(tmp_path, monkeypatch):
    """A resting bid does not exist until the POST lands. Paper used to return an
    order id instantly, which handed the maker legs every print in that window
    for free — the one place paper was more optimistic than live."""
    from polybot.execution.paper_trader import PaperTrader
    pt = PaperTrader.__new__(PaperTrader)
    pt.latency_scale = 1.0
    pt.latency_floor_s = 0.32
    t0 = time.time()
    oid = asyncio.run(pt.place_gtc_bid("tokU", 0.992, 50.0))
    took = time.time() - t0
    # The MEASURED GTC round trip (min 0.049, max 0.170) — deliberately NOT the
    # taker table: a resting bid never crosses, so it never pays the 250ms itode
    # hold, and charging it 436ms under-filled the leg that earns.
    assert oid and 0.045 <= took <= 0.40
    t1 = time.time()
    asyncio.run(pt.cancel_gtc(oid))
    assert 0.045 <= time.time() - t1 <= 0.40     # cancels round-trip too


def test_queue_ahead_must_drain_before_we_fill(tmp_path, monkeypatch):
    """The whole point of the model: size resting at or better than our price
    fills FIRST. Paper must require the same volume through the level that live
    would — otherwise it hands us fills a real queue would have absorbed."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    # 40 shares already resting at/above the 0.96 top rung.
    mgr.book_fn = lambda tok: {"bids": [{"price": "0.97", "size": "25"},
                                        {"price": "0.96", "size": "15"},
                                        {"price": "0.80", "size": "500"}]}
    _place(mgr, time.time() - 150.0, budget=30.0)
    top = mgr.active["rungs"][0]
    assert top["queue_ahead"] == pytest.approx(40.0)   # 0.80 is behind us
    mgr.on_print("tokU", {"price": "0.96", "size": "30"})
    assert top["filled"] == 0.0                        # all 30 went to the queue
    assert top["queue_ahead"] == pytest.approx(10.0)
    mgr.on_print("tokU", {"price": "0.96", "size": "12"})
    assert top["filled"] == pytest.approx(2.0)         # 10 drains, 2 reaches us


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


# ── post-close certainty phase ────────────────────────────────────────────────
PC_LADDER = [[0.995, 0.70], [0.97, 0.10], [0.95, 0.10], [0.90, 0.10]]
PC_CFG = dict(CFG, post_close_enabled=True, post_close_s=120.0,
              post_close_ladder=PC_LADDER, post_close_budget_frac=0.40)


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
    the whole post-close ladder arms — prices the pre-close edge floor forbids,
    legal here because the average is finished rather than projected."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0                      # window already closed
    mgr = _pc_mgr(strike=64000.0, final=64010.0)  # final > strike -> Up won
    _pc_place(mgr, w, side="Up")
    n_before = len(mgr.trader.placed)
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)                     # NOT cancelled at the close
    new = mgr.trader.placed[n_before:]
    assert [p for _, p, _ in new] == [px for px, _ in PC_LADDER]
    assert mgr.trader.cancelled == []
    asyncio.run(mgr.maintain())                  # never places twice
    assert len(mgr.trader.placed) == n_before + len(PC_LADDER)


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


def test_post_close_rung_obeys_the_measured_queue(tmp_path, monkeypatch):
    """Post-close is where this matters most: the winner's book carries many bid
    levels and zero asks, so whether we fill is entirely a queue question."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    # 20 shares resting at 0.999 sit ahead of our 0.995 rung; 0.990 is behind it.
    mgr.book_fn = lambda tok: {"bids": [{"price": "0.999", "size": "20"},
                                        {"price": "0.990", "size": "900"}]}
    _pc_place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    pc = next(r for r in mgr.active["rungs"] if r["price"] == 0.995)
    assert pc["queue_ahead"] == pytest.approx(20.0)
    mgr.on_print("tokU", {"price": 0.99, "size": 20.0})   # drains the 0.999 bid
    assert pc["filled"] == 0.0
    mgr.on_print("tokU", {"price": 0.99, "size": 5.0})    # now it reaches us
    assert pc["filled"] == pytest.approx(5.0)


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
    # the report arrives, and the ladder arms on the next tick
    mgr.chainlink._b["close"] = 64010.0
    asyncio.run(mgr.maintain())
    assert 0.995 in [p for _, p, _ in mgr.trader.placed]


def test_post_close_gives_up_after_the_grace(tmp_path, monkeypatch):
    """A genuine delivery hole still fails closed."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 300.0 - mb.PC_VERIFY_GRACE_S - 1.0
    mgr = _pc_mgr(strike=64000.0, final=None)
    _pc_place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)
    assert 0.995 not in [p for _, p, _ in mgr.trader.placed]


def test_holding_tokens_keeps_the_ws_subscribed_past_the_close(tmp_path, monkeypatch):
    """Rotation must not unsubscribe a token the ladder still rests on — that is
    what made the post-close phase place orders that could never fill."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    assert mgr.holding_tokens() == set()
    w = time.time() - 300.5
    _pc_place(mgr, w, side="Up")
    assert mgr.holding_tokens() == {"tokU"}
    asyncio.run(mgr._retire("test"))
    assert mgr.holding_tokens() == set()


# ── post-close decoupled from the ladder ──────────────────────────────────────
def _arm(mgr, w, budget=20.0):
    mgr.chainlink.window_ts = w
    mgr.arm_post_close(w, "cid", "q?", "tokU", "tokD", budget,
                       {"strike_price": 64000.0})


def test_post_close_arms_without_any_pre_close_ladder(tmp_path, monkeypatch):
    """The outcome is settled fact in EVERY window, so post-close must not depend
    on a ladder having rested — that limited it to a handful of windows a day."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=64010.0)      # final > strike -> Up won
    _arm(mgr, w)
    assert mgr.active is None                          # nothing until the close
    asyncio.run(mgr.maintain())
    assert mgr.active is not None and mgr.active["side"] == "Up"
    assert [p for _, p, _ in mgr.trader.placed] == [px for px, _ in PC_LADDER]
    assert all(t == "tokU" for t, _, _ in mgr.trader.placed)


def test_post_close_standalone_rests_on_the_settled_LOSER_side_never(tmp_path,
                                                                    monkeypatch):
    """final < strike settles Down — the bid must go on tokD. Resting on a $0
    token is this leg's only unbounded loss."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=63990.0)
    _arm(mgr, w)
    asyncio.run(mgr.maintain())
    assert mgr.active["side"] == "Down"
    assert all(t == "tokD" for t, _, _ in mgr.trader.placed)


def test_post_close_standalone_fails_closed_on_a_delivery_hole(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 300.0 - mb.PC_VERIFY_GRACE_S - 1.0
    mgr = _pc_mgr(strike=64000.0, final=None)          # closing report never came
    _arm(mgr, w)
    asyncio.run(mgr.maintain())
    assert mgr.active is None and mgr.pending is None
    assert mgr.trader.placed == []


def test_pending_keeps_both_tokens_subscribed(tmp_path, monkeypatch):
    """The winner is unknown until the closing boundary lands, so both sides stay
    subscribed — going deaf in that gap breaks paper/live parity silently."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 300.2                            # inside the verify grace
    mgr = _pc_mgr(strike=64000.0, final=None)
    _arm(mgr, w)
    assert mgr.holding_tokens() == {"tokU", "tokD"}


def test_post_close_is_sized_off_bankroll_not_the_ladder(tmp_path, monkeypatch):
    """A settled outcome is not a Kelly bet. Inheriting a fraction of the
    ladder's fractional-Kelly budget made every fill ~$2.15, so 71 perfect wins
    in one day earned $0.80. pc_budget must win over the ladder fraction."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    mgr.chainlink.window_ts = w
    asyncio.run(mgr.consider_placement(w, "cid", "q?", "Up", "tokU", 30.0, 2.0,
                                       {"strike_price": 64000.0},
                                       pc_budget=41.13))
    n_before = len(mgr.trader.placed)
    asyncio.run(mgr.maintain())
    top = next(x for x in mgr.trader.placed[n_before:] if x[1] == 0.995)
    notional = top[2] * 0.995
    assert abs(notional - 41.13 * 0.70) < 0.02      # bankroll-sized
    assert abs(notional - 30.0 * 0.40 * 0.70) > 1.0  # NOT the ladder fraction


def test_post_close_falls_back_to_the_ladder_fraction_without_pc_budget(
        tmp_path, monkeypatch):
    """Safety net: if no bankroll-sized budget reaches the manager, the leg still
    arms off the ladder rather than placing nothing."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 301.0
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    _pc_place(mgr, w, side="Up")                     # no pc_budget passed
    n_before = len(mgr.trader.placed)
    asyncio.run(mgr.maintain())
    top = next(x for x in mgr.trader.placed[n_before:] if x[1] == 0.995)
    assert abs(top[2] * 0.995 - 30.0 * 0.40 * 0.70) < 0.05


def test_arm_is_ignored_while_a_ladder_rests_and_never_goes_backwards(tmp_path,
                                                                     monkeypatch):
    """One entry path per window: a resting ladder handles its own post-close,
    and a stale window must not overwrite a newer intent."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    w = time.time() - 150.0
    mgr = _pc_mgr(strike=64000.0, final=64010.0)
    _place(mgr, w, side="Up", budget=30.0, headroom=2.0)
    _arm(mgr, w)
    assert mgr.pending is None                         # ladder owns the window
    asyncio.run(mgr._retire("test"))
    _arm(mgr, w + 300)
    _arm(mgr, w)                                       # older window ignored
    assert mgr.pending["window_ts"] == w + 300


# ── deep rungs survive a transient lock weakening ─────────────────────────────
DEEP_CFG = dict(CFG, maker_ladder=[[0.90, 0.15, 1.0], [0.60, 0.20, 1.0],
                                   [0.35, 0.30, 1.5], [0.20, 0.35, 1.5]],
                post_close_enabled=True, post_close_s=120.0,
                post_close_ladder=PC_LADDER, post_close_budget_frac=0.40)


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
    # A wick to 0.19 trades THROUGH every deep rung, so all three fill and book
    # as one position at the blend — which needs only ~38% to break even.
    mgr.on_print("tokU", {"price": 0.19, "size": 3.0})
    asyncio.run(mgr._retire("test"))
    assert len(mgr.trader.booked) == 1
    assert mgr.trader.booked[0]["shares_gross"] == pytest.approx(9.0)
    assert mgr.trader.booked[0]["price"] == pytest.approx(
        (3 * 0.60 + 3 * 0.35 + 3 * 0.20) / 9, abs=1e-4)
