"""Phase 3 — microstructure mispricing: is "the price is mechanically wrong
right now" a capturable edge, ignoring the BTC model entirely?

Data = DECISION-MOMENT stamps only (trades + ghosts + CF worst/scalp moments).
Q1  Cross-book sum deviation (s = ask_up + ask_down): lockable both-sides arb?
Q2  Near-expiry phantom fade: fade sides priced >=0.85/0.90 inside 90s/60s.
Q3  Thin/stale book premium via depth_usd_top20 (NOTE: this field is BINANCE
    BTC depth, main.py:1146 depth_feed.get_depth_usd(), not CLOB depth).
Q4  Verdict per sub-question.

Run: python scripts/goal_eval/phase3_mispricing.py
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")

MEM = Path(__file__).resolve().parent.parent.parent / "polybot" / "memory"
ET = ZoneInfo("America/New_York")
FEE = 0.07


# ---------------------------------------------------------------- loading
def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def rec_ts(r: dict) -> str:
    return max(r.get("timestamp") or "", r.get("exit_timestamp") or "")


def dedupe_by_pid(records: list[dict], label: str) -> list[dict]:
    best: dict[int, dict] = {}
    no_pid = []
    for r in records:
        pid = r.get("position_id")
        if pid is None:
            no_pid.append(r)
            continue
        if pid not in best or rec_ts(r) > rec_ts(best[pid]):
            best[pid] = r
    dropped = len(records) - len(no_pid) - len(best)
    print(f"  {label}: {len(records)} loaded -> {len(best) + len(no_pid)} after "
          f"position_id dedupe ({dropped} duplicates dropped, {len(no_pid)} no-pid kept)")
    return list(best.values()) + no_pid


def dedupe_ghosts(records: list[dict]) -> list[dict]:
    seen, out, dropped = set(), [], 0
    for r in records:
        key = (r.get("market_id"), r.get("gate_name"), r.get("timestamp"))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(r)
    print(f"  ghosts: {len(records)} loaded -> {len(out)} after exact-key dedupe "
          f"({dropped} duplicates dropped)")
    return out


def et_day(ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%Y-%m-%d")
    except (ValueError, AttributeError, TypeError):
        return None


def tc(r: dict) -> dict:
    return (r.get("indicator_snapshot") or {}).get("trade_context") or {}


# ---------------------------------------------------------------- stats
def pctile(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        return float("nan")
    idx = (len(s) - 1) * p / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def day_t(pairs: list[tuple[str, float]]) -> tuple[float, float, int]:
    """(mean of per-day means, t, n_days). t = mean / (pstdev/sqrt(n_days))."""
    by_day = defaultdict(list)
    for d, v in pairs:
        by_day[d].append(v)
    dms = [st.mean(v) for v in by_day.values()]
    n = len(dms)
    if n < 2:
        return (dms[0] if dms else float("nan")), float("nan"), n
    m, sd = st.mean(dms), st.pstdev(dms)
    t = m / (sd / n ** 0.5) if sd > 0 else float("nan")
    return m, t, n


def day_bootstrap(pairs: list[tuple[str, float]], iters: int = 1000):
    """Day-clustered bootstrap of the per-record mean. seed(42); resample days."""
    by_day = defaultdict(list)
    for d, v in pairs:
        by_day[d].append(v)
    days = sorted(by_day)
    random.seed(42)
    stats = []
    for _ in range(iters):
        sample = [v for d in random.choices(days, k=len(days)) for v in by_day[d]]
        if sample:
            stats.append(st.mean(sample))
    return st.mean(stats), pctile(stats, 10), pctile(stats, 90)


# ---------------------------------------------------------------- build pools
def build():
    trades = dedupe_by_pid(load_records("outcomes"), "outcomes")
    ghosts = dedupe_ghosts(load_records("ghost_outcomes"))
    cfs = dedupe_by_pid(load_records("counterfactuals"), "counterfactuals")

    # window winner for scalped trades via CF resolution price
    cf_res: dict[int, bool] = {}
    for c in cfs:
        cf = c.get("counterfactual") or {}
        rp = cf.get("resolution_price")
        if (c.get("actual") or {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res[c["position_id"]] = rp == 1.0

    # unified decision rows (trades + resolved ghosts)
    rows, skipped = [], Counter()
    for t in trades:
        ctx = tc(t)
        pu, pd_ = ctx.get("market_price_up"), ctx.get("market_price_down")
        if pu is None or pd_ is None:
            skipped["trade_missing_prices"] += 1
            continue
        if t.get("exit_reason") == "resolution":
            won = bool(t.get("correct"))
        elif t.get("position_id") in cf_res:
            won = cf_res[t["position_id"]]
        else:
            skipped["trade_scalp_no_cf"] += 1
            won = None
        rows.append(dict(kind="trade", side=t.get("side"), pu=pu, pd=pd_, won=won,
                         sr=ctx.get("seconds_remaining"),
                         depth=ctx.get("depth_usd_top20"),
                         day=et_day(t.get("timestamp") or "")))
    for g in ghosts:
        ctx = tc(g)
        pu, pd_ = ctx.get("market_price_up"), ctx.get("market_price_down")
        if pu is None or pd_ is None:
            skipped["ghost_missing_prices"] += 1
            continue
        if not g.get("resolved"):
            skipped["ghost_unresolved"] += 1
            continue
        rows.append(dict(kind="ghost", side=g.get("side"), pu=pu, pd=pd_,
                         won=bool(g.get("ghost_correct")),
                         sr=ctx.get("seconds_remaining"),
                         depth=ctx.get("depth_usd_top20"),
                         day=et_day(g.get("timestamp") or "")))
    print(f"  unified decisions: {len(rows)} "
          f"(trades {sum(r['kind'] == 'trade' for r in rows)}, "
          f"resolved ghosts {sum(r['kind'] == 'ghost' for r in rows)}); skipped {dict(skipped)}")

    # CF mid-window moments (chosen-side price + sr + winner)
    cf_moments, cf_skip = [], Counter()
    for c in cfs:
        actual = c.get("actual") or {}
        ctx = c.get("context_at_worst_moment") or c.get("context_at_scalp") or {}
        mp, sr = ctx.get("market_price"), ctx.get("seconds_remaining")
        if mp is None or sr is None:
            cf_skip["cf_missing_ctx"] += 1
            continue
        if actual.get("exit_reason") == "hold":
            xp = actual.get("exit_price")
            if xp not in (0.0, 1.0):
                cf_skip["cf_hold_unresolved"] += 1
                continue
            won = xp == 1.0
        else:
            rp = (c.get("counterfactual") or {}).get("resolution_price")
            if rp not in (0.0, 1.0):
                cf_skip["cf_scalp_no_resolution"] += 1
                continue
            won = rp == 1.0
        cf_moments.append(dict(kind="cf", side=c.get("side"), mp=mp, sr=sr, won=won,
                               day=et_day(c.get("timestamp") or "")))
    print(f"  CF moments: {len(cf_moments)} usable; skipped {dict(cf_skip)}")
    return rows, cf_moments


# ---------------------------------------------------------------- Q1
def q1_cross_book(rows: list[dict]) -> dict:
    print("\n" + "=" * 78)
    print("Q1  CROSS-BOOK SUM DEVIATION  (s = ask_up + ask_down at decision moment)")
    print("=" * 78)
    pool = [r for r in rows if r["pu"] is not None and r["pd"] is not None]
    sums = [(r["pu"] + r["pd"], r) for r in pool]
    s_vals = [s for s, _ in sums]
    print(f"n = {len(s_vals)} stamped decisions (trades + resolved ghosts)")
    print("percentiles of s: " + "  ".join(
        f"p{p}={pctile(s_vals, p):.3f}" for p in (1, 5, 25, 50, 75, 95, 99)))

    # histogram
    print("\nhistogram (bin = 0.01):")
    binned = Counter(round(min(max(s, 0.95), 1.10), 2) for s in s_vals)
    for b in sorted(binned):
        n = binned[b]
        bar = "#" * max(1, round(60 * n / max(binned.values())))
        print(f"  {b:5.2f} {n:6d} {bar}")

    # both-sides arb: cost per $1 payout = s + fee(both legs)
    arb = []
    for s, r in sums:
        fee_cost = FEE * (r["pu"] * (1 - r["pu"]) + r["pd"] * (1 - r["pd"]))
        profit = 1.0 - (s + fee_cost)
        if profit > 0:
            arb.append((profit, r))
    n_below = sum(1 for s in s_vals if s < 0.98)
    n_above = sum(1 for s in s_vals if s > 1.02)
    days = sorted({r["day"] for r in pool if r["day"]})
    print(f"\ns < 0.98 (would-be arb zone): {n_below}/{len(s_vals)} "
          f"({100 * n_below / len(s_vals):.2f}%)")
    print(f"s > 1.02 (sell-both — NOT capturable, no inventory/short): {n_above}/{len(s_vals)} "
          f"({100 * n_above / len(s_vals):.2f}%)")
    print(f"buy-both locks profit AFTER fee: {len(arb)}/{len(s_vals)} decisions")
    if arb:
        profits = [p for p, _ in arb]
        arb_days = Counter(r["day"] for _, r in arb)
        print(f"  mean locked profit = {st.mean(profits):.4f} /$1 payout, "
              f"max = {max(profits):.4f}, total = {sum(profits):.4f}")
        print(f"  frequency: {len(arb) / max(1, len(days)):.2f} per ET day over {len(days)} days")
        print(f"  day clustering: {dict(sorted(arb_days.items()))}")
    total_locked = sum(p for p, _ in arb)

    # censoring context from gate_stats
    stale_life = stale_today = None
    try:
        gs = json.loads((MEM / "state" / "gate_stats.json").read_text())
        stale_life = gs.get("counts", {}).get("stale_prices")
        gsc = json.loads((MEM / "state" / "gate_stats_current.json").read_text())
        stale_today = gsc.get("counts", {}).get("stale_prices")
    except (OSError, json.JSONDecodeError):
        pass
    print(f"\nCENSORING: the live gate skips price-sum outside [0.98,1.02] (main.py:1399)")
    print(f"and those ticks are NOT ghosted -> this stamped pool only sees sums that")
    print(f"passed the gate (or Gamma-fallback ticks that bypass it). gate_stats counts")
    print(f"'stale_prices' tick-skips: lifetime={stale_life}, today={stale_today}.")
    print(f"Those are per-TICK counts (a single bad window re-fires every tick), with no")
    print(f"magnitude/side recorded — we cannot tell what fraction were s<0.98 (arbable)")
    print(f"vs s>1.02 (unshortable), nor whether a fill was available at those prints.")
    return dict(n=len(s_vals), p50=pctile(s_vals, 50),
                frac_below_098=n_below / len(s_vals),
                frac_above_102=n_above / len(s_vals),
                n_arb_after_fee=len(arb), total_locked_profit=total_locked,
                stale_prices_lifetime=stale_life, stale_prices_today=stale_today)


# ---------------------------------------------------------------- Q2
def q2_phantom_fade(rows: list[dict], cf_moments: list[dict]) -> dict:
    print("\n" + "=" * 78)
    print("Q2  NEAR-EXPIRY PHANTOM FADE  (buy the cheap side when one side >= 0.85/0.90)")
    print("=" * 78)

    # spread proxy from entry stamps: ask_up + ask_down - 1 (>=0) ~ one full spread
    spreads = [max(0.0, r["pu"] + r["pd"] - 1.0) for r in rows]
    spread_proxy = pctile(spreads, 50)
    print(f"spread proxy (median of max(0, s-1) over entry stamps): {spread_proxy:.3f}")

    # build fade candidates: (day, price_extreme, price_cheap, cheap_won, source)
    def entry_candidates(thresh, max_sr):
        out = []
        for r in rows:
            if r["sr"] is None or r["sr"] >= max_sr or r["won"] is None:
                continue
            winner = r["side"] if r["won"] else ("Down" if r["side"] == "Up" else "Up")
            for ext, cheap, p_ext, p_cheap in (("Up", "Down", r["pu"], r["pd"]),
                                               ("Down", "Up", r["pd"], r["pu"])):
                if p_ext >= thresh and 0 < p_cheap < 1:
                    out.append(dict(day=r["day"], p_ext=p_ext, p_cheap=p_cheap,
                                    ext_won=winner == ext, cheap_won=winner == cheap,
                                    src=r["kind"]))
        return out

    def cf_candidates(thresh, max_sr):
        out = []
        for m in cf_moments:
            if m["sr"] >= max_sr or not (0 < m["mp"] < 1):
                continue
            if m["mp"] >= thresh:  # chosen side is the extreme one
                p_cheap = min(0.999, max(0.001, 1.0 - m["mp"] + spread_proxy))
                out.append(dict(day=m["day"], p_ext=m["mp"], p_cheap=p_cheap,
                                ext_won=m["won"], cheap_won=not m["won"], src="cf"))
        return out

    results = {}
    for max_sr in (90, 60):
        for thresh in (0.85, 0.90):
            cands = entry_candidates(thresh, max_sr) + cf_candidates(thresh, max_sr)
            label = f"sr<{max_sr}s, extreme>={thresh:.2f}"
            if not cands:
                print(f"\n[{label}] n=0 — no stamped decisions in this slice")
                continue
            n = len(cands)
            srcs = Counter(c["src"] for c in cands)
            days = sorted({c["day"] for c in cands if c["day"]})
            wr_ext = st.mean([c["ext_won"] for c in cands])
            mean_pext = st.mean([c["p_ext"] for c in cands])
            wr_cheap = st.mean([c["cheap_won"] for c in cands])
            mean_pcheap = st.mean([c["p_cheap"] for c in cands])
            # net per $1 staked on the cheap side (fee on entry, per stated convention)
            nets = []
            for c in cands:
                q = c["p_cheap"]
                fee1 = FEE * (1 - q)              # fee per $1 of size at price q
                nets.append(((1 - q) / q - fee1) if c["cheap_won"] else (-1.0 - fee1))
            mean_net = st.mean(nets)
            breakeven = st.mean([c["p_cheap"] + FEE * c["p_cheap"] * (1 - c["p_cheap"])
                                 for c in cands])
            print(f"\n[{label}]  n={n} (src: {dict(srcs)}), {len(days)} ET days")
            print(f"  EXTREME side: mean price={mean_pext:.3f}  realized WR={wr_ext:.3f}  "
                  f"(WR - price = {wr_ext - mean_pext:+.3f})")
            print(f"  CHEAP  side: mean cost={mean_pcheap:.3f}  realized WR={wr_cheap:.3f}  "
                  f"breakeven(price+fee)={breakeven:.3f}")
            print(f"  fade net per $1 staked = {mean_net:+.4f}")
            if n >= 30:
                pairs = [(c["day"], v) for c, v in zip(cands, nets) if c["day"]]
                dm, t, nd = day_t(pairs)
                bm, b10, b90 = day_bootstrap(pairs)
                print(f"  day-clustered: day-mean={dm:+.4f}  t={t:+.2f}  (n_days={nd})")
                print(f"  day-bootstrap (seed42,1000): mean={bm:+.4f}  "
                      f"p10={b10:+.4f}  p90={b90:+.4f}")
            else:
                t, nd = float("nan"), len(days)
                print(f"  n<30 — day-clustered t not computed")
            results[(max_sr, thresh)] = dict(
                n=n, wr_ext=wr_ext, mean_pext=mean_pext, wr_cheap=wr_cheap,
                mean_pcheap=mean_pcheap, net=mean_net, t=t, n_days=len(days))
    print("\nNOTE: pool is decision-moment stamps, NOT a tick stream. The bot rarely")
    print("evaluates entries with <90s left (time-multiplier/late-phase), so entry stamps")
    print("are sparse there; CF rows are worst/scalp moments of OUR OWN positions —")
    print("conditioned on us having traded, not a random sample of late-window prints.")
    print("CF cheap-side cost uses (1 - chosen_bid + spread_proxy), a proxy not an")
    print("executable ask. The prior 226k tick-level study that killed resolution-lag")
    print("fades is the stronger evidence; nothing here can resurrect it.")
    return results


# ---------------------------------------------------------------- Q3
def q3_thin_book(rows: list[dict]) -> dict:
    print("\n" + "=" * 78)
    print("Q3  THIN/STALE BOOK PREMIUM  (depth_usd_top20 quartiles vs price error)")
    print("=" * 78)
    print("WARNING: depth_usd_top20 = BINANCE BTC top-20 depth in USD")
    print("(main.py:1146 -> BinanceDepthFeed.get_depth_usd), NOT Polymarket CLOB depth.")
    print("It measures BTC-venue liquidity regime, not whether the CLOB book is thin/stale.")

    resolved = [r for r in rows if r["won"] is not None]
    have = [r for r in resolved if r["depth"] not in (None, 0)]
    cov = 100 * len(have) / len(resolved) if resolved else 0.0
    print(f"\ncoverage: {len(have)}/{len(resolved)} resolved decisions "
          f"({cov:.1f}%) carry a non-zero depth stamp")
    if len(have) < 40:
        print("too thin to bucket — skipping")
        return dict(coverage_pct=cov, q1_signed_edge=float("nan"),
                    q1_t=float("nan"), n=len(have))

    depths = sorted(r["depth"] for r in have)
    qs = [pctile(depths, p) for p in (25, 50, 75)]
    print(f"depth quartile breaks ($): q25={qs[0]:,.0f}  q50={qs[1]:,.0f}  q75={qs[2]:,.0f}")

    def chosen_price(r):
        return r["pd"] if r["side"] == "Down" else r["pu"]

    def bucket(r):
        d = r["depth"]
        return 0 if d <= qs[0] else 1 if d <= qs[1] else 2 if d <= qs[2] else 3

    print(f"\n{'quartile':>22} {'n':>5} {'mean_depth$':>12} {'|price-won|':>12} "
          f"{'signed(won-p)':>14} {'t_day':>7} {'n_days':>7}")
    out = {}
    for b, name in enumerate(["Q1 thinnest", "Q2", "Q3", "Q4 deepest"]):
        rs = [r for r in have if bucket(r) == b]
        if not rs:
            continue
        abs_err = st.mean([abs(chosen_price(r) - (1.0 if r["won"] else 0.0)) for r in rs])
        signed = [(1.0 if r["won"] else 0.0) - chosen_price(r) for r in rs]
        pairs = [(r["day"], s) for r, s in zip(rs, signed) if r["day"]]
        dm, t, nd = day_t(pairs)
        print(f"{name:>22} {len(rs):>5} {st.mean([r['depth'] for r in rs]):>12,.0f} "
              f"{abs_err:>12.3f} {st.mean(signed):>+14.4f} {t:>7.2f} {nd:>7}")
        out[b] = dict(n=len(rs), abs_err=abs_err, signed=st.mean(signed), t=t, n_days=nd)
    q1 = out.get(0, {})
    print("\nNOTE: 'price error' here = |chosen-side ask - resolution|, which is mostly")
    print("irreducible outcome variance at 5-min horizon, not mispricing; only the")
    print("SIGNED edge (won - price) speaks to systematic cheapness.")
    return dict(coverage_pct=cov, q1_signed_edge=q1.get("signed", float("nan")),
                q1_t=q1.get("t", float("nan")), n=len(have), buckets=out)


# ---------------------------------------------------------------- main
def main() -> None:
    print("Phase 3 — microstructure mispricing (decision-moment stamps only)")
    print("=" * 78)
    print("loading once...")
    rows, cf_moments = build()
    n_decisions = len(rows)
    days = sorted({r["day"] for r in rows if r["day"]})
    print(f"  decision pool spans {len(days)} ET days: {days[0]} .. {days[-1]}")

    q1 = q1_cross_book(rows)
    q2 = q2_phantom_fade(rows, cf_moments)
    q3 = q3_thin_book(rows)

    print("\n" + "=" * 78)
    print("Q4  VERDICT — MispricingEngine (internal price inconsistency, no BTC model)")
    print("=" * 78)
    print(f"""
