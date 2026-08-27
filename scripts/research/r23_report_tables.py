"""Markdown tables for r23_report.md from r23_summary.json (+ tape coverage)."""
import json
from pathlib import Path

OUT = Path(__file__).parent / "data" / "vps-0821"
S = json.load(open(OUT / "r23_summary.json"))
COV = json.load(open(OUT / "r23_tape_coverage.json")) if (OUT / "r23_tape_coverage.json").exists() else None


def f(x, p=2, sign=False):
    if x is None:
        return "-"
    return f"{x:+.{p}f}" if sign else f"{x:.{p}f}"


def row_a(label, g, anti):
    a, A, B = g["all"], g["A"], g["B"]
    return (f"| {label} | {a['arms']} | {a['fills']} | {a['flip_fills']} | {a['floor_fills']} | "
            f"{f(a['win_pct'], 1)} | {f(a['ew_cps'])} | {f(a['dollars'], 2, True)} | "
            f"{f(A['dollars'], 2, True)} / {A['fills']} | {f(B['dollars'], 2, True)} / {B['fills']} | "
            f"{f(A['ew_cps'])} / {f(B['ew_cps'])} | "
            f"{f(anti['all']['dollars'], 0, True)} ({anti['all']['fills']}) |")


print("### Table A — k_place_max x need, RE-FIT p99.5 (budget $60, k_min 6)\n")
print("| table / need / k_max | arms | fills | fill-on-flip | fill-on-floor | win% | EW c/sh | $ | half A $ / fills | half B $ / fills | EW A / B | ANTI $ (fills) |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for need in ("1.0", "0.75", "0.5"):
    for km in ("25", "20", "15"):
        g = S["grid"][f"refit_n{need}_k{km}"]
        an = S["anti"][f"anti_n{need}_k{km}"]
        print(row_a(f"re-fit / {need} / {km}", g, an))
g = S["frozen_ref"]["frozen_n1.0_k25"]
an = S["frozen_ref"].get("anti_frozen_n1.0_k25") or S["frozen_ref"].get("anti_n1.0_k25")
print(row_a("FROZEN / 1.0 / 25 (reference)", g, an))

print("\n### Table B — per rung (k_max 25)\n")
for name, title in (("refit_n1.0_k25", "re-fit x need 1.0"), ("refit_n0.5_k25", "re-fit x need 0.5"),
                    ("refit_n0.75_k25", "re-fit x need 0.75"), ("frozen_n1.0_k25", "FROZEN x need 1.0 (reference)")):
    rt = S["rungs"][name]
    print(f"\n**{title}** (placed = windows where the rung rested)\n")
    print("| rung | placed | fills | fill-on-flip | win% | break-even | R3 bar (be+5) | c/sh | $ | A fills / win% / $ | B fills / win% / $ |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for rp in ("0.8", "0.65", "0.5", "0.35", "0.2"):
        s = rt[rp]
        t, A, B = s["all"], s["A"], s["B"]
        print(f"| {float(rp):.2f} | {t['placements']} | {t['fills']} | {t['flip']} | {f(t['win_pct'], 1)} | "
              f"{t['be_pct']:.0f} | {t['bar_pct']:.0f} | {f(t['cps'])} | {f(t['dollars'], 2, True)} | "
              f"{A['fills']} / {f(A['win_pct'], 1)} / {f(A['dollars'], 2, True)} | "
              f"{B['fills']} / {f(B['win_pct'], 1)} / {f(B['dollars'], 2, True)} |")

print("\n### R2 checks (need 1.0, re-fit)\n")
print("| k_max | EW A (vs k25) | $ A (vs k25) | EW B (vs k25) | $ B (vs k25) | ANTI <= 0 | fills >= 70% of k25 | ADOPT |")
print("|---|---|---|---|---|---|---|---|")
for km, v in S["R2"].items():
    c = v["checks"]
    print(f"| {km} | {c['ew_A'][1]} vs {c['ew_A'][2]} {'OK' if c['ew_A'][0] else 'FAIL'} | "
          f"{c['dollars_A'][1]} vs {c['dollars_A'][2]} {'OK' if c['dollars_A'][0] else 'FAIL'} | "
          f"{c['ew_B'][1]} vs {c['ew_B'][2]} {'OK' if c['ew_B'][0] else 'FAIL'} | "
          f"{c['dollars_B'][1]} vs {c['dollars_B'][2]} {'OK' if c['dollars_B'][0] else 'FAIL'} | "
          f"{c['anti_le_0'][1]:+.0f} {'OK' if c['anti_le_0'][0] else 'FAIL'} | "
          f"{c['fills_ge_70pct'][1]}/{c['fills_ge_70pct'][2]} {'OK' if c['fills_ge_70pct'][0] else 'FAIL'} | "
          f"{'YES' if v['ADOPT'] else 'NO'} |")

print("\n### R2 checks at need 0.5 (descriptive only — the floor verdict is R1's)\n")
print("| k_max | EW A (vs k25) | $ A (vs k25) | EW B (vs k25) | $ B (vs k25) | ANTI <= 0 | fills >= 70% of k25 | would adopt |")
print("|---|---|---|---|---|---|---|---|")
for km, v in S["R2_need0.5_descriptive"].items():
    c = v["checks"]
    print(f"| {km} | {c['ew_A'][1]} vs {c['ew_A'][2]} {'OK' if c['ew_A'][0] else 'FAIL'} | "
          f"{c['dollars_A'][1]} vs {c['dollars_A'][2]} {'OK' if c['dollars_A'][0] else 'FAIL'} | "
          f"{c['ew_B'][1]} vs {c['ew_B'][2]} {'OK' if c['ew_B'][0] else 'FAIL'} | "
          f"{c['dollars_B'][1]} vs {c['dollars_B'][2]} {'OK' if c['dollars_B'][0] else 'FAIL'} | "
          f"{c['anti_le_0'][1]:+.0f} {'OK' if c['anti_le_0'][0] else 'FAIL'} | "
          f"{c['fills_ge_70pct'][1]}/{c['fills_ge_70pct'][2]} {'OK' if c['fills_ge_70pct'][0] else 'FAIL'} | "
          f"{'YES' if v['ADOPT'] else 'NO'} |")

print("\n### By ET day $ (re-fit x need 1.0 k25 | re-fit x need 0.5 k25 | frozen x 1.0 k25)\n")
d1 = S["grid"]["refit_n1.0_k25"]["by_day"]
d5 = S["grid"]["refit_n0.5_k25"]["by_day"]
df = S["frozen_ref"]["frozen_n1.0_k25"]["by_day"]
print("| ET day | half | re-fit 1.0 | re-fit 0.5 | frozen 1.0 |")
print("|---|---|---|---|---|")
for d in sorted(S["halves"]):
    print(f"| {d} | {S['halves'][d]} | {d1.get(d, 0):+.2f} | {d5.get(d, 0):+.2f} | {df.get(d, 0):+.2f} |")

if COV:
    print("\n### Tape coverage (UTC day: corpus windows with >= 1 print on either token)\n")
    print("| UTC day | windows | with prints | prints |")
    print("|---|---|---|---|")
    for d, s in sorted(COV["utc_day"].items()):
        print(f"| {d} | {s['windows']} | {s['with_prints']} | {s['prints']} |")
    print("\n| run | arms | zero prints whole window | zero prints from placement |")
    print("|---|---|---|---|")
    for n, s in COV["runs"].items():
        print(f"| {n} | {s['arms']} | {s['zero_prints_window']} | {s['zero_prints_from_place']} |")
