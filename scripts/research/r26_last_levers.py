"""R26 (09-04): the last undeployed levers, engine-true on the 18-day corpus.
Budget $200 (0.50 x $400); eighth rung 0.05; floor 0.5 at the wide zone; ANTI."""
import importlib.util
from pathlib import Path
SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
TAB = lr.r1_tables()["P995"]
R7 = [0.80, 0.65, 0.50, 0.35, 0.20, 0.15, 0.10]
R8 = R7 + [0.05]
def summ(res, rungs):
    f = [r for r in res if r["filled"] > 0]
    pnl = sum(r["pnl"] for r in f); wins = sum(1 for r in f if r["win"])
    flips = [r for r in f if r["why"] == "flip"]
    a = sum(r["pnl"] for r in res if int(r["ep"]//86400) % 2 == 1); b = sum(r["pnl"] for r in res if int(r["ep"]//86400) % 2 == 0)
    worst = min((r["pnl"] for r in f), default=0.0)
    st = lr.rung_stats(res, rungs)
    rs = " ".join(f"{rp}:{s['fills']}/{s['wins']}({s['dollars']:+.0f})" for rp, s in st.items())
    return f"fills {len(f)} wins {wins} pnl {pnl:+.2f} flip-fills {len(flips)} worst {worst:+.2f} halves {a:+.0f}/{b:+.0f} | {rs}"
lr.MIN_SHARES = 5.0
for name, kw in {
    "deployed 0.6 k58 r7 $200": dict(need=0.6, k_max=58.0, rungs=R7, budget=200.0),
    "eighth rung 0.6 k58 r8 $200": dict(need=0.6, k_max=58.0, rungs=R8, budget=200.0),
    "floor 0.5 k58 r7 $200": dict(need=0.5, k_max=58.0, rungs=R7, budget=200.0),
    "floor 0.75 k58 r7 $200": dict(need=0.75, k_max=58.0, rungs=R7, budget=200.0),
    "ANTI 0.6 k58 r8 $200": dict(need=0.6, k_max=58.0, rungs=R8, budget=200.0, anti=True),
}.items():
    print(f"{name}: {summ(lr.run(table=TAB, **kw), kw['rungs'])}", flush=True)
