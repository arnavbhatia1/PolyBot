"""H4: what does selling the boundary-certain winner into the post-close bid cost?

For every post-rule labeled window, take the WINNER token's tape prints in
[close+2, close+30] (certainty exists from ~close+3s: close boundary report
p50 +1.7s / p99 +2.9s). SELL-side prints are takers hitting bids — direct
observations of executable sell prices. Haircut/sh = 1 - exec + 0.07*p*(1-p).

Coverage caveat: no print != no bid (the 0.99 wall rests regardless); windows
without winner-side SELL prints are reported as unobserved, and the at-close
winner bid from window_paths (elapsed >= 299) is given as a floor.

Output: scripts/research/data/vps-0821/h4_haircut.json
"""
import gzip
import json
import sqlite3
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
ERA = 1786665600
SPAN = (2.0, 30.0)          # seconds after close
DAYS = ["2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]


def fee(p):
    return 0.07 * p * (1 - p)


def quantiles(v):
    if not v:
        return None
    v = sorted(v)
    def q(pct):
        return v[max(0, min(len(v) - 1, round(pct * (len(v) - 1))))]
    return {"n": len(v), "p05": q(.05), "p50": q(.50), "p95": q(.95),
            "min": min(v), "max": max(v)}


def main():
    # winner per labeled post-rule window
    con = sqlite3.connect(f"file:{DATA/'polybot_paper_0821.db'}?mode=ro", uri=True)
    winners = {}
    for wid, up in con.execute(
            "SELECT window_id, resolved_up FROM window_labels "
            "WHERE window_id LIKE 'btc-updown-5m-%'"):
        ep = int(wid.rsplit("-", 1)[1])
        if ep >= ERA:
            winners[ep] = "up" if up else "down"
    con.close()

    tok_map = json.load(open(DATA / "token_map.json"))["map"]
    tok_to_win = {}   # winner token id -> window open epoch
    for ts, sides in tok_map.items():
        ep = int(ts)
        side = winners.get(ep)
        if side and sides.get(side):
            tok_to_win[str(sides[side])] = ep

    per_window = {}   # ep -> {"sell": [(price, size)], "buy": [(price, size)]}
    for day in DAYS:
        for suffix, opener in ((".jsonl.gz", gzip.open), (".jsonl", open)):
            f = DATA / f"tape_{day}{suffix}"
            if not f.exists():
                continue
            with opener(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ep = tok_to_win.get(str(r.get("token")))
                    if ep is None:
                        continue
                    dt = r["ts"] - (ep + 300)
                    if not (SPAN[0] <= dt <= SPAN[1]):
                        continue
                    try:
                        px, sz = float(r["price"]), float(r["size"])
                    except (TypeError, ValueError):
                        continue
                    side = (r.get("side") or "").upper()
                    d = per_window.setdefault(ep, {"sell": [], "buy": []})
                    d["sell" if side == "SELL" else "buy"].append((px, sz))
            break   # prefer .gz if both exist

    # per-window executable sell price: size-weighted SELL print price;
    # best (max) SELL print price = top of the bid actually hit
    hc_sw, hc_best, sell_depth, best_px = [], [], [], []
    for ep, d in per_window.items():
        if not d["sell"]:
            continue
        tot = sum(sz for _, sz in d["sell"])
        sw = sum(px * sz for px, sz in d["sell"]) / tot
        best = max(px for px, _ in d["sell"])
        hc_sw.append(1 - sw + fee(sw))
        hc_best.append(1 - best + fee(best))
        best_px.append(best)
        sell_depth.append(tot)

    # at-close winner bid floor from window_paths final samples
    con = sqlite3.connect(f"file:{DATA/'window_paths_60s.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    close_bid, close_bidsz = [], []
    for ep, side in winners.items():
        r = con.execute(
            "SELECT bid_up, bid_down, bid_sz_up, bid_sz_down FROM window_paths "
            "WHERE window_id=? AND elapsed_s>=299 ORDER BY elapsed_s DESC LIMIT 1",
            (f"btc-updown-5m-{ep}",)).fetchone()
        if r is None:
            continue
        b = r["bid_up"] if side == "up" else r["bid_down"]
        s = r["bid_sz_up"] if side == "up" else r["bid_sz_down"]
        if b is not None:
            close_bid.append(b)
            if s is not None:
                close_bidsz.append(s)
    con.close()

    out = {
        "span_after_close_s": SPAN,
        "n_labeled_windows": len(winners),
        "n_windows_with_winner_prints": len(per_window),
        "n_windows_with_winner_SELL_prints": len(hc_sw),
        "haircut_sizeweighted_per_sh": quantiles(hc_sw),
        "haircut_at_best_sell_print": quantiles(hc_best),
        "best_sell_print_price": quantiles(best_px),
        "sell_print_shares_per_window": quantiles(sell_depth),
        "at_close_winner_bid_floor": quantiles(close_bid),
        "at_close_winner_bid_sz_sh": quantiles(close_bidsz),
    }
    (DATA / "h4_haircut.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
