"""H3 (a) pure complement arb: moments where buying BOTH asks costs < $1 net of
taker fees, at >=$2 executable per leg.

Pre-registered bar: viol = 1 - (ask_up + ask_down) - fee(au) - fee(ad) > 0,
fee(p) = 0.07*p*(1-p) per share/leg, both top-of-book sizes >= $2/leg;
bar = >=1 event/day AND >=$5/day at available depth. Control: re-run with the
down side time-shifted +-30s — a scan that still finds events on shifted books
is measuring staleness, not arbitrage.

Passes:
  1. sampled  — every synchronized window_paths row (whole window, 1Hz/5Hz),
                plus the +-30s shift control at the same cadence.
  2. micro    — event-true replay of joint ask BBO from micro_*.jsonl(.gz)
                (final 90s only; prices only, no sizes), plus shift control.
                Resumable: one JSON per day, existing days are skipped.

Column semantics verified against polybot/recording.py + an ordering audit:
ask_sz_* is the true touch size (WS books arrive price-ascending, levels[0]
is the best ASK but the WORST bid); bid-side size columns are never used here.
"""
import gzip
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
EPOCH = 1786665600          # 60s-rule era start (all local data is in-era)
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]
FEE = 0.07
MIN_LEG_USD = 2.0
SHIFTS = (0.0, 30.0, -30.0)


def fee(p: float) -> float:
    return FEE * p * (1.0 - p)


def viol_postfee(au: float, ad: float) -> float:
    return 1.0 - au - ad - fee(au) - fee(ad)


# ---------------------------------------------------------------- sampled ---

def sampled_scan() -> dict:
    conn = sqlite3.connect(f"file:{DATA / 'window_paths_60s.db'}?mode=ro", uri=True)
    cur = conn.execute(
        "SELECT window_id, ts, elapsed_s, ask_up, ask_down, ask_sz_up, ask_sz_down "
        "FROM window_paths WHERE ts >= ? ORDER BY window_id, ts", (EPOCH,))
    rows = cur.fetchall()
    conn.close()

    out = {"n_rows": len(rows), "n_rows_both_asks": 0,
           "prefee_lt1": [], "postfee_viol": [], "postfee_viol_deep": [],
           "min_prefee_sum": None,
           "shift": {f"{s:+.0f}s": {"n_pairs": 0, "prefee_lt1": 0,
                                    "postfee_viol": 0, "postfee_viol_deep": 0}
                     for s in SHIFTS if s}}
    by_win = defaultdict(list)
    for w, ts, el, au, ad, szu, szd in rows:
        if au is None or ad is None:
            continue
        out["n_rows_both_asks"] += 1
        s = au + ad
        if out["min_prefee_sum"] is None or s < out["min_prefee_sum"]:
            out["min_prefee_sum"] = round(s, 4)
        rec = None
        if s < 1.0 - 1e-9:
            v = viol_postfee(au, ad)
            deep = (szu is not None and szd is not None
                    and au * szu >= MIN_LEG_USD and ad * szd >= MIN_LEG_USD)
            avail = round(min(szu or 0.0, szd or 0.0) * max(v, 0.0), 2)
            rec = {"window": w, "ts": ts, "k": round(300 - el, 1),
                   "ask_up": au, "ask_down": ad, "prefee_viol": round(1 - s, 4),
                   "postfee_viol": round(v, 4), "deep_ok": deep, "usd_avail": avail}
            out["prefee_lt1"].append(rec)
            if v > 0:
                out["postfee_viol"].append(rec)
                if deep:
                    out["postfee_viol_deep"].append(rec)
        by_win[w].append((ts, au, ad, szu, szd))

    # cluster postfee violations into events (same window, gap <= 1.5s)
    events = []
    for r in out["postfee_viol"]:
        if events and events[-1]["window"] == r["window"] \
                and r["ts"] - events[-1]["ts_end"] <= 1.5:
            events[-1]["ts_end"] = r["ts"]
            events[-1]["usd_avail"] = max(events[-1]["usd_avail"], r["usd_avail"])
        else:
            events.append({"window": r["window"], "ts_start": r["ts"],
                           "ts_end": r["ts"], "k": r["k"],
                           "usd_avail": r["usd_avail"]})
    out["events"] = events

    # +-30s shift control at the sampled cadence: pair the up ask at ts with
    # the down ask at the nearest sample to ts+shift (same window, <=0.7s off).
    for w, seq in by_win.items():
        ts_list = [r[0] for r in seq]
        idx = {round(t, 1): i for i, t in enumerate(ts_list)}

        def nearest(t: float) -> int | None:
            for dt in (0.0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.5, -0.5, 0.7, -0.7):
                i = idx.get(round(t + dt, 1))
                if i is not None:
                    return i
            return None

        for shift in SHIFTS:
            if not shift:
                continue
            st = out["shift"][f"{shift:+.0f}s"]
            for ts, au, _ad, szu, _szd in seq:
                j = nearest(ts + shift)
                if j is None:
                    continue
                ad, szd = seq[j][2], seq[j][4]
                if au is None or ad is None:
                    continue
                st["n_pairs"] += 1
                if au + ad < 1.0 - 1e-9:
                    st["prefee_lt1"] += 1
                    if viol_postfee(au, ad) > 0:
                        st["postfee_viol"] += 1
                        if (szu is not None and szd is not None
                                and au * szu >= MIN_LEG_USD and ad * szd >= MIN_LEG_USD):
                            st["postfee_viol_deep"] += 1
    return out


