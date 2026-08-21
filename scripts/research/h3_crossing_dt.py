"""H3 mechanism discriminator: engine mint-cross vs coincident two-sided flow.

A true complement cross (BUY Up matched to BUY Down via mint) executes as one
exchange event, so its two prints carry the SAME exchange timestamp (ets) and
complementary prices. Coincident independent flow will not line up on ets.

For every print: nearest complementary-price print (|p_comp - (1-p)| <= 0.005)
on the complement token, bucketed by |d_ets|: exact 0ms, <=10ms, <=100ms,
<=500ms. Control: complement stream shifted +30s (coincidence base rate).
Split by own-print depth (<=0.35 deep / 0.35-0.65 mid / >=0.65 tight).
Output: h3_crossing_dt.json.
"""
import gzip
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]
PAIR_DP = 0.005
BUCKETS = [("ets_0ms", 0.0), ("ets_10ms", 0.010), ("ets_100ms", 0.100),
           ("ets_500ms", 0.500)]


def load_token_map() -> dict:
    m = json.loads((DATA / "token_map.json").read_text())["map"]
    return {d[s]: (int(ep), s) for ep, d in m.items() for s in ("up", "down")}


def price_band(p: float) -> str:
    return "deep<=0.35" if p <= 0.35 else ("tight>=0.65" if p >= 0.65 else "mid")


def match_stats(prints, comp, shift: float, res: dict):
    """prints/comp: sorted lists of (ets_s, price, size). Adds to res in place."""
    comp_ts = [c[0] + shift for c in comp]
    for ts, px, sz in prints:
        band = price_band(px)
        r = res[band]
        r["n"] += 1
        r["sh"] += sz
        lo = bisect_left(comp_ts, ts - 0.5)
        hi = bisect_right(comp_ts, ts + 0.5)
        best = None
        for j in range(lo, hi):
            if abs(comp[j][1] - (1.0 - px)) <= PAIR_DP + 1e-12:
                d = abs(comp_ts[j] - ts)
                if best is None or d < best:
                    best = d
        if best is None:
            continue
        for name, tol in BUCKETS:
            if best <= tol + 1e-9:
                r[name] += 1
                r[name + "_sh"] += sz
    return res


def new_bands() -> dict:
    return {b: {"n": 0, "sh": 0.0,
                **{name: 0 for name, _ in BUCKETS},
                **{name + "_sh": 0.0 for name, _ in BUCKETS}}
            for b in ("deep<=0.35", "mid", "tight>=0.65")}


def main():
    tok = load_token_map()
    real, ctrl = new_bands(), new_bands()
    n_no_ets = 0
    for day in DAYS:
        path = DATA / f"tape_{day}.jsonl.gz"
        if not path.exists():
            path = DATA / f"tape_{day}.jsonl"
        if not path.exists():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        wins = defaultdict(lambda: ([], []))
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = tok.get(r["token"])
                if t is None:
                    continue
                ep, side = t
                try:
                    ets = float(r["ets"]) / 1000.0
                    px, sz = float(r["price"]), float(r["size"])
                except (TypeError, ValueError, KeyError):
                    n_no_ets += 1
                    continue
                wins[ep][0 if side == "up" else 1].append((ets, px, sz))
        for ep, (ups, dns) in wins.items():
            ups.sort(); dns.sort()
            for prints, comp in ((ups, dns), (dns, ups)):
                match_stats(prints, comp, 0.0, real)
                match_stats(prints, comp, 30.0, ctrl)
        print(f"{day}: done ({len(wins)} windows)", flush=True)

    def finish(d):
        for band in d.values():
            band["sh"] = round(band["sh"], 1)
            for name, _ in BUCKETS:
                band[name + "_share"] = round(band[name] / max(band["n"], 1), 4)
                band[name + "_sh"] = round(band[name + "_sh"], 1)
        return d

    out = {"pair_dp": PAIR_DP, "n_no_ets": n_no_ets,
           "real": finish(real), "control_shift30": finish(ctrl)}
    (DATA / "h3_crossing_dt.json").write_text(json.dumps(out, indent=1))
    for band in ("deep<=0.35", "mid", "tight>=0.65"):
        r, c = out["real"][band], out["control_shift30"][band]
        print(f"{band}: n={r['n']} exact-ets={r['ets_0ms_share']} "
              f"<=100ms={r['ets_100ms_share']} <=500ms={r['ets_500ms_share']} "
              f"| control<=500ms={c['ets_500ms_share']}")


if __name__ == "__main__":
    main()
