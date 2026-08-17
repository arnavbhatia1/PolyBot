"""MakerBidManager — the projection-side deep ladder (execution/maker_bid.py).

Money-path invariants: one ladder at a time, the sign-quality (SNR) floor
gates placement and cancels everything once the sign is inside its own noise,
paper fills ONLY on strictly-below prints, post-close hold gated every tick on
the boundary-verified winner, all fills book as ONE blended position.
"""
import asyncio
import json
import time

import pytest

from polybot.execution import maker_bid as mb
from polybot.execution.maker_bid import MakerBidManager

LADDER = [[0.80, 0.20, 2.0], [0.65, 0.20, 2.0], [0.50, 0.20, 2.0],
          [0.35, 0.20, 2.0], [0.20, 0.20, 2.0]]
CFG = {"maker_bid_enabled": True, "maker_ladder": LADDER,
       "maker_k_place_max": 8.0, "maker_k_place_min": 6.0,
       "maker_bankroll_frac": 0.15, "post_close_hold_s": 60.0}
PRICES = [r[0] for r in LADDER]


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
    """Projection + boundary captures for one window (post-close tests)."""

    def __init__(self, proj=None, strike=None, final=None, trusted=True):
        self.proj = proj
        self._b = {}
        if strike is not None:
            self._b["open"] = strike
        if final is not None:
            self._b["close"] = final
        self.trusted = trusted
        self.window_ts = None

    def projected_final_twap(self, close_ts, now=None, bridged=False):
        return self.proj

    def _key(self, b):
        return "open" if b == self.window_ts else "close"

    def boundary_captured(self, b):
        return self._key(b) in self._b

    def strike_reliable(self, b):
        return self.trusted and self.boundary_captured(b)

    def get_strike(self, b):
        return self._b.get(self._key(b))


def _mgr(proj=None, paper=True, **cl_kw):
    return MakerBidManager(FakeTrader(), FakeChainlink(proj, **cl_kw), CFG, paper=paper)


def _place(mgr, window_ts, side="Up", budget=40.0, headroom=2.0):
    mgr.chainlink.window_ts = window_ts
    asyncio.run(mgr.consider_placement(
        window_ts, "btc-updown-5m-%d" % window_ts, "q", side, "tokU",
        budget, headroom,
        {"trade_context": {"signal_leg": "deep_proj", "strike_price": 64000.0},
         "strike_price": 64000.0}))


def test_full_ladder_places(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    w = time.time() - 285.0                      # k = 15s
    _place(mgr, w, budget=40.0, headroom=2.0)
    assert mgr.resting_on(w)
    assert [p for _, p, _ in mgr.trader.placed] == PRICES
    assert mgr.trader.placed[0][2] == pytest.approx(40.0 * 0.20 / 0.80, abs=0.01)
    _place(mgr, w)                               # second ladder is a no-op
    assert len(mgr.trader.placed) == len(PRICES)


def test_sign_below_every_need_places_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 285.0, headroom=1.5)   # < 2.0 for every rung
    assert mgr.active is None


def test_min_need_is_the_ladder_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    assert _mgr().min_need() == pytest.approx(2.0)


def test_sub_dollar_budget_never_places(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 285.0, budget=0.90)
    assert mgr.active is None


def test_fill_semantics_below_full_at_queued_above_never(tmp_path, monkeypatch):
    """Strictly below a rung = the book walked through our level, full fill.
    AT a rung = only the volume beyond the measured typical queue. Above = never."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 285.0, budget=40.0)
    mgr.on_print("tokU", {"price": "0.80", "size": "30"})    # AT top: inside queue
    assert all(r["filled"] == 0.0 for r in mgr.active["rungs"])
    mgr.on_print("tokU", {"price": "0.55", "size": "1"})     # below 0.80, 0.65
    fills = {r["price"]: r["filled"] for r in mgr.active["rungs"]}
    assert fills[0.80] > 0 and fills[0.65] > 0
    assert fills[0.50] == 0.0 and fills[0.35] == 0.0 and fills[0.20] == 0.0
    mgr.on_print("tokU", {"price": "0.15", "size": "1"})     # sweeps the rest
    assert all(r["filled"] == pytest.approx(r["shares"])
               for r in mgr.active["rungs"])


def test_price_clamped_to_the_exchange_range(tmp_path, monkeypatch):
    """LIVE rejected 0.992: "invalid price, min: 0.01 - max: 0.99"."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()

    async def tick_01(_t): return "0.01"

    mgr.tick_fn = tick_01
    assert asyncio.run(mgr.legal_price("tokU", 0.992)) == pytest.approx(0.99)
    mgr.tick_fn = None
    assert asyncio.run(mgr.legal_price("tokU", 0.992)) == pytest.approx(0.992)


