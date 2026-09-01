"""R22: rung set at the new floor (need 0.6) — 5 rungs vs +0.15 vs +0.15/+0.10, budgets $60 and $100."""
import importlib.util
from pathlib import Path
SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
TAB = lr.r1_tables()["P995"]
for budget in (60.0, 100.0):
    for name, rungs in {"r5": [0.80,0.65,0.50,0.35,0.20], "r6": [0.80,0.65,0.50,0.35,0.20,0.15],
                        "r7": [0.80,0.65,0.50,0.35,0.20,0.15,0.10]}.items():
        res = lr.run(need=0.6, k_max=25.0, table=TAB, budget=budget, rungs=rungs)
        f = [r for r in res if r["filled"] > 0]
        pnl = sum(r["pnl"] for r in f); wins = sum(1 for r in f if r["win"])
        st = lr.rung_stats(res, rungs)
        rs = " ".join(f"{rp}:{s['fills']}/{s['wins']}({s['dollars']:+.0f})" for rp, s in st.items())
        a = sum(r["pnl"] for r in res if int(r["ep"]//86400) % 2 == 1)
        b = sum(r["pnl"] for r in res if int(r["ep"]//86400) % 2 == 0)
        print(f"budget {budget:.0f} {name}: fills {len(f)} wins {wins} pnl {pnl:+.2f} halves {a:+.2f}/{b:+.2f} | {rs}")
