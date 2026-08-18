"""Where does deep winner-side supply print, relative to what the ladder can reach?

For every 60s-rule window: bucket winner-side prints <= 0.80 by time
(k>25 pre-close / 25>=k>6 / 6>=k>0 / post-close) and by window class
(armed / never-armed). The [k>25] bucket is structurally unreachable under
maker_k_place_max=25 — the 30s-era constant.
"""
import gzip
import json
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data"
REC = Path(__file__).resolve().parents[2] / "polybot" / "memory" / "recordings"
RULE_TS = 1786665600
import importlib.util
spec = importlib.util.spec_from_file_location("lr", SP / "ws2_ladder_replay.py")
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)   # reuse margin/proj_at/etc (main() not called)


def main():
    wins = []
    with gzip.open(DATA / "win_streams.jsonl.gz", "rt") as f:
        for line in f:
            wd = json.loads(line)
            if wd["ep"] >= RULE_TS:
                wins.append(wd)
    kl_ts, kl_px = lr.load_klines()
    lags = sorted(rx - ts for w in wins for rx, ts, _p in w["bz"] if rx and ts)
    bz_lag = lags[len(lags) // 2] if lags else 0.45

    # arming scan per window (same as replay) + first-clear time at ANY k<=58
    info = {}
    for wd in wins:
        ep = wd["ep"]
        close = ep + 300
        t0 = close - lr.HORIZON
        strike = wd["strike"]
        if not strike or not wd["final"]:
            continue
        l = sorted(wd["l"])
        l_rx = [(rx, p) for rx, _ts, p in l]
        trecs = sorted(wd.get("t") or [])
        bz = wd["bz"]
        if not bz and kl_ts:
            i0 = bisect_right(kl_ts, ep + 195)
            i1 = bisect_right(kl_ts, ep + 306)
            bz = [(S + 1 + bz_lag, S + 1.0, px)
                  for S, px in zip(kl_ts[i0:i1], kl_px[i0:i1])]
        placed = clear_any = None
        side_any = None
        for rx, _p in l_rx:
            k = close - rx
            if k > 58.0 or k < 1.0:
                continue
            if lr.twap_frozen_at(trecs, l_rx, rx):
                continue
            pr = lr.proj_at(l, l_rx, bz, rx, t0)
            if pr is None:
                continue
            disp = pr - strike
            if abs(disp) >= lr.NEED * lr.margin(k):
                if clear_any is None:
                    clear_any = rx
                    side_any = "Up" if disp >= 0 else "Down"
                if placed is None and lr.K_PLACE[0] <= k <= lr.K_PLACE[1]:
                    placed = rx
                    break
        winner = "Up" if wd["up"] else "Down"
        info[ep] = dict(placed=placed, clear_any=clear_any, side_any=side_any,
                        winner=winner,
                        wtok=wd["token_up"] if wd["up"] else wd["token_down"])

    tokmap = {v["wtok"]: ep for ep, v in info.items()}
    buckets = {"k>25": 0.0, "25>=k>6": 0.0, "6>=k>0": 0.0, "post": 0.0}
    armed_buckets = {k: 0.0 for k in buckets}
    clearable = {k: 0.0 for k in buckets}   # after first any-k clear, right side
    nwin = {k: set() for k in buckets}
    for day in ("2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"):
        p = REC / f"tape_{day}.jsonl.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt") as f:
            for line in f:
                r = json.loads(line)
                ep = tokmap.get(r["token"])
                if ep is None:
                    continue
                try:
                    ts, px, sz = float(r["ts"]), float(r["price"]), float(r["size"])
                except (TypeError, ValueError):
                    continue
                close = ep + 300
                if px > 0.80 + 1e-9 or not (close - 60 <= ts <= close + 60):
                    continue
                k = close - ts
                b = ("post" if k <= 0 else "6>=k>0" if k <= 6
                     else "25>=k>6" if k <= 25 else "k>25")
                buckets[b] += sz
                nwin[b].add(ep)
                v = info[ep]
                if v["placed"] is not None and ts >= v["placed"]:
                    armed_buckets[b] += sz
                if (v["clear_any"] is not None and ts >= v["clear_any"]
                        and v["side_any"] == v["winner"]):
                    clearable[b] += sz

    print("winner-side volume <= 0.80 by time bucket (60s-rule days):")
    print(f"{'bucket':10s} {'total sh':>10s} {'n_win':>6s} {'after k25-placement':>20s} {'after ANY-k clear':>18s}")
    for b in ("k>25", "25>=k>6", "6>=k>0", "post"):
        print(f"{b:10s} {buckets[b]:10.0f} {len(nwin[b]):6d} {armed_buckets[b]:20.0f} {clearable[b]:18.0f}")


if __name__ == "__main__":
    main()
