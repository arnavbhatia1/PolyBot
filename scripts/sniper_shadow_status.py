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
  python scripts/sniper_shadow_status.py --since 2026-07-08T17:15:00+00:00

--since scopes the read to fills recorded at/after an epoch (the clean-baseline
restart). It filters on each fill's resolution timestamp; the sniper resolves within
45s of entry, so this is the entry epoch to ~1min. Use it to exclude pre-fix fills
and any live-mode fills that share the outcomes/ directory — outcome files carry no
mode marker and position_id is NOT unique across modes/time, so a timestamp epoch is
the only robust cut. It therefore assumes ONE mode is running after the epoch (true
during a paper re-validation); if you later run live, live fills after --since would
also be counted.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from polybot.agents.outcome_reviewer import OutcomeReviewer  # noqa: E402
from polybot.agents.pipeline_analytics import utc_ts_to_et_date  # noqa: E402


def _parse_since(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("--since must include a timezone offset (e.g. +00:00)")
    return dt


def _config_epoch() -> datetime | None:
    """Default --since to late_window.validation_epoch from settings.yaml, so the
    manual read and the nightly health job scope to the same binding population.
    Pre-parsed: argparse applies ``type=`` only to CLI-provided values."""
    try:
        from polybot.config.loader import load_config
        epoch = load_config().get("late_window", {}).get("validation_epoch")
        return _parse_since(epoch) if epoch else None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Late-window sniper paper-shadow status.")
    ap.add_argument("--since", type=_parse_since, default=_config_epoch(),
                    help="ISO timestamp (tz-aware); only count fills resolved at/after it. "
                         "Defaults to late_window.validation_epoch from settings.yaml.")
    args = ap.parse_args()

    # load_all_outcomes dedups by (position_id, market_id) — a fill present in
    # both its per-trade file and a partial daily rollup counts once, which the
    # shadow-vs-harness kill-bar comparison depends on.
    outcomes = OutcomeReviewer(
        str(_ROOT / "polybot" / "memory" / "outcomes")).load_all_outcomes()
    snipes = []
    skipped_pre_epoch = 0
    for t in outcomes:
        ctx = (t.get("indicator_snapshot") or {}).get("trade_context") or {}
        if ctx.get("entry_phase") != "late_sniper":
            continue
        ts = t.get("timestamp") or ""
        if args.since is not None:
            try:
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) < args.since:
                    skipped_pre_epoch += 1
                    continue
            except ValueError:
                skipped_pre_epoch += 1     # unparseable ts can't be proven in-window → drop
                continue
        # ET day buckets — must match the harness's ET day-clustering (a UTC
        # [:10] slice would shift every 20:00-24:00 ET fill into the next day).
        snipes.append(dict(day=utc_ts_to_et_date(ts),
                           correct=t.get("correct"),
                           pnl=t.get("pnl"), gain=t.get("gain_pct"),
                           reason=t.get("exit_reason")))
    if args.since is not None:
        print(f"epoch: fills resolved >= {args.since.isoformat()} "
              f"({skipped_pre_epoch} pre-epoch sniper fills excluded)\n")
    if not snipes:
        print("No sniper paper fills in scope yet. The shadow fires only in the final 45s "
              "on a Coinbase move past strike with a still-cheap ask (a handful/day). Check "
              "the bot log for 'LATE-SNIPER fire' lines to confirm it is active.")
        return

    byday = defaultdict(list)
    for s in snipes:
        byday[s["day"]].append(s)
    print(f"{'day':>12} {'fills':>6} {'win%':>6} {'net/sh':>9} {'pnl$':>10}")
    tot_pnl = tot_n = wins = 0
    all_gains = []
    for d in sorted(byday):
        v = byday[d]
        n = len(v)
        w = sum(1 for s in v if s["correct"])
        pnl = sum(s["pnl"] or 0.0 for s in v)
        gains = [s["gain"] for s in v if s["gain"] is not None]
        all_gains += gains
        ns = statistics.mean(gains) if gains else float("nan")
        print(f"{d:>12} {n:>6} {w / n:>6.0%} {ns:>+9.4f} {pnl:>+10.2f}")
        tot_pnl += pnl
        tot_n += n
        wins += w
    pooled = statistics.mean(all_gains) if all_gains else float("nan")
    print(f"{'TOTAL':>12} {tot_n:>6} {wins / tot_n:>6.0%} {pooled:>+9.4f} {tot_pnl:>+10.2f}")
    print(f"\n{len(byday)} day(s) of shadow data — kill bar needs >=8 clean days, "
          f"equal-weight net/sh >= +0.02, t_day>=2, p10>0, shadow-vs-harness gap < 0.03. "
          f"Compare net/sh to analyze_late_window.py --rtt-sweep 0.135 (paper sim latency).")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
