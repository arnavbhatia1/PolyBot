"""Replay recorded scalp/hold decisions through candidate exit policies.

Each counterfactual record carries both arms (actual + counterfactual pnl)
plus the decision-moment context, so a candidate policy can be scored exactly:
pick its arm per record, sum the dollars. Fire criterion is the shared
effective_exit_threshold (same code live uses). Loss-cut scalps fire
independently of the threshold and always keep their actual arm.

Caveat: hold records snapshot only the single worst (most scalp-tempting)
moment; a candidate more patient than live could still be misjudged on
moments live never recorded. Thresholds MORE patient than -0.10 are therefore
only bounded here, not exactly scored.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.core.exit_boundary import effective_exit_threshold  # noqa: E402
from scripts.diagnose_edge import load_records  # noqa: E402


def build():
    # entry price + side per position, for the deep-loss-hold branch test
    entry_by_pid = {}
    for t in load_records("outcomes"):
        pid = t.get("position_id")
        if pid is not None and t.get("entry_price"):
            entry_by_pid[pid] = (t["entry_price"], t.get("side", ""))
    recs = []
    for c in load_records("counterfactuals"):
        actual, cf = c.get("actual") or {}, c.get("counterfactual") or {}
        kind = "hold" if actual.get("exit_reason") == "hold" else "scalp"
        ctx = c.get("context_at_scalp") or c.get("context_at_worst_moment") or {}
        if not ctx or actual.get("pnl") is None or cf.get("pnl") is None:
            continue
        he, sr = ctx.get("holding_edge"), ctx.get("seconds_remaining")
        mp = ctx.get("market_price")
        if he is None or sr is None or mp is None or not (0 < mp < 1):
            continue
        entry, side = entry_by_pid.get(c.get("position_id"), (None, c.get("side", "")))
        recs.append(dict(kind=kind, he=he, sr=sr, mp=mp,
                         model_prob=ctx.get("model_prob"),
                         loss_cut=bool(ctx.get("loss_cut")),
                         dist=ctx.get("btc_distance_atr"),
                         entry=entry, side=side or c.get("side", ""),
                         act=actual["pnl"], cf=cf["pnl"],
                         day=(c.get("timestamp") or "")[:10]))
    return recs


def policy_pnl(recs, thr):
    """Branch-faithful symmetric replay of the live scalp/hold exit decision:
    scalp records un-fire when the candidate's blended threshold wouldn't have
    (loss-cuts never re-priced); hold records flip to their worst-moment
    hypothetical scalp when the candidate WOULD have fired there — unless the
    whipsaw cushion or deep-loss-hold branch (threshold-independent) held live."""
    total = 0.0
    fired = 0
    for r in recs:
        eff = effective_exit_threshold(thr, r["sr"], r["mp"])
        if r["kind"] == "scalp":
            fire = r["loss_cut"] or r["he"] <= eff
            total += r["act"] if fire else r["cf"]
            fired += fire
            continue
        dist = r["dist"]
        wrong_side = dist is not None and (
            (r["side"] == "Up" and dist < 0) or (r["side"] == "Down" and dist > 0))
        whipsaw = wrong_side and abs(dist) <= 0.5
        deep_loss = (r["he"] < -0.10 and r["entry"] is not None and r["mp"] < r["entry"])
        if not whipsaw and not deep_loss and r["he"] <= eff:
            total += r["cf"]
            fired += 1
        else:
            total += r["act"]
    return total, fired


def main() -> None:
    recs = build()
    n_scalp = sum(r["kind"] == "scalp" for r in recs)
    print(f"records: {len(recs)} (scalp {n_scalp}, hold {len(recs) - n_scalp})")

    actual_total = sum(r["act"] for r in recs)
    oracle = sum(max(r["act"], r["cf"]) for r in recs)
    always_hold = sum(r["cf"] if r["kind"] == "scalp" else r["act"] for r in recs)
    always_scalp = sum(r["act"] if r["kind"] == "scalp" else r["cf"] for r in recs)
    print(f"actual policy: {actual_total:+.2f}   oracle: {oracle:+.2f}   "
          f"always-hold: {always_hold:+.2f}   always-scalp-at-worst: {always_scalp:+.2f}")

    days = sorted({r["day"] for r in recs})
    half = days[len(days) // 2]
    first = [r for r in recs if r["day"] < half]
    second = [r for r in recs if r["day"] >= half]

    print(f"\n{'thr':>6} {'pnl_all':>9} {'pnl_first':>10} {'pnl_second':>11} {'fired':>6}")
    for thr in (-0.14, -0.12, -0.10, -0.08, -0.06, -0.05, -0.04, -0.03):
        pa, fired = policy_pnl(recs, thr)
        pf, _ = policy_pnl(first, thr)
        ps, _ = policy_pnl(second, thr)
        print(f"{thr:>6.2f} {pa:>+9.2f} {pf:>+10.2f} {ps:>+11.2f} {fired:>6}")

    # where does the oracle's headroom live?
    print("\n== oracle headroom by segment (oracle - actual, $) ==")
    segs = [("scalp ITM (mp>=0.6)", lambda r: r["kind"] == "scalp" and r["mp"] >= 0.6),
            ("scalp ATM (0.4-0.6)", lambda r: r["kind"] == "scalp" and 0.4 <= r["mp"] < 0.6),
            ("scalp OTM (mp<0.4)", lambda r: r["kind"] == "scalp" and r["mp"] < 0.4),
            ("hold ITM (mp>=0.6)", lambda r: r["kind"] == "hold" and r["mp"] >= 0.6),
            ("hold ATM (0.4-0.6)", lambda r: r["kind"] == "hold" and 0.4 <= r["mp"] < 0.6),
            ("hold OTM (mp<0.4)", lambda r: r["kind"] == "hold" and r["mp"] < 0.4)]
    for name, f in segs:
        b = [r for r in recs if f(r)]
        if not b:
            continue
        gap = sum(max(r["act"], r["cf"]) - r["act"] for r in b)
        wrong = sum(1 for r in b if r["cf"] > r["act"])
        print(f"  {name:>22}: n={len(b):>5}  headroom={gap:>+9.2f}  wrong_calls={wrong}")

    # what separates wrong ITM scalps (should have held) from right ones?
    print("\n== scalp ITM (mp>=0.6): wrong (cf>act) vs right, feature means ==")
    itm = [r for r in recs if r["kind"] == "scalp" and r["mp"] >= 0.6 and not r["loss_cut"]]
    for label, b in [("wrong->hold", [r for r in itm if r["cf"] > r["act"]]),
                     ("right->scalp", [r for r in itm if r["cf"] <= r["act"]])]:
        if not b:
            continue
        mean = lambda k: sum(r[k] for r in b if r[k] is not None) / max(
            1, sum(1 for r in b if r[k] is not None))
        print(f"  {label:>13}: n={len(b):>4}  he={mean('he'):+.3f}  mp={mean('mp'):.3f}  "
              f"model_p={mean('model_prob'):.3f}  sec_rem={mean('sr'):.0f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
