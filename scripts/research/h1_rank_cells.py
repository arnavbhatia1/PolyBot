"""H1 pass 2: aggregate cell stats into maker-$/day tables, rank pockets,
run the half-split + shuffled-outcome controls.

Cell atom = (k_band, price_band, taker_side, token_won). Maker $ per entry:
BUY print -> maker sold at p: p*s - v*s ; SELL print -> maker bought: v*s - p*s.
Shuffle control: resolved_up permuted across windows, 500 draws, vectorized.

Output: data/vps-0821/h1_rank_results.json (tables + per-cell control stats).
"""
import json
import pickle
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SP = Path(__file__).parent
DATA = SP / "data" / "vps-0821"
K_BANDS = ["pre", "k300-60", "k60-25", "k25-6", "k6-0",
           "post0-30", "post30-150", "post150+"]
KB_ID = {k: i for i, k in enumerate(K_BANDS)}
SIDES = ["BUY", "SELL"]
HALF_SPLIT = 1787011200  # 2026-08-18 00:00 UTC
N_PERM = 500


def day_of(wts: int) -> str:
    return datetime.fromtimestamp(wts, tz=timezone.utc).strftime("%m-%d")


def main():
    with open(DATA / "h1_cellstats.pkl", "rb") as f:
        st = pickle.load(f)
    cells, labels = st["cells"], st["labels"]

    wins = sorted(cells.keys())
    widx = {w: i for i, w in enumerate(wins)}
    ru = np.array([labels[w] for w in wins], dtype=np.int8)
    half2 = np.array([w >= HALF_SPLIT for w in wins])
    days = [day_of(w) for w in wins]
    day_list = sorted(set(days))
    day_id = {d: i for i, d in enumerate(day_list)}
    wday = np.array([day_id[d] for d in days], dtype=np.int16)

    # effective trading-days per scope: labeled+taped windows / 288
    eff_days = {"era": len(wins) / 288,
                "h1": int((~half2).sum()) / 288,
                "h2": int(half2.sum()) / 288}
    eff_day_by_day = {d: sum(1 for x in days if x == d) / 288 for d in day_list}

    # flatten entries
    n = sum(len(d) for d in cells.values())
    e_w = np.empty(n, dtype=np.int32)
    e_kb = np.empty(n, dtype=np.int8)
    e_pb = np.empty(n, dtype=np.int8)
    e_side = np.empty(n, dtype=np.int8)   # 0 BUY, 1 SELL
    e_up = np.empty(n, dtype=np.int8)
    e_s = np.empty(n, dtype=np.float64)
    e_ps = np.empty(n, dtype=np.float64)
    e_n = np.empty(n, dtype=np.int64)
    i = 0
    for w, d in cells.items():
        for (kb, pb, side, is_up), (s, ps, cnt) in d.items():
            e_w[i] = widx[w]
            e_kb[i] = KB_ID[kb]
            e_pb[i] = pb
            e_side[i] = 0 if side == "BUY" else 1
            e_up[i] = is_up
            e_s[i] = s
            e_ps[i] = ps
            e_n[i] = cnt
            i += 1

    n_cells = len(K_BANDS) * 10 * 2 * 2   # kb * pb * side * won

    def cell_ids(v):
        return ((e_kb.astype(np.int32) * 10 + e_pb) * 2 + e_side) * 2 + v

    def maker_usd(v):
        return np.where(e_side == 0, e_ps - v * e_s, v * e_s - e_ps)

    def agg(mask, v):
        m = maker_usd(v)
        cid = cell_ids(v)
        usd = np.bincount(cid[mask], weights=m[mask], minlength=n_cells)
        vol = np.bincount(cid[mask], weights=e_s[mask], minlength=n_cells)
        npr = np.bincount(cid[mask], weights=e_n[mask], minlength=n_cells)
        return usd, vol, npr

    v_real = (e_up == ru[e_w]).astype(np.int8)
    all_m = np.ones(n, dtype=bool)
    usd_era, vol_era, npr_era = agg(all_m, v_real)
    usd_h1, _, _ = agg(~half2[e_w], v_real)
    usd_h2, _, _ = agg(half2[e_w], v_real)

    # per-day per-cell (for kill-bar style trailing reads if needed)
    m_real = maker_usd(v_real)
    cid_real = cell_ids(v_real)
    day_cell = np.zeros((len(day_list), n_cells))
    for di in range(len(day_list)):
        mask = wday[e_w] == di
        day_cell[di] = np.bincount(cid_real[mask], weights=m_real[mask],
                                   minlength=n_cells)

    # shuffled-outcome control: permute resolved_up across windows
    rng = np.random.default_rng(20260821)
    perm_usd = np.zeros((N_PERM, n_cells))
    for p in range(N_PERM):
        rp = rng.permutation(ru)
        vp = (e_up == rp[e_w]).astype(np.int8)
        mp = maker_usd(vp)
        cp = cell_ids(vp)
        perm_usd[p] = np.bincount(cp, weights=mp, minlength=n_cells)
    null_mean = perm_usd.mean(axis=0)
    null_sd = perm_usd.std(axis=0)

    def cell_name(cid):
        v = cid % 2
        side = (cid // 2) % 2
        pb = (cid // 4) % 10
        kb = cid // 40
        return (K_BANDS[kb], f"{pb/10:.1f}-{(pb+1)/10:.1f}",
                SIDES[side], "winner" if v else "loser")

    rows = []
    for cid in range(n_cells):
        if npr_era[cid] == 0:
            continue
        kb, pb, side, won = cell_name(cid)
        rows.append({
            "k_band": kb, "p_band": pb, "taker_side": side, "token": won,
            "maker_pos": "resting_ask" if side == "BUY" else "resting_bid",
            "usd_era": usd_era[cid], "usd_day": usd_era[cid] / eff_days["era"],
            "usd_day_h1": usd_h1[cid] / eff_days["h1"],
            "usd_day_h2": usd_h2[cid] / eff_days["h2"],
            "vol_sh": vol_era[cid], "n_prints": int(npr_era[cid]),
            "null_mean_day": null_mean[cid] / eff_days["era"],
            "null_sd_day": null_sd[cid] / eff_days["era"],
            "z_vs_null": ((usd_era[cid] - null_mean[cid]) / null_sd[cid])
                         if null_sd[cid] > 0 else 0.0,
            "day_usd": {day_list[di]: day_cell[di][cid] for di in range(len(day_list))
                        if day_cell[di][cid] != 0.0},
        })
    rows.sort(key=lambda r: -abs(r["usd_day"]))

    # coverage: how far past close / before open does the tape actually reach
    span = st["win_span"]
    post_reach = np.array([span[w][1] - (w + 300) for w in wins if w in span])
    pre_reach = np.array([w - span[w][0] for w in wins if w in span])
    coverage = {
        "eff_days": eff_days,
        "eff_day_by_day": eff_day_by_day,
        "post_reach_p10_50_90": np.percentile(post_reach, [10, 50, 90]).tolist(),
        "pre_reach_p10_50_90": np.percentile(pre_reach, [10, 50, 90]).tolist(),
        "frac_windows_post150": float((post_reach >= 150).mean()),
        "n_tape_gaps_gt120s": len(st["gaps"]),
        "gaps": st["gaps"][:40],
        "unmapped_prints": st["unmapped"],
        "unmapped_usd": st["unmapped_usd"],
        "fee_hist": st["fee_hist"],
    }

    # headline: total taker->maker transfer per scope
    headline = {
        "era_maker_usd": float(usd_era.sum()),
        "era_maker_usd_day": float(usd_era.sum() / eff_days["era"]),
        "h1_usd_day": float(usd_h1.sum() / eff_days["h1"]),
        "h2_usd_day": float(usd_h2.sum() / eff_days["h2"]),
        "era_volume_sh": float(vol_era.sum()),
        "era_notional_usd": float(e_ps.sum()),
        "n_prints": int(npr_era.sum()),
        "n_windows": len(wins),
    }

    out = {"headline": headline, "coverage": coverage, "cells": rows,
           "day_list": day_list, "n_perm": N_PERM}
    with open(DATA / "h1_rank_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=float)

    print(f"headline: makers net ${headline['era_maker_usd_day']:+,.0f}/day "
          f"(h1 {headline['h1_usd_day']:+,.0f}, h2 {headline['h2_usd_day']:+,.0f}) "
          f"on ${headline['era_notional_usd']:,.0f} era notional")
    print(f"post-close tape reach p50 {coverage['post_reach_p10_50_90'][1]:.0f}s, "
          f"{coverage['frac_windows_post150']*100:.0f}% of windows reach +150s")
    print("\ntop 25 cells by |maker $/day|:")
    hdr = f"{'k_band':>10} {'price':>8} {'taker':>5} {'token':>6} {'pos':>11} " \
          f"{'$/day':>9} {'h1':>8} {'h2':>8} {'null':>8} {'z':>6} {'sh/era':>10} {'n':>7}"
    print(hdr)
    for r in rows[:25]:
        print(f"{r['k_band']:>10} {r['p_band']:>8} {r['taker_side']:>5} "
              f"{r['token']:>6} {r['maker_pos']:>11} {r['usd_day']:>9,.0f} "
              f"{r['usd_day_h1']:>8,.0f} {r['usd_day_h2']:>8,.0f} "
              f"{r['null_mean_day']:>8,.0f} {r['z_vs_null']:>6.1f} "
              f"{r['vol_sh']:>10,.0f} {r['n_prints']:>7}")


if __name__ == "__main__":
    main()
