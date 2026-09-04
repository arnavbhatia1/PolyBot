"""R24 (09-04): arm earlier? k_place_max frontier at the deployed floor 0.6 (and 1.0),
engine-true, re-fit tables, $100 ladder, six rungs; ANTI controls. Motivated by the
09-01..03 observation that deep prints land at k 21-25 s while the ladder arms 9-15 s later."""
import importlib.util
from pathlib import Path
SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
TAB = lr.r1_tables()["P995"]
RUNGS = [0.80, 0.65, 0.50, 0.35, 0.20, 0.15]
def summ(res):
    f = [r for r in res if r["filled"] > 0]
    pnl = sum(r["pnl"] for r in f); wins = sum(1 for r in f if r["win"])
    flips = [r for r in f if r["why"] == "flip"]
    a = sum(r["pnl"] for r in res if int(r["ep"] // 86400) % 2 == 1)
    b = sum(r["pnl"] for r in res if int(r["ep"] // 86400) % 2 == 0)
    sh = sum(r["filled"] for r in f)
    st = lr.rung_stats(res, RUNGS)
    r80 = st[0.80]
    return (f"armed {len(res):4d} fills {len(f):3d} wins {wins:3d} pnl {pnl:+8.2f} "
            f"EW {100*pnl/sh if sh else 0:+6.1f}c flip-fills {len(flips):2d} ({sum(r['pnl'] for r in flips):+.0f}) "
            f"halves {a:+.0f}/{b:+.0f} | 0.80 rung {r80['fills']}f {100*r80['wins']/max(1,r80['fills']):.0f}% (be 80)")
for need in (0.6, 1.0):
    for kmax in (25.0, 30.0, 40.0, 58.0):
        res = lr.run(need=need, k_max=kmax, table=TAB, budget=100.0, rungs=RUNGS)
        print(f"need {need} k_max {kmax:4.0f}: {summ(res)}", flush=True)
for kmax in (30.0, 40.0):
    res = lr.run(need=0.6, k_max=kmax, table=TAB, budget=100.0, rungs=RUNGS, anti=True)
    print(f"ANTI need 0.6 k_max {kmax:4.0f}: {summ(res)}", flush=True)
