"""Which official-stream instant equals the served final_price, per day?

Replays the strike rule (first t-report at/after boundary) against labels and,
for mismatches, searches offsets: t-report at close+n for n in -3..+12, plus
'last report strictly before close'. Also re-runs the chain invariant
final(ep) == strike(ep+300) from labels alone.
"""
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
EPS = 1e-9


def main():
    wins = []
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wins.append(json.loads(line))
    labels = {w["ep"]: w for w in wins}

    daystat = {}
    for wd in wins:
        ep = wd["ep"]
        close = ep + 300
        day = datetime.fromtimestamp(ep, timezone.utc).strftime("%m-%d")
        st = daystat.setdefault(day, {"n": 0, "exact": 0, "off": {}, "chain_n": 0,
                                      "chain_ok": 0, "prev_ok": 0, "nomatch": 0,
                                      "delta_first": []})
        trecs = sorted(wd.get("t") or [])          # (rx, ts, p)
        final = wd["final"]
        if not final or not trecs:
            continue
        # first report at/after close (the engine's capture rule)
        first_after = next(((rx, ts, p) for rx, ts, p in trecs if ts >= close), None)
        last_before = None
        for rx, ts, p in trecs:
            if ts < close:
                last_before = (rx, ts, p)
        st["n"] += 1
        if first_after and abs(first_after[2] - final) < EPS:
            st["exact"] += 1
        else:
            if first_after:
                st["delta_first"].append(final - first_after[2])
            # which offset matches?
            hit = None
            for rx, ts, p in trecs:
                if abs(p - final) < EPS:
                    hit = ts - close
                    break
            if hit is None and last_before and abs(last_before[2] - final) < EPS:
                hit = "last_before"
            if hit is None:
                st["nomatch"] += 1
            else:
                key = round(hit, 0) if isinstance(hit, float) else hit
                st["off"][key] = st["off"].get(key, 0) + 1
        nxt = labels.get(ep + 300)
        if nxt:
            st["chain_n"] += 1
            st["chain_ok"] += abs(final - nxt["strike"]) < EPS

    print("day    n    final==first-at/after-close   chain final==next strike   "
          "offsets-that-match | nomatch")
    for day in sorted(daystat):
        st = daystat[day]
        if st["n"] == 0:
            continue
        offs = " ".join(f"{k}:{v}" for k, v in sorted(st["off"].items(), key=str))
        print(f"{day}  {st['n']:4d}   {st['exact']:4d} ({st['exact'] / st['n']:.0%})"
              f"                  {st['chain_ok']}/{st['chain_n']}"
              f"                    {offs} | {st['nomatch']}")
        d = sorted(abs(x) for x in st["delta_first"])
        if d:
            print(f"       |final - first_at/after|: med={d[len(d) // 2]:.3f} "
                  f"p90={d[int(0.9 * len(d))]:.3f} max={d[-1]:.3f}")


if __name__ == "__main__":
    main()
