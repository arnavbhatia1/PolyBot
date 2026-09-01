"""R9 (08-31 charter §3): deployed-ladder frontier rows not covered by r8.

need 1.25 (charter-mandated), and k_place [2,25] at need 1.0/0.75 + ANTI —
EVIDENCE ONLY: twap_k_min_s 6.0 is a standing scar (08-12 realized breach);
nothing here changes config. Re-fit R1 tables, $60 budget, corpus 08-14..30.
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
    "refit_n1.25_k25": dict(need=1.25, k_min=6.0),
    "refit_n1.0_k2": dict(need=1.0, k_min=2.0),
    "refit_n0.75_k2": dict(need=0.75, k_min=2.0),
    "anti_n1.0_k2": dict(need=1.0, k_min=2.0, anti=True),
}.items():
    res = lr.run(k_max=25.0, table=TAB, budget=60.0, **kw)
    print(f"\n=== {name} ===")
    lr.print_run(name, res, lr.RUNGS)
    ek = [r for r in res if r["filled"] > 0 and r["place_k"] < 6.0]
    print(f"  fills armed at k<6: {len(ek)}  pnl {sum(r['pnl'] for r in ek):+.2f}"
          f"  detail: {[(r['ep'], round(r['place_k'], 1), r['side'], r['winner'], round(r['pnl'], 2)) for r in ek]}")
    out[name] = res
json.dump(out, open(OUT / "r9_frontier_results.json", "w"))
print(f"\nsaved {OUT / 'r9_frontier_results.json'}")