# ------------------------------------------------------------------ micro ---

def load_token_map() -> dict:
    m = json.loads((DATA / "token_map.json").read_text())["map"]
    tok = {}
    for ep_s, d in m.items():
        ep = int(ep_s)
        tok[d["up"]] = (ep, 0)
        tok[d["down"]] = (ep, 1)
    return tok


def replay_window(ep: int, recs: list, shift: float) -> dict:
    """Event-true joint-ask replay; down-side timestamps shifted by `shift`.

    Returns violation intervals + exposure (seconds both sides initialized,
    clamped to the window's own record span so a shift can't fabricate time).
    """
    ups = [(ts, a) for ts, side, a in recs if side == 0]
    dns = [(ts + shift, a) for ts, side, a in recs if side == 1]
    merged = sorted(((ts, 0, a) for ts, a in ups),
                    key=lambda r: r[0])
    merged = sorted(merged + [(ts, 1, a) for ts, a in dns], key=lambda r: r[0])
    if not merged:
        return {"exposure_s": 0.0, "prefee": [], "postfee": []}
    t_end = max(ts for ts, _, _ in recs)  # unshifted span end
    au = ad = None
    exposure = 0.0
    pre_start = post_start = None
    pre_min = post_max = None
    pre_at = post_at = None
    pre, post = [], []
    last_ts = None
    for ts, side, a in merged:
        if ts > t_end:
            break
        if au is not None and ad is not None and last_ts is not None:
            exposure += ts - last_ts
            s = au + ad
            if pre_start is not None:
                pre_min = min(pre_min, s)
            if pre_start is not None and s < pre_min:
                pre_min, pre_at = s, (au, ad)
            if s < 1.0 - 1e-9 and pre_start is None:
                pre_start, pre_min, pre_at = last_ts, s, (au, ad)
            elif s >= 1.0 - 1e-9 and pre_start is not None:
                # the state evaluated over [last_ts, ts] is clean, so the
                # violation ended at last_ts (the update that fixed it)
                pre.append({"ts": pre_start, "dur_s": round(last_ts - pre_start, 3),
                            "min_sum": round(pre_min, 4), "au": pre_at[0], "ad": pre_at[1],
                            "k": round(ep + 300 - pre_start, 1)})
                pre_start = None
            v = viol_postfee(au, ad)
            if post_start is not None and v > post_max:
                post_max, post_at = v, (au, ad)
            if v > 0 and post_start is None:
                post_start, post_max, post_at = last_ts, v, (au, ad)
            elif v <= 0 and post_start is not None:
                post.append({"ts": post_start, "dur_s": round(last_ts - post_start, 3),
                             "max_viol": round(post_max, 4), "au": post_at[0], "ad": post_at[1],
                             "k": round(ep + 300 - post_start, 1)})
                post_start = None
        if side == 0:
            au = a
        else:
            ad = a
        last_ts = ts
    if pre_start is not None:
        pre.append({"ts": pre_start, "dur_s": round(t_end - pre_start, 3),
                    "min_sum": round(pre_min, 4), "au": pre_at[0], "ad": pre_at[1],
                    "k": round(ep + 300 - pre_start, 1), "open_at_end": 1})
    if post_start is not None:
        post.append({"ts": post_start, "dur_s": round(t_end - post_start, 3),
                     "max_viol": round(post_max, 4), "au": post_at[0], "ad": post_at[1],
                     "k": round(ep + 300 - post_start, 1), "open_at_end": 1})
    return {"exposure_s": round(exposure, 1), "prefee": pre, "postfee": post}