def test_rungs_below_the_exchange_min_size_are_skipped(tmp_path, monkeypatch):
    """LIVE rejected a 2.49-share rung: "Size (2.49) lower than the minimum: 5"."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 285.0, budget=8.0)     # 0.80 rung -> 2 sh
    assert all(s >= mb.MIN_SHARES for _, _, s in mgr.trader.placed)


def test_gtc_placement_pays_the_measured_post_rtt(tmp_path, monkeypatch):
    from polybot.execution.paper_trader import PaperTrader
    pt = PaperTrader.__new__(PaperTrader)
    pt.latency_scale = 1.0
    pt.latency_floor_s = 0.32
    t0 = time.time()
    oid = asyncio.run(pt.place_gtc_bid("tokU", 0.40, 50.0))
    assert oid and 0.045 <= time.time() - t0 <= 0.40
    t1 = time.time()
    asyncio.run(pt.cancel_gtc(oid))
    assert 0.045 <= time.time() - t1 <= 0.40


def test_sign_inside_noise_cancels_everything(tmp_path, monkeypatch):
    """disp under min_need x p99.5-error picks nothing — no depth survives."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64010.0)                 # signed +10 < 2 x p995(15)=28
    w = time.time() - 285.0
    _place(mgr, w, budget=40.0)
    asyncio.run(mgr.maintain())
    assert mgr.active is None
    assert len(mgr.trader.cancelled) == len(PRICES)


def test_projection_flip_cancels_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=63999.0)                 # signed negative
    w = time.time() - 285.0
    _place(mgr, w, budget=40.0)
    asyncio.run(mgr.maintain())
    assert mgr.active is None and len(mgr.trader.cancelled) == len(PRICES)


def test_above_the_floor_holds_every_rung(tmp_path, monkeypatch):
    """Clearing the floor holds the whole ladder — the wick can come."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64030.0)                 # signed +30 > 2 x p995(15)=28
    w = time.time() - 285.0
    _place(mgr, w, budget=40.0)
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)
    assert mgr.trader.cancelled == []
    mgr.on_print("tokU", {"price": 0.15, "size": 3.0})   # sweeps all rungs
    asyncio.run(mgr._retire("test"))
    assert len(mgr.trader.booked) == 1
    sh = [round(40.0 * 0.20 / p, 2) for p in PRICES]
    assert mgr.trader.booked[0]["shares_gross"] == pytest.approx(sum(sh))
    blend = sum(s * p for s, p in zip(sh, PRICES)) / sum(sh)
    assert mgr.trader.booked[0]["price"] == pytest.approx(blend, abs=1e-4)


def test_projection_cold_cancels_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=None)
    _place(mgr, time.time() - 285.0)
    asyncio.run(mgr.maintain())
    assert mgr.active is None and len(mgr.trader.cancelled) == len(PRICES)
    assert mgr.trader.booked == []


def test_sign_held_keeps_resting(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0)                 # signed +200, far above any floor
    w = time.time() - 285.0
    _place(mgr, w)
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)
    assert mgr.trader.cancelled == []


# ── post-close hold, gated on the boundary-verified winner ────────────────────

def test_post_close_holds_while_winner_verified_equal(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0, strike=64000.0, final=64010.0)   # Up won
    w = time.time() - 301.0
    _place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)
    assert mgr.trader.cancelled == []


def test_post_close_pulls_when_the_side_missed(tmp_path, monkeypatch):
    """A bid resting on a $0 token is the one unbounded loss — checked every tick."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0, strike=64000.0, final=63990.0)   # DOWN won
    w = time.time() - 301.0
    _place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)
    assert len(mgr.trader.cancelled) == len(PRICES)


