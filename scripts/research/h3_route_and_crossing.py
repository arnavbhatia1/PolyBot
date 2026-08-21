"""H3 (b) cheaper-route + the mechanism question.

Mechanism first (tape): does the matching engine cross complements (a BUY Up
matching a BUY Down via minting)? Evidence: near-simultaneous complementary
print pairs — token X print at p, complement print within +-0.5s at 1-p +-
0.005. If pervasive, "route choice" is illusory: the engine already routes.

Route (window_paths): at ladder arm times (k = close - ts in [6,25], one row
per second), spread_route_up = ask_up - (1 - bid_down) — how much cheaper the
synthetic route (mint $1, sell Down at bid) is vs lifting the Up ask.
Pre-registered bar: >=1 tick (0.01) median improvement on >=20% of arms.

Outputs: h3_crossing.json, h3_route.json.
"""
import gzip
import json
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
EPOCH = 1786665600
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]
PAIR_DT = 0.5        # s — near-simultaneous
PAIR_DP = 0.005      # $ — |p_comp - (1-p)| tolerance
SIZE_REL = 0.10      # similar size = within 10% of the larger


def load_token_map() -> dict:
    m = json.loads((DATA / "token_map.json").read_text())["map"]
    return {d[s]: (int(ep), s) for ep, d in m.items() for s in ("up", "down")}


# --------------------------------------------------------------- crossing ---

def crossing_scan(tok: dict) -> dict:
    out = {"pair_dt_s": PAIR_DT, "pair_dp": PAIR_DP, "size_rel": SIZE_REL,
           "days": {}}
    for day in DAYS:
        path = DATA / f"tape_{day}.jsonl.gz"
        if not path.exists():
            path = DATA / f"tape_{day}.jsonl"
        if not path.exists():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        wins = defaultdict(lambda: ([], []))   # ep -> (up_prints, down_prints)
        n_unknown = 0
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = tok.get(r["token"])
                if t is None:
                    n_unknown += 1
                    continue
                ep, side = t
                try:
                    wins[ep][0 if side == "up" else 1].append(
                        (float(r["ts"]), float(r["price"]), float(r["size"]),
                         r.get("side", "")))
                except (TypeError, ValueError):
                    continue
        d = {"n_prints": 0, "n_matched_price": 0, "n_matched_price_size": 0,
             "vol_sh": 0.0, "vol_matched_price": 0.0, "n_unknown_token": n_unknown,
             "n_windows": len(wins)}
        for ep, (ups, dns) in wins.items():
            ups.sort(); dns.sort()
            for prints, comp in ((ups, dns), (dns, ups)):
                comp_ts = [c[0] for c in comp]
                for ts, px, sz, _sd in prints:
                    d["n_prints"] += 1
                    d["vol_sh"] += sz
                    lo = bisect_left(comp_ts, ts - PAIR_DT)
                    hi = bisect_right(comp_ts, ts + PAIR_DT)
                    hit = hit_sz = False
                    for j in range(lo, hi):
                        if abs(comp[j][1] - (1.0 - px)) <= PAIR_DP + 1e-12:
                            hit = True
                            big = max(sz, comp[j][2])
                            if big > 0 and abs(sz - comp[j][2]) <= SIZE_REL * big:
                                hit_sz = True
                                break
                    if hit:
                        d["n_matched_price"] += 1
                        d["vol_matched_price"] += sz
                    if hit_sz:
                        d["n_matched_price_size"] += 1
        d["vol_sh"] = round(d["vol_sh"], 1)
        d["vol_matched_price"] = round(d["vol_matched_price"], 1)
        out["days"][day] = d
        print(f"{day}: prints={d['n_prints']} matched_price="
              f"{d['n_matched_price']} (+size {d['n_matched_price_size']})", flush=True)
    tot = {k: sum(d[k] for d in out["days"].values())
           for k in ("n_prints", "n_matched_price", "n_matched_price_size",
                     "vol_sh", "vol_matched_price")}
    tot["share_matched_price"] = round(tot["n_matched_price"] / max(tot["n_prints"], 1), 4)
    tot["share_matched_price_size"] = round(tot["n_matched_price_size"] / max(tot["n_prints"], 1), 4)
    tot["vol_share_matched"] = round(tot["vol_matched_price"] / max(tot["vol_sh"], 1e-9), 4)
    out["total"] = tot
    return out


# ------------------------------------------------------------------ route ---

def route_scan() -> dict:
    conn = sqlite3.connect(f"file:{DATA / 'window_paths_60s.db'}?mode=ro", uri=True)
    cur = conn.execute(
        "SELECT window_id, ts, elapsed_s, bid_up, ask_up, bid_down, ask_down "
        "FROM window_paths WHERE ts >= ? AND elapsed_s >= 275 AND elapsed_s < 294.5 "
        "ORDER BY window_id, ts", (EPOCH,))
    seen = set()
    imp_up, imp_dn = [], []
    n_arm_rows = n_missing = 0
    for w, ts, el, bu, au, bd, ad in cur:
        sec = (w, int(ts))          # one row per arm-second (final 45s is 5Hz)
        if sec in seen:
            continue
        seen.add(sec)
        n_arm_rows += 1
        if au is not None and bd is not None:
            imp_up.append(au - (1.0 - bd))
        else:
            n_missing += 1
        if ad is not None and bu is not None:
            imp_dn.append(ad - (1.0 - bu))
    conn.close()

    def dist(v: list) -> dict:
        if not v:
            return {"n": 0}
        v = sorted(v)
        n = len(v)
        q = lambda p: round(v[min(int(p * n), n - 1)], 4)
        return {"n": n, "median": q(0.5), "mean": round(sum(v) / n, 4),
                "p10": q(0.10), "p90": q(0.90), "max": round(v[-1], 4),
                "share_ge_1tick": round(sum(1 for x in v if x >= 0.01 - 1e-9) / n, 4),
                "share_gt_0": round(sum(1 for x in v if x > 1e-9) / n, 4)}

    return {"arm_def": "k=close-ts in [6,25], 1 row/s, era rows",
            "n_arm_seconds": n_arm_rows, "n_missing_side": n_missing,
            "up_entry": dist(imp_up), "down_entry": dist(imp_dn)}


def main():
    outp = DATA / "h3_route.json"
    if not outp.exists():
        r = route_scan()
        outp.write_text(json.dumps(r, indent=1))
        print("route:", json.dumps(r["up_entry"]), flush=True)
    else:
        print("route: cached", flush=True)
    outp = DATA / "h3_crossing.json"
    if not outp.exists():
        tok = load_token_map()
        outp.write_text(json.dumps(crossing_scan(tok), indent=1))
    else:
        print("crossing: cached", flush=True)


if __name__ == "__main__":
    main()