def micro_day(day: str, tok: dict) -> dict | None:
    path = DATA / f"micro_{day}.jsonl.gz"
    if not path.exists():
        path = DATA / f"micro_{day}.jsonl"
    if not path.exists():
        return None
    opener = gzip.open if path.suffix == ".gz" else open
    buf: dict[int, list] = defaultdict(list)
    res = {"day": day, "n_b": 0, "n_unknown_token": 0, "n_windows": 0}
    agg = {f"{s:+.0f}s": {"exposure_s": 0.0, "prefee_events": 0, "prefee_dur_s": 0.0,
                          "postfee_events": 0, "postfee_dur_s": 0.0,
                          "min_sum": None, "prefee_list": [], "postfee_list": []}
           for s in SHIFTS}

    def flush(ep: int):
        recs = buf.pop(ep)
        res["n_windows"] += 1
        for shift in SHIFTS:
            key = f"{shift:+.0f}s"
            r = replay_window(ep, recs, shift)
            a = agg[key]
            a["exposure_s"] += r["exposure_s"]
            a["prefee_events"] += len(r["prefee"])
            a["prefee_dur_s"] += sum(e["dur_s"] for e in r["prefee"])
            a["postfee_events"] += len(r["postfee"])
            a["postfee_dur_s"] += sum(e["dur_s"] for e in r["postfee"])
            for e in r["prefee"]:
                if a["min_sum"] is None or e["min_sum"] < a["min_sum"]:
                    a["min_sum"] = e["min_sum"]
            if not shift:   # full event detail for the real replay only
                for e in r["prefee"]:
                    if len(a["prefee_list"]) < 3000:
                        a["prefee_list"].append({"window": ep, **e})
                for e in r["postfee"]:
                    if len(a["postfee_list"]) < 3000:
                        a["postfee_list"].append({"window": ep, **e})

    last_ts = 0.0
    with opener(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if '"k": "b"' not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = tok.get(r["token"])
            if t is None:
                res["n_unknown_token"] += 1
                continue
            ep, side = t
            try:
                ask = float(r["ask"])
            except (TypeError, ValueError):
                continue
            res["n_b"] += 1
            ts = float(r["ts"])
            last_ts = max(last_ts, ts)
            buf[ep].append((ts, side, None if ask <= 0 else ask))
            if i % 200000 == 0:
                for e in [e for e in buf if e + 400 < last_ts]:
                    flush(e)
    for e in sorted(buf):
        flush(e)
    for a in agg.values():
        a["exposure_s"] = round(a["exposure_s"], 1)
        a["prefee_dur_s"] = round(a["prefee_dur_s"], 3)
        a["postfee_dur_s"] = round(a["postfee_dur_s"], 3)
    res["by_shift"] = agg
    return res


def main():
    out_sampled = DATA / "h3_arb_sampled.json"
    if not out_sampled.exists():
        print("sampled scan ...", flush=True)
        out_sampled.write_text(json.dumps(sampled_scan(), indent=1))
        print("sampled scan done", flush=True)
    else:
        print("sampled scan: cached", flush=True)

    tok = load_token_map()
    for day in DAYS:
        outp = DATA / f"h3_arb_micro_{day}.json"
        if outp.exists():
            print(f"{day}: cached", flush=True)
            continue
        r = micro_day(day, tok)
        if r is None:
            print(f"{day}: no micro file", flush=True)
            continue
        outp.write_text(json.dumps(r, indent=1))
        real = r["by_shift"]["+0s"]
        print(f"{day}: {r['n_b']} b-recs, {r['n_windows']} win, "
              f"real prefee={real['prefee_events']} postfee={real['postfee_events']} "
              f"min_sum={real['min_sum']}  "
              f"shift+30 prefee={r['by_shift']['+30s']['prefee_events']} "
              f"shift-30 prefee={r['by_shift']['-30s']['prefee_events']}", flush=True)


if __name__ == "__main__":
    main()
