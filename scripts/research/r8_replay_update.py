"""R8 (08-31 charter): engine-true ladder replay updated through 08-30.

Same conventions as r23_ladder_grid (ws2_ladder_replay.run, re-fit R1 tables,
$60 budget, k [6,25]) over the extended corpus — the question is whether the
08-28..30 days moved the fill rate at the deployed floor (need 1.0) and the
recorded alternates (0.75, 0.5), plus the ANTI control.
"""
import importlib.util
import json
from pathlib import Path

SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)

TAB = lr.r1_tables()["P995"]
OUT = SP / "data" / "vps-0831"

out = {}
for name, kw in {
    "refit_n1.0": dict(need=1.0, table=TAB, budget=60.0),
    "refit_n0.75": dict(need=0.75, table=TAB, budget=60.0),
    "refit_n0.5": dict(need=0.5, table=TAB, budget=60.0),
    "anti_n1.0": dict(need=1.0, table=TAB, budget=60.0, anti=True),
}.items():
    res = lr.run(k_max=25.0, **kw)
    print(f"\n=== {name} ===")
    lr.print_run(name, res, lr.RUNGS)
    out[name] = res
    new = [r for r in res if r["ep"] >= 1787875200 and r["filled"] > 0]  # 08-28+
    print(f"  08-28.. fills: {len(new)}  pnl {sum(r['pnl'] for r in new):+.2f}"
          f"  detail: {[(r['ep'], r['side'], r['winner'], round(r['pnl'], 2), r['why']) for r in new]}")
json.dump(out, open(OUT / "r8_replay_results.json", "w"))
print(f"\nsaved {OUT / 'r8_replay_results.json'}")
