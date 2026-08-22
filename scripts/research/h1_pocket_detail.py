"""H1 pass 4: per-day stability + avg fill price + book-queue read for the
candidate pockets that cleared $200/day outside the known ones.

Pockets:
  A  underdog-ask longshot tax: taker-BUY at 0.0-0.2, k300-60 + k60-25
  B  terminal loser-ask:        taker-BUY at 0.0-0.1, k6-0
  C  mid-window bid capture:    taker-SELL all bands, k300-60 (symmetric-MM family)
  D  near-lock favorite bids:   taker-SELL 0.9-1.0, k60-25 + k300-60 (k>25 refuted family)

Queue read (window_paths_60s.db, era windows only):
  - cheap-side touch ask size when ask in (0,0.1]/(0.1,0.2], mid-window vs final 6s
  - winner-side touch bid size when bid in [0.9,1.0), k in (25,60]
Writes data/vps-0821/h1_pocket_detail.json and prints a summary.
"""
import json
import pickle
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
ERA0 = 1786665600


def main():
    res = json.load(open(DATA / "h1_rank_results.json"))
    idx = {(r["k_band"], r["p_band"], r["taker_side"], r["token"]): r
           for r in res["cells"]}
    day_list = res["day_list"]
    effd = res["coverage"]["eff_day_by_day"]

    def daily(cells_spec):
        """sum day_usd over (kb,pb,side) winner+loser, normalized per eff day"""
        tot = defaultdict(float)
        for kb, pb, side in cells_spec:
            for tok in ("winner", "loser"):
                r = idx.get((kb, pb, side, tok))
                if r:
                    for d, v in r["day_usd"].items():
                        tot[d] += v
        return {d: tot.get(d, 0.0) / effd[d] for d in day_list}

    pb = [f"{i/10:.1f}-{(i+1)/10:.1f}" for i in range(10)]
    pockets = {
        "A_underdog_ask_lowp": [(kb, p, "BUY") for kb in ("k300-60", "k60-25")
                                for p in (pb[0], pb[1])],
        "B_terminal_loser_ask": [("k6-0", pb[0], "BUY")],
        "C_midwindow_bids": [("k300-60", p, "SELL") for p in pb],
        "D_nearlock_favorite_bids": [(kb, pb[9], "SELL")
                                     for kb in ("k300-60", "k60-25")],
        "OUR_k25-6_deep_bids": [("k25-6", p, "SELL") for p in pb[2:8]],
    }
    per_day = {name: daily(spec) for name, spec in pockets.items()}

    # avg fill price per pocket cell (margin proxy) from cellstats
    with open(DATA / "h1_cellstats.pkl", "rb") as f:
        st = pickle.load(f)
    lab = st["labels"]
    avg_price = {}
    acc = defaultdict(lambda: [0.0, 0.0])
    for w, d in st["cells"].items():
        ru = lab[w]
        for (kb, pbi, side, is_up), (s, ps, n) in d.items():
            tok = "winner" if is_up == ru else "loser"
            a = acc[(kb, pbi, side, tok)]
            a[0] += s
            a[1] += ps
    for k, (s, psum) in acc.items():
        if s > 0:
            avg_price[str(k)] = psum / s
    key_avg = {
        "A k300-60 p0 loser": acc[("k300-60", 0, "BUY", "loser")],
        "A k300-60 p1 loser": acc[("k300-60", 1, "BUY", "loser")],
        "A k60-25 p0 loser": acc[("k60-25", 0, "BUY", "loser")],
        "B k6-0 p0 loser": acc[("k6-0", 0, "BUY", "loser")],
        "B k6-0 p0 winner": acc[("k6-0", 0, "BUY", "winner")],
    }
    key_avg = {k: {"sh": v[0], "avg_px": (v[1] / v[0] if v[0] else None)}
               for k, v in key_avg.items()}

    # ---- queue read from window_paths ----
    con = sqlite3.connect(f"file:{DATA / 'window_paths_60s.db'}?mode=ro", uri=True)
    q = """SELECT window_id, elapsed_s, ask_up, ask_down, ask_sz_up, ask_sz_down,
                  bid_up, bid_down, bid_sz_up, bid_sz_down
           FROM window_paths WHERE CAST(substr(window_id, 15) AS INTEGER) >= ?"""
    cheap_mid, cheap_p1_mid, cheap_final6 = [], [], []
    lock_bid_2560 = []
    n_rows = 0
    for wid, el, au, ad, szu, szd, bu, bd, bszu, bszd in con.execute(q, (ERA0,)):
        n_rows += 1
        if None in (au, ad):
            continue
        # cheap side = lower ask
        a, asz = (au, szu) if au <= ad else (ad, szd)
        if el is None:
            continue
        if el < 240:
            if a <= 0.10 and asz:
                cheap_mid.append(asz)
            elif 0.10 < a <= 0.20 and asz:
                cheap_p1_mid.append(asz)
        elif el >= 294 and el <= 300:
            if a <= 0.10 and asz:
                cheap_final6.append(asz)
        if 240 <= el <= 275:  # k in (25,60]
            b, bsz = (bu, bszu) if (bu or 0) >= (bd or 0) else (bd, bszd)
            if b and b >= 0.90 and bsz:
                lock_bid_2560.append(bsz)
    con.close()

    def pct(x):
        if not x:
            return None
        return {p: float(np.percentile(x, p)) for p in (10, 25, 50, 75, 90)}

    queue = {
        "n_rows_era": n_rows,
        "cheap_ask_le0.10_mid_touch_sz": pct(cheap_mid),
        "cheap_ask_0.10-0.20_mid_touch_sz": pct(cheap_p1_mid),
        "cheap_ask_le0.10_final6_touch_sz": pct(cheap_final6),
        "winner?_bid_ge0.90_k25-60_touch_sz": pct(lock_bid_2560),
        "n_samples": {"cheap_mid": len(cheap_mid), "cheap_p1_mid": len(cheap_p1_mid),
                      "cheap_final6": len(cheap_final6), "lock_2560": len(lock_bid_2560)},
    }

    out = {"per_day": per_day, "key_avg_px": key_avg, "queue": queue}
    with open(DATA / "h1_pocket_detail.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=float)

    print("per-day maker $/eff-day:")
    print(f"{'pocket':>26} | " + " ".join(f"{d:>7}" for d in day_list))
    for name, dd in per_day.items():
        print(f"{name:>26} | " + " ".join(f"{dd[d]:>7,.0f}" for d in day_list))
    print("\navg fill px (loser-side asks):")
    for k, v in key_avg.items():
        print(f"  {k}: {v['sh']:,.0f} sh @ avg {v['avg_px']:.4f}" if v['avg_px']
              else f"  {k}: none")
    print("\nqueue (touch size, shares):")
    for k, v in queue.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
