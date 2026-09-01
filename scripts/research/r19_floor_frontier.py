"""R19 (09-01, operator-directed): floor frontier under live-realistic execution.

need in {0.25,0.35,0.5,0.6,0.75,1.0} x GTC latency {56ms measured-idle, 300ms,
500ms in-anger stress}, engine-true (ws2 conventions), re-fit tables, $60,
k[6,25]. Reports fills/day, EW, per-rung win vs break-even, flip-fill losses,
alternating-ET-day halves, ANTI controls at the low floors.
"""
import importlib.util, json
from pathlib import Path
SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
TAB = lr.r1_tables()["P995"]
OUT = SP / "data" / "vps-0831"
NEEDS = [0.25, 0.35, 0.5, 0.6, 0.75, 1.0]
LATS = {"56ms": (0.056, 0.054), "300ms": (0.30, 0.30), "500ms": (0.50, 0.50)}

def halves(res):
    a = sum(r["pnl"] for r in res if int(r["ep"] // 86400) % 2 == 1)
    b = sum(r["pnl"] for r in res if int(r["ep"] // 86400) % 2 == 0)
    return a, b

def summarize(res, ndays):
    f = [r for r in res if r["filled"] > 0]
    sh = sum(r["filled"] for r in f)
    pnl = sum(r["pnl"] for r in f)
    wins = sum(1 for r in f if r["win"])
    flips = [r for r in f if r["why"] == "flip"]
    st = lr.rung_stats(res, lr.RUNGS)
    rung = {rp: (s["fills"], round(100*s["wins"]/s["fills"]) if s["fills"] else None,
                 round(s["dollars"], 2)) for rp, s in st.items()}
    a, b = halves(res)
    return dict(armed=len(res), fills=len(f), fills_per_day=round(len(f)/ndays, 2),
                wins=wins, pnl=round(pnl, 2), ew_c=round(100*pnl/sh, 2) if sh else None,
                flip_fills=len(flips), flip_pnl=round(sum(r["pnl"] for r in flips), 2),
                half_a=round(a, 2), half_b=round(b, 2), rung=rung,
                days_to_20=round(20/(len(f)/ndays), 0) if f else None)

c = lr.load_corpus()
ndays = len({lr.datetime.fromtimestamp(w["ep"]-4*3600, tz=lr.timezone.utc).date() for w in c["wins"]})
print(f"{len(c['wins'])} windows, {ndays} ET days")
out = {}
for lname, (pl, cl) in LATS.items():
    lr.PLACE_LAT, lr.CANCEL_LAT = pl, cl
    for need in NEEDS:
        res = lr.run(need=need, k_max=25.0, table=TAB, budget=60.0)
        s = summarize(res, ndays)
        out[f"{lname}_n{need}"] = s
        print(f"[{lname}] need {need:4.2f}: armed {s['armed']:4d} fills {s['fills']:3d} "
              f"({s['fills_per_day']:.2f}/d, 20-fill bar {s['days_to_20']}d) wins {s['wins']:3d} "
              f"pnl {s['pnl']:+8.2f} EW {s['ew_c']} c/sh  flip-fills {s['flip_fills']} "
              f"({s['flip_pnl']:+.2f})  halves {s['half_a']:+.2f}/{s['half_b']:+.2f}")
        print(f"      rungs fills/win%/$: {s['rung']}")
    if lname == "56ms":
        for need in (0.25, 0.5):
            res = lr.run(need=need, k_max=25.0, table=TAB, budget=60.0, anti=True)
            s = summarize(res, ndays)
            out[f"{lname}_anti_n{need}"] = s
            print(f"[{lname}] ANTI need {need}: fills {s['fills']} pnl {s['pnl']:+.2f} EW {s['ew_c']}")
json.dump(out, open(OUT / "r19_frontier.json", "w"), indent=1)
print("saved r19_frontier.json")
