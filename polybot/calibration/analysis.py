"""Calibration statistics + the pre-registered kill bar.

Drift-robust headline = the logit calibration slope (b>1 = underconfident/favorites
underpriced; b<1 = overconfident). Inference is EVENT-CLUSTERED: within one ladder all
rungs are one correlated bet, so the independent unit is the resolution event (slug),
NOT the rung — clustering by rung would manufacture significance (the t_day -4.3 lesson).

Kill bar (from the adversarial pass), evaluated per coin/family x lead-bucket once there
are >= MIN_CLUSTERS independent resolution events (else ACCUMULATING):
  * net edge per share, both tradeable directions:
      buy-favorite&hold : WR - ask - fee(ask)
      fade-longshot&hold: yes_bid - WR - fee   (buy the NO of an overpriced Yes)
  * CONFIRM a direction whose net edge p10 > NET_EDGE_MIN AND clustered t >= T_MIN AND the
          IV cross-check agrees in that direction (favorites: PM<IV; longshots: PM>IV).
  * KILL    if the IV cross-check says options-fair (|gap| < EXEC_COST in both bands; already
          arbed), OR neither direction is significant (net edge not > NET_EDGE_MIN at t >= T_MIN).
  * UNDETERMINED if a direction is significant on resolution but options disagree (conflict).

The clustered t = base/se uses a sqrt(G/(G-1)) small-G inflation, but t >= 2 remains a normal
approximation that is still mildly anti-conservative near the G = MIN_CLUSTERS floor (~100+
events is ideal to have seen the rare loss tail). CI bounds are percentile-bootstrap; t is Wald.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

from polybot.execution.base import DEFAULT_FEE_RATE

# kill-bar thresholds (pre-registered)
NET_EDGE_MIN = 0.02       # >~2c/share at favorite prices to clear spread+carry
T_MIN = 2.0               # event-clustered t
MIN_CLUSTERS = 20         # independent resolution events before any verdict (tail caveat: ~100+ ideal)
EXEC_COST = 0.015         # half-spread+fee proxy for the IV "are they equal" band
FAV_BAND = (0.85, 0.98)
LONGSHOT_BAND = (0.02, 0.20)
LEAD_BUCKETS = [(0, 86400, "<1d"), (86400, 7 * 86400, "1-7d"),
                (7 * 86400, 30 * 86400, "7-30d"), (30 * 86400, 90 * 86400, "30-90d"),
                (90 * 86400, 10 ** 12, ">90d")]


def fee_per_share(p: float) -> float:
    return DEFAULT_FEE_RATE * p * (1.0 - p)


def brier(rows: list[tuple[float, int]]) -> float | None:
    return sum((p - w) ** 2 for p, w in rows) / len(rows) if rows else None


def logloss(rows: list[tuple[float, int]]) -> float | None:
    eps = 1e-6
    if not rows:
        return None
    return -sum(math.log(max(p, eps)) if w else math.log(max(1 - p, eps))
                for p, w in rows) / len(rows)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def logistic_slope(pairs: list[tuple[float, int]], iters: int = 60) -> float | None:
    """Slope b of outcome ~ a + b*logit(price), Newton-Raphson. b=1 => calibrated."""
    if len(pairs) < 25:
        return None
    xs = [_logit(p) for p, _ in pairs]
    ys = [float(y) for _, y in pairs]
    a, b = 0.0, 1.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            z = max(min(a + b * x, 30.0), -30.0)
            pr = 1.0 / (1.0 + math.exp(-z))
            w = pr * (1.0 - pr)
            e = pr - y
            ga += e; gb += e * x
            haa += w; hab += w * x; hbb += w * x * x
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (-hab * ga + haa * gb) / det
        a -= da; b -= db
        if abs(da) + abs(db) < 1e-9:
            break
    return b


def clustered_bootstrap(rows: list[dict], statfn, cluster_key: str = "slug",
                        nboot: int = 1500, seed: int = 13):
    """Block-bootstrap a statistic by resampling whole clusters (events). Returns
    dict(base, lo, hi, p10, se, t, n, nclusters) or None if the stat is undefined."""
    base = statfn(rows)
    if base is None:
        return None
    by_cluster: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cluster[r.get(cluster_key, "_")].append(r)
    clusters = list(by_cluster)
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(nboot):
        samp: list[dict] = []
        for _ in range(len(clusters)):
            samp.extend(by_cluster[rng.choice(clusters)])
        v = statfn(samp)
        if v is not None:
            boots.append(v)
    if len(boots) < 50:
        return dict(base=base, lo=None, hi=None, p10=None, se=None, t=None,
                    n=len(rows), nclusters=len(clusters))
    boots.sort()
    mean = sum(boots) / len(boots)
    se = (sum((v - mean) ** 2 for v in boots) / (len(boots) - 1)) ** 0.5
    # finite-cluster inflation: the pairs-cluster bootstrap SE is downward-biased for few
    # clusters (G), so the Wald t is anti-conservative near the MIN_CLUSTERS floor. Scale by
    # sqrt(G/(G-1)) (the standard CRVE small-G correction) before forming t.
    g = len(clusters)
    if g > 1:
        se *= (g / (g - 1)) ** 0.5
    return dict(base=base,
                lo=boots[int(0.025 * len(boots))],
                hi=boots[int(0.975 * len(boots))],
                p10=boots[int(0.10 * len(boots))],
                se=se,
                t=(base / se if se and se > 0 else None),
                n=len(rows), nclusters=len(clusters))


# ── statistics over row sets (each row: pm_ask, pm_bid, outcome, slug) ──

def _slope_stat(rows):
    return logistic_slope([(r["pm_ask"], r["outcome"]) for r in rows
                           if r.get("pm_ask") is not None and r.get("outcome") is not None])


def _favorite_net_stat(rows):
    sel = [r for r in rows if r.get("outcome") is not None
           and FAV_BAND[0] <= r["pm_ask"] < FAV_BAND[1]]
    if len(sel) < 10:
        return None
    wr = sum(r["outcome"] for r in sel) / len(sel)
    ask = sum(r["pm_ask"] for r in sel) / len(sel)
    return wr - ask - fee_per_share(ask)


def _fade_net_stat(rows):
    """Buy the NO of an overpriced Yes-longshot and hold: net = yes_bid - WR - fee."""
    sel = [r for r in rows if r.get("outcome") is not None and r.get("pm_bid") is not None
           and LONGSHOT_BAND[0] <= r["pm_ask"] < LONGSHOT_BAND[1]]
    if len(sel) < 10:
        return None
    wr = sum(r["outcome"] for r in sel) / len(sel)
    yes_bid = sum(r["pm_bid"] for r in sel) / len(sel)
    return yes_bid - wr - fee_per_share(1.0 - yes_bid)


def _iv_gap_stat_factory(lo, hi):
    def f(rows):
        sel = [r for r in rows if r.get("iv_implied") is not None
               and lo <= r["pm_ask"] < hi]
        if len(sel) < 8:
            return None
        return sum(r["pm_ask"] - r["iv_implied"] for r in sel) / len(sel)
    return f


def reliability(rows: list[dict],
                edges=(0, .05, .15, .30, .50, .70, .85, .95, 1.0001)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [r for r in rows if r.get("outcome") is not None and lo <= r["pm_ask"] < hi]
        if not sel:
            continue
        mp = sum(r["pm_ask"] for r in sel) / len(sel)
        wr = sum(r["outcome"] for r in sel) / len(sel)
        out.append(dict(lo=lo, hi=hi, n=len(sel), mean_ask=mp, win_rate=wr, gap=wr - mp))
    return out


def iv_cross_check(snapshot_rows: list[dict]) -> dict:
    """The instant 'already arbed?' test — needs only snapshots (no resolution).
    Per coin/family: mean(pm_ask - iv_implied) by band, and whether |gap| < EXEC_COST
    (=> arbed). Only rows with iv_implied (i.e. coins with a Deribit surface) qualify."""
    by_fam: dict[str, list[dict]] = defaultdict(list)
    for r in snapshot_rows:
        if r.get("iv_implied") is not None:
            by_fam[f"{r.get('coin') or '?'}/{r.get('family') or '?'}"].append(r)
    report = {}
    for fam, rows in by_fam.items():
        bands = {}
        for name, (lo, hi) in {"longshot": LONGSHOT_BAND, "mid": (0.20, 0.80),
                               "favorite": FAV_BAND}.items():
            st = clustered_bootstrap(rows, _iv_gap_stat_factory(lo, hi))
            if st:
                bands[name] = st
        gaps = [b["base"] for b in bands.values() if b.get("base") is not None]
        arbed = bool(gaps) and all(abs(g) < EXEC_COST for g in gaps)
        report[fam] = dict(bands=bands, n=len(rows),
                           verdict=("OPTIONS-FAIR (arbed; no edge)" if arbed
                                    else "deviates from options — keep recording"))
    return report


def _verdict(fav, fade, iv_gap_fav, iv_gap_long, nclusters: int) -> str:
    """Faithful kill bar. Needs MIN_CLUSTERS independent events for any verdict, then:
      CONFIRM   a direction with net-edge p10>NET_EDGE_MIN AND clustered t>=T_MIN AND the
                IV cross-check agreeing in that direction.
      KILL      if the IV cross-check says options-fair (|gap|<EXEC_COST both bands), OR
                neither direction is significant (net edge not >NET_EDGE_MIN at t>=T_MIN).
      UNDETERMINED a direction is significant on resolution but options disagree (conflict).
    """
    if nclusters < MIN_CLUSTERS:
        return f"ACCUMULATING (n_events={nclusters}<{MIN_CLUSTERS})"

    def significant(stat) -> bool:
        return bool(stat and stat.get("p10") is not None and stat["p10"] > NET_EDGE_MIN
                    and stat.get("t") is not None and stat["t"] >= T_MIN)

    fav_sig, fade_sig = significant(fav), significant(fade)
    # IV cross-check must agree with the trade direction:
    #   favorites underpriced  => PM ask < option-implied => gap < -EXEC_COST
    #   longshots overpriced (fade) => PM ask > option-implied => gap > +EXEC_COST
    iv_fav_ok = bool(iv_gap_fav and iv_gap_fav.get("base") is not None
                     and iv_gap_fav["base"] < -EXEC_COST)
    iv_fade_ok = bool(iv_gap_long and iv_gap_long.get("base") is not None
                      and iv_gap_long["base"] > EXEC_COST)
    if fav_sig and iv_fav_ok:
        return "CONFIRM: buy-favorite edge (resolution + options agree)"
    if fade_sig and iv_fade_ok:
        return "CONFIRM: fade-longshot edge (resolution + options agree)"

    # options-fair across both available bands => already arbed => dead immediately
    gaps = [g["base"] for g in (iv_gap_fav, iv_gap_long)
            if g and g.get("base") is not None]
    if gaps and all(abs(g) < EXEC_COST for g in gaps):
        return "KILL: options-fair (PM ~= option-implied; already arbed)"
    # adequate power, no significant net edge either direction (net<=threshold or t<2)
    if not fav_sig and not fade_sig:
        return "KILL: no significant net edge either direction (net<=threshold or t<2)"
    # a direction is significant on resolution but the options cross-check disagrees
    return "UNDETERMINED (resolution signal present but options cross-check disagrees)"


def evaluate(join_rows: list[dict], snapshot_rows: list[dict] | None = None) -> dict:
    """Full report: IV cross-check (snapshots) + resolution-based calibration & kill bar.

    join_rows: snapshots joined to resolutions; each has family, pricing_kind, lead_s,
               pm_ask, pm_bid, iv_implied, outcome (0/1), slug.
    snapshot_rows: all snapshots (for the instant IV check even before resolutions exist).
    """
    out: dict = {"iv_cross_check": iv_cross_check(snapshot_rows or join_rows),
                 "families": {}}
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in join_rows:
        if r.get("outcome") is None:
            continue
        lead = r.get("lead_s", 0) or 0
        bucket = next((name for lo, hi, name in LEAD_BUCKETS if lo <= lead < hi), ">90d")
        by_key[(f"{r.get('coin') or '?'}/{r.get('family') or '?'}", bucket)].append(r)
    for (fam, bucket), rows in sorted(by_key.items()):
        slope = clustered_bootstrap(rows, _slope_stat)
        fav = clustered_bootstrap(rows, _favorite_net_stat)
        fade = clustered_bootstrap(rows, _fade_net_stat)
        iv_fav = clustered_bootstrap([r for r in rows if r.get("iv_implied") is not None],
                                     _iv_gap_stat_factory(*FAV_BAND))
        iv_long = clustered_bootstrap([r for r in rows if r.get("iv_implied") is not None],
                                      _iv_gap_stat_factory(*LONGSHOT_BAND))
        nclusters = slope["nclusters"] if slope else len({r.get("slug") for r in rows})
        out["families"][f"{fam}/{bucket}"] = dict(
            n=len(rows), nclusters=nclusters, slope=slope,
            favorite_net=fav, fade_net=fade,
            iv_gap_favorite=iv_fav, iv_gap_longshot=iv_long,
            reliability=reliability(rows),
            verdict=_verdict(fav, fade, iv_fav, iv_long, nclusters),
        )
    return out
