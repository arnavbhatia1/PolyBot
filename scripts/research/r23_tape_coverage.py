"""Tape coverage behind the R2/R3 fill counts.

A rung that never fills because the tape has no prints is a data hole, not a
market fact. Per UTC day: corpus windows with >= 1 print on either token.
Per run (from r23_results_v0.json): armed windows whose rested token has zero
prints anywhere in the window / zero prints inside the resting interval.
Writes data/vps-0821/r23_tape_coverage.json.
"""
import json
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ws2_ladder_replay as ws2  # noqa: E402

OUT = ws2.DATA / "vps-0821"
RUNS = ("refit_n1.0_k25", "refit_n1.0_k20", "refit_n1.0_k15",
        "refit_n0.75_k25", "refit_n0.5_k25", "frozen_n1.0_k25")


def n_prints(pr, a, b):
    return bisect_right(pr, (b, float("inf"), float("inf"))) - bisect_left(pr, (a, -1.0, -1.0))


def main():
    R = json.load(open(OUT / "r23_results_v0.json"))
    c = ws2.load_corpus()
    by_ep = {w["ep"]: w for w in c["wins"]}
    day = {}
    for w in c["wins"]:
        d = datetime.fromtimestamp(w["ep"], tz=timezone.utc).strftime("%m-%d")
        s = day.setdefault(d, dict(windows=0, with_prints=0, prints=0))
        s["windows"] += 1
        n = sum(n_prints(c["prints"].get(t, []), w["ep"], w["ep"] + 400)
                for t in (w["token_up"], w["token_down"]))
        s["prints"] += n
        s["with_prints"] += 1 if n else 0
    out = dict(utc_day=day, runs={})
    for name in RUNS:
        rows = R[name]
        z_win = z_rest = 0
        for r in rows:
            w = by_ep[r["ep"]]
            tok = w["token_up"] if r["side"] == "Up" else w["token_down"]
            pr = c["prints"].get(tok, [])
            close = r["ep"] + 300
            place_t = close - r["place_k"]
            if n_prints(pr, r["ep"], close + 100) == 0:
                z_win += 1
            # resting interval end is not in the row; use the close+hold upper bound
            if n_prints(pr, place_t, close + ws2.POST_CLOSE_HOLD) == 0:
                z_rest += 1
        out["runs"][name] = dict(arms=len(rows), zero_prints_window=z_win,
                                 zero_prints_from_place=z_rest)
        print(name, out["runs"][name])
    print(json.dumps(day, indent=0))
    json.dump(out, open(OUT / "r23_tape_coverage.json", "w"), indent=1)


if __name__ == "__main__":
    main()
