"""R5 weekly census reducer over data/vps-0821/r5_pm_trades (08-21..27 sample).

Per wallet: realized P&L (cash flow + $1/share terminal payout from
window_labels), maker% (tx+asset group majority side = maker; 1v1 groups are
unknown — no local tape for this week), median BUY price, fill-k median
(k = close - ts, BUY fills), windows active. Also the deep-bid pocket read:
BUY fills at k in [6,25] with price <= 0.80, by wallet, winner-side share.
Output: r5_census.json + printed tables.
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
PM = DATA / "r5_pm_trades"
DB = DATA / "paper_0827.db"
CAP_ROWS = 3500
WATCH = {  # WALLETS.md leaderboard + 08-21 uncensused wall wallet
    "0x251c1a28": "0xAAAAA", "0x568b0798": "-", "0x3725d52f": "almach",
    "0x0cb03848": "-", "0xc2ad03f7": "bosona", "0x32ed2e54": "mo-money",
    "0xfc369971": "gesinimen", "0xce50c96b": "honey-spot", "0xe0229e10": "JetFadil",
    "0x48ac40fc": "BoneOhio", "0x3b840769": "1723", "0x6fc44ec4": "wall-0821",
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
        deep_n=0, deep_sh=0.0, deep_win_sh=0.0, deep_pnl=0.0))
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
                if r["side"] == "BUY":
                    k = close - ts
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
        for wal, outs in inv.items():
            pnl = cash[wal] + outs.get(win_out, 0.0)
            W[wal]["pnl"] += pnl
            per_day[wal][(ep - 4 * 3600) // 86400] += pnl   # ET-ish day bucket

    print(f"{n_win} windows, {n_rows_total} rows, {capped} row-capped, {len(W)} wallets")

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
            deep_pnl=round(w["deep_pnl"], 0))

    ranked = sorted(W.items(), key=lambda kv: -kv[1]["pnl"])
    top = [rec(wal, w) for wal, w in ranked[:15]]
    bottom = [rec(wal, w) for wal, w in ranked[-5:]]
    watch = [rec(wal, w) for wal, w in W.items() if wal[:10] in WATCH]
    for o in watch:
        o["alias"] = WATCH[o["wallet"][:10]]
    deep = sorted((rec(wal, w) for wal, w in W.items() if w["deep_n"] >= 5),
                  key=lambda o: -o["deep_sh"])[:15]
    tot_pnl = sum(w["pnl"] for w in W.values())
    tot_vol = sum(w["vol"] for w in W.values())
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
    print("\nWATCHLIST (WALLETS.md leaderboard + 0x6fc44)\n" + hdr)
    for o in sorted(watch, key=lambda o: -o["pnl"]):
        print(line(o))
    print("\nDEEP POCKET k in [6,25], px<=0.80, BUY fills, >=5 fills, by shares\n" + hdr)
    for o in deep:
        print(line(o), f" deep_sh {o['deep_sh']} deep_pnl {o['deep_pnl']}")
    json.dump(dict(n_windows=n_win, n_rows=n_rows_total, capped=capped, n_wallets=len(W),
                   top=top, bottom=bottom, watch=watch, deep=deep),
              open(DATA / "r5_census.json", "w"), indent=1)


if __name__ == "__main__":
    main()
