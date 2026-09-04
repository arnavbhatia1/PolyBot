"""R25: the exact configuration being deployed 09-04 — need 0.6, k[6,58], seven rungs
incl. 0.10, $160 ladder (0.40 x $400) — engine-true on the 18-day corpus, plus ANTI."""
import importlib.util
from pathlib import Path
SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
TAB = lr.r1_tables()["P995"]
R7 = [0.80, 0.65, 0.50, 0.35, 0.20, 0.15, 0.10]
for name, kw in {"DEPLOY need0.6 k58 r7 $160": dict(need=0.6, k_max=58.0, rungs=R7, budget=160.0),
                 "ref   need0.6 k58 r6 $160": dict(need=0.6, k_max=58.0, rungs=R7[:6], budget=160.0),
                 "ref   need0.6 k25 r6 $100": dict(need=0.6, k_max=25.0, rungs=R7[:6], budget=100.0),
                 "ANTI  need0.6 k58 r7 $160": dict(need=0.6, k_max=58.0, rungs=R7, budget=160.0, anti=True)}.items():
    res = lr.run(table=TAB, **kw)
    f = [r for r in res if r["filled"] > 0]
    pnl = sum(r["pnl"] for r in f); wins = sum(1 for r in f if r["win"])
    flips = [r for r in f if r["why"] == "flip"]
    a = sum(r["pnl"] for r in res if int(r["ep"]//86400) % 2 == 1); b = sum(r["pnl"] for r in res if int(r["ep"]//86400) % 2 == 0)
    worst = min((r["pnl"] for r in f), default=0.0)
    st = lr.rung_stats(res, kw["rungs"])
    rs = " ".join(f"{rp}:{s['fills']}/{s['wins']}({s['dollars']:+.0f})" for rp, s in st.items())
    print(f"{name}: armed {len(res)} fills {len(f)} wins {wins} pnl {pnl:+.2f} flip-fills {len(flips)} worst-fill {worst:+.2f} halves {a:+.0f}/{b:+.0f} | {rs}", flush=True)
