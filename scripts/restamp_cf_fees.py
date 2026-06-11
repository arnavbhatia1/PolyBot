"""One-off repair: bring pre-fee-fix counterfactual records onto the 0.07 basis.

The 06-02 fee correction restamped outcome pnl/fees to the true 0.07 coefficient
but never touched counterfactuals: their actual arm is the live pnl at 0.018 and
their hypothetical arm embeds 0.018-basis shares (the entry fee is paid in
shares, so shares_held was computed with the coefficient active at entry).

Per CF whose matching outcome carries fee_restamped:
- actual arm := the outcome's restamped pnl (gain_pct = pnl/size).
- hypothetical arm: recover the entry fill from the 0.018-implied shares
  (shares = size / (fill * (1 + r*(1-fill)))), recompute shares at 0.07, and
  re-price the arm (resolution arm fee-free at $0/$1; scalp arm nets the 0.07
  exit fee). delta_pnl / *_was_optimal recomputed; record flagged
  fee_restamped: 0.07.

Dry-run by default; --apply mutates. Idempotent (flag-guarded).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.paths import COUNTERFACTUALS_DIR, OUTCOMES_DIR  # noqa: E402

OLD_RATE = 0.018
NEW_RATE = 0.07


def load_outcomes() -> dict[int, dict]:
    outs: dict[int, dict] = {}
    for fp in sorted(OUTCOMES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for r in data if isinstance(data, list) else [data]:
            pid = r.get("position_id")
            if pid is not None:
                outs[pid] = r
    return outs


def fill_from_shares(size: float, shares: float, rate: float) -> float | None:
    """Invert shares = size / (fill * (1 + rate*(1-fill))) for fill in (0,1)."""
    if size <= 0 or shares <= 0:
        return None
    q = size / shares
    a, b = rate, -(1.0 + rate)
    disc = b * b - 4 * a * q
    if disc < 0:
        return None
    fill = (-b - math.sqrt(disc)) / (2 * a)
    return fill if 0.01 < fill < 0.99 else None


def shares_at(size: float, fill: float, rate: float) -> float:
    return size / (fill * (1.0 + rate * (1.0 - fill)))


def restamp(rec: dict, out: dict, skips: dict[str, int]) -> bool:
    """Mutate rec onto the 0.07 basis. False (with a skip reason) when the
    record can't be safely recomputed."""
    size = float(out.get("size") or 0)
    if size <= 0:
        skips["no_size"] += 1
        return False
    actual, cf = rec["actual"], rec["counterfactual"]
    old_actual_pnl = float(actual.get("pnl") or 0)

    if actual.get("exit_reason") == "hold":
        # Resolution arm is the actual; hypothetical is a scalp at the worst moment.
        wp = float(cf.get("exit_price") or 0)
        if not (0 < wp < 1):
            skips["bad_worst_price"] += 1
            return False
        res_win = float(actual.get("exit_price") or 0) == 1.0
        if res_win:
            shares_old = old_actual_pnl + size
        else:
            denom = wp * (1.0 - OLD_RATE * (1.0 - wp))
            shares_old = (float(cf.get("pnl") or 0) + size) / denom if denom > 0 else 0.0
        fill = fill_from_shares(size, shares_old, OLD_RATE)
        if fill is None:
            skips["fill_unrecoverable"] += 1
            return False
        shares_new = shares_at(size, fill, NEW_RATE)
        hypo_pnl = shares_new * wp * (1.0 - NEW_RATE * (1.0 - wp)) - size
        actual["pnl"] = round(float(out["pnl"]), 6)
        actual["gain_pct"] = round(float(out["pnl"]) / size, 6)
        cf["pnl"] = round(hypo_pnl, 6)
        cf["gain_pct"] = round(hypo_pnl / size, 6)
        rec["delta_pnl"] = round(actual["pnl"] - cf["pnl"], 6)
        rec["hold_was_optimal"] = actual["pnl"] >= cf["pnl"]
    else:
        # Scalp is the actual; hypothetical is hold-to-resolution (fee-free at $0/$1).
        res = cf.get("resolution_price")
        if res not in (0.0, 1.0):
            skips["bad_resolution_price"] += 1
            return False
        if res == 1.0:
            shares_old = float(cf.get("pnl") or 0) + size
            fill = fill_from_shares(size, shares_old, OLD_RATE)
            if fill is None:
                skips["fill_unrecoverable"] += 1
                return False
            hold_pnl = shares_at(size, fill, NEW_RATE) - size
        else:
            hold_pnl = -size
        actual["pnl"] = round(float(out["pnl"]), 6)
        actual["gain_pct"] = round(float(out["pnl"]) / size, 6)
        cf["pnl"] = round(hold_pnl, 6)
        cf["gain_pct"] = round(hold_pnl / size, 6)
        rec["delta_pnl"] = round(cf["pnl"] - actual["pnl"], 6)
        rec["scalp_was_optimal"] = actual["pnl"] >= cf["pnl"]

    rec["fee_restamped"] = NEW_RATE
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate files (default: dry run)")
    args = ap.parse_args()

    outs = load_outcomes()
    n_done = n_already = n_native = files_rewritten = 0
    skips: dict[str, int] = {k: 0 for k in
                             ("no_outcome", "no_size", "bad_worst_price",
                              "bad_resolution_price", "fill_unrecoverable")}
    actual_shift = cf_shift = 0.0

    for fp in sorted(COUNTERFACTUALS_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        records = data if isinstance(data, list) else [data]
        changed = False
        for rec in records:
            if rec.get("fee_restamped"):
                n_already += 1
                continue
            out = outs.get(rec.get("position_id"))
            if out is None:
                skips["no_outcome"] += 1
                continue
            if not out.get("fee_restamped"):
                n_native += 1
                continue
            before_a = float((rec.get("actual") or {}).get("pnl") or 0)
            before_c = float((rec.get("counterfactual") or {}).get("pnl") or 0)
            if restamp(rec, out, skips):
                actual_shift += rec["actual"]["pnl"] - before_a
                cf_shift += rec["counterfactual"]["pnl"] - before_c
                n_done += 1
                changed = True
        if changed and args.apply:
            tmp = fp.with_suffix(".json.tmp")
            payload = records if isinstance(data, list) else records[0]
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(fp)
            files_rewritten += 1

    print(f"restamped: {n_done}   already-flagged: {n_already}   native-0.07: {n_native}")
    print(f"skips: { {k: v for k, v in skips.items() if v} }")
    print(f"actual-arm pnl shift: {actual_shift:+.2f}   counterfactual-arm shift: {cf_shift:+.2f}")
    print(f"files rewritten: {files_rewritten}")
    print("APPLIED" if args.apply else "DRY RUN — re-run with --apply to mutate")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
