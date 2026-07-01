"""Side-by-side P&L: the NORMAL strategy vs the SNIPER vs BOTH combined.

Reads the reliable per-trade outcome records (memory/outcomes/*) — each carries the
realized pnl, gain_pct, win flag, and the entry_phase that tags how it was entered:
  - SNIPER  = entry_phase == 'late_sniper'
  - NORMAL  = every other phase (normal / late / final)
The positions table is NOT used (its resolution flags have been unreliable).

Observe one, the other, or both:
  python scripts/strategy_compare.py                 # all days
  python scripts/strategy_compare.py --since 2026-06-18   # from a clean cutover

Dollar P&L is the realized pnl; net/$ is the mean per-dollar return (gain_pct), the
size-independent edge measure. After the clean reset, pass --since <reset-date> and
this shows only current-bot data.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.diagnose_edge import load_records  # noqa: E402


def _bucket(o):
    ctx = (o.get("indicator_snapshot") or {}).get("trade_context") or {}
    return "sniper" if ctx.get("entry_phase") == "late_sniper" else "normal"


def _summ(rows):
    """(n, pnl$, win%, net/$ mean) for a list of outcome dicts."""
    if not rows:
        return (0, 0.0, float("nan"), float("nan"))
    n = len(rows)
    pnl = sum(r.get("pnl") or 0.0 for r in rows)
    wins = sum(1 for r in rows if r.get("correct"))
    gains = [r.get("gain_pct") for r in rows if r.get("gain_pct") is not None]
    return (n, pnl, wins / n, statistics.mean(gains) if gains else float("nan"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=str, default=None, help="only count outcomes on/after this ET date (YYYY-MM-DD)")
    args = ap.parse_args()

    by_day = defaultdict(lambda: {"normal": [], "sniper": []})
    for o in load_records("outcomes"):
        day = (o.get("timestamp") or "")[:10]
        if not day or (args.since and day < args.since):
            continue
        by_day[day][_bucket(o)].append(o)

    if not by_day:
        print("No outcomes in range.")
        return

    days = sorted(by_day)
    print(f"Side-by-side {days[0]} .. {days[-1]}"
          + (f"  (since {args.since})" if args.since else "  (all days — pass --since for a clean cutover)"))
    print(f"\n{'day':>11} | {'NORMAL pnl$':>12} {'n':>5} {'win%':>5} | {'SNIPER pnl$':>12} {'n':>4} {'win%':>5} | {'COMBINED$':>11}")
    print("-" * 86)
    agg = {"normal": [], "sniper": []}
    for d in days:
        nb, sb = by_day[d]["normal"], by_day[d]["sniper"]
        agg["normal"] += nb
        agg["sniper"] += sb
        nn, npnl, nwin, _ = _summ(nb)
        sn, spnl, swin, _ = _summ(sb)
        nwin_s = f"{nwin:.0%}" if nn else "  -"
        swin_s = f"{swin:.0%}" if sn else "  -"
        print(f"{d:>11} | {npnl:>+12.2f} {nn:>5} {nwin_s:>5} | {spnl:>+12.2f} {sn:>4} {swin_s:>5} | {npnl+spnl:>+11.2f}")

    print("-" * 86)
    print(f"\n{'strategy':>10} {'trades':>7} {'pnl$':>11} {'win%':>6} {'net/$':>9}")
    for name, rows in (("normal", agg["normal"]), ("sniper", agg["sniper"]),
                       ("COMBINED", agg["normal"] + agg["sniper"])):
        n, pnl, win, perd = _summ(rows)
        win_s = f"{win:.1%}" if n else "-"
        perd_s = f"{perd:+.4f}" if n == n and n else "-"   # n==n guards nan
        print(f"{name:>10} {n:>7} {pnl:>+11.2f} {win_s:>6} {perd_s:>9}")
    print(f"\nSNIPER is the alpha edge under validation (still tiny n). NORMAL is the existing")
    print(f"strategy. net/$ is the size-independent edge; dollar pnl scales with bankroll.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