1) CROSS-BOOK ARB: {'NO on this data' if q1['n_arb_after_fee'] == 0 else 'see counts above'} —
   {q1['n_arb_after_fee']} of {q1['n']} stamped decisions lock a both-sides profit after fee;
   {100 * q1['frac_below_098']:.2f}% of stamps sit below 0.98 at all. BUT the pool is censored:
   the [0.98,1.02] gate skips exactly the interesting region without ghosting it
   ({q1['stale_prices_lifetime']} lifetime tick-skips recorded as 'stale_prices'). UNTESTABLE
   for the out-of-band region. To test: log (s, best_ask_up, best_ask_down, top-of-book
   sizes, ts) on every price-sum gate fire in main.py:_fetch_market_prices (line ~1399),
   or record continuous BBA snapshots in clob_ws.ClobWebSocket._on_best_bid_ask /
   _on_book — that is the tick-level recorder the deleted 226k-row study used.

2) PHANTOM FADE: testable only on sparse, self-selected late-window stamps (see table) —
   treat as weak evidence; the deleted tick-level study already killed resolution-lag
   fades on 226k rows. Nothing here overturns that.

3) THIN-BOOK PREMIUM: UNTESTABLE as specified — depth_usd_top20 is Binance BTC depth,
   not CLOB depth. CLOB thinness is never stamped on outcomes/ghosts. To test: stamp
   per-side CLOB top-5/top-20 depth USD + book age (clob_ws.get_book already holds it)
   into trade_context in main.py where depth_usd_top20 is stamped (line ~1146), for
   trades AND ghosts, at every decision.
""")


if __name__ == "__main__":
    main()
