"""One-off repair: delete chimera hold-CF records written by the (fixed) flip
mis-keying bug.

A hold-type counterfactual (actual.exit_reason == 'hold') is only ever written
when a HELD position resolves. One found under a position whose outcome was a
scalp is a chimera — worst-moment context/shares from the scalped position,
actual pnl from a different position that held the same window. Invalid for
either position, so it is removed (per-trade file deleted / rollup rewritten).

Dry-run by default; --apply mutates. Idempotent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.paths import COUNTERFACTUALS_DIR, OUTCOMES_DIR  # noqa: E402


def load_outcome_exit_reasons() -> dict[int, str]:
    reasons: dict[int, str] = {}
    for fp in sorted(OUTCOMES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for r in data if isinstance(data, list) else [data]:
            pid = r.get("position_id")
            if pid is not None:
                reasons[pid] = r.get("exit_reason") or "?"
    return reasons


def is_chimera(rec: dict, reasons: dict[int, str]) -> str | None:
    """Reason string when rec is a corrupt hold-CF, else None."""
    if (rec.get("actual") or {}).get("exit_reason") != "hold":
        return None
    pid = rec.get("position_id")
    reason = reasons.get(pid)
    if reason is None:
        return "hold_cf_without_outcome"
    if reason != "resolution":
        return f"hold_cf_under_{reason}_outcome"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate files (default: dry run)")
    args = ap.parse_args()

    reasons = load_outcome_exit_reasons()
    print(f"outcomes indexed: {len(reasons)}")

    removed: dict[str, int] = {}
    removed_pnl = 0.0
    files_deleted = rollups_rewritten = kept = 0

    for fp in sorted(COUNTERFACTUALS_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            bad = [(r, is_chimera(r, reasons)) for r in data]
            keep = [r for r, why in bad if why is None]
            drops = [(r, why) for r, why in bad if why is not None]
            if drops:
                for r, why in drops:
                    removed[why] = removed.get(why, 0) + 1
                    removed_pnl += float((r.get("actual") or {}).get("pnl") or 0)
                    print(f"  drop pid={r.get('position_id')} {fp.name}: {why} "
                          f"(actual.pnl={(r.get('actual') or {}).get('pnl')})")
                if args.apply:
                    tmp = fp.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(keep, indent=2), encoding="utf-8")
                    tmp.replace(fp)
                rollups_rewritten += 1
            kept += len(keep)
        else:
            why = is_chimera(data, reasons)
            if why is not None:
                removed[why] = removed.get(why, 0) + 1
                removed_pnl += float((data.get("actual") or {}).get("pnl") or 0)
                print(f"  drop pid={data.get('position_id')} {fp.name}: {why}")
                if args.apply:
                    fp.unlink()
                files_deleted += 1
            else:
                kept += 1

    total = sum(removed.values())
    print(f"\nchimeras found: {total} (actual.pnl sum {removed_pnl:+.2f})")
    for why, n in sorted(removed.items()):
        print(f"  {why}: {n}")
    print(f"records kept: {kept}  per-trade files deleted: {files_deleted}  "
          f"rollups rewritten: {rollups_rewritten}")
    print("APPLIED" if args.apply else "DRY RUN — re-run with --apply to mutate")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
