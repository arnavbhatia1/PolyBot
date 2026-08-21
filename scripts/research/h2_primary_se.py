"""Standard errors + full-corpus EW for the H2 primary (addendum to
h2_open_mispricing.py). Reuses its loaders; per-window realized net is the
unit of variance (one bet per window — fill-weighting banned).

Usage: python scripts/research/h2_primary_se.py
Writes: scripts/research/data/vps-0821/h2_primary_se.json
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from h2_open_mispricing import (DATA, DELTAS, MIN_FOK_USD, fee_per_share,
                                load_labels, load_snapshots, et_day)


def main() -> None:
    labels = load_labels()
    snaps = load_snapshots()
    out = {}
    for dl in DELTAS:
        nets_all, nets_score = [], []
        days_all = set()
        for ts, (up, fp, ptb) in sorted(labels.items()):
            if ptb is None:
                continue
            r = snaps.get(ts, {}).get(dl)
            if r is None or r["chainlink_price"] is None or r["chainlink_age_s"] is None \
                    or r["chainlink_age_s"] > 3.0:
                continue
            d = r["chainlink_price"] - ptb
            fav_up = d >= 0
            ask = r["ask_up"] if fav_up else r["ask_down"]
            asz = r["ask_sz_up"] if fav_up else r["ask_sz_down"]
            if ask is None or asz is None or not (0.0 < ask < 1.0) \
                    or ask * asz < MIN_FOK_USD:
                continue
            won = bool(up) == fav_up
            net = ((1.0 - ask) if won else -ask) - fee_per_share(ask)
            nets_all.append(net)
            days_all.add(et_day(ts))
        days = sorted(days_all)
        score_days = set(days[1::2])
        # recompute score-half nets with the day set derived from THIS delta's
        # observations (matches the primary script's split on shared days)
        for ts, (up, fp, ptb) in sorted(labels.items()):
            pass  # split below instead

        # simpler: split nets by day directly
        nets_all, nets_score = [], []
        for ts, (up, fp, ptb) in sorted(labels.items()):
            if ptb is None:
                continue
            r = snaps.get(ts, {}).get(dl)
            if r is None or r["chainlink_price"] is None or r["chainlink_age_s"] is None \
                    or r["chainlink_age_s"] > 3.0:
                continue
            d = r["chainlink_price"] - ptb
            fav_up = d >= 0
            ask = r["ask_up"] if fav_up else r["ask_down"]
            asz = r["ask_sz_up"] if fav_up else r["ask_sz_down"]
            if ask is None or asz is None or not (0.0 < ask < 1.0) \
                    or ask * asz < MIN_FOK_USD:
                continue
            won = bool(up) == fav_up
            net = ((1.0 - ask) if won else -ask) - fee_per_share(ask)
            nets_all.append(net)
            if et_day(ts) in score_days:
                nets_score.append(net)

        def stats(v):
            n = len(v)
            if n < 2:
                return {"n": n}
            m = sum(v) / n
            sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
            se = sd / math.sqrt(n)
            return {"n": n, "mean_cents": round(100 * m, 2),
                    "se_cents": round(100 * se, 2),
                    "t": round(m / se, 2) if se else None}

        out[dl] = {"score_half": stats(nets_score), "full_corpus": stats(nets_all)}
        print(dl, out[dl])

    (DATA / "h2_primary_se.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
