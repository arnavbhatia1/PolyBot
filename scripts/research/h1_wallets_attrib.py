"""H1 pass 5b: wallet attribution for pockets B and C on the pulled sample.

Match rule (per tape print, taker side s at level price p, size z, token T):
  same-book maker  = data-api row, token T,       side != s, price==p,   size~z
  cross-book maker = data-api row, complement(T), side == s, price~=1-p, size~z
(cross = Polymarket mint/merge adapter: a taker BUY on T can fill resting
BIDS on complement(T); maker rows keep their own book's level price.)
Time tolerance +/-15s (data-api stamps lag the exchange feed by seconds).

Pocket B: taper-BUY prints, k6-0, px<=0.10, loser token  -> who sells the
          dying side: true loser-book asks vs winner-book bid wall via cross?
Pocket C: taker-SELL prints, k300-60 (coverage: late slice of the band),
          all prices -> who owns the mid-window bid wall?

Output: data/vps-0821/h1_wallets.json + printed summary.
"""
import gzip
import json
import pickle
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]


def load_sample():
    meta = json.load(open(DATA / "h1_wallet_sample.json"))
    tm = json.load(open(DATA / "token_map.json"))["map"]
    with open(DATA / "h1_cellstats.pkl", "rb") as f:
        labels = pickle.load(f)["labels"]
    return meta, tm, labels


def tape_prints_for(windows, tm):
    toks = {}
    for w in windows:
        d = tm[str(w)]
        toks[d["up"]] = (w, 1)
        toks[d["down"]] = (w, 0)
    prints = defaultdict(list)   # win -> [(ts, tok, is_up, px, sz, side)]
    for day in DAYS:
        gz = DATA / f"tape_{day}.jsonl.gz"
        path = gz if gz.exists() else DATA / f"tape_{day}.jsonl"
        op = (lambda p: gzip.open(p, "rt", encoding="utf-8")) if path.suffix == ".gz" \
            else (lambda p: open(p, encoding="utf-8"))
        with op(path) as f:
            for line in f:
                r = json.loads(line)
                t = toks.get(r["token"])
                if t is None:
                    continue
                w, is_up = t
                prints[w].append((float(r["ts"]), r["token"], is_up,
                                  float(r["price"]), float(r["size"]), r["side"]))
    return prints


def match_pocket(win, plist, rows, tm, labels, pocket):
    """returns list of (wallet, name, book, maker_usd, sz, px) for matched makers"""
    ru = labels[win]
    up_tok = tm[str(win)]["up"]
    down_tok = tm[str(win)]["down"]
    comp = {up_tok: down_tok, down_tok: up_tok}
    close = win + 300

    if pocket == "B":
        sel = [p for p in plist
               if p[5] == "BUY" and p[3] <= 0.10 and 0 < close - p[0] <= 6
               and (p[2] != ru)]                       # loser token
    else:  # C
        sel = [p for p in plist
               if p[5] == "SELL" and 60 < close - p[0] <= 300]

    # index data-api rows by (token, side, round(price,4)) -> [(ts, size, row)]
    ridx = defaultdict(list)
    for r in rows:
        ridx[(r["asset"], r["side"], round(float(r["price"]), 4))].append(
            (r["timestamp"], float(r["size"]), r))
    used = set()
    out = []
    n_unmatched = 0
    for ts, tok, is_up, px, sz, side in sel:
        v = 1.0 if is_up == ru else 0.0
        cands = []
        opp = "SELL" if side == "BUY" else "BUY"
        for rts, rsz, r in ridx.get((tok, opp, round(px, 4)), []):
            if abs(rts - ts) <= 15 and abs(rsz - sz) <= 0.02:
                cands.append(("same", rts, r))
        for rts, rsz, r in ridx.get((comp[tok], side, round(1 - px, 4)), []):
            if abs(rts - ts) <= 15 and abs(rsz - sz) <= 0.02:
                cands.append(("cross", rts, r))
        cands = [c for c in cands if id(c[2]) not in used]
        if not cands:
            n_unmatched += 1
            continue
        book, _, r = min(cands, key=lambda c: abs(c[1] - ts))
        used.add(id(r))
        # maker P&L vs resolution, in the taker-book convention
        if side == "BUY":
            usd = (px - v) * sz     # maker gave up the token at px
        else:
            usd = (v - px) * sz     # maker bought at px
        out.append((r["proxyWallet"], r.get("name") or "", book, usd, sz, px))
    return out, len(sel), n_unmatched


def main():
    meta, tm, labels = load_sample()
    res = {}
    for pocket, wins in (("B", meta["B"]), ("C", meta["C"])):
        prints = tape_prints_for(wins, tm)
        wallets = defaultdict(lambda: [0.0, 0.0, 0, ""])   # usd, sh, n, name
        book_split = defaultdict(float)
        tot_sel = tot_unm = 0
        for w in wins:
            f = DATA / "h1_pm_trades" / f"{w}.jsonl"
            if not f.exists():
                continue
            rows = [json.loads(l) for l in open(f, encoding="utf-8")]
            matched, n_sel, n_unm = match_pocket(w, prints.get(w, []), rows,
                                                 tm, labels, pocket)
            tot_sel += n_sel
            tot_unm += n_unm
            for wal, name, book, usd, sz, px in matched:
                a = wallets[wal]
                a[0] += usd
                a[1] += sz
                a[2] += 1
                a[3] = name
                book_split[book] += usd
                book_split[book + "_sh"] += sz
        rank = sorted(wallets.items(), key=lambda kv: -kv[1][0])
        tot_usd = sum(v[0] for v in wallets.values())
        res[pocket] = {
            "windows": len(wins), "prints_selected": tot_sel,
            "prints_unmatched": tot_unm,
            "match_rate": 1 - tot_unm / tot_sel if tot_sel else None,
            "total_maker_usd_matched": tot_usd,
            "book_split_usd": {k: v for k, v in book_split.items() if not k.endswith("_sh")},
            "book_split_sh": {k.replace("_sh", ""): v for k, v in book_split.items() if k.endswith("_sh")},
            "n_wallets": len(rank),
            "top1_share": rank[0][1][0] / tot_usd if tot_usd else None,
            "top5_share": sum(v[0] for _, v in rank[:5]) / tot_usd if tot_usd else None,
            "top15": [{"wallet": k, "name": v[3], "usd": v[0], "sh": v[1],
                       "fills": v[2]} for k, v in rank[:15]],
        }
        print(f"\n=== pocket {pocket} === windows {len(wins)} "
              f"prints {tot_sel} matched {tot_sel - tot_unm} "
              f"({(1 - tot_unm / tot_sel) * 100 if tot_sel else 0:.0f}%)")
        print(f"  book split ($): {res[pocket]['book_split_usd']}")
        print(f"  book split (sh): {res[pocket]['book_split_sh']}")
        print(f"  wallets {len(rank)}  top1 {res[pocket]['top1_share'] and res[pocket]['top1_share']*100:.0f}% "
              f" top5 {res[pocket]['top5_share'] and res[pocket]['top5_share']*100:.0f}%")
        for e in res[pocket]["top15"][:10]:
            print(f"    {e['wallet'][:10]} {e['name'][:16]:>16} "
                  f"${e['usd']:>8,.0f} {e['sh']:>10,.0f} sh {e['fills']:>5} fills")

    with open(DATA / "h1_wallets.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
