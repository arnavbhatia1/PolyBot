"""R1: 60s margin-table re-fit at >=14 real-final days (RESEARCH.md #2 / R1 bar).

Same conventions as ws1_freeze_tables (p99.5 real-final only, one sample per
(window, k-knot), veto-passing, coverage-gated, round UP $0.5, monotone) and
ws1_interval_max (MAX per-tick interval maxima, real + synthetic union, round
UP $1, monotone). Adds what the freeze script does not print: side-by-side vs
the frozen tables, per-knot n, a reproduction check on the 08-18 freeze span,
leave-one-day-out p99.5 at the thin knots, and the largest per-tick errors at
k in [23,25]. Writes data/vps-0821/r1_tables.json.
"""
import csv
import gzip
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
OUT = DATA / "vps-0821"
sys.path.insert(0, str(SP))
from ws1_interval_max import (KNOTS, RULE_TS, HORIZON, running_avg, covered,  # noqa: E402
                              twap_frozen_at, scan, interval_max_knots)

FREEZE_END = 1787011200          # 2026-08-18 00:00 UTC — the 08-18 freeze span ends here
ET = timezone(timedelta(hours=-4))
FROZEN_P995 = dict([(2.0, 1.0), (4.0, 1.0), (6.0, 1.5), (8.0, 2.0), (10.0, 3.5),
                    (12.0, 3.5), (15.0, 5.0), (20.0, 6.0), (25.0, 8.0), (29.0, 10.5),
                    (35.0, 13.0), (40.0, 18.0), (45.0, 26.5), (50.0, 30.5), (55.0, 36.5),
                    (58.0, 38.0)])
FROZEN_MAX = dict([(2.0, 2.0), (4.0, 2.0), (6.0, 3.0), (8.0, 5.0), (10.0, 11.0),
                   (12.0, 11.0), (15.0, 18.0), (20.0, 24.0), (25.0, 24.0), (29.0, 36.0),
                   (35.0, 50.0), (40.0, 112.0), (45.0, 119.0), (50.0, 120.0), (55.0, 120.0),
                   (58.0, 120.0)])


def et_day(ep):
    return datetime.fromtimestamp(ep, ET).strftime("%m-%d")


def utc_day(ep):
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")


def p995(xs):
    xs = sorted(xs)
    if len(xs) < 50:
        return None
    return xs[min(math.ceil(0.995 * len(xs)) - 1, len(xs) - 1)]


def up(x, step):
    return math.ceil(x / step - 1e-9) * step


def fit_p995(rows):
    """Rounded, monotone p99.5 knots + raw fits + n, from gated real rows."""
    raw, n, tab = {}, {}, {}
    prev = 0.0
    for k in KNOTS:
        xs = [r["err"] for r in rows if r["k"] == k]
        q = p995(xs)
        if q is None:
            continue
        raw[k], n[k] = q, len(xs)
        v = max(up(q, 0.5), prev)
        tab[k] = v
        prev = v
    return tab, raw, n


def load_rows():
    rows = []
    for r in csv.DictReader(open(DATA / "ws1_errors60.csv")):
        if r["veto"] != "0" or float(r["gap"]) > 10.0 or not r["plain"]:
            continue
        if r["src"] != "real":
            continue
        k = float(r["k"])
        if k not in KNOTS:
            continue
        rows.append(dict(ep=int(r["ep"]), k=k,
                         err=abs(float(r["final"]) - float(r["plain"]))))
    return rows


def tick_scan_k23_25():
    """Per-tick gated plain errors with k in [23,25] — the worst windows."""
    out = []
    for line in gzip.open(DATA / "win_streams.jsonl.gz", "rt"):
        wd = json.loads(line)
        ep = wd["ep"]
        if ep < RULE_TS or not wd["final"] or not wd["strike"]:
            continue
        close = ep + 300
        t0 = close - HORIZON
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        best = None
        for rx, p in l_rx:
            k = close - rx
            if k < 23.0 or k > 25.0:
                continue
            if not covered(l_rx, t0, rx) or twap_frozen_at(trecs, l_rx, rx):
                continue
            A = running_avg(l_rx, t0, rx)
            if A is None:
                continue
            w = (rx - t0) / HORIZON
            proj = w * A + (1 - w) * p
            err = abs(wd["final"] - proj)
            if best is None or err > best[0]:
                best = (err, k, proj)
        if best:
            out.append(dict(err=round(best[0], 2), k=round(best[1], 2),
                            proj=round(best[2], 2), ep=ep,
                            utc=datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d %H:%M"),
                            strike=round(wd["strike"], 2), final=round(wd["final"], 2),
                            gap=round(abs(wd["final"] - wd["strike"]), 2)))
    out.sort(key=lambda r: -r["err"])
    return out


