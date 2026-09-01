"""Info program addendum: book-anchored nested models (pre-registered 09-01).

M0 = logistic on logit(book_p) alone (book recalibration — miscalibration test).
M1(f) = logistic on [x0, f] per feature (incremental information per feature).
M2 = GBT on [x0 + all features]. M3 = logistic on [x0 + top-3 by train value].
Walk-forward by ET day, same splits as r17. Significance: day bootstrap,
Bonferroni 27x5. Output: r18_report.json + printed tables.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

D = Path(__file__).parent / "data" / "vps-0831"
df = pd.read_parquet(D / "info_dataset.parquet")
FEATS = [c for c in df.columns if c not in
         ("ep", "k", "ts", "et_day", "label", "book_p", "ask_up", "ask_dn",
          "exec_ask_up", "exec_ask_dn", "ask_sz_up", "ask_sz_dn", "spot_px")]
df["x0"] = np.log(df["book_p"] / (1 - df["book_p"]))
rng = np.random.default_rng(20260901)
BONF = 0.05 / (27 * 5)

def logit_fit(tr, te, cols):
    m = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                      LogisticRegression(C=1.0, max_iter=2000))
    m.fit(tr[cols], tr["label"])
    return np.clip(m.predict_proba(te[cols])[:, 1], 0.005, 0.995), m

def ll_vec(y, p):
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))

report = {}
for k in sorted(df["k"].unique()):
    dk = df[df["k"] == k].sort_values("ts").reset_index(drop=True)
    days = sorted(dk["et_day"].unique())
    folds = []
    for di in range(3, len(days)):
        tr = dk[dk["et_day"].isin(days[:di])]
        te = dk[dk["et_day"] == days[di]].copy()
        if len(te) == 0 or tr["label"].nunique() < 2:
            continue
        folds.append((tr, te))
    # collect OOS predictions per model
    oos = {name: [] for name in ["y", "day", "book", "M0", "M2", "M3"]}
    oos_f = {f: [] for f in FEATS}
    m3_picks = []
    coef_sign = {f: [] for f in FEATS}
    for tr, te in folds:
        oos["y"].append(te["label"].to_numpy())
        oos["day"].append(te["et_day"].to_numpy())
        oos["book"].append(te["book_p"].to_numpy())
        p0, _ = logit_fit(tr, te, ["x0"])
        oos["M0"].append(p0)
        # per-feature anchored; track train incremental value for M3 pick
        tr_gain = {}
        for f in FEATS:
            if tr[f].notna().sum() < 100:
                oos_f[f].append(np.full(len(te), np.nan))
                tr_gain[f] = -np.inf
                continue
            p1, m = logit_fit(tr, te, ["x0", f])
            oos_f[f].append(p1)
            coef_sign[f].append(float(np.sign(m[-1].coef_[0][-1])))
            p_tr = np.clip(m.predict_proba(
                make_pipeline(SimpleImputer(strategy="median")).fit(tr[["x0", f]]).transform(tr[["x0", f]])
            )[:, 1], 0.005, 0.995) if False else None
            # train incremental value: fit-on-train ll vs M0-on-train (in-sample, pick only)
            p1_tr = np.clip(m.predict_proba(tr[["x0", f]].fillna(tr[["x0", f]].median()))[:, 1], 0.005, 0.995)
            p0_tr, _ = logit_fit(tr, tr, ["x0"])
            tr_gain[f] = float(ll_vec(tr["label"].to_numpy(), p0_tr).mean()
                               - ll_vec(tr["label"].to_numpy(), p1_tr).mean())
        top3 = sorted(tr_gain, key=tr_gain.get, reverse=True)[:3]
        m3_picks.append(top3)
        p3, _ = logit_fit(tr, te, ["x0"] + top3)
        oos["M3"].append(p3)
        gb = HistGradientBoostingClassifier(learning_rate=0.1, max_leaf_nodes=31,
                                            max_iter=300, early_stopping=False,
                                            random_state=0)
        Xtr = tr[["x0"] + FEATS]
        gb.fit(Xtr, tr["label"])
        oos["M2"].append(np.clip(gb.predict_proba(te[["x0"] + FEATS])[:, 1], 0.005, 0.995))
    y = np.concatenate(oos["y"])
    day = np.concatenate(oos["day"])
    res = {"n": int(len(y))}
    base = {}
    for name in ("book", "M0", "M2", "M3"):
        p = np.clip(np.concatenate(oos[name]), 0.005, 0.995)
        base[name] = ll_vec(y, p)
        res[f"ll_{name}"] = float(base[name].mean())
    def boot_p(ll_model):
        d = pd.DataFrame({"day": day, "diff": base["book"] - ll_model})
        per = d.groupby("day")["diff"].mean().to_numpy()
        boots = rng.choice(per, size=(10000, len(per)), replace=True).mean(axis=1)
        return float((boots <= 0).mean())
    for name in ("M0", "M2", "M3"):
        res[f"p_{name}"] = boot_p(base[name])
    # per-feature table
    feat_rows = []
    for f in FEATS:
        parts = oos_f[f]
        if any(np.isnan(x).all() for x in parts):
            continue
        p = np.clip(np.concatenate(parts), 0.005, 0.995)
        llf = ll_vec(y, p)
        signs = coef_sign[f]
        feat_rows.append(dict(f=f, ll=float(llf.mean()),
                              d_vs_book=float(base["book"].mean() - llf.mean()),
                              p=boot_p(llf),
                              sign_stable=bool(signs and (abs(sum(np.sign(s) for s in signs)) == len(signs)))))
    feat_rows.sort(key=lambda r: -r["d_vs_book"])
    res["top_features"] = feat_rows[:6]
    res["m3_picks"] = pd.Series([f for row in m3_picks for f in row]).value_counts().head(5).to_dict()
    report[int(k)] = res
    print(f"\n=== k={k}s (n={res['n']}) ===")
    print(f"  book {res['ll_book']:.4f} | M0 recal {res['ll_M0']:.4f} (p={res['p_M0']:.3f})"
          f" | M2 gbt+x0 {res['ll_M2']:.4f} (p={res['p_M2']:.3f})"
          f" | M3 top3+x0 {res['ll_M3']:.4f} (p={res['p_M3']:.3f})")
    print(f"  best single features (delta vs book, +=better, Bonf alpha {BONF:.5f}):")
    for r_ in feat_rows[:5]:
        print(f"    {r_['f']:18s} d={r_['d_vs_book']:+.5f}  p={r_['p']:.4f}  "
              f"sign_stable={r_['sign_stable']}")
    print(f"  M3 most-picked: {res['m3_picks']}")

json.dump(report, open(D / "r18_report.json", "w"), indent=1)
hits = [(k, r_["f"], r_["p"]) for k, res in report.items()
        for r_ in res["top_features"] if r_["d_vs_book"] > 0 and r_["p"] < BONF]
print(f"\nBonferroni-significant single-feature improvements: {hits if hits else 'NONE'}")
m0_hits = [k for k, res in report.items()
           if res["ll_M0"] < res["ll_book"] and res["p_M0"] < 0.05]
print(f"Book miscalibration (M0 beats book, p<0.05): {m0_hits if m0_hits else 'NONE'}")
print("saved r18_report.json")
