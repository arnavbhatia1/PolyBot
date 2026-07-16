"""Watch the late-window sniper PAPER-SHADOW.

Reports the sniper's realized paper fills and their net-of-fee P&L, day by day.
Run it any time once the shadow is live (sniper_enabled: true in paper). Fills are
sparse — the sniper only fires in the final 45s on a Coinbase move past strike with
a still-cheap ask — so expect a handful per day.

This is a THIN wrapper over analyze_late_window.live_health_read(PAPER_DB, epoch) —
the SAME reader the nightly health job uses — so the manual read and the binding
gate compute the IDENTICAL metric: fee-correct net $/share = pnl / shares_held
(pnl is already net of fees; see live_health_read). It reads the paper DB's audited
shares_held, not the outcome JSON (which has no shares field). Post-gut every paper
fill is a sniper fire (base entries are unconditionally suppressed), so the epoch
scope alone isolates the shadow population.

These are PAPER fills at the box's MEASURED order latency (paper_trader shim, p50
~0.30s after scale), so the right harness comparison is
`python scripts/analyze_late_window.py --rtt-sweep 0.44` (the Stockholm ledger's
measured POST RTT), NOT the optimistic 40ms read. Go-live still requires the full
kill bar: t_day>=2, p10>0 over >=8 clean ET days, with this shadow tracking the
harness to <3c/sh.

  python scripts/sniper_shadow_status.py
  python scripts/sniper_shadow_status.py --since 2026-07-08T17:15:00+00:00

--since scopes the read to fills resolved at/after an epoch (the clean-baseline
restart), excluding pre-fix fills that ran different code/config. Defaults to
late_window.validation_epoch from settings.yaml.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


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


def _load_harness():
    hp = _ROOT / "scripts" / "analyze_late_window.py"
    spec = importlib.util.spec_from_file_location("analyze_late_window", hp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser(description="Late-window sniper paper-shadow status.")
    ap.add_argument("--since", type=_parse_since, default=_config_epoch(),
                    help="ISO timestamp (tz-aware); only count fills resolved at/after it. "
                         "Defaults to late_window.validation_epoch from settings.yaml.")
    args = ap.parse_args()

    mod = _load_harness()
    since_iso = args.since.isoformat() if args.since else None
    r = mod.live_health_read(mod.PAPER_DB, since_iso)   # the ONE binding-gate reader
    if r is None or r["n_fills"] == 0:
        print("No sniper paper fills in scope yet. The shadow fires only in the final 45s "
              "on a Coinbase move past strike with a still-cheap ask (a handful/day). Check "
              "the bot log for 'SNIPE' lines to confirm it is active.")
        return

    if since_iso:
        print(f"epoch: fills resolved >= {since_iso}  (paper DB, sniper-only)\n")
    print(f"{'day':>12} {'fills':>6} {'win%':>6} {'net/sh':>9} {'pnl$':>10}")
    for day, n, win, nps, pnl in r["day_detail"]:
        print(f"{day:>12} {n:>6} {win:>6.0%} {nps:>+9.4f} {pnl:>+10.2f}")
    tot_pnl = sum(d[4] for d in r["day_detail"])
    print(f"{'TOTAL':>12} {r['n_fills']:>6} {r['win_rate']:>6.0%} "
          f"{r['net_per_sh']:>+9.4f} {tot_pnl:>+10.2f}")

    t4 = "n/a (<4d)" if r["trailing4_mean"] is None else f"{r['trailing4_mean']*100:+.1f}c/sh"
    t8 = "n/a (<8d)" if r["trailing8_t"] is None else f"t {r['trailing8_t']:+.2f}"
    print(f"\nnet/sh = fee-correct net $/share (pnl/shares_held), equal-weight, ET-day-clustered "
          f"-- the SAME unit the nightly health job and the harness use.")
    print(f"t_day {r['t_day']:+.2f}, p10 {r['p10']:+.4f}, days+ {r['days_pos']}/{r['n_days']}, "
          f"last-4 {t4}, last-8 {t8}.")
    print(f"\n{r['n_days']} day(s) of shadow data -- kill bar needs >=8 clean days, "
          f"equal-weight net/sh >= +0.02, t_day>=2, p10>0, AND shadow-vs-harness gap < 0.03. "
          f"Compare net/sh to `analyze_late_window.py --rtt-sweep 0.44` (the box's measured RTT).")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
