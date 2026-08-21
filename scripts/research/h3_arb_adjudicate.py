"""H3 (a) adjudication: turn the per-day micro event JSONs into a verdict.

Splits event-true violations at the close (k > 0 in-window vs k <= 0
post-close — post-close sub-$1 is the known certainty surface, not complement
arb), gives duration distributions vs the executable floor (two FOK legs,
POST RTT p50 ~410ms), and depth-verifies in-window post-fee survivors against
the nearest window_paths sample (+-0.6s; ask_sz_* touch sizes, verified
semantics). Output: h3_arb_verdict.json.
"""
import json
import sqlite3
from pathlib import Path

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
DAYS = [f"2026-08-{d:02d}" for d in range(14, 22)]
MIN_LEG_USD = 2.0
FEE = 0.07


def pct(v, p):
    v = sorted(v)
    return round(v[min(int(p * len(v)), len(v) - 1)], 3) if v else None


def main():
    conn = sqlite3.connect(f"file:{DATA / 'window_paths_60s.db'}?mode=ro", uri=True)

    def depth_at(window_ep: int, ts: float):
        cur = conn.execute(
            "SELECT ts, ask_up, ask_down, ask_sz_up, ask_sz_down FROM window_paths "
            "WHERE window_id = ? AND ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
            (f"btc-updown-5m-{window_ep}", ts - 0.6, ts + 0.6, ts))
        return cur.fetchone()

    days = {}
    all_pre_in, all_pre_post = [], []      # prefee events, in-window / post-close
    all_post_in, all_post_post = [], []    # postfee events
    exposure = {"+0s": 0.0, "+30s": 0.0, "-30s": 0.0}
    shift_counts = {s: {"prefee": 0, "postfee": 0} for s in exposure}
    n_days = 0
    for day in DAYS:
        p = DATA / f"h3_arb_micro_{day}.json"
        if not p.exists():
            continue
        n_days += 1
        r = json.loads(p.read_text())
        for s in exposure:
            exposure[s] += r["by_shift"][s]["exposure_s"]
            shift_counts[s]["prefee"] += r["by_shift"][s]["prefee_events"]
            shift_counts[s]["postfee"] += r["by_shift"][s]["postfee_events"]
        real = r["by_shift"]["+0s"]
        days[day] = {"prefee": real["prefee_events"], "postfee": real["postfee_events"],
                     "min_sum": real["min_sum"]}
        for e in real["prefee_list"]:
            (all_pre_in if e["k"] > 0 else all_pre_post).append(e)
        for e in real["postfee_list"]:
            (all_post_in if e["k"] > 0 else all_post_post).append(e)

    def summarize(evs, label):
        d = {"n": len(evs), "n_windows": len({e["window"] for e in evs}),
             "dur_p50": pct([e["dur_s"] for e in evs], 0.5),
             "dur_p90": pct([e["dur_s"] for e in evs], 0.9),
             "dur_max": pct([e["dur_s"] for e in evs], 1.0),
             "n_dur_ge_1s": sum(1 for e in evs if e["dur_s"] >= 1.0),
             "k_p50": pct([e["k"] for e in evs], 0.5)}
        if evs and "min_sum" in evs[0]:
            d["min_sum"] = min(e["min_sum"] for e in evs)
        return {label: d}

    out = {"n_days": n_days, "exposure_s": {k: round(v, 0) for k, v in exposure.items()},
           "shift_event_counts": shift_counts, "per_day": days}
    out.update(summarize(all_pre_in, "prefee_in_window"))
    out.update(summarize(all_pre_post, "prefee_post_close"))
    out.update(summarize(all_post_in, "postfee_in_window"))
    out.update(summarize(all_post_post, "postfee_post_close"))

    # depth-verify every in-window post-fee event against window_paths
    verified = []
    n_no_row = 0
    for e in sorted(all_post_in, key=lambda x: -x["dur_s"]):
        ep = int(e["window"])
        row = depth_at(ep, e["ts"])
        if row is None:
            n_no_row += 1
            continue
        _ts, au, ad, szu, szd = row
        ok = (au is not None and ad is not None and szu is not None and szd is not None
              and au * szu >= MIN_LEG_USD and ad * szd >= MIN_LEG_USD)
        v_at_row = (1 - au - ad - FEE * au * (1 - au) - FEE * ad * (1 - ad)
                    if au is not None and ad is not None else None)
        usd = (round(min(szu, szd) * max(v_at_row, 0), 2)
               if ok and v_at_row is not None else 0.0)
        verified.append({**e, "row_au": au, "row_ad": ad, "row_sz_up": szu,
                         "row_sz_dn": szd, "row_postfee_viol":
                         round(v_at_row, 4) if v_at_row is not None else None,
                         "deep_ok": ok, "usd_avail_at_row": usd})
    out["postfee_in_window_depth_check"] = {
        "n_checked": len(verified), "n_no_paths_row": n_no_row,
        "n_deep_ok": sum(1 for v in verified if v["deep_ok"]),
        "n_row_confirms_viol": sum(1 for v in verified
                                   if v["row_postfee_viol"] is not None
                                   and v["row_postfee_viol"] > 0),
        "usd_sum": round(sum(v["usd_avail_at_row"] for v in verified), 2),
        "top": verified[:25]}
    conn.close()
    (DATA / "h3_arb_verdict.json").write_text(json.dumps(out, indent=1))
    for k in ("prefee_in_window", "prefee_post_close",
              "postfee_in_window", "postfee_post_close"):
        print(k, json.dumps(out[k]))
    print("depth check:", json.dumps({k: v for k, v in
          out["postfee_in_window_depth_check"].items() if k != "top"}))


if __name__ == "__main__":
    main()
