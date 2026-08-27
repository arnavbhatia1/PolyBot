"""R1 reference: engine-true ladder replay on ALL 60s-era days with a FIXED
table (frozen 08-18 vs the R1 re-fit), need 0.5 / 1.0, k_place [6,25], plus
ANTI controls. In-sample by construction — context for the ws1_oos verdict
(which is the binding out-of-fit read), never a substitute for it.
"""
import importlib.util
import json
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
OUT = DATA / "vps-0821"
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)


def main():
    refit = [tuple(x) for x in json.load(open(OUT / "r1_tables.json"))["P995"]]
    c = lr.load_corpus()
    print(f"{len(c['wins'])} 60s-era windows; tape tokens {len(c['prints'])}")
    out = {}
    for tname, tab in (("frozen", lr.P995), ("refit", refit)):
        for need in (0.5, 1.0):
            for anti in (False, True):
                name = f"{tname} need {need}{' ANTI' if anti else ''}"
                res = lr.run(need=need, table=tab, anti=anti)
                print(f"\n== {name} ==")
                lr.print_run(name, res, lr.RUNGS)
                out[name] = res
        # clause-(iv)-style: 0.5 losses on arms the 1.0 floor never armed
        armed_10 = {r["ep"] for r in out[f"{tname} need 1.0"]}
        thin = [r for r in out[f"{tname} need 0.5"] if r["filled"] > 0 and not r["win"]
                and r["ep"] not in armed_10]
        print(f"\n{tname}: 0.5-only losses (arms 1.0 vetoed): {len(thin)} "
              f"{[(r['ep'], round(r['pnl'], 2)) for r in thin]}")
    json.dump(out, open(OUT / "r1_oos_ref.json", "w"))


if __name__ == "__main__":
    main()
