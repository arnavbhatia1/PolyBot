"""Did Kelly-on-claimed-edge add value, and which claimed-edge band loses real money?

1. Size vs outcome: if claimed edge is noise, bigger bets shouldn't earn higher
   gain_pct. Compare gain_pct (actual, exit-inclusive) across size quartiles.
2. Actual P&L by claimed-raw-edge bucket, trades only, exits included.
"""
from __future__ import annotations

import sys
from collections import defaultdict

from diagnose_edge import load_records


def main() -> None:
    trades = load_records("outcomes")
    rows = []
    for t in trades:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        p_raw = ctx.get("model_probability_raw")
        price = ctx.get("market_price_down" if t["side"] == "Down" else "market_price_up")
        if p_raw is None or price is None or t.get("pnl") is None or not t.get("size"):
            continue
        rows.append(dict(edge=p_raw - price, size=t["size"], pnl=t["pnl"],
                         gain=t["pnl"] / t["size"]))
    print(f"trades with full context: {len(rows)}")

    # 1) size quartiles vs realized gain_pct
    by_size = sorted(rows, key=lambda r: r["size"])
    q = len(by_size) // 4
    print("\n== realized gain_pct by size quartile (exit-inclusive) ==")
    for i in range(4):
        b = by_size[i * q:(i + 1) * q] if i < 3 else by_size[3 * q:]
        mg = sum(r["gain"] for r in b) / len(b)
        print(f"  Q{i+1}: n={len(b):>4}  size {b[0]['size']:>6.2f}-{b[-1]['size']:>7.2f}  "
              f"mean_gain={mg:>+7.4f}  sum_pnl={sum(r['pnl'] for r in b):>+9.2f}")

    # 2) actual P&L by claimed edge bucket
    print("\n== actual P&L by claimed raw edge (trades only, exits included) ==")
    buckets = [(-1.0, 0.04), (0.04, 0.07), (0.07, 0.12), (0.12, 0.20), (0.20, 1.0)]
    for lo, hi in buckets:
        b = [r for r in rows if lo <= r["edge"] < hi]
        if not b:
            continue
        pnl = sum(r["pnl"] for r in b)
        mg = sum(r["gain"] for r in b) / len(b)
        print(f"  {lo:>+5.2f}..{hi:<+5.2f}: n={len(b):>5}  pnl={pnl:>+9.2f}  mean_gain={mg:>+7.4f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
