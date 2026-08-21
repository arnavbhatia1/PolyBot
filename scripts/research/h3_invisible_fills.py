"""H3 side-finding: the invisible-fill rate for the paper maker matcher.

The paper matcher counts only prints on OUR token. If the engine crosses
complements (mint), a resting deep Up bid at price p can be filled by a taker
BUYING Down at q >= 1-p — flow that may print only on the Down token. For
each rung price p and every window, count complement BUY prints at q >= 1-p
inside the rung's resting span [close-25, close+60] that have NO mirror print
on our token (within +-0.5s at 1-q +- 0.005): those are fills paper cannot see.

Symmetric both directions (hypothetical Up bid and Down bid; the real ladder
rests on the projection-favored side only, so per-side rates are the honest
unit). Visible flow proxy = prints on our own token at <= p in the same span
(the paper matcher is price-only). Output: h3_invisible.json.
"""
import gzip
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]
RUNGS = [0.80, 0.65, 0.50, 0.35, 0.20]
REST_PRE_S = 25          # rung can rest from k=25
REST_POST_S = 60         # post-close hold
PAIR_DT = 0.5
PAIR_DP = 0.005


def load_token_map() -> dict:
    m = json.loads((DATA / "token_map.json").read_text())["map"]
    return {d[s]: (int(ep), s) for ep, d in m.items() for s in ("up", "down")}


def main():
    tok = load_token_map()
    per_rung = {p: {"cand_prints": 0, "cand_sh": 0.0,
                    "invis_prints": 0, "invis_sh": 0.0,
                    "visible_own_prints": 0, "visible_own_sh": 0.0,
                    "windows_with_invis": set()} for p in RUNGS}
    n_days = 0
    n_windows = 0
    for day in DAYS:
        path = DATA / f"tape_{day}.jsonl.gz"
        if not path.exists():
            path = DATA / f"tape_{day}.jsonl"
        if not path.exists():
            continue
        n_days += 1
        opener = gzip.open if path.suffix == ".gz" else open
        wins = defaultdict(lambda: {"up": [], "down": []})
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
                    ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
                except (TypeError, ValueError):
                    continue
                close = ep + 300
                if not (close - REST_PRE_S <= ts <= close + REST_POST_S):
                    continue
                wins[ep][side].append((ts, px, sz, r.get("side", "")))
        n_windows += len(wins)
        for ep, sides in wins.items():
            for own, comp in (("up", "down"), ("down", "up")):
                comp_prints = sorted(sides[comp])
                own_prints = sorted(sides[own])
                own_ts = [o[0] for o in own_prints]
                for ts, q, sz, taker in comp_prints:
                    if taker != "BUY":
                        continue          # only complement BUY flow can mint-cross our bid
                    # mirror on our token: 1-q +- dp within +-dt
                    lo = bisect_left(own_ts, ts - PAIR_DT)
                    hi = bisect_right(own_ts, ts + PAIR_DT)
                    mirrored = any(abs(own_prints[j][1] - (1.0 - q)) <= PAIR_DP + 1e-12
                                   for j in range(lo, hi))
                    for p in RUNGS:
                        if q >= (1.0 - p) - 1e-9:
                            s = per_rung[p]
                            s["cand_prints"] += 1
                            s["cand_sh"] += sz
                            if not mirrored:
                                s["invis_prints"] += 1
                                s["invis_sh"] += sz
                                s["windows_with_invis"].add((ep, own))
                for _ts, px, sz, _taker in own_prints:
                    for p in RUNGS:
                        if px <= p + 1e-9:
                            per_rung[p]["visible_own_prints"] += 1
                            per_rung[p]["visible_own_sh"] += sz
        print(f"{day}: {len(wins)} windows scanned", flush=True)

    out = {"rest_span": f"[close-{REST_PRE_S}s, close+{REST_POST_S}s]",
           "pair_dt_s": PAIR_DT, "pair_dp": PAIR_DP,
           "n_days": n_days, "n_window_sides": 2 * n_windows, "rungs": {}}
    for p, s in per_rung.items():
        invis_sh, vis_sh = s["invis_sh"], s["visible_own_sh"]
        out["rungs"][f"{p:.2f}"] = {
            "cand_prints": s["cand_prints"], "cand_sh": round(s["cand_sh"], 1),
            "invis_prints": s["invis_prints"], "invis_sh": round(invis_sh, 1),
            "invis_prints_per_day": round(s["invis_prints"] / max(n_days, 1), 1),
            "window_sides_with_invis": len(s["windows_with_invis"]),
            "visible_own_prints": s["visible_own_prints"],
            "visible_own_sh": round(vis_sh, 1),
            "undercount_share_of_flow": round(invis_sh / max(invis_sh + vis_sh, 1e-9), 4),
        }
    (DATA / "h3_invisible.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out["rungs"], indent=1))


if __name__ == "__main__":
    main()
