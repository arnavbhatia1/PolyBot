"""Edge forensics: does the model have signal, and are the gates eating it?

Q1  Reliability of model_probability_raw vs realized window outcomes.
Q2  Per-gate ghost EV: what the gates rejected and what it would have paid.
Q3  Model vs market: Brier/log-loss of raw prob against the CLOB price.

Reads outcomes/, ghost_outcomes/, counterfactuals/ (per-trade + rollups).
Run: python scripts/diagnose_edge.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

MEM = Path(__file__).resolve().parent.parent / "polybot" / "memory"


def load_records(dirname: str) -> list[dict]:
    out = []
    for f in sorted((MEM / dirname).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def brier(rows):  # rows: (p, won)
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in rows) / len(rows)


def logloss(rows):
    eps = 1e-6
    return -sum(
        math.log(max(p, eps)) if w else math.log(max(1 - p, eps)) for p, w in rows
    ) / len(rows)


def main() -> None:
    trades = load_records("outcomes")
    ghosts = load_records("ghost_outcomes")
    cfs = load_records("counterfactuals")

    # hold-to-resolution outcome for scalped trades, via counterfactual resolution price
    cf_res: dict[int, bool] = {}
    scalp_cfs = []
    for c in cfs:
        cf = c.get("counterfactual") or {}
        rp = cf.get("resolution_price")
        if c.get("actual", {}).get("exit_reason") != "hold" and rp in (0.0, 1.0):
            cf_res[c["position_id"]] = rp == 1.0
            scalp_cfs.append(c)

    # ---- build unified decision rows: (p_raw, p_cal, side_price, won, kind, day)
    rows = []
    skipped = defaultdict(int)
    for t in trades:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw, p_cal = ctx.get("model_probability_raw"), ctx.get("model_probability")
        price = ctx.get("market_price_down" if t["side"] == "Down" else "market_price_up")
        if p_raw is None or price is None:
            skipped["trade_missing_fields"] += 1
            continue
        if t.get("exit_reason") == "resolution":
            won = bool(t["correct"])
        elif t.get("position_id") in cf_res:
            won = cf_res[t["position_id"]]
        else:
            skipped["scalp_no_cf"] += 1
            continue
        rows.append(dict(p_raw=p_raw, p_cal=p_cal, price=price, won=won,
                         kind="trade", day=(t.get("timestamp") or "")[:10],
                         gain=t.get("gain_pct"), pnl=t.get("pnl")))
    for g in ghosts:
        if not g.get("resolved"):
            skipped["ghost_unresolved"] += 1
            continue
        ctx = (g.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw = ctx.get("model_probability_raw")
        price = ctx.get("market_price_down" if g["side"] == "Down" else "market_price_up")
        if p_raw is None or price is None:
            skipped["ghost_missing_fields"] += 1
            continue
        rows.append(dict(p_raw=p_raw, p_cal=g.get("signal_prob"), price=price,
                         won=bool(g["ghost_correct"]), kind="ghost",
                         day=(g.get("timestamp") or "")[:10],
                         gate=g.get("gate_name"), gain=g.get("ghost_gain_pct")))

    print(f"decisions: {len(rows)}  "
          f"(trades {sum(r['kind'] == 'trade' for r in rows)}, "
          f"ghosts {sum(r['kind'] == 'ghost' for r in rows)})  skipped: {dict(skipped)}")

    # ---- Q1: reliability table on raw prob -------------------------------
    def reliability(rs, field, title):
        edges = [0.0, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 1.01]
        print(f"\n== Q1 reliability: {title} (n={len(rs)}) ==")
        print(f"{'bin':>12} {'n':>5} {'mean_p':>7} {'real_WR':>8} {'mkt_p':>6} {'gap_model':>9} {'gap_mkt':>8}")
        for lo, hi in zip(edges, edges[1:]):
            b = [r for r in rs if r[field] is not None and lo <= r[field] < hi]
            if len(b) < 5:
                continue
            mp = sum(r[field] for r in b) / len(b)
            wr = sum(r["won"] for r in b) / len(b)
            mk = sum(r["price"] for r in b) / len(b)
            print(f"{lo:>5.2f}-{hi:<6.2f} {len(b):>5} {mp:>7.3f} {wr:>8.3f} {mk:>6.3f} "
                  f"{wr - mp:>+9.3f} {wr - mk:>+8.3f}")

    reliability(rows, "p_raw", "model_probability_raw — ALL decisions")
    reliability([r for r in rows if r["kind"] == "trade"], "p_raw", "raw — trades only")
    reliability([r for r in rows if r["kind"] == "trade"], "p_cal", "CALIBRATED — trades only")

    # ---- Q3: model vs market ---------------------------------------------
    print("\n== Q3 model vs market (lower = better) ==")
    for label, sel in [("ALL", rows), ("trades", [r for r in rows if r["kind"] == "trade"])]:
        pr = [(r["p_raw"], r["won"]) for r in sel]
        pm = [(r["price"], r["won"]) for r in sel]
        pc = [(r["p_cal"], r["won"]) for r in sel if r["p_cal"] is not None]
        coin = [(0.5, r["won"]) for r in sel]
        print(f"  [{label}] n={len(sel)}")
        print(f"    brier   raw={brier(pr):.4f}  cal={brier(pc):.4f}  market={brier(pm):.4f}  coin={brier(coin):.4f}")
        print(f"    logloss raw={logloss(pr):.4f}  cal={logloss(pc):.4f}  market={logloss(pm):.4f}  coin={logloss(coin):.4f}")

    # predicted-edge buckets: does bigger claimed edge realize bigger actual edge?
    print("\n== Q3b predicted edge (raw - price) vs realized edge (WR - price) ==")
    buckets = [(-1.0, 0.0), (0.0, 0.02), (0.02, 0.04), (0.04, 0.07), (0.07, 0.12), (0.12, 1.0)]
    print(f"{'pred_edge':>14} {'n':>5} {'realized_edge':>14} {'mean_pred':>10}")
    for lo, hi in buckets:
        b = [r for r in rows if lo <= (r["p_raw"] - r["price"]) < hi]
        if len(b) < 5:
            continue
        realized = sum(r["won"] for r in b) / len(b) - sum(r["price"] for r in b) / len(b)
        pred = sum(r["p_raw"] - r["price"] for r in b) / len(b)
        print(f"{lo:>+6.2f}..{hi:<+6.2f} {len(b):>5} {realized:>+14.3f} {pred:>+10.3f}")

    # time stability: first vs second half of days
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    for label, sel in [("first-half days", [r for r in rows if r["day"] < half]),
                       ("second-half days", [r for r in rows if r["day"] >= half])]:
        pr = [(r["p_raw"], r["won"]) for r in sel]
        pm = [(r["price"], r["won"]) for r in sel]
        print(f"  [{label}] n={len(sel)}  brier raw={brier(pr):.4f} vs market={brier(pm):.4f}")

    # ---- Q2: what the gates rejected --------------------------------------
    print("\n== Q2 ghost EV by gate (fee-aware ghost_gain_pct; >0 means the gate rejected profit) ==")
    by_gate = defaultdict(list)
    for r in rows:
        if r["kind"] == "ghost" and r.get("gain") is not None:
            by_gate[r.get("gate") or "?"].append(r)
    print(f"{'gate':>28} {'n':>5} {'WR':>6} {'mean_gain':>10} {'sum_gain':>9}")
    for gate, rs in sorted(by_gate.items(), key=lambda kv: -len(kv[1])):
        wr = sum(r["won"] for r in rs) / len(rs)
        mg = sum(r["gain"] for r in rs) / len(rs)
        print(f"{gate:>28} {len(rs):>5} {wr:>6.3f} {mg:>+10.4f} {sum(r['gain'] for r in rs):>+9.2f}")

    taken = [r for r in rows if r["kind"] == "trade" and r.get("gain") is not None]
    if taken:
        print(f"{'TAKEN (resolution basis)':>28} {len(taken):>5} "
              f"{sum(r['won'] for r in taken) / len(taken):>6.3f} "
              f"{sum(r['gain'] for r in taken) / len(taken):>+10.4f} "
              f"{sum(r['gain'] for r in taken):>+9.2f}")

    # ---- realized P&L by exit reason (actual money, all trades) -----------
    print("\n== realized P&L by exit_reason (all outcome records) ==")
    by_reason = defaultdict(list)
    for t in trades:
        if t.get("pnl") is not None:
            by_reason[t.get("exit_reason") or "?"].append(t)
    for reason, ts in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        pnl = sum(t["pnl"] for t in ts)
        wr = sum(1 for t in ts if t["pnl"] > 0) / len(ts)
        print(f"  {reason:>12}: n={len(ts):>5}  pnl={pnl:>+9.2f}  win%={wr:.3f}")
    print(f"  {'TOTAL':>12}: n={sum(len(v) for v in by_reason.values()):>5}  "
          f"pnl={sum(t['pnl'] for ts in by_reason.values() for t in ts):>+9.2f}")

    # ---- scalp vs hold-to-resolution counterfactual ------------------------
    print("\n== scalp exits: actual vs hold-to-resolution counterfactual ==")
    act = sum(c["actual"]["pnl"] for c in scalp_cfs)
    cfp = sum(c["counterfactual"]["pnl"] for c in scalp_cfs)
    opt = sum(1 for c in scalp_cfs if c.get("scalp_was_optimal"))
    print(f"  n={len(scalp_cfs)}  actual_pnl={act:+.2f}  hold_pnl={cfp:+.2f}  "
          f"delta={cfp - act:+.2f}  scalp_optimal={opt}/{len(scalp_cfs)}")
    # segment by whether the scalp locked a loss or banked a profit
    for label, sel in [("loss scalps", [c for c in scalp_cfs if c["actual"]["pnl"] < 0]),
                       ("profit scalps", [c for c in scalp_cfs if c["actual"]["pnl"] >= 0])]:
        if not sel:
            continue
        a = sum(c["actual"]["pnl"] for c in sel)
        h = sum(c["counterfactual"]["pnl"] for c in sel)
        wr = sum(1 for c in sel if c["counterfactual"]["resolution_price"] == 1.0) / len(sel)
        print(f"  {label:>14}: n={len(sel):>5}  actual={a:>+9.2f}  hold={h:>+9.2f}  "
          f"hold_would_win={wr:.3f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
