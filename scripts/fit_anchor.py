"""Fit the market-anchor shrink factor k and re-derive entry thresholds.

p_anchored = sigmoid(logit(price) + k * (logit(p_raw) - logit(price)))

k=0 trusts the market entirely; k=1 trusts the model entirely. Sweeps k on
all recorded decisions (trades w/ resolution outcome + ghosts), validates on
a time split, then reports realized edge by ANCHORED-edge bucket so entry
thresholds can be set in honest units.
"""
from __future__ import annotations

import math
import sys

from diagnose_edge import load_records, logloss, brier  # noqa: E402


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def anchored(price: float, p_raw: float, k: float) -> float:
    return sigmoid(logit(price) + k * (logit(p_raw) - logit(price)))


def build_rows():
    trades = load_records("outcomes")
    ghosts = load_records("ghost_outcomes")
    cfs = load_records("counterfactuals")
    cf_res = {}
    for c in cfs:
        rp = (c.get("counterfactual") or {}).get("resolution_price")
        if c.get("actual", {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res[c["position_id"]] = rp == 1.0
    rows = []
    for t in trades:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw = ctx.get("model_probability_raw")
        price = ctx.get("market_price_down" if t["side"] == "Down" else "market_price_up")
        if p_raw is None or price is None or not (0 < price < 1):
            continue
        if t.get("exit_reason") == "resolution":
            won = bool(t["correct"])
        elif t.get("position_id") in cf_res:
            won = cf_res[t["position_id"]]
        else:
            continue
        rows.append((p_raw, price, won, (t.get("timestamp") or "")[:10], "trade"))
    for g in ghosts:
        ctx = (g.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw = ctx.get("model_probability_raw")
        price = ctx.get("market_price_down" if g["side"] == "Down" else "market_price_up")
        if not g.get("resolved") or p_raw is None or price is None or not (0 < price < 1):
            continue
        rows.append((p_raw, price, bool(g["ghost_correct"]),
                     (g.get("timestamp") or "")[:10], "ghost"))
    return rows


def main() -> None:
    rows = build_rows()
    days = sorted({r[3] for r in rows})
    half = days[len(days) // 2]
    first = [r for r in rows if r[3] < half]
    second = [r for r in rows if r[3] >= half]
    print(f"n={len(rows)}  (first {len(first)}, second {len(second)}; split at {half})")

    print(f"\n{'k':>5} {'LL_all':>8} {'LL_first':>9} {'LL_second':>10} {'brier_all':>10}")
    best = (9e9, None)
    for k10 in range(0, 21):
        k = k10 / 20
        f = lambda rs: [(anchored(pr, p, k), w) for p, pr, w, *_ in
                        [(r[0], r[1], r[2]) for r in rs]]
        # note arg order: anchored(price, p_raw, k)
        def blend(rs):
            return [(anchored(r[1], r[0], k), r[2]) for r in rs]
        ll = logloss(blend(rows))
        ll1, ll2 = logloss(blend(first)), logloss(blend(second))
        br = brier(blend(rows))
        marker = ""
        if ll < best[0]:
            best = (ll, k)
            marker = "  <-"
        print(f"{k:>5.2f} {ll:>8.4f} {ll1:>9.4f} {ll2:>10.4f} {br:>10.4f}{marker}")
    print(f"\nbest k by pooled log-loss: {best[1]}")

    # fit k on first half only, evaluate second half (honest OOS pick)
    def ll_at(rs, k):
        return logloss([(anchored(r[1], r[0], k), r[2]) for r in rs])
    k_oos = min((ll_at(first, k10 / 20), k10 / 20) for k10 in range(0, 21))[1]
    print(f"k fit on first half: {k_oos}  -> second-half LL {ll_at(second, k_oos):.4f} "
          f"(market {ll_at(second, 0.0):.4f}, raw {ll_at(second, 1.0):.4f})")

    # realized edge by ANCHORED edge bucket, at the chosen k
    for k in (best[1], k_oos):
        print(f"\n== realized edge by anchored-edge bucket (k={k}) ==")
        buckets = [(-1, 0.0), (0.0, 0.01), (0.01, 0.02), (0.02, 0.03),
                   (0.03, 0.045), (0.045, 0.07), (0.07, 1.0)]
        print(f"{'anch_edge':>16} {'n':>5} {'realized':>9} {'mean_pred':>10} {'mean_price':>11} {'fee_est':>8}")
        for lo, hi in buckets:
            b = [r for r in rows if lo <= anchored(r[1], r[0], k) - r[1] < hi]
            if len(b) < 10:
                continue
            wr = sum(r[2] for r in b) / len(b)
            mp = sum(r[1] for r in b) / len(b)
            pred = sum(anchored(r[1], r[0], k) - r[1] for r in b) / len(b)
            fee = 0.07 * mp * (1 - mp)  # entry fee as fraction of $1 payout per share
            print(f"{lo:>+7.3f}..{hi:<+7.3f} {len(b):>5} {wr - mp:>+9.3f} {pred:>+10.3f} "
                  f"{mp:>11.3f} {fee:>8.4f}")
        # second-half-only stability for the same buckets
        print("  second half only:")
        for lo, hi in buckets:
            b = [r for r in second if lo <= anchored(r[1], r[0], k) - r[1] < hi]
            if len(b) < 10:
                continue
            wr = sum(r[2] for r in b) / len(b)
            mp = sum(r[1] for r in b) / len(b)
            print(f"{lo:>+7.3f}..{hi:<+7.3f} {len(b):>5} {wr - mp:>+9.3f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
