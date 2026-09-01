"""R11 (08-31 charter §2.3): displacement of deep winner-token sells at sell time.

For every winner-token SELL row (px <= 0.80, k in [0,25]) in the census samples
(r5: 08-21..27, r6: 08-28..31), compute the projection displacement toward the
winner in re-fit p99.5 units at the sell timestamp (engine-faithful, bridged).
Split: mult >= 1.0 (outcome already locked at deployed tables — flow the
need-1.0 ladder CAN eat, if it arms in time), mult in [0,1), mult < 0
(projection pointed the other way), cold. Per-seller for the pseudonym cluster.
"""
import importlib.util
import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)

TAB = lr.r1_tables()["P995"]
CLUSTER = {"0xbfa0ed0c": "seabears", "0xd6d2b81f": "pinkypanda",
           "0x809f2752": "porkypie12", "0x32c4922d": "grumbong",
           "0xa3338de3": "wundawally", "0x1dd2a69e": "spork30"}
SPANS = {"0821": (SP / "data" / "vps-0821" / "r5_pm_trades",
                  SP / "data" / "vps-0821" / "paper_0827.db"),
         "0831": (SP / "data" / "vps-0831" / "r6_pm_trades",
                  SP / "data" / "vps-0831" / "paper_0831.db")}


def main():
    wins = {}
    import gzip
    with gzip.open(SP / "data" / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            if wd["ep"] >= lr.RULE_TS:
                wins[wd["ep"]] = wd
    kl_ts, kl_px = lr.load_klines()

    for span, (pm, db) in SPANS.items():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        labels = {int(r["window_id"].rsplit("-", 1)[1]): dict(r) for r in con.execute(
            "SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'")}
        con.close()
        buckets = defaultdict(lambda: dict(sh=0.0, val=0.0, n=0))
        cl_buckets = defaultdict(lambda: dict(sh=0.0, val=0.0, n=0))
        mults = []
        missing = 0
        for pf in sorted(pm.glob("*.jsonl")):
            ep = int(pf.stem)
            lab = labels.get(ep)
            wd = wins.get(ep)
            if lab is None:
                continue
            winner = "Up" if lab["resolved_up"] else "Down"
            close = ep + 300
            l = sorted(wd["l"]) if wd else None
            l_rx = [(rx, p) for rx, _ts, p in l] if l else None
            bz = wd["bz"] if wd else None
            if wd and not bz and kl_ts:
                i0 = bisect_right(kl_ts, ep + 195)
                i1 = bisect_right(kl_ts, ep + 306)
                bz = [(S + 1 + 0.45, S + 1.0, px)
                      for S, px in zip(kl_ts[i0:i1], kl_px[i0:i1])]
            strike = wd["strike"] if wd else None
            for line in open(pf, encoding="utf-8"):
                if not line.strip():
                    continue
                r = json.loads(line)
                if r["side"] != "SELL" or r["outcome"] != winner:
                    continue
                try:
                    px, sz, ts = float(r["price"]), float(r["size"]), int(r["timestamp"])
                except (TypeError, ValueError):
                    continue
                k = close - ts
                if not (0 <= k <= 25) or px > 0.80 + 1e-9:
                    continue
                if wd is None or not strike:
                    missing += 1
                    continue
                pr = lr.proj_at(l, l_rx, bz, ts, close - lr.HORIZON)
                if pr is None:
                    key = "cold"
                    mult = None
                else:
                    d = (pr - strike) if winner == "Up" else (strike - pr)
                    mult = d / lr.margin(max(k, 0.01), TAB)
                    mults.append(mult)
                    key = ("locked>=1" if mult >= 1.0 else
                           "0<=m<1" if mult >= 0 else "anti<0")
                b = buckets[key]
                b["sh"] += sz
                b["val"] += sz * (1 - px)
                b["n"] += 1
                if r["proxyWallet"][:10] in CLUSTER:
                    cb = cl_buckets[key]
                    cb["sh"] += sz
                    cb["val"] += sz * (1 - px)
                    cb["n"] += 1
        tot_sh = sum(b["sh"] for b in buckets.values())
        tot_val = sum(b["val"] for b in buckets.values())
        print(f"\n[{span}] deep winner-token SELLs k in [0,25] px<=0.80: "
              f"{tot_sh:.0f} sh ${tot_val:.2f} ceded (no-stream rows {missing})")
        for key in ("locked>=1", "0<=m<1", "anti<0", "cold"):
            b = buckets[key]
            cb = cl_buckets[key]
            pct = 100 * b["sh"] / tot_sh if tot_sh else 0
            print(f"  {key:9s}: {b['sh']:8.0f} sh ({pct:4.1f}%)  ${b['val']:8.2f}"
                  f"   [cluster: {cb['sh']:6.0f} sh ${cb['val']:7.2f}]")
        ms = sorted(mults)
        if ms:
            qq = lambda f: ms[min(int(f * len(ms)), len(ms) - 1)]
            print(f"  mult q10/25/50/75/90: {qq(.1):.2f}/{qq(.25):.2f}/{qq(.5):.2f}"
                  f"/{qq(.75):.2f}/{qq(.9):.2f}")


if __name__ == "__main__":
    main()
