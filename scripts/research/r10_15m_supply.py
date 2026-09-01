"""R10 reducer: btc-updown-15m deep-flow supply in the deep_proj resting span.

Winner from Gamma outcomePrices (meta files). Deep supply = SELL rows on the
winner outcome at px <= 0.80, k in [-60, 25] (k = close - ts, close = ep+900)
— one SELL row per fill, so this is total deep fill volume (same convention as
the r6 census ps_* numbers on btc-5m). Maker/taker split via tx-group majority.
Also the 0.99-wall read and total row stats for scale context.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
PM = SP / "data" / "vps-0831" / "r10_pm_15m"
CAP = 3500


def et_day(ep):
    return datetime.fromtimestamp(ep - 4 * 3600, tz=timezone.utc).strftime("%m-%d")


def q(xs, f):
    xs = sorted(xs)
    return xs[min(int(f * len(xs)), len(xs) - 1)] if xs else None


def main():
    day = defaultdict(lambda: dict(n_win=0, capped=0, sh=0.0, val=0.0, tk_sh=0.0,
                                   mk_sh=0.0, w_any=0, w_50=0, w_20=0,
                                   notional=0.0, rows=0))
    px_all, k_all = [], []
    sellers = defaultdict(lambda: dict(sh=0.0, val=0.0, n=0, name=""))
    n_win = n_res = 0
    for mf in sorted(PM.glob("*.meta.json")):
        ep = int(mf.stem.split(".")[0])
        meta = json.loads(mf.read_text())
        try:
            prices = [float(x) for x in json.loads(meta["outcomePrices"])]
            outcomes = json.loads(meta["outcomes"])
        except (TypeError, ValueError, KeyError):
            continue
        if 1.0 not in prices:
            continue        # unresolved
        winner = outcomes[prices.index(1.0)]
        n_res += 1
        pf = PM / f"{ep}.jsonl"
        if not pf.exists():
            continue
        rows = [json.loads(l) for l in open(pf, encoding="utf-8") if l.strip()]
        if not rows:
            continue
        n_win += 1
        d = day[et_day(ep)]
        d["n_win"] += 1
        d["rows"] += len(rows)
        if len(rows) >= CAP:
            d["capped"] += 1
        close = ep + 900
        groups = defaultdict(list)
        for r in rows:
            groups[(r["transactionHash"], r["asset"])].append(r)
        w_sh = 0.0
        w_min_px = 1.0
        for g in groups.values():
            n_buy = sum(1 for r in g if r["side"] == "BUY")
            maker_side = ("BUY" if n_buy > len(g) - n_buy
                          else ("SELL" if len(g) - n_buy > n_buy else None))
            for r in g:
                try:
                    px, sz, ts = float(r["price"]), float(r["size"]), int(r["timestamp"])
                except (TypeError, ValueError):
                    continue
                d["notional"] += px * sz if r["side"] == "BUY" else 0.0
                k = close - ts
                if (r["side"] == "SELL" and r["outcome"] == winner
                        and -60 <= k <= 25 and px <= 0.80 + 1e-9):
                    d["sh"] += sz
                    d["val"] += sz * (1 - px)
                    w_sh += sz
                    w_min_px = min(w_min_px, px)
                    px_all.append(px)
                    k_all.append(k)
                    s = sellers[r["proxyWallet"]]
                    s["sh"] += sz
                    s["val"] += sz * (1 - px)
                    s["n"] += 1
                    s["name"] = r.get("name") or s["name"]
                    if maker_side is None:
                        pass
                    elif maker_side == "SELL":
                        d["mk_sh"] += sz
                    else:
                        d["tk_sh"] += sz
        if w_sh > 0:
            d["w_any"] += 1
            if w_min_px < 0.50:
                d["w_50"] += 1
            if w_min_px < 0.20:
                d["w_20"] += 1
    print(f"{n_res} resolved metas, {n_win} windows with rows")
    print(f"{'ET day':>6} {'nwin':>4} {'cap':>4} {'sh':>8} {'$val':>8} {'tk_sh':>7} "
          f"{'mk_sh':>7} {'w/any':>5} {'w<.50':>5} {'w<.20':>5} {'row/w':>6} {'ntl/w':>8}")
    for dy in sorted(day):
        d = day[dy]
        print(f"{dy:>6} {d['n_win']:4d} {d['capped']:4d} {d['sh']:8.0f} {d['val']:8.2f} "
              f"{d['tk_sh']:7.0f} {d['mk_sh']:7.0f} {d['w_any']:5d} {d['w_50']:5d} "
              f"{d['w_20']:5d} {d['rows'] / max(1, d['n_win']):6.0f} "
              f"{d['notional'] / max(1, d['n_win']):8.0f}")
    tot_win = sum(d["n_win"] for d in day.values())
    tot_val = sum(d["val"] for d in day.values())
    tot_sh = sum(d["sh"] for d in day.values())
    print(f"\nTOTAL: {tot_win} sampled windows, {tot_sh:.0f} deep sh, ${tot_val:.2f} ceded"
          f" -> per-window ${tot_val / max(1, tot_win):.2f}, x96 windows/day = "
          f"${96 * tot_val / max(1, tot_win):.2f}/day extrapolated")
    print(f"deep px q25/50/75: {q(px_all, .25)}/{q(px_all, .5)}/{q(px_all, .75)}   "
          f"k q10/50/90: {q(k_all, .1)}/{q(k_all, .5)}/{q(k_all, .9)}")
    top = sorted(sellers.items(), key=lambda kv: -kv[1]["sh"])[:12]
    tot_seller_sh = sum(s["sh"] for s in sellers.values())
    print(f"\n{len(sellers)} deep sellers; top12 by shares "
          f"(top5 share {100 * sum(s['sh'] for _, s in top[:5]) / max(1, tot_seller_sh):.0f}%):")
    for wal, s in top:
        print(f"  {wal[:14]} {s['name'][:18]:18s} sh {s['sh']:7.0f}  ${s['val']:7.2f}  n {s['n']}")
    json.dump({dy: d for dy, d in day.items()},
              open(SP / "data" / "vps-0831" / "r10_15m_supply.json", "w"), indent=1)


if __name__ == "__main__":
    main()