def main():
    rows = load_rows()
    eps = {r["ep"] for r in rows}
    et_days = sorted({et_day(e) for e in eps})
    utc_days = sorted({utc_day(e) for e in eps})
    per_et = {d: len({e for e in eps if et_day(e) == d}) for d in et_days}
    print(f"corpus: {len(eps)} real-final windows with >=1 gated knot sample; "
          f"{len(et_days)} ET days {et_days[0]}..{et_days[-1]}; {len(utc_days)} UTC days")
    print("windows per ET day:", per_et)

    # ---- full re-fit ----
    tab, raw, n = fit_p995(rows)
    imax_real, imax_all, n_ticks = scan()
    imax = dict(interval_max_knots(imax_real, imax_all))
    mx, prev = {}, 0.0
    for k in KNOTS:
        if k not in tab:
            continue
        v = max(imax[k], prev, tab[k] + 0.5)
        mx[k] = v
        prev = v

    print("\nknot k | frozen p99.5 | re-fit p99.5 (raw fit) | n real | frozen MAX | re-fit MAX (interval real / all)")
    moved = []
    for k in KNOTS:
        fp, fm = FROZEN_P995[k], FROZEN_MAX[k]
        i = KNOTS.index(k)
        flag = ""
        if tab[k] != fp:
            flag += f" P995 {tab[k] - fp:+.1f} ({tab[k] / fp:.2f}x)"
        if mx[k] != fm:
            flag += f" MAX {mx[k] - fm:+.0f} ({mx[k] / fm:.2f}x)"
        if flag:
            moved.append((k, flag.strip()))
        print(f"k={k:4.1f} | {fp:5.1f} | {tab[k]:5.1f} ({raw[k]:6.2f}) | {n[k]:5d} | {fm:6.1f} | "
              f"{mx[k]:6.1f} ({imax_real[i]:7.2f} / {imax_all[i]:7.2f}){flag}")
    print(f"\n{len(moved)}/{len(KNOTS)} knots moved:")
    for k, f in moved:
        print(f"  k={k:.0f}: {f}")

    # ---- reproduction check on the 08-18 freeze span (08-14..17 UTC) ----
    rows_fz = [r for r in rows if r["ep"] < FREEZE_END]
    tab_fz, raw_fz, n_fz = fit_p995(rows_fz)
    match = sum(1 for k in KNOTS if tab_fz.get(k) == FROZEN_P995[k])
    print(f"\nreproduction on the freeze span (ep < 08-18 00:00 UTC, "
          f"{len({r['ep'] for r in rows_fz})} windows): {match}/{len(KNOTS)} frozen p99.5 knots reproduced")
    for k in KNOTS:
        if tab_fz.get(k) != FROZEN_P995[k]:
            print(f"  k={k:.0f}: frozen {FROZEN_P995[k]} vs span re-fit {tab_fz.get(k)} "
                  f"(raw {raw_fz.get(k, float('nan')):.2f}, n {n_fz.get(k)})")

    # ---- LODO at k=25 and k=12 ----
    lodo = {}
    for label, dayf, days in (("ET", et_day, et_days), ("UTC", utc_day, utc_days)):
        print(f"\nLODO p99.5 (fit on all-but-one {label} day) — raw fit / rounded:")
        for k in (25.0, 12.0):
            vals = []
            for d in days:
                xs = [r["err"] for r in rows if r["k"] == k and dayf(r["ep"]) != d]
                q = p995(xs)
                vals.append((d, round(q, 2), up(q, 0.5)))
            qs = [v[1] for v in vals]
            print(f"  k={k:.0f}: full {raw[k]:.2f} -> {tab[k]}; folds min {min(qs):.2f} max {max(qs):.2f} "
                  f"(rounded {min(v[2] for v in vals)}..{max(v[2] for v in vals)})")
            print("    " + " ".join(f"{d}:{q:.1f}" for d, q, _ in vals))
            lodo[f"{label}_k{k:.0f}"] = vals

    # ---- worst per-tick errors at k in [23,25] ----
    worst = tick_scan_k23_25()
    print("\nlargest gated per-tick |error| at k in [23,25] (one per window):")
    for r in worst[:10]:
        print(f"  ${r['err']:6.2f} @k={r['k']:5.2f}  {r['utc']} UTC ep {r['ep']}  strike {r['strike']}  "
              f"final {r['final']}  proj {r['proj']}  |final-strike| {r['gap']}")
    for target in (24.0, 17.8, 13.7):
        hits = [r for r in worst if abs(r["err"] - target) <= 0.6]
        print(f"  ~${target}: " + (", ".join(f"${r['err']} {r['utc']}" for r in hits[:3]) or "none within $0.6"))
    grid25 = sorted((r for r in rows if r["k"] == 25.0), key=lambda r: -r["err"])[:5]
    print("  grid k=25 top 5: " + ", ".join(f"${r['err']:.2f} {utc_day(r['ep'])}" for r in grid25))

    lit_p = "TWAP_MARGIN_P995: tuple[tuple[float, float], ...] = (\n    " + ", ".join(
        f"({k:.1f}, {tab[k]:.1f})" for k in KNOTS) + ",\n)"
    lit_m = "TWAP_MARGIN_MAX: tuple[tuple[float, float], ...] = (\n    " + ", ".join(
        f"({k:.1f}, {mx[k]:.1f})" for k in KNOTS) + ",\n)"
    print("\n" + lit_p + "\n" + lit_m)
    json.dump(dict(
        corpus=dict(real_final_windows=len(eps), et_days=et_days, windows_per_et_day=per_et,
                    n_ticks_interval_scan=n_ticks),
        P995=[(k, tab[k]) for k in KNOTS], MAX=[(k, mx[k]) for k in KNOTS],
        p995_raw={str(k): raw[k] for k in KNOTS}, n_real={str(k): n[k] for k in KNOTS},
        interval_max_real={str(k): imax_real[i] for i, k in enumerate(KNOTS)},
        interval_max_all={str(k): imax_all[i] for i, k in enumerate(KNOTS)},
        frozen=dict(P995=sorted(FROZEN_P995.items()), MAX=sorted(FROZEN_MAX.items())),
        moved=moved, freeze_span_reproduction=dict(match=match, table=sorted(tab_fz.items()),
                                                    raw=sorted(raw_fz.items())),
        lodo=lodo, worst_k23_25=worst[:15], literals=dict(P995=lit_p, MAX=lit_m),
    ), open(OUT / "r1_tables.json", "w"), indent=1)
    print(f"\nsaved {OUT / 'r1_tables.json'}")


if __name__ == "__main__":
    main()
