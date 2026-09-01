"""R6 census reducer (08-31 charter §2.1 + §2.3): wallet census over a pulled
pm_trades sample, buyer-side (who occupies the deep-bid seat) AND seller-side
(who panics into it — the loser the ladder eats).

Usage: python r6_census.py [0831|0821]
  0831 -> data/vps-0831/r6_pm_trades + paper_0831.db (08-28..31)
  0821 -> data/vps-0821/r5_pm_trades + paper_0827.db (08-21..27, seller re-read
          of the r5 sample for week-over-week stationarity)
Output: r6_census_<span>.json + printed tables.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
SPAN = sys.argv[1] if len(sys.argv) > 1 else "0831"
if SPAN == "0831":
    DATA = SP / "data" / "vps-0831"
    PM = DATA / "r6_pm_trades"
    DB = DATA / "paper_0831.db"
else:
    DATA = SP / "data" / "vps-0821"
    PM = DATA / "r5_pm_trades"
    DB = DATA / "paper_0827.db"
OUT = SP / "data" / "vps-0831" / f"r6_census_{SPAN}.json"
CAP_ROWS = 3500
WATCH = {  # WALLETS.md leaderboard + r5 (08-21..27) top names
    "0x251c1a28": "0xAAAAA", "0x568b0798": "-", "0x3725d52f": "almach",
    "0x0cb03848": "-", "0xc2ad03f7": "bosona", "0x32ed2e54": "mo-money",
    "0xfc369971": "gesinimen", "0xce50c96b": "honey-spot", "0xe0229e10": "JetFadil",
    "0x48ac40fc": "BoneOhio", "0x3b840769": "1723", "0x6fc44ec4": "wall-0821",
    "0xeebde7a0": "Bonereaper", "0x56991cfb": "Bogeymann", "0x3c58ef42": "antsaslyku",
    "0xc5e62509": "peipeipei", "0x5195a3d4": "wqewqa", "0x31393e2f": "hot-garbage",
    "0x44832d0d": "dp-lookalike", "0xb27bc932": "cheap-side", "0x239e726f": "dp-lookalike2",
    "0x931cd225": "StayFocusLuia", "0x3dcea528": "LuiaLeQuartier", "0xee65685d": "0x50f7",
    "0x57dec8fb": "fav-buyer",
}


def load_labels():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return {int(r["window_id"].rsplit("-", 1)[1]): dict(r) for r in db.execute(
        "SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'")}


def q(xs, f):
    xs = sorted(xs)
    return xs[min(int(f * len(xs)), len(xs) - 1)] if xs else None


def main():
    labels = load_labels()
    W = defaultdict(lambda: dict(
        pnl=0.0, vol=0.0, n_rows=0, maker=0, taker=0, unk=0, buy_px=[], buy_k=[],
        n_windows=set(), sells=0, name="", wins=0, buys=0,
        deep_n=0, deep_sh=0.0, deep_win_sh=0.0, deep_pnl=0.0,
        # seller-side: SELL rows on the WINNER token, k in [6,25]+post<=60, px<=0.80
        ps_n=0, ps_sh=0.0, ps_val=0.0, ps_px=[], ps_k=[], ps_windows=set(),
        ps_taker_sh=0.0, ps_maker_sh=0.0, ps_unk_sh=0.0))
    capped = n_win = n_rows_total = 0
    per_day = defaultdict(lambda: defaultdict(float))
    for pf in sorted(PM.glob("*.jsonl")):
        ep = int(pf.stem)
        lab = labels.get(ep)
        if lab is None:
            continue
        rows = [json.loads(l) for l in open(pf, encoding="utf-8") if l.strip()]
        if not rows:
            continue
        n_win += 1
        n_rows_total += len(rows)
        if len(rows) >= CAP_ROWS:
            capped += 1
        groups = defaultdict(list)
        for r in rows:
            groups[(r["transactionHash"], r["asset"])].append(r)
        win_out = "Up" if lab["resolved_up"] else "Down"
        close = ep + 300
        inv = defaultdict(lambda: defaultdict(float))
        cash = defaultdict(float)
        for (tx, asset), g in groups.items():
            n_buy = sum(1 for r in g if r["side"] == "BUY")
            n_sell = len(g) - n_buy
            maker_side = "BUY" if n_buy > n_sell else ("SELL" if n_sell > n_buy else None)
            for r in g:
                wal = r["proxyWallet"]
                w = W[wal]
                w["name"] = r.get("name") or w["name"]
                try:
                    px, sz, ts = float(r["price"]), float(r["size"]), int(r["timestamp"])
                except (TypeError, ValueError):
                    continue
                out = r["outcome"]
                w["n_rows"] += 1
                w["vol"] += px * sz
                w["n_windows"].add(ep)
                if maker_side is None:
                    w["unk"] += 1
                elif r["side"] == maker_side:
                    w["maker"] += 1
                else:
                    w["taker"] += 1
                k = close - ts
                if r["side"] == "BUY":
                    inv[wal][out] += sz
                    cash[wal] -= px * sz
                    w["buy_px"].append(px)
                    w["buy_k"].append(k)
                    w["buys"] += 1
                    w["wins"] += (out == win_out)
                    if 6 <= k <= 25 and px <= 0.80:
                        w["deep_n"] += 1
                        w["deep_sh"] += sz
                        if out == win_out:
                            w["deep_win_sh"] += sz
                            w["deep_pnl"] += sz * (1 - px)
                        else:
                            w["deep_pnl"] -= sz * px
                else:
                    inv[wal][out] -= sz
                    cash[wal] += px * sz
                    w["sells"] += 1
                    if out == win_out and -60 <= k <= 25 and px <= 0.80:
                        w["ps_n"] += 1
                        w["ps_sh"] += sz
                        w["ps_val"] += sz * (1 - px)   # value ceded to the buyer
                        w["ps_px"].append(px)
                        w["ps_k"].append(k)
                        w["ps_windows"].add(ep)
                        if maker_side is None:
                            w["ps_unk_sh"] += sz
                        elif maker_side == "SELL":
                            w["ps_maker_sh"] += sz   # resting ask, lifted by a buyer
                        else:
                            w["ps_taker_sh"] += sz   # taker sell = hit resting bids
        for wal, outs in inv.items():
            pnl = cash[wal] + outs.get(win_out, 0.0)
            W[wal]["pnl"] += pnl
            per_day[wal][(ep - 4 * 3600) // 86400] += pnl

    print(f"[{SPAN}] {n_win} windows, {n_rows_total} rows, {capped} row-capped, {len(W)} wallets")

    def rec(wal, w):
        mt = w["maker"] + w["taker"]
        days = per_day[wal]
        return dict(
            wallet=wal, name=w["name"], pnl=round(w["pnl"], 0), vol=round(w["vol"], 0),
            n_windows=len(w["n_windows"]), n_rows=w["n_rows"], sells=w["sells"],
            buys=w["buys"], win_pct=round(100 * w["wins"] / w["buys"]) if w["buys"] else None,
            maker_pct=round(100 * w["maker"] / mt) if mt else None,
            unk_pct=round(100 * w["unk"] / w["n_rows"]) if w["n_rows"] else None,
            buy_px_med=q(w["buy_px"], .5), buy_px_q25=q(w["buy_px"], .25), buy_px_q75=q(w["buy_px"], .75),
            k_med=q(w["buy_k"], .5), k_q10=q(w["buy_k"], .1), k_q90=q(w["buy_k"], .9),
            days_pos=sum(1 for v in days.values() if v > 0), days_n=len(days),
            deep_n=w["deep_n"], deep_sh=round(w["deep_sh"]),
            deep_win_pct=round(100 * w["deep_win_sh"] / w["deep_sh"]) if w["deep_sh"] else None,
            deep_pnl=round(w["deep_pnl"], 0),
            ps_n=w["ps_n"], ps_sh=round(w["ps_sh"]), ps_val=round(w["ps_val"], 0),
            ps_px_med=q(w["ps_px"], .5), ps_k_med=q(w["ps_k"], .5),
            ps_k_q10=q(w["ps_k"], .1), ps_k_q90=q(w["ps_k"], .9),
            ps_windows=len(w["ps_windows"]),
            ps_taker_sh=round(w["ps_taker_sh"]), ps_maker_sh=round(w["ps_maker_sh"]),
            ps_unk_sh=round(w["ps_unk_sh"]))

    ranked = sorted(W.items(), key=lambda kv: -kv[1]["pnl"])
    top = [rec(wal, w) for wal, w in ranked[:15]]
    bottom = [rec(wal, w) for wal, w in ranked[-5:]]
    watch = [rec(wal, w) for wal, w in W.items() if wal[:10] in WATCH]
    for o in watch:
        o["alias"] = WATCH[o["wallet"][:10]]
    deep = sorted((rec(wal, w) for wal, w in W.items() if w["deep_n"] >= 5),
                  key=lambda o: -o["deep_sh"])[:15]
    panic = sorted((rec(wal, w) for wal, w in W.items() if w["ps_n"] >= 3),
                   key=lambda o: -o["ps_sh"])[:20]
    tot_pnl = sum(w["pnl"] for w in W.values())
    tot_vol = sum(w["vol"] for w in W.values())
    # seller concentration: share of total panic value held by top-N sellers
    all_ps = sorted((w["ps_sh"] for w in W.values() if w["ps_sh"] > 0), reverse=True)
    tot_ps_sh = sum(all_ps)
    tot_ps_val = sum(w["ps_val"] for w in W.values())
    n_sellers = len(all_ps)
    hdr = (f"{'wallet':14s} {'name':16s} {'pnl':>7s} {'vol':>8s} {'nwin':>4s} {'mk%':>4s} "
           f"{'unk%':>4s} {'win%':>4s} {'px50':>5s} {'k50':>5s} {'d+/d':>5s} {'deepN':>5s} {'deepW%':>6s}")

    def line(o):
        return (f"{o['wallet'][:14]:14s} {(o.get('alias') or o['name'] or '')[:16]:16s} {o['pnl']:7.0f} "
                f"{o['vol']:8.0f} {o['n_windows']:4d} {str(o['maker_pct'] if o['maker_pct'] is not None else '-'):>4s} "
                f"{str(o['unk_pct']):>4s} {str(o['win_pct']):>4s} {str(o['buy_px_med']):>5s} "
                f"{str(o['k_med']):>5s} {o['days_pos']}/{o['days_n']:<3d} {o['deep_n']:5d} "
                f"{str(o['deep_win_pct'] if o['deep_win_pct'] is not None else '-'):>6s}")

    print(f"\nsum pnl {tot_pnl:.0f}  sum vol {tot_vol:.0f}  (zero-sum check)")
    print("\nTOP 15 by P&L\n" + hdr)
    for o in top:
        print(line(o))
    print("\nBOTTOM 5\n" + hdr)
    for o in bottom:
        print(line(o))
    print("\nWATCHLIST\n" + hdr)
    for o in sorted(watch, key=lambda o: -o["pnl"]):
        print(line(o))
    print("\nDEEP-BID POCKET BUYERS k in [6,25], px<=0.80, >=5 fills, by shares\n" + hdr)
    for o in deep:
        print(line(o), f" deep_sh {o['deep_sh']} deep_pnl {o['deep_pnl']}")
    print(f"\nPANIC SELLERS (winner-token SELL, k in [-60,25], px<=0.80): "
          f"{n_sellers} wallets, {tot_ps_sh:.0f} sh, ${tot_ps_val:.0f} ceded")
    if all_ps and tot_ps_sh:
        for topn in (1, 5, 10, 20):
            print(f"  top{topn} share of panic sh: {100 * sum(all_ps[:topn]) / tot_ps_sh:.0f}%")
    tk = sum(w["ps_taker_sh"] for w in W.values())
    mk = sum(w["ps_maker_sh"] for w in W.values())
    uk = sum(w["ps_unk_sh"] for w in W.values())
    print(f"  attribution (sh): taker-sell {tk:.0f} | maker-ask-lifted {mk:.0f} | unknown {uk:.0f}")
    print(f"{'wallet':14s} {'name':16s} {'pnl':>7s} {'psN':>4s} {'ps_sh':>7s} {'ps$':>6s} "
          f"{'px50':>5s} {'k50':>5s} {'k10':>5s} {'k90':>5s} {'nwin':>4s} {'tk_sh':>6s} {'mk_sh':>6s}")
    for o in panic:
        print(f"{o['wallet'][:14]:14s} {(o.get('alias') or o['name'] or '')[:16]:16s} "
              f"{o['pnl']:7.0f} {o['ps_n']:4d} {o['ps_sh']:7.0f} {o['ps_val']:6.0f} "
              f"{str(o['ps_px_med']):>5s} {str(o['ps_k_med']):>5s} {str(o['ps_k_q10']):>5s} "
              f"{str(o['ps_k_q90']):>5s} {o['ps_windows']:4d} {o['ps_taker_sh']:6d} {o['ps_maker_sh']:6d}")
    json.dump(dict(span=SPAN, n_windows=n_win, n_rows=n_rows_total, capped=capped,
                   n_wallets=len(W), top=top, bottom=bottom, watch=watch, deep=deep,
                   panic=panic, panic_total_sh=tot_ps_sh, panic_total_val=tot_ps_val,
                   panic_n_sellers=n_sellers),
              open(OUT, "w"), indent=1)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
