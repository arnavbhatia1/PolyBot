"""H1 pass 3: pocket analysis on the ranked cells.

Two views per (k_band, price_band, taker_side):
  NET    = winner-cell + loser-cell maker $ (what a symmetric, signal-free
           maker collects; mechanical flow -> survives outcome shuffle)
  COND   = winner-side cell alone (needs an outcome signal to occupy;
           shuffle destroys it by construction)
Prints full tables ($/day, era + halves) and a candidate list vs the $200/day
pre-registered bar. Writes data/vps-0821/h1_pockets.json.
"""
import json
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
K_BANDS = ["pre", "k300-60", "k60-25", "k25-6", "k6-0",
           "post0-30", "post30-150", "post150+"]


def main():
    res = json.load(open(DATA / "h1_rank_results.json"))
    cells = res["cells"]
    idx = {}
    for r in cells:
        idx[(r["k_band"], r["p_band"], r["taker_side"], r["token"])] = r

    def g(kb, pb, side, tok, field="usd_day"):
        r = idx.get((kb, pb, side, tok))
        return r[field] if r else 0.0

    pbands = [f"{i/10:.1f}-{(i+1)/10:.1f}" for i in range(10)]

    print("=" * 100)
    print("NET maker $/day by k_band x price_band x taker_side (winner+loser merged)")
    print("  taker BUY  -> maker position = resting ASK at that price")
    print("  taker SELL -> maker position = resting BID at that price")
    for side in ("BUY", "SELL"):
        print(f"\n--- taker {side} ---")
        print(f"{'k_band':>10} | " + " ".join(f"{pb:>9}" for pb in pbands) + f" | {'row_net':>9}")
        for kb in K_BANDS:
            vals = [g(kb, pb, side, "winner") + g(kb, pb, side, "loser") for pb in pbands]
            print(f"{kb:>10} | " + " ".join(f"{v:>9,.0f}" for v in vals) +
                  f" | {sum(vals):>9,.0f}")

    print("\n" + "=" * 100)
    print("CONDITIONED maker $/day: winner-side token only (needs outcome signal)")
    for side in ("BUY", "SELL"):
        print(f"\n--- taker {side} (winner-side token) ---")
        print(f"{'k_band':>10} | " + " ".join(f"{pb:>9}" for pb in pbands))
        for kb in K_BANDS:
            vals = [g(kb, pb, side, "winner") for pb in pbands]
            print(f"{kb:>10} | " + " ".join(f"{v:>9,.0f}" for v in vals))

    print("\n" + "=" * 100)
    print("Volume (shares/era) winner+loser, to spot thin cells")
    for side in ("BUY", "SELL"):
        print(f"\n--- taker {side} ---")
        print(f"{'k_band':>10} | " + " ".join(f"{pb:>9}" for pb in pbands))
        for kb in K_BANDS:
            vals = [g(kb, pb, side, "winner", "vol_sh") + g(kb, pb, side, "loser", "vol_sh")
                    for pb in pbands]
            print(f"{kb:>10} | " + " ".join(f"{v:>9,.0f}" for v in vals))

    # candidate pockets: NET view >= +200/day, and COND view >= +200/day
    out = {"net": [], "cond": []}
    for side in ("BUY", "SELL"):
        for kb in K_BANDS:
            for pb in pbands:
                w, l = idx.get((kb, pb, side, "winner")), idx.get((kb, pb, side, "loser"))
                net = (w["usd_day"] if w else 0) + (l["usd_day"] if l else 0)
                net_h1 = (w["usd_day_h1"] if w else 0) + (l["usd_day_h1"] if l else 0)
                net_h2 = (w["usd_day_h2"] if w else 0) + (l["usd_day_h2"] if l else 0)
                null = (w["null_mean_day"] if w else 0) + (l["null_mean_day"] if l else 0)
                if net >= 200:
                    out["net"].append({"k": kb, "p": pb, "side": side, "usd_day": net,
                                       "h1": net_h1, "h2": net_h2, "null_day": null})
                if w and w["usd_day"] >= 200:
                    out["cond"].append({"k": kb, "p": pb, "side": side,
                                        "usd_day": w["usd_day"], "h1": w["usd_day_h1"],
                                        "h2": w["usd_day_h2"],
                                        "null_day": w["null_mean_day"],
                                        "z": w["z_vs_null"],
                                        "vol_sh": w["vol_sh"], "n": w["n_prints"]})
    out["net"].sort(key=lambda r: -r["usd_day"])
    out["cond"].sort(key=lambda r: -r["usd_day"])

    print("\n" + "=" * 100)
    print("NET candidates >= $200/day (mechanical unless null ~ 0):")
    for r in out["net"]:
        print(f"  {r['k']:>10} {r['p']} taker-{r['side']:<4} net {r['usd_day']:>8,.0f}/d "
              f"(h1 {r['h1']:>8,.0f}  h2 {r['h2']:>8,.0f}  shuffle-null {r['null_day']:>8,.0f})")
    print("\nCONDITIONED (winner-side) candidates >= $200/day:")
    for r in out["cond"]:
        print(f"  {r['k']:>10} {r['p']} taker-{r['side']:<4} {r['usd_day']:>8,.0f}/d "
              f"(h1 {r['h1']:>8,.0f}  h2 {r['h2']:>8,.0f}  null {r['null_day']:>8,.0f} "
              f"z {r['z']:>6.1f}  {r['vol_sh']:>10,.0f} sh  {r['n']:>6} prints)")

    with open(DATA / "h1_pockets.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
