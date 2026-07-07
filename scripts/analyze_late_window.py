"""Late-window sniper KILL BAR — does a bot-FORMABLE final-seconds signal survive
a realistic 135ms FOK fill, net of fee?

The winning wallets (e.g. 0x565ca5, +33c/$1) make their entire edge in the final
~60s by buying a directional side the CLOB hasn't fully repriced. The open question
the offline 1Hz corpus could NOT answer: is there a signal the BOT can form from its
OWN feeds (Coinbase = resolution venue, Binance aggTrade = order flow) that, hit with
a 135ms FOK, nets positive? This needs the 5 Hz late-window samples + Binance columns
the extended WindowPathRecorder now writes (recording.py). Run AFTER >= ~8 clean ET
days of post-extension recording.

Method (lookahead-safe): for each window, at the FIRST late-window instant a signal
fires (decision row i, using only info <= t_i), model the FOK fill at the ask one
sample later (~0.2s >= the 135ms RTT — conservative), settle at resolution (window_
labels), net of the dynamic taker fee. Day-cluster + block-bootstrap. Pre-registered
thresholds (not swept-then-cherry-picked); the binding gate is holding them FORWARD.

KILL BAR (all): realistic-fill day-clustered t_day >= 2.0 AND block-bootstrap p10 > 0
(net of fee, executable asks) over >= 8 clean ET days, >= 6 positive. A control
(buy spot-side at ask, no filter) must stay ~0 (G-M sanity); oracle = upper bound.

  python scripts/analyze_late_window.py [--cb-move 8] [--ask-cap 0.92] \
      [--rtt-sweep 0.04,0.08,0.135,0.20] [--max-slip 0.05]

--rtt-sweep is the edge-vs-latency curve (the fill is the ask interpolated at
decision+RTT along its repricing path) — the RTT at which momentum first clears the
bar is the latency the host must hit. --max-slip is the FOK limit tolerance (the key
reachability sensitivity; re-run at 0.02 for a strict read).

FIDELITY CAVEAT (live vs this harness): the live sniper (signal_engine.evaluate_late_sniper)
additionally requires an L1 model edge >= sniper_min_edge, which this momentum() signal
deliberately does NOT (no prob/edge floor — a conservative directional gate whose n_fills
overstates live's count; live trades the higher-conviction SUBSET). window_paths now stamps
the live-L1 `atr` and `model_prob_up` per sample (recording.py appended columns), so an exact
sniper_min_edge-subset read is possible if ever needed. Before any deploy, ALSO paper-shadow
the sniper (sniper_enabled in PAPER mode) for >= the same span and compare the realized
fills/edge head-to-head.
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PATHS_DB = ROOT / "polybot" / "db" / "window_paths.db"
LIVE_DB = ROOT / "polybot" / "db" / "polybot_live.db"   # real fills for the live kill-rule read
# window_labels accrue in the ACTIVE mode's DB — paper holds the pre-07-04
# history, live everything since the flip. Read both or the corpus freezes
# at the mode switch (and the post-live kill rule can never trip).
LABEL_DBS = [ROOT / "polybot" / "db" / "polybot_paper.db",
             ROOT / "polybot" / "db" / "polybot_live.db"]
ET = ZoneInfo("America/New_York")  # DST-correct; a fixed UTC-4 mis-buckets EST days
FEE_RATE = 0.07
LATE_START = 255.0          # only the final 45s
FILL_GAP_MAX = 0.45         # if the next sample is > this many s away, can't model the fill
MIN_FILLS = 40              # below this, "not enough data"


def fee(p: float) -> float:
    return FEE_RATE * p * (1 - p)


def et_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


def tstat(xs: list[float]):
    n = len(xs)
    if n < 2:
        return (statistics.mean(xs) if xs else float("nan"), float("nan"), n)
    m = statistics.mean(xs)
    sd = statistics.stdev(xs)
    se = sd / math.sqrt(n)
    return (m, (m / se if se > 0 else float("nan")), n)


def block_bootstrap_p10(daily: list[float], iters: int = 2000) -> float:
    """Resample whole days with replacement; p10 of the resampled day-mean.
    Seeded stdlib RNG, deterministic across runs. (A raw LCG's low bits cycle
    with period 8: at n_days=8 every draw became a permutation of all 8 days,
    so p10 degenerated to exactly the mean and the leg checked nothing.)"""
    if len(daily) < 2:
        return float("nan")
    n = len(daily)
    rng = random.Random(12345)
    means = []
    for _ in range(iters):
        means.append(sum(daily[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.10 * len(means))]


def load_windows():
    """window_id -> sorted list of late rows (dicts), for windows with a label and
    post-extension data (binance_price not null somewhere in the window)."""
    pc = sqlite3.connect(f"file:{PATHS_DB}?mode=ro", uri=True)
    pc.row_factory = sqlite3.Row
    cols = {r[1] for r in pc.execute("PRAGMA table_info(window_paths)")}
    if "binance_price" not in cols:
        pc.close()
        return None, None      # recorder hasn't run the schema migration yet
    labels = {}
    for label_db in LABEL_DBS:
        if not label_db.exists():
            continue
        lc = sqlite3.connect(f"file:{label_db}?mode=ro", uri=True)
        try:
            labels.update({r[0]: (r[1], r[2]) for r in lc.execute(
                "SELECT window_id, resolved_up, price_to_beat FROM window_labels "
                "WHERE window_id LIKE 'btc%' AND price_to_beat IS NOT NULL")})
        except sqlite3.OperationalError:
            pass  # mode DB without a window_labels table yet
        finally:
            lc.close()
    rows_by_win = defaultdict(list)
    cur = pc.execute(
        "SELECT window_id, ts, elapsed_s, bid_up, ask_up, bid_down, ask_down, "
        "coinbase_price, strike, binance_price, binance_cvd_10s, binance_cvd_30s "
        "FROM window_paths WHERE elapsed_s >= ? AND binance_price IS NOT NULL "
        "ORDER BY window_id, ts", (LATE_START - 8,))
    for r in cur:
        if r["window_id"] in labels:
            rows_by_win[r["window_id"]].append(dict(r))
    pc.close()
    return rows_by_win, labels


def cb_move(rows, i, lookback_s=2.0):
    """Coinbase price change over the ~lookback_s before row i (signed)."""
    t = rows[i]["ts"]
    cb_now = rows[i]["coinbase_price"]
    if cb_now is None:
        return None
    j = i
    while j > 0 and rows[j]["ts"] > t - lookback_s:
        j -= 1
    cb_then = rows[j]["coinbase_price"]
    if cb_then is None:
        return None
    return cb_now - cb_then


MAX_SLIP = 0.05      # default FOK limit tolerance: miss if the fill would be >this above the decision ask


def fill_ask(rows, i, side_up, rtt, max_slip=MAX_SLIP):
    """Modeled fill price for a taker order sent at the decision tick and arriving
    `rtt` seconds later. The 5Hz tape can't resolve sub-200ms timing directly, so we
    INTERPOLATE the ask along its repricing path between the two samples bracketing
    `decision_ts + rtt`. Lower rtt -> fill nearer the (stale, cheap) decision-tick
    ask; higher rtt -> nearer the repriced ask. This is what turns the harness into
    an edge-vs-latency curve. Assumes ~linear repricing within a ~0.2s inter-sample
    gap (the agent measured the trajectory is ~linear over the first sample).

    None = miss: the book gapped out, there is no sample after arrival to confirm the
    quote still existed, or it repriced >max_slip above the decision ask before arrival
    (a FOK with limit = decision_ask + max_slip would not have filled). max_slip is the
    key reachability sensitivity — a tighter limit fills fewer windows but at better prices.
    """
    dec_ask = rows[i]["ask_up"] if side_up else rows[i]["ask_down"]
    if dec_ask is None:
        return None
    arrive = rows[i]["ts"] + rtt
    # j = last sample at/*before* arrival, k = first sample *after* arrival
    j = i
    while j + 1 < len(rows) and rows[j + 1]["ts"] <= arrive:
        j += 1
    k = j + 1
    if k >= len(rows):                         # nothing after arrival -> can't confirm the quote -> miss
        return None
    if rows[k]["ts"] - rows[j]["ts"] > FILL_GAP_MAX:
        return None
    aj = rows[j]["ask_up"] if side_up else rows[j]["ask_down"]
    ak = rows[k]["ask_up"] if side_up else rows[k]["ask_down"]
    if aj is None or ak is None:
        return None
    span = rows[k]["ts"] - rows[j]["ts"]
    frac = 0.0 if span <= 0 else max(0.0, min(1.0, (arrive - rows[j]["ts"]) / span))
    fa = aj + (ak - aj) * frac
    if not (0.01 < fa < 0.99):
        return None
    if fa > dec_ask + max_slip:                # repriced past our FOK limit before arrival -> miss
        return None
    return fa


def momentum_signal(rows, i, strike, cb_move_thr=8.0, ask_cap=0.92):
    """The deployed sniper's directional rule, module-level so the CLI and the
    nightly health job share ONE implementation: a >= cb_move_thr Coinbase move
    over 2s that pushed price past strike, on the move side, if its ask <= cap."""
    mv = cb_move(rows, i, 2.0)
    cb = rows[i]["coinbase_price"]
    if mv is None or cb is None or strike is None:
        return None
    up = mv > 0
    if abs(mv) < cb_move_thr:
        return None
    if not ((up and cb > strike) or ((not up) and cb < strike)):  # move pushed it past strike
        return None
    a = rows[i]["ask_up"] if up else rows[i]["ask_down"]
    return up if (a is not None and a <= ask_cap) else None


def evaluate(rows_by_win, labels, signal_fn, label, rtt, max_slip):
    """signal_fn(rows, i) -> side_up (bool) or None. First fire per window. `rtt` is
    the modeled order round-trip (s); the fill is the ask interpolated at decision+rtt,
    a miss if it exceeds the FOK limit (decision_ask + max_slip)."""
    per_day = defaultdict(list)
    fills = []
    for wid, rows in rows_by_win.items():
        resolved_up, strike = labels[wid]
        for i in range(len(rows)):
            if rows[i]["elapsed_s"] < LATE_START:
                continue
            side_up = signal_fn(rows, i, strike)
            if side_up is None:
                continue
            fa = fill_ask(rows, i, side_up, rtt, max_slip)
            if fa is None:
                continue
            win = 1.0 if (side_up == (resolved_up == 1)) else 0.0
            net = win - fa - fee(fa)
            per_day[et_day(rows[i]["ts"])].append(net)
            fills.append((net, fa, win))
            break  # one entry per window
    if len(fills) < 2:
        return None
    series = [(day, statistics.mean(v)) for day, v in sorted(per_day.items())]
    daily = [m for _, m in series]
    m, t, n = tstat(daily)
    p10 = block_bootstrap_p10(daily)
    win_rate = statistics.mean(f[2] for f in fills)
    avg_fill = statistics.mean(f[1] for f in fills)
    net_sum = sum(f[0] for f in fills)
    npos = sum(1 for d in daily if d > 0)
    return dict(label=label, n_fills=len(fills), n_days=len(daily), win_rate=win_rate,
                avg_fill=avg_fill, mean_net_day=m, t_day=t, p10=p10,
                net_per_sh=statistics.mean(f[0] for f in fills), net_sum=net_sum,
                days_pos=npos, series=series)


def health_read(rtt=0.135, max_slip=0.05, cb_move_thr=8.0, ask_cap=0.92):
    """One-call momentum read for the nightly health job: the kill-bar momentum
    result plus the post-live kill-rule metrics (trailing-4-day mean, trailing-
    8-day t). Returns None if the corpus isn't ready. kill_rule_tripped is None
    until >= 8 ET days exist (not evaluable), then True/False."""
    rows_by_win, labels = load_windows()
    if not rows_by_win:
        return None
    r = evaluate(rows_by_win, labels,
                 lambda rows, i, s: momentum_signal(rows, i, s, cb_move_thr, ask_cap),
                 "momentum(cb_move)", rtt, max_slip)
    if r is None:
        return None
    vals = [m for _, m in r["series"]]
    r["trailing4_mean"] = statistics.mean(vals[-4:]) if len(vals) >= 4 else None
    r["trailing8_t"] = tstat(vals[-8:])[1] if len(vals) >= 8 else None
    if r["trailing8_t"] is None:
        r["kill_rule_tripped"] = None                      # < 8 days: not evaluable
    else:
        r["kill_rule_tripped"] = (r["trailing4_mean"] < 0.02) or (r["trailing8_t"] < 2.0)
    return r


def live_health_read():
    """Post-live kill-rule metrics computed from the REAL fills (polybot_live.db
    trade_history), the money-side analog of health_read() (which reads the SIM
    corpus). Same convention as the kill bar so the two are directly comparable:
    EQUAL-WEIGHT per-fill net $/share, day-clustered by ET.

    Per-fill net = (pnl - fees) / shares_held — pnl is gross (shares*outcome - size),
    fees is the entry taker fee, shares_held is the audited fill count; this equals
    the harness's win - fill - fee(fill) (verified on winners and losers) and folds
    in the live exit engine (scalp / loss-cut outcomes, not just hold-to-resolution).
    Live runs sniper_only, so every trade_history row is a sniper fire.

    kill_rule_tripped mirrors CLAUDE.md's OR-rule but activates each leg as soon as
    it has the days: trailing-4-day mean < +0.02 (+2c/sh) once >= 4 ET days, OR
    trailing-8-day t < 2.0 once >= 8. None until >= 4 live days exist. Alert-only —
    the caller never flips config (kill bars are operator authority)."""
    if not LIVE_DB.exists():
        return None
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT t.pnl AS pnl, t.fees AS fees, t.exit_timestamp AS ts, "
            "p.shares_held AS shares FROM trade_history t JOIN positions p ON t.id = p.id "
            "WHERE t.exit_timestamp IS NOT NULL AND p.shares_held > 0"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    per_day = defaultdict(list)
    fills = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        nps = (r["pnl"] - (r["fees"] or 0.0)) / r["shares"]   # net $/share, harness-consistent
        per_day[et_day(ts)].append(nps)
        fills.append((nps, 1.0 if (r["pnl"] or 0) > 0 else 0.0))
    if not fills:
        return None
    series = [(day, statistics.mean(v)) for day, v in sorted(per_day.items())]
    daily = [m for _, m in series]
    m, t, _ = tstat(daily)
    trailing4 = statistics.mean(daily[-4:]) if len(daily) >= 4 else None
    trailing8_t = tstat(daily[-8:])[1] if len(daily) >= 8 else None
    if len(daily) < 4:
        tripped = None                                        # too few live days to judge
    else:
        tripped = (trailing4 < 0.02) or (trailing8_t is not None and trailing8_t < 2.0)
    return dict(label="live(trade_history)", n_fills=len(fills), n_days=len(daily),
                win_rate=statistics.mean(f[1] for f in fills), avg_fill=float("nan"),
                mean_net_day=m, t_day=t, p10=block_bootstrap_p10(daily),
                net_per_sh=statistics.mean(f[0] for f in fills),
                net_sum=sum(f[0] for f in fills),
                days_pos=sum(1 for d in daily if d > 0), series=series,
                trailing4_mean=trailing4, trailing8_t=trailing8_t,
                kill_rule_tripped=tripped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cb-move", type=float, default=8.0, help="$ Coinbase move over ~2s to fire momentum")
    ap.add_argument("--cvd", type=float, default=1.5, help="|binance_cvd_10s| (BTC) to fire order-flow")
    ap.add_argument("--ask-cap", type=float, default=0.92, help="only buy if the side's ask is still <= this")
    ap.add_argument("--rtt-sweep", type=str, default="0.04,0.08,0.135,0.20",
                    help="comma-list of modeled order RTTs (s) to sweep — the edge-vs-latency curve. "
                         "0.04~Dublin VPS, 0.135~current Canada VPN, 0.20~one-5Hz-sample (most conservative).")
    ap.add_argument("--max-slip", type=float, default=MAX_SLIP,
                    help="FOK limit tolerance (default 0.05): a fill is a MISS if the ask repriced more "
                         "than this above the decision ask by arrival. Tighter = stricter reachability "
                         "(fewer fills, better prices). The key sensitivity — re-run at 0.02 for a strict read.")
    args = ap.parse_args()
    rtt_list = [float(x) for x in args.rtt_sweep.split(",") if x.strip()]

    rows_by_win, labels = load_windows()
    if rows_by_win is None:
        print("DATA NOT READY: window_paths has no binance_price column yet — the extended "
              "WindowPathRecorder (recording.py) has not run. Start the bot on the new code, "
              "let it accrue ~8 clean ET days of late-window samples, then re-run this.")
        return
    total_late = sum(len(v) for v in rows_by_win.values())
    n_win = len(rows_by_win)
    days = sorted({et_day(r["ts"]) for v in rows_by_win.values() for r in v})
    print(f"post-extension late-window data: {n_win} windows, {total_late} rows, "
          f"{len(days)} ET days {days[:1]}..{days[-1:]}")
    if n_win < MIN_FILLS:
        print(f"\nDATA NOT READY: only {n_win} windows with post-extension (binance_price) "
              f"late samples. The extended recorder must run first. Need ~8 clean ET days "
              f"(~{8*288} windows). Re-run this after the recorder has accrued them.")
        return

    cap = args.ask_cap

    def side_ask(rows, i, up):
        return rows[i]["ask_up"] if up else rows[i]["ask_down"]

    # --- pre-registered candidate signals (held fixed; the gate is holding them FORWARD) ---
    def momentum(rows, i, strike):
        return momentum_signal(rows, i, strike, args.cb_move, cap)

    def orderflow(rows, i, strike):
        cvd = rows[i]["binance_cvd_10s"]
        if cvd is None or abs(cvd) < args.cvd:
            return None
        up = cvd > 0
        a = side_ask(rows, i, up)
        return up if (a is not None and a <= cap) else None

    def lead(rows, i, strike):
        bn, cb = rows[i]["binance_price"], rows[i]["coinbase_price"]
        if bn is None or cb is None or strike is None:
            return None
        # Binance already past strike while Coinbase hasn't crossed as far -> buy Binance's side
        up = bn > strike
        if abs(bn - strike) < 5:
            return None
        a = side_ask(rows, i, up)
        return up if (a is not None and a <= cap) else None

    def control_spotside(rows, i, strike):     # G-M sanity: buy spot-side at ask, no filter
        cb = rows[i]["coinbase_price"]
        if cb is None or strike is None:
            return None
        up = cb > strike
        a = side_ask(rows, i, up)
        return up if (a is not None and a <= cap) else None

    sigs = [("momentum(cb_move)", momentum), ("orderflow(binance_cvd)", orderflow),
            ("lead(binance_vs_strike)", lead), ("CONTROL spot-side@ask", control_spotside)]

    print(f"\nthresholds: cb_move>=${args.cb_move:.0f}/2s, |cvd|>={args.cvd}, ask_cap<={cap}, "
          f"max_slip(FOK limit)={args.max_slip}")
    print("RTT sweep = the edge-vs-latency curve: fill is the ask interpolated at "
          "decision+RTT along its repricing path. Lower RTT -> nearer the stale (cheap) ask.")
    for rtt in rtt_list:
        print(f"\n=== modeled RTT = {rtt*1000:.0f} ms ===")
        print(f"{'signal':>26} {'fills':>6} {'days':>5} {'win%':>6} {'avg_fill':>8} "
              f"{'net/sh':>8} {'net/day':>8} {'t_day':>6} {'p10':>7} {'days+':>6}  bar")
        for name, fn in sigs:
            r = evaluate(rows_by_win, labels, fn, name, rtt, args.max_slip)
            if r is None:
                print(f"{name:>26}  (no fills)")
                continue
            passed = (r["t_day"] >= 2.0 and r["p10"] > 0 and r["n_days"] >= 8
                      and r["days_pos"] >= 6 and r["n_fills"] >= MIN_FILLS
                      and name.startswith(("momentum", "orderflow", "lead")))
            bar = "PASS" if passed else ("--" if name.startswith("CONTROL") else "fail")
            print(f"{name:>26} {r['n_fills']:>6} {r['n_days']:>5} {r['win_rate']:>6.1%} "
                  f"{r['avg_fill']:>8.3f} {r['net_per_sh']:>+8.4f} {r['mean_net_day']:>+8.4f} "
                  f"{r['t_day']:>+6.2f} {r['p10']:>+7.4f} {r['days_pos']:>4}/{r['n_days']:<2} [{bar}]")

    print("\nKILL BAR: a signal PASSES only with t_day>=2.0 AND p10>0 AND >=8 ET days "
          f"AND >=6 positive days AND >={MIN_FILLS} fills, AT A REACHABLE RTT. The one leg "
          "the print cannot enforce: CONTROL must be ~0 (G-M) at every RTT — check its row "
          "by eye. Thresholds are pre-registered; the binding gate is the SAME threshold "
          "holding FORWARD. The RTT at which momentum first clears the bar = the latency "
          "you must hit.")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
