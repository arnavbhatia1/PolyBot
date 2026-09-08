"""R27 (09-08): k_max frontier at need 0.6 / eight rungs / $200 on the corpus extended
through 09-07 - which now contains the live flip-fill loss (ep 1788743700, k=49).
Engine-true check: that window must be a flip-fill loss in the k>=50 arms."""
import importlib.util
from pathlib import Path
SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
TAB = lr.r1_tables()["P995"]
R8 = [0.80, 0.65, 0.50, 0.35, 0.20, 0.15, 0.10, 0.05]
LOSS_EP = 1788743700
c = lr.load_corpus()
days = sorted({lr.datetime.fromtimestamp(w["ep"] - 4 * 3600, tz=lr.timezone.utc).date() for w in c["wins"]})
print(f"corpus: {len(c['wins'])} windows, {len(days)} ET days {days[0]}..{days[-1]}")
for kmax in (25.0, 30.0, 40.0, 50.0, 58.0):
    res = lr.run(need=0.6, k_max=kmax, table=TAB, budget=200.0, rungs=R8)
    f = [r for r in res if r["filled"] > 0]
    pnl = sum(r["pnl"] for r in f); wins = sum(1 for r in f if r["win"])
    flips = [r for r in f if not r["win"]]
    sept = [r for r in f if r["ep"] >= 1788220800]
    loss_row = next((r for r in res if r["ep"] == LOSS_EP), None)
    ls = ("not armed" if loss_row is None else
          f"armed k={loss_row['place_k']:.1f} side={loss_row['side']} winner={loss_row['winner']} "
          f"why={loss_row['why']} filled={loss_row['filled']:.1f} pnl={loss_row['pnl']:+.2f}")
    print(f"k_max {kmax:4.0f}: fills {len(f):3d} wins {wins:3d} pnl {pnl:+9.2f} losses {len(flips)} "
          f"({sum(r['pnl'] for r in flips):+.0f}) | Sept fills {len(sept)} pnl {sum(r['pnl'] for r in sept):+.2f} "
          f"| loss window: {ls}", flush=True)