def test_post_close_fails_closed_on_a_delivery_hole(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0, strike=64000.0, final=None)      # no closing report
    w = time.time() - 300.0 - mb.PC_VERIFY_GRACE_S - 1.0
    _place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)


def test_post_close_waits_out_the_normal_report_lag(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0, strike=64000.0, final=None)
    w = time.time() - 300.5
    _place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert mgr.resting_on(w)


def test_post_close_hold_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0, strike=64000.0, final=64010.0)
    w = time.time() - 301.0 - 60.0
    _place(mgr, w, side="Up")
    asyncio.run(mgr.maintain())
    assert not mgr.resting_on(w)


def test_holding_tokens_tracks_the_active_ladder(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr(proj=64200.0)
    assert mgr.holding_tokens() == set()
    w = time.time() - 285.0
    _place(mgr, w)
    assert mgr.holding_tokens() == {"tokU"}
    asyncio.run(mgr._retire("test"))
    assert mgr.holding_tokens() == set()


def test_nightly_file_moves_prices_only_and_clamps(tmp_path, monkeypatch):
    lp = tmp_path / "maker_ladder.json"
    lp.write_text(json.dumps({"ladder": [[0.999, 0.9, 9.0], [0.70, 0.9, 9.0],
                                          [0.10, 0.9, 9.0], [0.30, 0.9, 9.0],
                                          [0.22, 0.9, 9.0]]}))
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", lp)
    mgr = _mgr()
    rungs = mgr.ladder()
    # Prices clamped to [0.15, 0.95]; fractions + needs stay the SEED's.
    assert [r[0] for r in rungs] == [0.95, 0.70, 0.15, 0.30, 0.22]
    assert [r[1] for r in rungs] == [0.20] * 5
    assert [r[2] for r in rungs] == [2.0] * 5


def test_at_price_prints_fill_only_beyond_the_measured_queue(tmp_path, monkeypatch):
    """A print AT our price credits only the volume beyond the live-measured
    typical queue (AT_PRICE_QUEUE_SH) — accumulated across the window."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")
    mgr = _mgr()
    _place(mgr, time.time() - 285.0, budget=200.0)      # 0.80 rung = 50 sh
    top = mgr.active["rungs"][0]
    mgr.on_print("tokU", {"price": "0.80", "size": "40"})   # queue eats it
    assert top["filled"] == 0.0
    mgr.on_print("tokU", {"price": "0.80", "size": "40"})   # 80 seen: 25 beyond
    assert top["filled"] == pytest.approx(80 - mb.AT_PRICE_QUEUE_SH)
    assert top.get("filled_at_px") is True
    mgr.on_print("tokU", {"price": "0.80", "size": "500"})  # capped at rung size
    assert top["filled"] == pytest.approx(top["shares"])
    # a strictly-below print still fills any rung in full, regardless of queue
    r2 = mgr.active["rungs"][1]
    mgr.on_print("tokU", {"price": "0.60", "size": "1"})
    assert r2["filled"] == pytest.approx(r2["shares"])


def test_live_retire_reads_the_final_matched_size(tmp_path, monkeypatch):
    """A live fill can land inside the cancel round trip — the booking must
    re-read each order's final matched size, or the wallet holds unbooked shares."""
    monkeypatch.setattr(mb, "MAKER_LADDER_PATH", tmp_path / "none.json")

    class RaceTrader(FakeTrader):
        async def poll_gtc_fill(self, order_id):
            # the 0.80 rung matched 10 sh, visible only after its cancel landed
            return 10.0 if (order_id == "o1" and "o1" in self.cancelled) else 0.0

    mgr = MakerBidManager(RaceTrader(), FakeChainlink(64200.0), CFG, paper=False)
    _place(mgr, time.time() - 285.0, budget=200.0)
    asyncio.run(mgr._retire("test"))
    assert len(mgr.trader.booked) == 1
    assert mgr.trader.booked[0]["shares_gross"] == pytest.approx(10.0)
    assert mgr.trader.booked[0]["price"] == pytest.approx(0.80)
