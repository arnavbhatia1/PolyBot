"""Watch the late-window sniper PAPER-SHADOW.

Reports the sniper's paper fills (entry_phase == 'late_sniper') and their realized
P&L, day by day. Run it any time once the shadow is live (sniper_enabled: true in
paper). Fills are sparse — the sniper only fires in the final 45s on a Coinbase move
past strike with a still-cheap ask — so expect a handful per day.

These are PAPER fills at the sim latency (~0.135s), so the right comparison is
`python scripts/analyze_late_window.py --rtt-sweep 0.135`, NOT the optimistic 40ms
read (that needs a low-latency host). Go-live still requires the full kill bar:
t_day>=2, p10>0 over >=8 clean ET days at the host's MEASURED RTT, with this shadow
tracking the harness.

  python scripts/sniper_shadow_status.py
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from polybot.agents.outcome_reviewer import OutcomeReviewer  # noqa: E402


def main() -> None:
    # load_all_outcomes dedups by (position_id, market_id) — a fill present in
    # both its per-trade file and a partial daily rollup counts once, which the
    # shadow-vs-harness kill-bar comparison depends on.
    outcomes = OutcomeReviewer(
        str(_ROOT / "polybot" / "memory" / "outcomes")).load_all_outcomes()
    snipes = []
    for t in outcomes:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        if ctx.get("entry_phase") != "late_sniper":
            continue
        snipes.append(dict(day=(t.get("timestamp") or "")[:10], correct=t.get("correct"),
                           pnl=t.get("pnl"), gain=t.get("gain_pct"),
                           reason=t.get("exit_reason")))
    if not snipes:
        print("No sniper paper fills recorded yet. The shadow fires only in the final 45s "
              "on a Coinbase move past strike with a still-cheap ask (a handful/day). Check "
              "the bot log for 'LATE-SNIPER fire' lines to confirm it is active.")
        return

    byday = defaultdict(list)
    for s in snipes:
        byday[s["day"]].append(s)
    print(f"{'day':>12} {'fills':>6} {'win%':>6} {'net/sh':>9} {'pnl$':>10}")
    tot_pnl = tot_n = wins = 0
    for d in sorted(byday):
        v = byday[d]
        n = len(v)
        w = sum(1 for s in v if s["correct"])
        pnl = sum(s["pnl"] or 0.0 for s in v)
        gains = [s["gain"] for s in v if s["gain"] is not None]
        ns = statistics.mean(gains) if gains else float("nan")
        print(f"{d:>12} {n:>6} {w / n:>6.0%} {ns:>+9.4f} {pnl:>+10.2f}")
        tot_pnl += pnl
        tot_n += n
        wins += w
    print(f"{'TOTAL':>12} {tot_n:>6} {wins / tot_n:>6.0%} {'':>9} {tot_pnl:>+10.2f}")
    print(f"\n{len(byday)} day(s) of shadow data — kill bar needs >=8 clean days. "
          f"Compare net/sh to analyze_late_window.py --rtt-sweep 0.135 (paper sim latency).")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
