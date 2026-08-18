"""WS3.1: rank all wallets by realized P&L on btc-updown-5m since 08-07.

Per wallet per window: cash flow +- and terminal payout from window_labels.
Maker/taker per row: within a transactionHash+asset group, the side with more
rows is the maker side (one taker matches many makers); 1v1 groups classified
by our own tape's aggressor side when a matching print exists, else unknown.

Outputs data/wallet_census.json: per-wallet aggregates + per-fill records for
the top wallets (for the drift/sign analyses).
"""
import gzip
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
PM = DATA / "pm_trades"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
TWAP_SWITCH = 1786060800
RULE60 = 1786665600
CAP_ROWS = 3500        # data-api offset ceiling -> oldest rows of heavy windows missing


def load_labels():
    labels = {}
    for name in ("polybot_paper.db", "polybot_live.db"):
        p = DATA / name
        if not p.exists():
            continue
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        for r in db.execute("SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'"):
            ep = int(r["window_id"].rsplit("-", 1)[1])
            if ep >= TWAP_SWITCH:
                labels.setdefault(ep, dict(r))
        db.close()
    return labels


def main():
    labels = load_labels()
    # tape aggressor side per (rounded ts, price) for 1v1 classification
    tape_sides = {}
    for f in sorted(REC.glob("tape_2026-08-*.jsonl.gz")):
        with gzip.open(f, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                try:
                    key = (r["token"][:20], round(float(r["price"]), 3),
                           round(float(r["size"]), 2), int(float(r["ts"])))
                except (TypeError, ValueError):
                    continue
                tape_sides[key] = r.get("side")

    W = defaultdict(lambda: dict(
        pnl=0.0, vol=0.0, n_rows=0, maker=0, taker=0, unk=0,
        buy_px=[], wins=0.0, losses=0.0, n_windows=set(), sells=0,
        pnl_pre=0.0, pnl_post=0.0, name=""))
    capped = 0
    for pf in sorted(PM.glob("*.jsonl")):
        ep = int(pf.stem)
        lab = labels.get(ep)
        if lab is None:
            continue
        rows = [json.loads(l) for l in open(pf, encoding="utf-8")]
        if len(rows) >= CAP_ROWS:
            capped += 1
        # group by (tx, asset) for maker/taker
        groups = defaultdict(list)
        for r in rows:
            groups[(r["transactionHash"], r["asset"])].append(r)
        win_out = "Up" if lab["resolved_up"] else "Down"
        # per wallet inventory in this window
        inv = defaultdict(lambda: defaultdict(float))   # wallet -> outcome -> shares
        cash = defaultdict(float)
        for (tx, asset), g in groups.items():
            n_buy = sum(1 for r in g if r["side"] == "BUY")
            n_sell = len(g) - n_buy
            if n_buy > n_sell:
                maker_side = "BUY"
            elif n_sell > n_buy:
                maker_side = "SELL"
            else:
                maker_side = None
                r0 = g[0]
                try:
                    key = (asset[:20], round(float(r0["price"]), 3),
                           round(float(r0["size"]), 2), int(r0["timestamp"]))
                    agg = tape_sides.get(key)
                    if agg:
                        maker_side = "BUY" if agg == "SELL" else "SELL"
                except (TypeError, ValueError):
                    pass
            for r in g:
                wal = r["proxyWallet"]
                w = W[wal]
                w["name"] = r.get("name") or w["name"]
                try:
                    px = float(r["price"])
                    sz = float(r["size"])
                    ts = int(r["timestamp"])
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
                    inv[wal][out] += sz
                    cash[wal] -= px * sz
                    w["buy_px"].append(px)
                else:
                    inv[wal][out] -= sz
                    cash[wal] += px * sz
                    w["sells"] += 1
        for wal, outs in inv.items():
            payout = outs.get(win_out, 0.0) * 1.0
            pnl = cash[wal] + payout
            W[wal]["pnl"] += pnl
            if ep >= RULE60:
                W[wal]["pnl_post"] += pnl
            else:
                W[wal]["pnl_pre"] += pnl

    print(f"{len(W)} wallets, {capped} row-capped windows")
    ranked = sorted(W.items(), key=lambda kv: -kv[1]["pnl"])
    out = []
    for wal, w in ranked[:40]:
        bp = sorted(w["buy_px"])
        q = lambda f: bp[min(int(f * len(bp)), len(bp) - 1)] if bp else None
        mt = w["maker"] + w["taker"]
        out.append(dict(wallet=wal, name=w["name"], pnl=round(w["pnl"], 0),
                        pnl_pre=round(w["pnl_pre"], 0), pnl_post=round(w["pnl_post"], 0),
                        vol=round(w["vol"], 0), n_windows=len(w["n_windows"]),
                        n_rows=w["n_rows"], sells=w["sells"],
                        maker_pct=round(100 * w["maker"] / mt, 0) if mt else None,
                        buy_px_q=[q(0.1), q(0.25), q(0.5), q(0.75), q(0.9)]))
    json.dump(out, open(DATA / "wallet_census_top.json", "w"), indent=1)
    print(f"{'wallet':14s} {'name':16s} {'pnl':>8s} {'pre':>8s} {'post60':>8s} "
          f"{'vol':>9s} {'nwin':>5s} {'mk%':>4s} {'sells':>5s}  buy_px q10/50/90")
    for o in out[:25]:
        bq = o["buy_px_q"]
        print(f"{o['wallet'][:14]:14s} {(o['name'] or '')[:16]:16s} {o['pnl']:8.0f} "
              f"{o['pnl_pre']:8.0f} {o['pnl_post']:8.0f} {o['vol']:9.0f} "
              f"{o['n_windows']:5d} {str(o['maker_pct'] or '-'):>4s} {o['sells']:5d}  "
              f"{bq[0]} / {bq[2]} / {bq[4]}")

    # per-fill records: top-10 overall + top-10 post-60s + 1723 explicitly
    by_post = sorted(out, key=lambda o: -o["pnl_post"])
    top_set = ({o["wallet"] for o in out[:10]}
               | {o["wallet"] for o in by_post[:10]}
               | {w for w in W if w.startswith("0x3b8407699e83")})
    fills = []
    for pf in sorted(PM.glob("*.jsonl")):
        ep = int(pf.stem)
        if ep not in labels:
            continue
        for line in open(pf, encoding="utf-8"):
            r = json.loads(line)
            if r["proxyWallet"] in top_set:
                fills.append(dict(w=r["proxyWallet"], ep=ep, side=r["side"],
                                  out=r["outcome"], px=r["price"], sz=r["size"],
                                  ts=r["timestamp"]))
    json.dump(fills, open(DATA / "top_wallet_fills.json", "w"))
    print(f"\n{len(fills)} per-fill rows for top-12 wallets -> top_wallet_fills.json")


if __name__ == "__main__":
    main()
