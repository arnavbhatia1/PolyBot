"""H2 primary: N+1 opening mispricing vs the known incoming strike.

For each labeled 60s-era window W, at delta in {2,5,10,20,30}s after open:
    d = chainlink_price(open+delta) - strike(W)   [strike = served price_to_beat]
Calibrate P(resolved_up | signed d bucket, delta) on alternating ET days
(FIT half), score the OTHER half (SCORE): buy the d-favored side at the
recorded ask, one bet per window, taker fee = 0.07 * p * (1-p) per share.

Books are 1Hz window_paths snapshots (NOT event-true) — stated in the report.
None rows are excluded, never zero-filled.

Usage: python scripts/research/h2_open_mispricing.py
Writes: scripts/research/data/vps-0821/h2_primary_results.json
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data" / "vps-0821"
ERA_TS = 1786665600           # 2026-08-14 00:00 UTC — 60s-rule era start
ET_OFFSET = 4 * 3600          # August: ET = UTC-4
DELTAS = [2, 5, 10, 20, 30]
ROW_TOL = 0.75                # accept the 1Hz row within +/-0.75s of open+delta
CL_MAX_AGE = 3.0              # spot older than 3s cannot define d (projection rule)
FEE_RATE = 0.07
MIN_FOK_USD = 5.0
ABS_EDGES = [0.0, 2.0, 5.0, 10.0, 20.0, 50.0, float("inf")]  # |d| buckets
EDGE_EDGES = [-float("inf"), -0.05, 0.0, 0.05, 0.10, 0.15, float("inf")]
STAKE_CAP = 50.0              # $400 bankroll / 8 — no compounding


def et_day(ts: float) -> int:
    return int((ts - ET_OFFSET) // 86400)


def abs_bucket(d: float) -> int:
    a = abs(d)
    for i in range(len(ABS_EDGES) - 1):
        if ABS_EDGES[i] <= a < ABS_EDGES[i + 1]:
            return i
    return len(ABS_EDGES) - 2


def signed_bucket(d: float) -> int:
    """Signed d bucket id: 0..5 negative (|d| desc), 6..11 positive (|d| asc).
    d == 0 goes to the smallest positive bucket (tie -> Up)."""
    b = abs_bucket(d)
    return (5 - b) if d < 0 else (6 + b)


def edge_bucket(e: float) -> int:
    for i in range(len(EDGE_EDGES) - 1):
        if EDGE_EDGES[i] <= e < EDGE_EDGES[i + 1]:
            return i
    return len(EDGE_EDGES) - 2


def fee_per_share(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def load_labels() -> dict[int, tuple[int, float, float]]:
    """open_ts -> (resolved_up, final_price, price_to_beat) for era windows."""
    con = sqlite3.connect(f"file:{DATA / 'polybot_paper_0821.db'}?mode=ro", uri=True)
    out: dict[int, tuple[int, float, float]] = {}
    for wid, up, fp, ptb in con.execute(
            "SELECT window_id, resolved_up, final_price, price_to_beat FROM window_labels"):
        ts = int(wid.rsplit("-", 1)[1])
        if ts >= ERA_TS:
            out[ts] = (up, fp, ptb)
    con.close()
    # chain-invariant fallback: missing ptb <- previous window's final
    for ts, (up, fp, ptb) in list(out.items()):
        if ptb is None and (ts - 300) in out and out[ts - 300][1] is not None:
            out[ts] = (up, fp, out[ts - 300][1])
    return out


def load_snapshots() -> dict[int, dict[int, dict]]:
    """open_ts -> delta -> row dict for the row nearest open+delta (within tol)."""
    con = sqlite3.connect(f"file:{DATA / 'window_paths_60s.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    best: dict[int, dict[int, tuple[float, dict]]] = defaultdict(dict)
    q = ("SELECT window_id, elapsed_s, chainlink_price, chainlink_age_s, "
         "ask_up, ask_sz_up, ask_down, ask_sz_down, bid_up, bid_down "
         "FROM window_paths WHERE elapsed_s <= 31.5")
    for r in con.execute(q):
        ts = int(r["window_id"].rsplit("-", 1)[1])
        for dl in DELTAS:
            gap = abs(r["elapsed_s"] - dl)
            if gap <= ROW_TOL:
                cur = best[ts].get(dl)
                if cur is None or gap < cur[0]:
                    best[ts][dl] = (gap, dict(r))
    con.close()
    return {ts: {dl: v[1] for dl, v in per.items()} for ts, per in best.items()}


def main() -> None:
    labels = load_labels()
    snaps = load_snapshots()

    # Build per-(window, delta) observations
    obs: dict[int, list[dict]] = {dl: [] for dl in DELTAS}
    excl = defaultdict(int)
    for ts, (up, fp, ptb) in sorted(labels.items()):
        if ptb is None:
            excl["no_strike"] += 1
            continue
        per = snaps.get(ts)
        if not per:
            excl["no_paths_rows"] += 1
            continue
        for dl in DELTAS:
            r = per.get(dl)
            if r is None:
                excl[f"d{dl}_no_row"] += 1
                continue
            cl, age = r["chainlink_price"], r["chainlink_age_s"]
            if cl is None or age is None:
                excl[f"d{dl}_cl_none"] += 1
                continue
            if age > CL_MAX_AGE:
                excl[f"d{dl}_cl_stale"] += 1
                continue
            d = cl - ptb
            fav_up = d >= 0  # tie -> Up
            ask = r["ask_up"] if fav_up else r["ask_down"]
            asz = r["ask_sz_up"] if fav_up else r["ask_sz_down"]
            anti_ask = r["ask_down"] if fav_up else r["ask_up"]
            anti_asz = r["ask_sz_down"] if fav_up else r["ask_sz_up"]
            obs[dl].append({
                "ts": ts, "day": et_day(ts), "d": d, "fav_up": fav_up,
                "won": bool(up) == fav_up,
                "ask": ask, "asz": asz,
                "anti_ask": anti_ask, "anti_asz": anti_asz,
                "anti_won": bool(up) != fav_up,
            })

    days = sorted({o["day"] for dl in DELTAS for o in obs[dl]})
    fit_days = set(days[0::2])
    score_days = set(days[1::2])

    results = {"days": days,
               "fit_days": sorted(fit_days), "score_days": sorted(score_days),
               "exclusions": dict(excl), "per_delta": {}}

    for dl in DELTAS:
        rows = obs[dl]
        fit = [o for o in rows if o["day"] in fit_days]
        score = [o for o in rows if o["day"] in score_days]

        # --- calibration on FIT: P(resolved_up | signed d bucket) ---
        cal_n = [0] * 12
        cal_up = [0] * 12
        for o in fit:
            b = signed_bucket(o["d"])
            cal_n[b] += 1
            resolved_up = o["won"] if o["fav_up"] else (not o["won"])
            cal_up[b] += 1 if resolved_up else 0
        cal_p = [cal_up[i] / cal_n[i] if cal_n[i] else None for i in range(12)]

        def p_cal_favored(o) -> float | None:
            p = cal_p[signed_bucket(o["d"])]
            if p is None:
                return None
            return p if o["fav_up"] else 1.0 - p

        # --- score the OTHER half ---
        def executable(ask, asz):
            return (ask is not None and asz is not None
                    and 0.0 < ask < 1.0 and ask * asz >= MIN_FOK_USD)

        exe = [o for o in score if executable(o["ask"], o["asz"])]
        n_ask_missing = sum(1 for o in score if o["ask"] is None or o["asz"] is None)
        n_too_thin = sum(1 for o in score
                         if o["ask"] is not None and o["asz"] is not None
                         and not executable(o["ask"], o["asz"]))

        def realized(o, ask):
            gross = (1.0 - ask) if o["won"] else -ask
            return gross - fee_per_share(ask)

        per_abs = defaultdict(lambda: {"n": 0, "ew_sum": 0.0, "wins": 0,
                                       "model_ew_sum": 0.0, "model_n": 0})
        per_edge = defaultdict(lambda: {"n": 0, "ew_sum": 0.0, "wins": 0})
        per_day = defaultdict(lambda: {"n": 0, "ew_sum": 0.0, "usd_capped": 0.0,
                                       "usd_flat5": 0.0, "fired_n": 0})
        anti_stats = {"n": 0, "ew_sum": 0.0, "wins": 0}
        ew_all, win_all = 0.0, 0

        for o in exe:
            ask = o["ask"]
            net = realized(o, ask)
            ew_all += net
            win_all += 1 if o["won"] else 0
            ab = abs_bucket(o["d"])
            per_abs[ab]["n"] += 1
            per_abs[ab]["ew_sum"] += net
            per_abs[ab]["wins"] += 1 if o["won"] else 0
            pc = p_cal_favored(o)
            if pc is not None:
                medge = pc - ask - fee_per_share(ask)
                per_abs[ab]["model_ew_sum"] += medge
                per_abs[ab]["model_n"] += 1
                eb = edge_bucket(medge)
                per_edge[eb]["n"] += 1
                per_edge[eb]["ew_sum"] += net
                per_edge[eb]["wins"] += 1 if o["won"] else 0
                # $/day: fire only when the held-out model says edge > 0
                if medge > 0:
                    touch = ask * o["asz"]
                    stake = min(STAKE_CAP, touch)
                    shares_c = stake / ask
                    shares_f = MIN_FOK_USD / ask
                    per_day[o["day"]]["usd_capped"] += net * shares_c
                    per_day[o["day"]]["usd_flat5"] += net * shares_f
                    per_day[o["day"]]["fired_n"] += 1
            per_day[o["day"]]["n"] += 1
            per_day[o["day"]]["ew_sum"] += net
            # anti-side control (its own executability)
            if executable(o["anti_ask"], o["anti_asz"]):
                anet = ((1.0 - o["anti_ask"]) if o["anti_won"] else -o["anti_ask"]) \
                    - fee_per_share(o["anti_ask"])
                anti_stats["n"] += 1
                anti_stats["ew_sum"] += anet
                anti_stats["wins"] += 1 if o["anti_won"] else 0

        n = len(exe)
        mean_ew = ew_all / n if n else None

        # per-day table + $/day
        day_rows = {}
        for dday, v in sorted(per_day.items()):
            day_rows[dday] = {
                "n": v["n"],
                "ew_cents": round(100 * v["ew_sum"] / v["n"], 2) if v["n"] else None,
                "usd_capped": round(v["usd_capped"], 2),
                "usd_flat5": round(v["usd_flat5"], 2),
                "fired_n": v["fired_n"],
            }
        n_days = len(day_rows)
        usd_per_day_capped = (sum(v["usd_capped"] for v in day_rows.values()) / n_days
                              if n_days else None)
        usd_per_day_flat5 = (sum(v["usd_flat5"] for v in day_rows.values()) / n_days
                             if n_days else None)

        # monotonicity across model-edge buckets (needs an edge<0 cell with n>0)
        eb_rows = []
        for i in range(len(EDGE_EDGES) - 1):
            v = per_edge.get(i)
            eb_rows.append({
                "lo": EDGE_EDGES[i], "hi": EDGE_EDGES[i + 1],
                "n": v["n"] if v else 0,
                "net_cents": round(100 * v["ew_sum"] / v["n"], 2) if v and v["n"] else None,
                "win_rate": round(v["wins"] / v["n"], 3) if v and v["n"] else None,
            })
        seq = [r["net_cents"] for r in eb_rows if r["net_cents"] is not None and r["n"] >= 10]
        monotone = (len(seq) >= 3 and all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)))
        has_neg_cell = any(r["n"] >= 10 and r["hi"] <= 0 for r in eb_rows)

        results["per_delta"][dl] = {
            "n_score_windows": len(score),
            "n_executable": n,
            "n_ask_missing": n_ask_missing,
            "n_too_thin_for_5usd": n_too_thin,
            "mean_ew_cents": round(100 * mean_ew, 2) if mean_ew is not None else None,
            "win_rate": round(win_all / n, 3) if n else None,
            "calibration": {"n": cal_n, "p_up": [round(p, 3) if p is not None else None
                                                 for p in cal_p]},
            "per_abs_bucket": {
                f"[{ABS_EDGES[i]},{ABS_EDGES[i+1]})": {
                    "n": per_abs[i]["n"],
                    "ew_cents": round(100 * per_abs[i]["ew_sum"] / per_abs[i]["n"], 2)
                    if per_abs[i]["n"] else None,
                    "win_rate": round(per_abs[i]["wins"] / per_abs[i]["n"], 3)
                    if per_abs[i]["n"] else None,
                    "model_ew_cents": round(100 * per_abs[i]["model_ew_sum"]
                                            / per_abs[i]["model_n"], 2)
                    if per_abs[i]["model_n"] else None,
                } for i in range(len(ABS_EDGES) - 1) if per_abs[i]["n"]},
            "edge_buckets": eb_rows,
            "monotone": monotone,
            "has_edge_neg_control": has_neg_cell,
            "anti_side": {
                "n": anti_stats["n"],
                "ew_cents": round(100 * anti_stats["ew_sum"] / anti_stats["n"], 2)
                if anti_stats["n"] else None,
                "win_rate": round(anti_stats["wins"] / anti_stats["n"], 3)
                if anti_stats["n"] else None,
            },
            "per_day": day_rows,
            "usd_per_day_capped50": round(usd_per_day_capped, 2)
            if usd_per_day_capped is not None else None,
            "usd_per_day_flat5": round(usd_per_day_flat5, 2)
            if usd_per_day_flat5 is not None else None,
            "bar": {
                "n_ge_300": n >= 300,
                "ew_ge_5c": mean_ew is not None and mean_ew >= 0.05,
                "monotone_with_neg_control": monotone and has_neg_cell,
                "anti_le_0": anti_stats["n"] > 0 and anti_stats["ew_sum"] <= 0,
                "usd10_per_day": usd_per_day_capped is not None
                and usd_per_day_capped >= 10.0,
            },
        }

    out = DATA / "h2_primary_results.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"wrote {out}")
    for dl in DELTAS:
        r = results["per_delta"][dl]
        print(f"delta={dl:>2}s n_exe={r['n_executable']:>4} "
              f"EW={r['mean_ew_cents']}c win={r['win_rate']} "
              f"anti={r['anti_side']['ew_cents']}c "
              f"$?/day={r['usd_per_day_capped50']} bar={r['bar']}")


if __name__ == "__main__":
    main()
