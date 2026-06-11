"""Follow-up check: is the claimed 'mean_diff' the mean of ET-day means?"""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from verify_p2_segmented_l1_vs_market import (  # noqa: E402
    AtrFloor, dedupe_by_pid, et_day, load_records, p_l1_chosen)


def main() -> None:
    trades, _ = dedupe_by_pid(load_records("outcomes"))
    cfs, _ = dedupe_by_pid(load_records("counterfactuals"))
    cf_res = {}
    for c in cfs:
        cf = c.get("counterfactual") or {}
        rp = cf.get("resolution_price")
        if (c.get("actual") or {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res[c["position_id"]] = rp == 1.0
    poolA = []
    for t in sorted(trades, key=lambda r: r.get("timestamp") or ""):
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        p = ctx.get("model_probability_raw")
        side = t.get("side")
        price = ctx.get("market_price_down" if side == "Down" else "market_price_up")
        if p is None or price is None:
            continue
        if t.get("exit_reason") == "resolution":
            won = bool(t.get("correct"))
        elif t.get("position_id") in cf_res:
            won = cf_res[t["position_id"]]
        else:
            continue
        poolA.append(dict(ts=t["timestamp"], day=et_day(t["timestamp"]), side=side,
                          p_raw=float(p), price=float(price), won=won, ctx=ctx))
    fl = AtrFloor()
    for r in sorted(poolA, key=lambda r: r["ts"]):
        r["p_l1"] = p_l1_chosen(r["ctx"], r["side"], fl)

    def daymean(rows, pred):
        bd = defaultdict(list)
        for r in rows:
            p = r["p_raw"] if pred == "raw" else r["p_l1"]
            y = 1.0 if r["won"] else 0.0
            bd[r["day"]].append((r["price"] - y) ** 2 - (p - y) ** 2)
        ms = [sum(v) / len(v) for v in bd.values()]
        return sum(ms) / len(ms), len(ms)

    print("overall A raw day-mean: %+.5f (nd=%d)" % daymean(poolA, "raw"))
    print("overall A l1  day-mean: %+.5f (nd=%d)" % daymean(poolA, "l1"))
    tu = [r for r in poolA if r["ctx"].get("regime_state") == "trending_up"]
    vo = [r for r in poolA if r["ctx"].get("regime_state") == "volatile"]
    atrs = sorted(r["ctx"].get("atr") or 0 for r in poolA)
    t2 = atrs[2 * len(atrs) // 3]
    t3 = [r for r in poolA if (r["ctx"].get("atr") or 0) > t2]
    tr = [r for r in poolA if r["ctx"].get("regime_autocorr") is not None
          and r["ctx"]["regime_autocorr"] > 0.15]
    print("trending_up raw day-mean: %+.5f (nd=%d)" % daymean(tu, "raw"))
    print("volatile l1 day-mean: %+.5f (nd=%d)" % daymean(vo, "l1"))
    print("atr_T3 l1 day-mean: %+.5f (nd=%d)" % daymean(t3, "l1"))
    print("ac_trend l1 day-mean: %+.5f (nd=%d)" % daymean(tr, "l1"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
