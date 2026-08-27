"""R2/R3 (08-27 pass): deep_proj ladder on the R1 RE-FIT p99.5 tables.

Grid = need {0.5, 0.75, 1.0} x k_place_max {15, 20, 25} (+ ANTI each), the
frozen table at need 1.0 / k 25 as the reference row, per-rung economics at
need 1.0 and 0.5. OOS halves = alternating ET days (fixed UTC-4). Engine-true
via ws2_ladder_replay.run (budget $60 = $400 x 0.15, MIN_SHARES 5).

Usage: python r23_ladder_grid.py            (full grid, ~20 replay runs)
Writes data/vps-0821/r23_results.json (per-window rows per run) and
r23_summary.json (all tables + verdicts); r23_report.md is written by hand.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ws2_ladder_replay as ws2  # noqa: E402

OUT = ws2.DATA / "vps-0821"
BUDGET = 60.0
NEEDS = (0.5, 0.75, 1.0)
K_MAXES = (15.0, 20.0, 25.0)
RUNGS = ws2.RUNGS


def et_day(ep):
    return datetime.fromtimestamp(ep - 4 * 3600, tz=timezone.utc).strftime("%m-%d")


def half_map(all_days):
    """Alternating ET days: A = even index, B = odd index (sorted)."""
    return {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(sorted(all_days))}


def agg(rows):
    fills = [r for r in rows if r["filled"] > 0]
    sh = sum(r["filled"] for r in fills)
    pnl = sum(r["pnl"] for r in fills)
    wins = sum(1 for r in fills if r["win"])
    return dict(arms=len(rows), fills=len(fills), wins=wins,
                win_pct=round(100.0 * wins / len(fills), 1) if fills else None,
                flip_fills=sum(1 for r in fills if r["why"] == "flip"),
                floor_fills=sum(1 for r in fills if r["why"] == "floor"),
                cold_fills=sum(1 for r in fills if r["why"] == "cold"),
                wrong_winner_fills=sum(1 for r in fills if r["why"] == "wrong-winner"),
                sign_ok=sum(1 for r in rows if r["side"] == r["winner"]),
                sh=round(sh, 2), dollars=round(pnl, 2),
                ew_cps=round(100.0 * pnl / sh, 2) if sh else None)


def agg_halves(rows, hm):
    out = dict(all=agg(rows))
    for h in ("A", "B"):
        out[h] = agg([r for r in rows if hm[et_day(r["ep"])] == h])
    out["by_day"] = ws2.day_split(rows)
    return out


def rung_table(rows, hm):
    """Per rung: fills / wins / sh / dollars overall and per half."""
    st = {}
    for rp in RUNGS:
        st[rp] = {k: dict(placements=0, fills=0, wins=0, flip=0, sh=0.0, dollars=0.0)
                  for k in ("all", "A", "B")}
    for r in rows:
        h = hm[et_day(r["ep"])]
        for rp in r.get("placed", []):
            for k in ("all", h):
                st[rp][k]["placements"] += 1
        for rp, sh in r["rungs"].items():
            for k in ("all", h):
                s = st[rp][k]
                s["fills"] += 1
                s["wins"] += 1 if r["win"] else 0
                s["flip"] += 1 if r["why"] == "flip" else 0
                s["sh"] += sh
                s["dollars"] += sh * ((1 - rp) if r["win"] else -rp)
    for rp in RUNGS:
        for k, s in st[rp].items():
            s["sh"] = round(s["sh"], 2)
            s["dollars"] = round(s["dollars"], 2)
            s["win_pct"] = round(100.0 * s["wins"] / s["fills"], 1) if s["fills"] else None
            s["cps"] = round(100.0 * s["dollars"] / s["sh"], 2) if s["sh"] else None
            s["be_pct"] = round(100.0 * rp, 1)          # fee-free maker break-even
            s["bar_pct"] = round(100.0 * rp + 5.0, 1)   # R3 re-weight bar
    return st


def r3_verdicts(rt):
    v = {}
    for rp in RUNGS:
        a, b = rt[rp]["A"], rt[rp]["B"]
        enough = a["fills"] >= 15 and b["fills"] >= 15
        drop = enough and a["dollars"] < 0 and b["dollars"] < 0
        reweight = (a["fills"] > 0 and b["fills"] > 0
                    and a["win_pct"] >= a["bar_pct"] and b["win_pct"] >= b["bar_pct"])
        v[f"{rp:.2f}"] = dict(fills_A=a["fills"], fills_B=b["fills"],
                              dollars_A=a["dollars"], dollars_B=b["dollars"],
                              win_A=a["win_pct"], win_B=b["win_pct"],
                              bar=a["bar_pct"], enough_for_drop=enough,
                              DROP=drop, REWEIGHT=reweight,
                              verdict=("DROP" if drop else "REWEIGHT" if reweight
                                       else "KEEP (insufficient fills)" if not enough
                                       else "KEEP"))
    return v


def r2_verdict(grid, anti, need=1.0):
    base = grid[(need, 25.0)]
    out = {}
    for km in (15.0, 20.0):
        g = grid[(need, km)]
        checks = {}
        for h in ("A", "B"):
            ew_g, ew_b = g[h]["ew_cps"], base[h]["ew_cps"]
            checks[f"ew_{h}"] = (ew_g is not None and ew_b is not None and ew_g > ew_b,
                                 ew_g, ew_b)
            checks[f"dollars_{h}"] = (g[h]["dollars"] > base[h]["dollars"],
                                     g[h]["dollars"], base[h]["dollars"])
        checks["anti_le_0"] = (anti[(need, km)]["all"]["dollars"] <= 0,
                               anti[(need, km)]["all"]["dollars"])
        fill_ok = g["all"]["fills"] >= 0.7 * base["all"]["fills"]
        checks["fills_ge_70pct"] = (fill_ok, g["all"]["fills"], base["all"]["fills"])
        out[f"k{km:.0f}"] = dict(checks=checks,
                                 ADOPT=all(c[0] for c in checks.values()))
    return out


def main():
    c = ws2.load_corpus()
    tabs = ws2.r1_tables()
    refit, frozen = tabs["P995"], tabs["frozen_P995"]
    assert frozen == [tuple(x) for x in ws2.P995], "frozen table drift vs ws2.P995"
    all_days = sorted({et_day(w["ep"]) for w in c["wins"]})
    hm = half_map(all_days)
    print(f"{len(c['wins'])} 60s-rule windows; ET days {all_days}")
    print("halves:", {h: [d for d in all_days if hm[d] == h] for h in "AB"})

    rows_out, summary = {}, dict(halves=hm, budget=BUDGET, refit_P995=refit,
                                frozen_P995=frozen, grid={}, anti={},
                                frozen_ref={}, rungs={})
    grid, anti = {}, {}
    res_p, sum_p = OUT / "r23_results.json", OUT / "r23_summary.json"

    def save():
        json.dump(rows_out, open(res_p, "w"))
        json.dump(summary, open(sum_p, "w"), indent=1, default=str)

    for need in NEEDS:
        for km in K_MAXES:
            for is_anti in (False, True):
                name = f"{'anti' if is_anti else 'refit'}_n{need}_k{km:.0f}"
                res = ws2.run(need=need, k_max=km, table=refit, budget=BUDGET,
                              anti=is_anti)
                rows_out[name] = res
                a = agg_halves(res, hm)
                (anti if is_anti else grid)[(need, km)] = a
                summary["anti" if is_anti else "grid"][name] = a
                s = a["all"]
                print(f"{name:20s} arms {s['arms']:5d} fills {s['fills']:3d} "
                      f"flip {s['flip_fills']:2d} win% {s['win_pct']} "
                      f"EW {s['ew_cps']} $ {s['dollars']:+.2f} "
                      f"A {a['A']['dollars']:+.2f}/{a['A']['fills']} "
                      f"B {a['B']['dollars']:+.2f}/{a['B']['fills']}")
                if not is_anti and km == 25.0:
                    summary["rungs"][name] = rung_table(res, hm)
                save()

    for is_anti in (False, True):
        name = f"{'anti_frozen' if is_anti else 'frozen'}_n1.0_k25"
        res = ws2.run(need=1.0, k_max=25.0, table=frozen, budget=BUDGET, anti=is_anti)
        rows_out[name] = res
        a = agg_halves(res, hm)
        summary["frozen_ref"][name] = a
        if not is_anti:
            summary["rungs"][name] = rung_table(res, hm)
        s = a["all"]
        print(f"{name:20s} arms {s['arms']:5d} fills {s['fills']:3d} "
              f"flip {s['flip_fills']:2d} win% {s['win_pct']} EW {s['ew_cps']} "
              f"$ {s['dollars']:+.2f}")
        save()

    summary["R2"] = r2_verdict(grid, anti, need=1.0)
    summary["R2_need0.5_descriptive"] = r2_verdict(grid, anti, need=0.5)
    summary["R3"] = r3_verdicts(summary["rungs"]["refit_n1.0_k25"])
    summary["R3_need0.5_descriptive"] = r3_verdicts(summary["rungs"]["refit_n0.5_k25"])
    save()
    print("R2:", json.dumps(summary["R2"], default=str))
    print("R3:", json.dumps(summary["R3"], default=str))
    print(f"saved {res_p} {sum_p}")


if __name__ == "__main__":
    main()
