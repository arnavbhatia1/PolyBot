"""Long-horizon crypto calibration harness — measurement only, no capital, no trading.

Tests whether Polymarket's longer-horizon crypto markets (daily up/down ladders,
weekly/monthly/yearly "hit $X" touch ladders) carry a tradeable calibration edge, gated
behind a pre-registered kill bar. The >1-week zone is NOT retrospectively measurable
(closed-market CLOB history is truncated to ~the last week), so prices are recorded
FORWARD; the Deribit options cross-check can kill the edge in a single snapshot.

Standalone — its own gitignored sqlite DB (polybot/db/calibration.db); never touches the
trading DBs and is NOT auto-launched by run_polybot.ps1. Operator-invoked:

  python scripts/calibration_harness.py ivcheck    # instant 'already arbed?' test (no DB)
  python scripts/calibration_harness.py snapshot    # record one forward snapshot pass
  python scripts/calibration_harness.py label        # read resolution for settled markets
  python scripts/calibration_harness.py analyze       # calibration + kill-bar verdicts
  python scripts/calibration_harness.py status         # DB row counts
  python scripts/calibration_harness.py monitor         # continuous snapshot+label loop

Forward accumulation runs automatically as a supervised child of run_polybot.ps1 (the
`monitor` command). You don't run anything by hand — just `analyze` (or `ivcheck`) whenever
you want the current read.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.calibration import harness  # noqa: E402
from polybot.calibration.store import CalibrationStore, DEFAULT_DB_PATH  # noqa: E402


def _fmt(x, nd=3):
    return f"{x:+.{nd}f}" if isinstance(x, (int, float)) else "n/a"


def _print_stat(name, st):
    if not st:
        print(f"    {name:<16} n/a (thin)")
        return
    ci = (f"[{_fmt(st['lo'])},{_fmt(st['hi'])}]"
          if st.get("lo") is not None else "[CI n/a]")
    t = f"t={st['t']:.2f}" if st.get("t") is not None else "t=n/a"
    print(f"    {name:<16} {_fmt(st['base'])} {ci} {t}  p10={_fmt(st.get('p10'))}  "
          f"n={st['n']} events={st['nclusters']}")


async def _run(cmd: str, db_path: str, max_pages: int,
               snapshot_interval: float = 3600.0, label_interval: float = 43200.0) -> int:
    if cmd == "ivcheck":
        rep = await harness.ivcheck_pass(max_pages=max_pages)
        spot = f"${rep['spot']:,.0f}" if rep.get("spot") else "n/a"
        print(f"\nspot={spot}   rows={rep['n_rows']}")
        print("\n== Deribit options cross-check (PM ask - option-implied) ==")
        for fam, r in rep["cross_check"].items():
            print(f"\n  [{fam}]  n={r['n']}  -> {r['verdict']}")
            for band, st in r["bands"].items():
                _print_stat(band, st)
        # per-rung table, grouped by family, sorted by strike
        rows = sorted(rep["rows"], key=lambda x: (x["family"], x["strike"] or 0))
        print("\n== per-rung (PM ask vs option-implied) ==")
        print(f"  {'family':<16}{'strike':>10} {'side':>5} {'PM_ask':>7} {'opt_impl':>9} {'PM-opt':>8}")
        for x in rows:
            if x.get("iv_implied") is None:
                continue
            diff = x["pm_ask"] - x["iv_implied"]
            flag = "  <-- PM rich" if diff > 0.05 else ("  <-- PM cheap" if diff < -0.05 else "")
            print(f"  {x['family']:<16}{(x['strike'] or 0):>10,.0f} {x['side']:>5} "
                  f"{x['pm_ask']:>7.3f} {x['iv_implied']:>9.3f} {diff:>+8.3f}{flag}")
        return 0

    store = CalibrationStore(db_path)
    await store.initialize()
    try:
        if cmd == "snapshot":
            rep = await harness.snapshot_pass(store, max_pages=max_pages)
            spot = f"${rep['spot']:,.0f}" if rep.get("spot") else "n/a"
            print(f"snapshot: discovered={rep['discovered']} stored={rep['snapshotted']} "
                  f"spot={spot} surfaces={rep['surfaces']}")
            print(f"  by family: {rep['by_family']}")
        elif cmd == "label":
            rep = await harness.label_pass(store)
            print(f"label: checked={rep['checked']} labeled={rep['labeled']}")
        elif cmd == "monitor":
            await harness.monitor_loop(store, snapshot_interval_s=snapshot_interval,
                                       label_interval_s=label_interval, max_pages=max_pages)
        elif cmd == "status":
            print(f"status ({db_path}): {await store.status()}")
        elif cmd == "analyze":
            rep = await harness.analyze_pass(store)
            print(f"\nstatus: {rep['status']}")
            print("\n== instant IV cross-check (latest snapshots) ==")
            for fam, r in rep["iv_cross_check"].items():
                print(f"  [{fam}] n={r['n']} -> {r['verdict']}")
            print("\n== calibration + kill bar (resolved markets) ==")
            if not rep["families"]:
                print("  (no resolved markets yet — run `snapshot` over time, then `label`)")
            for key, f in rep["families"].items():
                print(f"\n  === {key}  n={f['n']} events={f['nclusters']} ===")
                print(f"    VERDICT: {f['verdict']}")
                _print_stat("slope_b", f["slope"])
                _print_stat("favorite_net", f["favorite_net"])
                _print_stat("fade_net", f["fade_net"])
                _print_stat("iv_gap_fav", f["iv_gap_favorite"])
                _print_stat("iv_gap_long", f["iv_gap_longshot"])
                if f["reliability"]:
                    print(f"    reliability: " + "  ".join(
                        f"[{b['lo']:.2f},{b['hi']:.2f}) n={b['n']} wr={b['win_rate']:.2f} "
                        f"gap={_fmt(b['gap'])}" for b in f["reliability"]))
    finally:
        await store.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Long-horizon crypto calibration harness")
    ap.add_argument("command",
                    choices=["ivcheck", "snapshot", "label", "analyze", "status", "monitor"])
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="calibration DB path")
    ap.add_argument("--max-pages", type=int, default=8,
                    help="Gamma discovery pages (100 events each)")
    ap.add_argument("--snapshot-interval", type=float, default=3600.0,
                    help="monitor: seconds between snapshots (default 1h)")
    ap.add_argument("--label-interval", type=float, default=43200.0,
                    help="monitor: seconds between resolution labelings (default 12h)")
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.command, args.db, args.max_pages,
                              args.snapshot_interval, args.label_interval)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
