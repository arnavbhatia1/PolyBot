"""Tests for the long-horizon crypto calibration harness.

Covers: Deribit pricing invariants, surface parsing, calibration slope recovery,
event-clustered bootstrap (incl. the degenerate single-cluster case), the kill-bar
verdict logic, the instant IV cross-check, Gamma discovery/parse/resolution, CLOB book
parsing, the async store, and an end-to-end snapshot->label->analyze with a fake client.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from polybot.calibration import analysis, clob, deribit, discovery, harness
from polybot.calibration.store import CalibrationStore

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)


# ───────────────────────── Deribit pricing math ─────────────────────────

def test_one_touch_at_barrier_is_certain():
    # B == S0 -> already touching -> ~1.0
    assert deribit.one_touch_prob(60000, 60000, 0.5, 0.45, 60000) == pytest.approx(1.0, abs=1e-6)


def test_one_touch_monotone_in_distance():
    base = 60000.0
    probs = [deribit.one_touch_prob(base, B, 0.5, 0.45, base)
             for B in (70000, 80000, 100000, 150000)]
    assert all(p2 < p1 for p1, p2 in zip(probs, probs[1:])), probs  # decreasing as barrier rises
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_one_touch_geq_terminal_digital():
    # P(touch B before T) must be >= P(S_T >= B) for an up-barrier (touch is easier).
    S0, B, T, sig, fwd = 60000, 80000, 0.5, 0.45, 60500
    touch = deribit.one_touch_prob(S0, B, T, sig, fwd)
    term = deribit.terminal_digital_prob(S0, B, T, sig, fwd)
    assert touch >= term - 1e-9
    assert touch > term  # strictly, away from degeneracy


def test_terminal_digital_limits_and_monotonicity():
    S0, T, sig, fwd = 60000, 0.5, 0.45, 60000
    deep_itm = deribit.terminal_digital_prob(S0, 1000, T, sig, fwd)
    deep_otm = deribit.terminal_digital_prob(S0, 500000, T, sig, fwd)
    atm = deribit.terminal_digital_prob(S0, fwd, T, sig, fwd)
    assert deep_itm > 0.99
    assert deep_otm < 0.01
    assert 0.40 < atm < 0.55          # ~0.5 at the forward (slightly below from -sig^2 T/2)
    ks = [deribit.terminal_digital_prob(S0, k, T, sig, fwd) for k in (40000, 60000, 80000, 120000)]
    assert all(p2 < p1 for p1, p2 in zip(ks, ks[1:]))


def test_down_touch_high_when_barrier_just_below_spot():
    p = deribit.one_touch_prob(60000, 59000, 0.5, 0.45, 60000)
    assert p > 0.5  # a barrier 1.7% below spot over 6mo at 45% vol is very likely touched


def _mock_book_summary():
    # two expiries, a small smile each; mark_iv in percent, underlying_price = forward
    rows = []
    for days, fwd, ivs in [(30, 61000, {"50000": 60, "61000": 42, "80000": 45}),
                           (180, 62000, {"40000": 68, "62000": 43, "120000": 47})]:
        edate = (NOW + timedelta(days=days)).strftime("%d%b%y").upper()
        for k, iv in ivs.items():
            for cp in ("C", "P"):
                rows.append({"instrument_name": f"BTC-{edate}-{k}-{cp}",
                             "mark_iv": iv, "underlying_price": fwd})
    return rows


def test_build_surface_and_interpolation():
    surf = deribit.build_surface(_mock_book_summary(), asof=NOW)
    assert surf.spot > 0
    assert len(surf.term) == 2
    # ATM IV at ~30d should be near the 42% smile center of the near expiry
    iv30, fwd30 = surf.atm_forward(surf.years_to(NOW + timedelta(days=30)))
    assert 0.40 < iv30 < 0.55
    # skew: deep OTM put strike carries higher IV than ATM
    iv_low = surf.iv_at(40000, surf.years_to(NOW + timedelta(days=180)))
    iv_atm = surf.iv_at(62000, surf.years_to(NOW + timedelta(days=180)))
    assert iv_low > iv_atm


def test_implied_prob_dispatch():
    surf = deribit.build_surface(_mock_book_summary(), asof=NOW)
    end = NOW + timedelta(days=180)
    dig = deribit.implied_prob(surf, "digital", 80000, end)
    touch = deribit.implied_prob(surf, "touch", 80000, end)
    assert dig is not None and touch is not None
    assert touch >= dig          # touch easier than terminal-above
    assert deribit.implied_prob(surf, "bogus", 80000, end) is None


# ───────────────────────── calibration statistics ─────────────────────────

def _synthetic(slope_true: float, n=4000, seed=1):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        p = rng.uniform(0.05, 0.95)
        true = min(max(0.5 + slope_true * (p - 0.5), 0.01), 0.99)
        rows.append({"pm_ask": p, "pm_bid": p - 0.01,
                     "outcome": 1 if rng.random() < true else 0,
                     "slug": f"evt{i % 80}", "iv_implied": p})
    return rows


def test_logistic_slope_recovers_calibrated():
    b = analysis.logistic_slope([(r["pm_ask"], r["outcome"]) for r in _synthetic(1.0)])
    assert 0.8 < b < 1.2


def test_logistic_slope_detects_over_and_under_confidence():
    over = analysis.logistic_slope([(r["pm_ask"], r["outcome"]) for r in _synthetic(0.5)])
    under = analysis.logistic_slope([(r["pm_ask"], r["outcome"]) for r in _synthetic(1.8)])
    assert over < 1.0 < under


def test_logistic_slope_thin_returns_none():
    assert analysis.logistic_slope([(0.5, 1)] * 10) is None


def test_clustered_bootstrap_brackets_base_with_many_clusters():
    rows = _synthetic(1.0)
    st = analysis.clustered_bootstrap(rows, analysis._slope_stat, nboot=400)
    assert st is not None
    assert st["lo"] <= st["base"] <= st["hi"]
    assert st["nclusters"] == 80
    assert st["t"] is not None


def test_clustered_bootstrap_single_cluster_degenerate():
    # the bug the earlier scratchpad run hit: one cluster -> zero-width, no spurious CI
    rows = [{"pm_ask": 0.6, "pm_bid": 0.59, "outcome": i % 2, "slug": "only"}
            for i in range(40)]
    st = analysis.clustered_bootstrap(rows, analysis._favorite_net_stat, nboot=200)
    # favorite band excludes 0.6, so stat is None -> bootstrap returns None
    assert st is None
    st2 = analysis.clustered_bootstrap(rows, analysis._slope_stat, nboot=200)
    assert st2 is not None and st2["nclusters"] == 1


def test_fee_brier_logloss():
    assert analysis.fee_per_share(0.5) == pytest.approx(analysis.DEFAULT_FEE_RATE * 0.25)
    assert analysis.fee_per_share(0.99) < 0.001
    assert analysis.brier([(1.0, 1), (0.0, 0)]) == pytest.approx(0.0)
    assert analysis.brier([(0.5, 1), (0.5, 0)]) == pytest.approx(0.25)
    assert analysis.logloss([(0.99, 1)]) < 0.02


def test_reliability_bins():
    rows = [{"pm_ask": 0.9, "outcome": 1} for _ in range(8)] + \
           [{"pm_ask": 0.1, "outcome": 0} for _ in range(8)]
    rel = analysis.reliability(rows)
    bands = {(round(b["lo"], 2), round(b["hi"], 2)): b for b in rel}
    hi = bands[(0.85, 0.95)]
    assert hi["win_rate"] == pytest.approx(1.0) and hi["gap"] == pytest.approx(0.1, abs=1e-6)


# ───────────────────────── kill-bar verdicts ─────────────────────────

def test_iv_cross_check_flags_options_fair():
    rows = [{"coin": "bitcoin", "family": "touch_window", "pm_ask": p,
             "iv_implied": p + 0.005, "slug": f"e{i%30}"}
            for i, p in enumerate([0.05, 0.1, 0.5, 0.9] * 30)]
    rep = analysis.iv_cross_check(rows)
    fam = rep["bitcoin/touch_window"]
    assert "OPTIONS-FAIR" in fam["verdict"]
    # validate the underlying gap, not just the string: pm_ask - iv = -0.005 within EXEC_COST
    assert all(abs(b["base"]) < analysis.EXEC_COST for b in fam["bands"].values())
    assert fam["bands"]["longshot"]["base"] == pytest.approx(-0.005, abs=1e-6)


def test_iv_cross_check_flags_deviation():
    rows = [{"coin": "bitcoin", "family": "touch_window", "pm_ask": p,
             "iv_implied": p - 0.08, "slug": f"e{i%30}"}
            for i, p in enumerate([0.05, 0.1, 0.15] * 40)]
    rep = analysis.iv_cross_check(rows)
    assert "deviates" in rep["bitcoin/touch_window"]["verdict"]


def test_iv_cross_check_skips_coins_without_surface():
    # rows with iv_implied None (no Deribit surface, e.g. SOL/XRP) are excluded
    rows = [{"coin": "solana", "family": "daily_updown", "pm_ask": 0.9,
             "iv_implied": None, "slug": f"e{i}"} for i in range(20)]
    assert analysis.iv_cross_check(rows) == {}


def test_verdict_accumulating_when_few_events():
    v = analysis._verdict(None, None, None, None, nclusters=5)
    assert "ACCUMULATING" in v


def test_verdict_kill_when_no_edge():
    dead = {"base": -0.05, "lo": -0.1, "hi": -0.01, "p10": -0.08, "t": -1.0, "n": 200, "nclusters": 40}
    v = analysis._verdict(dead, dead, None, None, nclusters=40)
    assert v.startswith("KILL")


def test_verdict_confirm_fade_when_edge_and_iv_agree():
    fade = {"base": 0.04, "lo": 0.02, "hi": 0.06, "p10": 0.025, "t": 3.0, "n": 300, "nclusters": 40}
    no_fav = {"base": -0.01, "lo": -0.05, "hi": 0.01, "p10": -0.04, "t": -0.5, "n": 300, "nclusters": 40}
    iv_long = {"base": 0.05, "lo": 0.02, "hi": 0.08, "p10": 0.03, "t": 3.0, "n": 200, "nclusters": 40}
    v = analysis._verdict(no_fav, fade, None, iv_long, nclusters=40)
    assert "CONFIRM" in v and "fade" in v


def test_verdict_kill_on_low_t_despite_positive_point():
    # adequate events, positive point estimate but t<2 -> not significant -> KILL
    weak = {"base": 0.05, "lo": -0.01, "hi": 0.10, "p10": 0.005, "t": 1.2, "n": 300, "nclusters": 40}
    v = analysis._verdict(weak, weak, None, None, nclusters=40)
    assert v.startswith("KILL")


def test_verdict_kill_on_options_fair():
    iv_fav = {"base": -0.005, "lo": -0.02, "hi": 0.01, "p10": -0.015, "t": -0.5, "n": 200, "nclusters": 40}
    iv_long = {"base": 0.004, "lo": -0.01, "hi": 0.02, "p10": -0.006, "t": 0.4, "n": 200, "nclusters": 40}
    v = analysis._verdict(None, None, iv_fav, iv_long, nclusters=40)
    assert "KILL" in v and "options-fair" in v


def test_verdict_undetermined_resolution_signal_but_options_disagree():
    fade = {"base": 0.04, "lo": 0.02, "hi": 0.06, "p10": 0.025, "t": 3.0, "n": 300, "nclusters": 40}
    # longshot IV gap points the WRONG way for a fade (PM cheaper than options, not richer)
    iv_long = {"base": -0.05, "lo": -0.08, "hi": -0.02, "p10": -0.07, "t": -3.0, "n": 200, "nclusters": 40}
    v = analysis._verdict(None, fade, None, iv_long, nclusters=40)
    assert v.startswith("UNDETERMINED")


# ───────────────────────── discovery / parsing ─────────────────────────

def test_classify_families():
    assert discovery.classify("bitcoin-above-on-june-25-2026") == ("daily_updown", "bitcoin", "digital")
    assert discovery.classify("what-price-will-ethereum-hit-in-june-2026") == ("touch_window", "ethereum", "touch")
    assert discovery.classify("bitcoin-all-time-high-by") == ("touch_milestone", "bitcoin", "touch")
    assert discovery.classify("when-will-bitcoin-hit-150k") == ("touch_milestone", "bitcoin", "touch")
    assert discovery.classify("bitcoin-price-on-june-25-2026") is None  # brackets excluded
    assert discovery.classify("trump-2028") is None


def test_strike_parsing():
    assert discovery._strike_of({"groupItemTitle": "60,000"}) == 60000
    assert discovery._strike_of({"groupItemTitle": "↑ 115,000"}) == 115000
    assert discovery._strike_of({"groupItemTitle": "60k"}) == 60000
    assert discovery._strike_of({"groupItemTitle": "no number"}) is None


def _daily_event(closed=False, prices=None):
    # production-shaped: JSON-stringified clobTokenIds / outcomePrices
    def mk(strike, tok, px):
        return {"groupItemTitle": f"{strike:,}", "conditionId": f"c{strike}",
                "clobTokenIds": f'["{tok}", "{tok}b"]',
                "outcomePrices": px, "closed": closed}
    return {"slug": "bitcoin-above-on-june-25-2026", "endDate": "2026-06-25T16:00:00Z",
            "negRisk": False, "closed": closed,
            "markets": [mk(54000, "t54", prices[0]), mk(64000, "t64", prices[1])]}


def test_parse_event_open():
    refs = discovery.parse_event(_daily_event(closed=False, prices=['["0.99","0.01"]', '["0.05","0.95"]']))
    assert len(refs) == 2
    assert refs[0].coin == "bitcoin" and refs[0].pricing_kind == "digital"
    assert refs[0].strike == 54000 and refs[0].token0_id == "t54"
    assert all(r.outcome is None for r in refs)  # open -> unresolved


def test_parse_event_resolution():
    refs = discovery.parse_event(_daily_event(closed=True, prices=['["1","0"]', '["0","1"]']))
    by_strike = {r.strike: r for r in refs}
    assert by_strike[54000].outcome == 1   # above 54k resolved YES
    assert by_strike[64000].outcome == 0   # above 64k resolved NO


def test_parse_event_midlife_price_not_resolved():
    # closed flag but a non-degenerate price -> not counted as resolved
    refs = discovery.parse_event(_daily_event(closed=True, prices=['["0.60","0.40"]', '["0.55","0.45"]']))
    assert all(r.outcome is None for r in refs)


def test_parse_event_non_target_returns_empty():
    assert discovery.parse_event({"slug": "trump-wins", "markets": [{}]}) == []


def test_parse_event_non_btc_coin_still_parses():
    # parse_event must NOT filter by coin (the no-surface handling happens later); SOL parses.
    ev = {"slug": "solana-above-on-june-25-2026", "endDate": "2026-06-25T16:00:00Z",
          "closed": False, "negRisk": False,
          "markets": [{"groupItemTitle": "140", "conditionId": "cs140",
                       "clobTokenIds": '["ts140","ts140n"]', "outcomePrices": '["0.4","0.6"]'}]}
    refs = discovery.parse_event(ev)
    assert len(refs) == 1 and refs[0].coin == "solana" and refs[0].strike == 140


def test_strike_parsing_across_families():
    assert discovery._strike_of({"groupItemTitle": "↑ 72,500"}) == 72500
    assert discovery._strike_of({"groupItemTitle": "150,000"}) == 150000
    assert discovery._strike_of({"groupItemTitle": "2.20"}) == pytest.approx(2.20)


# ───────────────────────── CLOB book parsing ─────────────────────────

def test_quote_from_book():
    book = {"bids": [{"price": "0.49", "size": "100"}, {"price": "0.50", "size": "200"}],
            "asks": [{"price": "0.53", "size": "50"}, {"price": "0.52", "size": "80"}]}
    q = clob.quote_from_book(book)
    assert q["pm_bid"] == 0.50          # best bid = highest
    assert q["pm_ask"] == 0.52          # best ask = lowest
    assert q["pm_mid"] == pytest.approx(0.51)
    assert q["bid_depth_usd"] == pytest.approx(0.49 * 100 + 0.50 * 200)


def test_quote_from_empty_book():
    q = clob.quote_from_book({})
    assert q["pm_bid"] is None and q["pm_ask"] is None and q["pm_mid"] is None


# ───────────────────────── async store ─────────────────────────

@pytest.mark.asyncio
async def test_store_roundtrip(tmp_path):
    store = CalibrationStore(str(tmp_path / "calib.db"))
    await store.initialize()
    try:
        end = (NOW + timedelta(days=5)).timestamp()
        row = {"taken_ts": NOW.isoformat(), "taken_epoch": NOW.timestamp(),
               "condition_id": "c1", "token0_id": "t1", "slug": "bitcoin-above-on-june-29-2026",
               "coin": "bitcoin", "family": "daily_updown", "pricing_kind": "digital",
               "strike": 60000, "side": "ABOVE", "end_ts": end, "horizon_days": 5,
               "lead_s": 5 * 86400, "pm_bid": 0.79, "pm_ask": 0.80, "pm_mid": 0.795,
               "ask_depth_usd": 500, "bid_depth_usd": 600, "iv_implied": 0.83,
               "iv_used": 0.45, "spot": 61000, "fwd": 61050, "t_years": 0.0137}
        assert await store.insert_snapshots([row]) == 1
        st = await store.status()
        assert st["snapshots"] == 1 and st["markets_tracked"] == 1

        # not yet ended -> nothing to label
        assert await store.conditions_needing_label(NOW.timestamp()) == []
        # after end + grace -> needs label
        future = end + 7200
        needing = await store.conditions_needing_label(future)
        assert len(needing) == 1 and needing[0]["condition_id"] == "c1"

        await store.upsert_resolution("c1", row["slug"], "t1", 1, "2026-06-29T16:00:00Z")
        joined = await store.join_rows()
        assert len(joined) == 1 and joined[0]["outcome"] == 1 and joined[0]["pm_ask"] == 0.80
        # idempotent re-label
        await store.upsert_resolution("c1", row["slug"], "t1", 1, "2026-06-29T16:00:00Z")
        assert (await store.status())["resolutions"] == 1
        assert len(await store.latest_snapshots()) == 1
    finally:
        await store.close()


# ───────────────────────── end-to-end with a fake client ─────────────────────────

class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._p = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeClient:
    """Routes Gamma /events, CLOB /book, Deribit book-summary by URL."""
    def __init__(self, events, book):
        self._events = events
        self._book = book

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if "deribit.com" in url:
            return _FakeResp({"result": _mock_book_summary()})
        if url.endswith("/events"):
            if params and params.get("offset", 0) > 0:
                return _FakeResp([])           # single page
            return _FakeResp(self._events)
        if url.endswith("/book"):
            return _FakeResp(self._book)
        return _FakeResp({})


@pytest.mark.asyncio
async def test_end_to_end_snapshot_label_analyze(tmp_path, monkeypatch):
    # an OPEN long-horizon touch ladder with a couple of rungs
    open_event = {"slug": "what-price-will-bitcoin-hit-before-2027",
                  "endDate": (NOW + timedelta(days=180)).isoformat(),
                  "negRisk": True, "closed": False,
                  "markets": [
                      {"groupItemTitle": "80,000", "conditionId": "cd80",
                       "clobTokenIds": '["tok80","tok80n"]', "outcomePrices": '["0.30","0.70"]'},
                      {"groupItemTitle": "120,000", "conditionId": "cd120",
                       "clobTokenIds": '["tok120","tok120n"]', "outcomePrices": '["0.08","0.92"]'}]}
    book = {"bids": [{"price": "0.29", "size": "100"}], "asks": [{"price": "0.31", "size": "100"}]}

    def fake_client_factory(*a, **k):
        return _FakeClient([open_event], book)

    monkeypatch.setattr(harness.httpx, "AsyncClient", fake_client_factory)

    store = CalibrationStore(str(tmp_path / "calib.db"))
    await store.initialize()
    try:
        snap = await harness.snapshot_pass(store, max_pages=1)
        assert snap["snapshotted"] == 2
        assert snap["by_family"]["touch_window"] == 2
        assert snap["spot"] > 0

        # ivcheck (no DB) should run and return per coin/family cross-check
        iv = await harness.ivcheck_pass(max_pages=1)
        assert iv["n_rows"] == 2 and "bitcoin/touch_window" in iv["cross_check"]

        # analyze with no resolutions yet
        rep = await harness.analyze_pass(store)
        assert rep["status"]["snapshots"] == 2
        assert rep["families"] == {}   # nothing resolved yet
    finally:
        await store.close()


def _ref(coin, kind, strike, cid, tok, end_days=180, family="touch_window"):
    return discovery.MarketRef(
        slug=f"{coin}-ladder", coin=coin, family=family, pricing_kind=kind,
        condition_id=cid, token0_id=tok, strike=strike, title=str(strike),
        end_dt=NOW + timedelta(days=end_days), neg_risk=True, closed=False, outcome=None)


@pytest.mark.asyncio
async def test_collect_rows_touch_side_up_down():
    surf = deribit.build_surface(_mock_book_summary(), asof=NOW)  # spot ~61000
    book = {"bids": [{"price": "0.29", "size": "100"}], "asks": [{"price": "0.31", "size": "100"}]}
    client = _FakeClient([], book)
    refs = [_ref("bitcoin", "touch", surf.spot + 20000, "cu", "tu"),   # above spot -> UP
            _ref("bitcoin", "touch", surf.spot - 20000, "cd", "td")]   # below spot -> DN
    rows = await harness._collect_rows(client, {"bitcoin": surf}, refs)
    sides = {r["strike"]: r["side"] for r in rows}
    assert sides[surf.spot + 20000] == "UP"
    assert sides[surf.spot - 20000] == "DN"
    assert all(r["iv_implied"] is not None for r in rows)


@pytest.mark.asyncio
async def test_collect_rows_coin_without_surface_gets_none_iv():
    surf = deribit.build_surface(_mock_book_summary(), asof=NOW)
    book = {"bids": [{"price": "0.29", "size": "100"}], "asks": [{"price": "0.31", "size": "100"}]}
    client = _FakeClient([], book)
    # solana has no surface in the dict -> iv_implied None, side None, but still recorded
    refs = [_ref("solana", "touch", 140, "cs", "ts", family="daily_updown")]
    rows = await harness._collect_rows(client, {"bitcoin": surf}, refs)
    assert len(rows) == 1
    assert rows[0]["iv_implied"] is None and rows[0]["side"] is None
    assert rows[0]["pm_ask"] == 0.31   # still forward-recorded


@pytest.mark.asyncio
async def test_collect_rows_skips_no_ask_market():
    surf = deribit.build_surface(_mock_book_summary(), asof=NOW)
    client = _FakeClient([], {})   # empty book -> no ask
    refs = [_ref("bitcoin", "touch", 80000, "c1", "t1")]
    rows = await harness._collect_rows(client, {"bitcoin": surf}, refs)
    assert rows == []


@pytest.mark.asyncio
async def test_latest_snapshots_picks_most_recent(tmp_path):
    store = CalibrationStore(str(tmp_path / "c.db"))
    await store.initialize()
    try:
        base = dict(condition_id="c1", token0_id="t1", slug="s", coin="bitcoin",
                    family="touch_window", pricing_kind="touch", strike=80000, side="UP",
                    end_ts=(NOW + timedelta(days=180)).timestamp(), horizon_days=180,
                    lead_s=180 * 86400, pm_bid=0.29, pm_mid=0.30, ask_depth_usd=1,
                    bid_depth_usd=1, iv_used=0.45, spot=61000, fwd=61050, t_years=0.49)
        await store.insert_snapshots([
            {**base, "taken_ts": "2026-06-20T00:00:00+00:00", "taken_epoch": 1000.0,
             "pm_ask": 0.30, "iv_implied": 0.25},
            {**base, "taken_ts": "2026-06-24T00:00:00+00:00", "taken_epoch": 2000.0,
             "pm_ask": 0.34, "iv_implied": 0.25}])
        latest = await store.latest_snapshots()
        assert len(latest) == 1 and latest[0]["pm_ask"] == 0.34
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_monitor_loop_schedules_and_survives_errors(monkeypatch):
    calls = {"snap": 0, "label": 0}

    async def fake_snap(store, max_pages=8):
        calls["snap"] += 1
        if calls["snap"] == 2:
            raise RuntimeError("transient gamma blip")  # must NOT kill the loop
        return {"snapshotted": 1, "discovered": 1, "by_family": {"touch_window": 1}}

    async def fake_label(store):
        calls["label"] += 1
        return {"checked": 0, "labeled": 0}

    monkeypatch.setattr(harness, "snapshot_pass", fake_snap)
    monkeypatch.setattr(harness, "label_pass", fake_label)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    clk = [0.0]  # frozen clock -> label fires only on the first iteration
    await harness.monitor_loop(store=None, snapshot_interval_s=10, label_interval_s=100,
                               iterations=3, sleep_fn=fake_sleep, clock=lambda: clk[0],
                               log=lambda *a: None)
    assert calls["snap"] == 3            # snapshot attempted every iteration (incl. the error one)
    assert calls["label"] == 1           # labeled once (clock didn't advance past interval)
    assert sleeps == [10, 10]            # slept snapshot_interval between iters, not after the last


@pytest.mark.asyncio
async def test_label_pass_end_to_end(tmp_path, monkeypatch):
    store = CalibrationStore(str(tmp_path / "c.db"))
    await store.initialize()
    try:
        # a snapshot for a market that has since ended (end_ts ~2 days ago)
        end_ts = (NOW - timedelta(days=2)).timestamp()
        await store.insert_snapshots([{
            "taken_ts": (NOW - timedelta(days=9)).isoformat(),
            "taken_epoch": (NOW - timedelta(days=9)).timestamp(),
            "condition_id": "cd80", "token0_id": "tok80",
            "slug": "what-price-will-bitcoin-hit-before-2027", "coin": "bitcoin",
            "family": "touch_window", "pricing_kind": "touch", "strike": 80000, "side": "UP",
            "end_ts": end_ts, "horizon_days": 7, "lead_s": 7 * 86400,
            "pm_bid": 0.29, "pm_ask": 0.31, "pm_mid": 0.30, "ask_depth_usd": 100,
            "bid_depth_usd": 100, "iv_implied": 0.25, "iv_used": 0.45,
            "spot": 61000, "fwd": 61050, "t_years": 0.02}])

        # the now-CLOSED event resolves token0 (touched $80k) -> YES
        closed_event = {"slug": "what-price-will-bitcoin-hit-before-2027",
                        "endDate": "2026-06-22T16:00:00Z", "closed": True, "negRisk": True,
                        "markets": [{"groupItemTitle": "80,000", "conditionId": "cd80",
                                     "clobTokenIds": '["tok80","tok80n"]',
                                     "outcomePrices": '["1","0"]', "closed": True}]}
        monkeypatch.setattr(harness.httpx, "AsyncClient",
                            lambda *a, **k: _FakeClient([closed_event], {}))

        rep = await harness.label_pass(store)
        assert rep["labeled"] == 1
        joined = await store.join_rows()
        assert len(joined) == 1 and joined[0]["outcome"] == 1
        # idempotent: a second label pass does not double-count
        rep2 = await harness.label_pass(store)
        assert rep2["checked"] == 0   # cd80 now in resolutions, excluded
    finally:
        await store.close()
