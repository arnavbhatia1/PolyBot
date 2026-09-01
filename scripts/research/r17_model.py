"""Info program Phase B: walk-forward models vs the book, B1/B2 verdicts.

Protocol and bars exactly as pre-registered in
docs/research/info_program_2026-09-01.md (frozen before this ran):
  B1: model OOS log-loss < book-implied log-loss at >=1 horizon,
      day-level block bootstrap p < 0.05.
  B2: taker sim at the staleness-shifted executable ask, fee 0.07*p*(1-p),
      one bet per window per horizon, >=5-share touch (NaN sizes pass —
      unmeasurable pre-08-21): EW >= +2c/sh on >=100 OOS trades at the
      engine's 4c edge floor; net c/sh monotone across edge buckets;
      the edge<0 control <= 0.
Models fixed: L2 logistic (C=1) and HistGradientBoosting with grid
{lr .05/.1} x {leaves 15/31} picked on the last train day, refit on full train.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

D = Path(__file__).parent / "data" / "vps-0831"
df = pd.read_parquet(D / "info_dataset.parquet")
FEATS = [c for c in df.columns if c not in
         ("ep", "k", "ts", "et_day", "label", "book_p", "ask_up", "ask_dn",
          "exec_ask_up", "exec_ask_dn", "ask_sz_up", "ask_sz_dn", "spot_px")]
print(f"{len(df)} rows, {df.ep.nunique()} windows, {len(FEATS)} features:")
print(" ", FEATS)
rng = np.random.default_rng(20260901)
report = {}

def fit_predict(tr, te):
    Xtr, ytr = tr[FEATS].to_numpy(), tr["label"].to_numpy()
    Xte = te[FEATS].to_numpy()
    days = sorted(tr["et_day"].unique())
    val_mask = tr["et_day"] == days[-1]
    preds = {}
    # logistic
    lo = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                       LogisticRegression(C=1.0, max_iter=2000))
    lo.fit(Xtr, ytr)
    preds["logit"] = lo.predict_proba(Xte)[:, 1]
    # GBT: grid picked on last train day, refit on all
    best, best_ll = None, np.inf
    if val_mask.sum() > 50 and (~val_mask).sum() > 200:
        for lr_ in (0.05, 0.1):
            for lv in (15, 31):
                m = HistGradientBoostingClassifier(
                    learning_rate=lr_, max_leaf_nodes=lv, max_iter=300,
                    early_stopping=False, random_state=0)
                m.fit(tr[~val_mask][FEATS], tr[~val_mask]["label"])
                ll = log_loss(tr[val_mask]["label"],
                              np.clip(m.predict_proba(tr[val_mask][FEATS])[:, 1], 1e-6, 1 - 1e-6))
                if ll < best_ll:
                    best_ll, best = ll, (lr_, lv)
    lr_, lv = best or (0.1, 31)
    gb = HistGradientBoostingClassifier(learning_rate=lr_, max_leaf_nodes=lv,
                                        max_iter=300, early_stopping=False,
                                        random_state=0)
    gb.fit(Xtr, ytr)
    preds["gbt"] = gb.predict_proba(Xte)[:, 1]
    return preds

for k in sorted(df["k"].unique()):
    dk = df[df["k"] == k].sort_values("ts").reset_index(drop=True)
    days = sorted(dk["et_day"].unique())
    oos = []
    for di in range(3, len(days)):
        tr = dk[dk["et_day"].isin(days[:di])]
        te = dk[dk["et_day"] == days[di]].copy()
        if len(te) == 0 or tr["label"].nunique() < 2:
            continue
        pr = fit_predict(tr, te)
        te["p_logit"] = np.clip(pr["logit"], 0.01, 0.99)
        te["p_gbt"] = np.clip(pr["gbt"], 0.01, 0.99)
        oos.append(te)
    o = pd.concat(oos, ignore_index=True)
    res = {"n": len(o), "days": len(oos), "base": float(o["label"].mean())}
    y = o["label"].to_numpy()
    per_day = {}
    for name in ("book_p", "p_logit", "p_gbt"):
        p = o[name].to_numpy()
        ll = -(y * np.log(p) + (1 - y) * np.log(1 - p))
        res[f"ll_{name}"] = float(ll.mean())
        per_day[name] = o.assign(ll=ll).groupby("et_day")["ll"].mean()
    for name in ("p_logit", "p_gbt"):
        diff = (per_day["book_p"] - per_day[name]).to_numpy()  # >0 = model better
        boots = rng.choice(diff, size=(10000, len(diff)), replace=True).mean(axis=1)
        res[f"p_beats_book_{name}"] = float((boots <= 0).mean())
    # ---- B2 taker sim on the better model ----
    best_name = "p_gbt" if res["ll_p_gbt"] <= res["ll_p_logit"] else "p_logit"
    p = o[best_name].to_numpy()
    edge_up = p - o["exec_ask_up"].to_numpy()
    edge_dn = (1 - p) - o["exec_ask_dn"].to_numpy()
    side_up = edge_up >= edge_dn
    edge = np.where(side_up, edge_up, edge_dn)
    px = np.where(side_up, o["exec_ask_up"], o["exec_ask_dn"])
    sz = np.where(side_up, o["ask_sz_up"], o["ask_sz_dn"])
    ok_sz = ~(sz < 5)                         # NaN passes
    win = np.where(side_up, y == 1, y == 0)
    fee = 0.07 * px * (1 - px)
    pnl_sh = np.where(win, (1 - px) - fee, -px - fee)
    buckets = [(-1.0, 0.0, "control<0"), (0.0, 0.02, "0-2c"), (0.02, 0.04, "2-4c"),
               (0.04, 0.06, "4-6c"), (0.06, 0.10, "6-10c"), (0.10, 1.0, ">10c")]
    res["buckets"] = {}
    for lo_, hi, nameb in buckets:
        m = (edge >= lo_) & (edge < hi) & ok_sz
        res["buckets"][nameb] = dict(
            n=int(m.sum()), ew=float(100 * pnl_sh[m].mean()) if m.sum() else None,
            win=float(win[m].mean()) if m.sum() else None)
    m = (edge >= 0.04) & ok_sz
    res["trade_n"] = int(m.sum())
    res["trade_ew_c"] = float(100 * pnl_sh[m].mean()) if m.sum() else None
    res["trade_win"] = float(win[m].mean()) if m.sum() else None
    res["best_model"] = best_name
    report[int(k)] = res
    print(f"\n=== k={k}s: n={res['n']} base={res['base']:.3f} ===")
    print(f"  log-loss  book {res['ll_book_p']:.4f} | logit {res['ll_p_logit']:.4f} "
          f"(p={res['p_beats_book_p_logit']:.3f}) | gbt {res['ll_p_gbt']:.4f} "
          f"(p={res['p_beats_book_p_gbt']:.3f})")
    print(f"  B2 [{best_name}] trades(edge>=4c): {res['trade_n']}  "
          f"EW {res['trade_ew_c'] if res['trade_ew_c'] is None else round(res['trade_ew_c'], 2)}c/sh  "
          f"win {res['trade_win'] if res['trade_win'] is None else round(res['trade_win'], 3)}")
    print("  buckets:", {kk: (v["n"], None if v["ew"] is None else round(v["ew"], 1))
                         for kk, v in res["buckets"].items()})

json.dump(report, open(D / "r17_report.json", "w"), indent=1)
b1 = any(min(r["p_beats_book_p_logit"], r["p_beats_book_p_gbt"]) < 0.05
         and min(r["ll_p_logit"], r["ll_p_gbt"]) < r["ll_book_p"] for r in report.values())
print(f"\nB1 (model beats book, p<0.05, any horizon): {'PASS' if b1 else 'FAIL'}")
print("saved r17_report.json")
