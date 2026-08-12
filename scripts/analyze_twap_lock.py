"""TWAP lock kill-bar harness: replay the lock-dip signal over the event-true
micro-tape, net of fee, with FOK reachability modeled.

Method (lookahead-safe): per window, tick through every raw Chainlink report
and every locked-side book change inside the final-30s averaging zone using
only info <= t. Fire at the first tick where the projection's displacement
clears the frozen margin (signal_engine.TWAP_MARGIN_*) AND the locked side's
ask <= tier cap (tier_prob - min_edge). The FOK is reachable only if the ask
is still within the one-tick pad at decision + RTT — otherwise it's a KILL
and the scan re-arms (live semantics). Settle at the window_labels truth.

CEILING, NOT AUTHORITY: fills book the decision ask — queue depth and
sub-RTT reprices inside the pad are invisible here. The BINDING gate is the
paper-shadow's REALIZED fills (sniper_shadow_status.py / live_health_read).
Never deploy on this print alone.

The same pass verifies the resolution mechanism: every t-record at a labeled
boundary must equal the served price_to_beat/final_price bit-exact, and each
close must chain into the next strike. Any mismatch prints loudly — it means
Polymarket changed the rule again.

  python scripts/analyze_twap_lock.py [--days 8] [--min-edge 0.04] [--rtt 0.45]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
# Standalone runs from any cwd still need polybot.* importable (margin tables).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polybot.core.signal_engine import (      # noqa: E402
    TWAP_MARGIN_MAX, TWAP_MARGIN_P995,
    TWAP_PROB_DETERMINISTIC, TWAP_PROB_P995, twap_margin,
)

LIVE_DB = ROOT / "polybot" / "db" / "polybot_live.db"
PAPER_DB = ROOT / "polybot" / "db" / "polybot_paper.db"
LABEL_DBS = [PAPER_DB, LIVE_DB]   # labels accrue in the ACTIVE mode's DB — read both
RECORDINGS = ROOT / "polybot" / "memory" / "recordings"
ET = ZoneInfo("America/New_York")
FEE_RATE = 0.07
TWAP_SWITCH_TS = 1786060800       # 2026-08-07 00:00 UTC — never score earlier windows
ZONE_S = 30.0
K_MIN_S = 0.8
DEFAULT_RTT = 0.45                # conservative decision->exchange leg for reachability
FOK_PAD = 0.01                    # one tick — the live sniper_fok_slip


def fee(p: float) -> float:
    return FEE_RATE * p * (1 - p)


def et_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


def tstat(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def load_labels(since_ts: float) -> dict[int, dict]:
    """window epoch -> label row (strike, final, side, tokens), TWAP era only."""
    out: dict[int, dict] = {}
    for db_path in LABEL_DBS:
        if not Path(db_path).exists():
            continue
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT * FROM window_labels WHERE window_id LIKE 'btc-updown-5m-%'").fetchall()
        except sqlite3.OperationalError:
            db.close()
            continue
        for r in rows:
            try:
                ep = int(r["window_id"].rsplit("-", 1)[1])
            except (ValueError, IndexError):
                continue
            if ep < max(TWAP_SWITCH_TS, since_ts):
                continue
            out.setdefault(ep, dict(r))
        db.close()
    return out


def _tape_files(since_ts: float) -> list[Path]:
    # Never touch pre-TWAP tape: those files are 1.2-2.6 GB each and can't
    # contribute a label — streaming them hung the first nightly job into a
    # systemd SIGKILL and silenced the kill-rule ping.
    since_ts = max(since_ts, TWAP_SWITCH_TS)
    start = datetime.fromtimestamp(since_ts, timezone.utc).date()
    end = datetime.now(timezone.utc).date()
    files = []
    d = start
    while d <= end:
        # Finished days are gzipped by the nightly compress job (~39x); today's
        # is still plain. Prefer whichever exists.
        for suffix in (".jsonl", ".jsonl.gz"):
            p = RECORDINGS / f"micro_{d.isoformat()}{suffix}"
            if p.exists():
                files.append(p)
                break
        d += timedelta(days=1)
    return files


def _open_tape(path: Path):
    if path.suffix == ".gz":
        import gzip
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path)


def load_windows(labels: dict[int, dict], since_ts: float):
    """Stream the micro-tape once; bucket per-window raw reports, official TWAP
    boundary values, and both sides' book (ask) events for the final zone."""
    toks: dict[str, tuple[int, int]] = {}
    for ep, lab in labels.items():
        toks[lab["token_up"]] = (ep, 1)
        toks[lab["token_down"]] = (ep, 0)
    eps = sorted(labels)
    lrec: dict[int, list] = {ep: [] for ep in eps}
    trec: dict[int, dict] = {ep: {} for ep in eps}
    books: dict[int, dict[int, list]] = {ep: {1: [], 0: []} for ep in eps}
    for path in _tape_files(since_ts):
        with _open_tape(path) as f:
            for line in f:
                head = line[:12]
                if '"k": "b"' in head:
                    i = line.find('"token": "')
                    if i < 0:
                        continue
                    # token ids are variable-length uint256 decimals (76-78+
                    # digits) — slice generously, then cut at the close quote,
                    # or long-id tokens silently drop their whole book stream.
                    tok = line[i + 10: i + 120].split('"', 1)[0]
                    hit = toks.get(tok)
                    if hit is None:
                        continue
                    r = json.loads(line)
                    ep, side = hit
                    if ep + 300 - ZONE_S - 60 <= r["ts"] <= ep + 301:
                        try:
                            books[ep][side].append((r["ts"], float(r["ask"])))
                        except (ValueError, TypeError):
                            pass
                elif '"k": "l"' in head:
                    r = json.loads(line)
                    rx = r.get("rx") or r["ts"]
                    i = bisect.bisect_right(eps, rx) - 1
                    if i >= 0:
                        ep = eps[i]
                        if ep + 300 - ZONE_S - 10 <= rx <= ep + 301:
                            lrec[ep].append((rx, r["p"]))
                elif '"k": "t"' in head:
                    r = json.loads(line)
                    for ep in (int(r["ts"]) - 300, int(r["ts"])):
                        if ep in trec and (r["ts"] == ep or r["ts"] == ep + 300):
                            trec[ep][int(r["ts"])] = r["p"]
    return lrec, trec, books


def running_avg(recs: list, start: float, end: float) -> float | None:
    """rx-clock ZOH average — the exact estimator the frozen margins bind to."""
    seed = None
    pts = []
    for rx, p in recs:
        if rx <= start:
            seed = p
        elif rx <= end:
            pts.append((rx, p))
    if seed is None:
        if not pts or pts[0][0] > start + 2.0:
            return None
        seed = pts[0][1]
    acc, prev_t, prev_p = 0.0, start, seed
    for rx, p in pts:
        acc += prev_p * (rx - prev_t)
        prev_t, prev_p = rx, p
    acc += prev_p * (end - prev_t)
    return acc / (end - start) if end > start else prev_p


def _ask_at(seq: list, t: float) -> float | None:
    out = None
    for ts, a in seq:
        if ts <= t:
            out = a
        else:
            break
    return out


def replay_window(ep: int, lab: dict, recs: list, books: dict[int, list],
                  min_edge: float, rtt: float):
    """First reachable lock-dip fire in this window, live semantics (kills re-arm).

    Returns (fire dict | None, kills:int, locked_wrong:bool)."""
    close = ep + 300
    t0 = close - 30.0
    strike = lab["price_to_beat"]
    recs = sorted(recs)
    if len(recs) < 3 or not strike or strike <= 0:
        return None, 0, False
    # decision ticks: every raw report + every book event of either side
    ticks = sorted({rx for rx, _ in recs}
                   | {ts for side in (0, 1) for ts, _ in books[side]})
    kills = 0
    locked_wrong = False
    spot = None
    for t in ticks:
        k = close - t
        if k > ZONE_S or k < K_MIN_S:
            continue
        for rx, p in recs:
            if rx <= t:
                spot = p
            else:
                break
        if spot is None:
            continue
        avg = running_avg(recs, t0, t)
        if avg is None:
            continue
        w = (t - t0) / 30.0
        proj = w * avg + (1 - w) * spot
        disp = proj - strike
        side = 1 if disp >= 0 else 0
        adisp = abs(disp)
        m995 = twap_margin(TWAP_MARGIN_P995, k)
        if adisp < m995:
            continue
        tier_max = adisp >= twap_margin(TWAP_MARGIN_MAX, k)
        prob = TWAP_PROB_DETERMINISTIC if tier_max else TWAP_PROB_P995
        if side != lab["resolved_up"]:
            locked_wrong = True
        ask = _ask_at(books[side], t)
        if ask is None or not (0.0 < ask < 1.0) or prob - ask < min_edge:
            continue
        # FOK reachability: the ask at decision+RTT must still sit inside the pad
        landed = _ask_at(books[side], t + rtt)
        if landed is None or landed > ask + FOK_PAD:
            kills += 1
            continue
        win = side == lab["resolved_up"]
        net = (1.0 if win else 0.0) - ask - fee(ask)
        return ({"ep": ep, "k": k, "side": side, "ask": ask, "tier":
                 "max" if tier_max else "p995", "win": win, "net": net}, kills, locked_wrong)
    return None, kills, locked_wrong


def mechanism_check(labels: dict[int, dict], trec: dict[int, dict]) -> dict:
    """Bit-exact: t-record at open == served strike, at close == served final,
    and each close chains into the next strike."""
    s_ok = s_n = f_ok = f_n = c_ok = c_n = 0
    worst = 0.0
    for ep, lab in labels.items():
        s = trec.get(ep, {}).get(ep)
        fz = trec.get(ep, {}).get(ep + 300)
        if s is not None:
            s_n += 1
            d = abs(s - lab["price_to_beat"])
            worst = max(worst, d)
            s_ok += d < 1e-9
        if fz is not None:
            f_n += 1
            d = abs(fz - lab["final_price"])
            worst = max(worst, d)
            f_ok += d < 1e-9
        nxt = labels.get(ep + 300)
        if nxt:
            c_n += 1
            c_ok += abs(lab["final_price"] - nxt["price_to_beat"]) < 1e-9
    return {"strike_exact": (s_ok, s_n), "final_exact": (f_ok, f_n),
            "chain": (c_ok, c_n), "worst": worst}


def run_replay(since_ts: float, min_edge: float, rtt: float):
    labels = load_labels(since_ts)
    if not labels:
        return None
    lrec, trec, books = load_windows(labels, since_ts)
    fires, kills_total, breaches = [], 0, 0
    scored = 0
    for ep, lab in sorted(labels.items()):
        fire, kills, locked_wrong = replay_window(
            ep, lab, lrec[ep], books[ep], min_edge, rtt)
        scored += 1
        kills_total += kills
        breaches += locked_wrong
        if fire:
            fires.append(fire)
    mech = mechanism_check(labels, trec)
    days: dict[str, list[float]] = {}
    for f in fires:
        days.setdefault(et_day(f["ep"]), []).append(f["net"])
    day_means = [sum(v) / len(v) for v in days.values()]
    return {
        "n_windows": scored,
        "n_fills": len(fires),
        "kills": kills_total,
        "lock_breaches": breaches,
        "net_per_sh": (sum(f["net"] for f in fires) / len(fires)) if fires else 0.0,
        "win_rate": (sum(f["win"] for f in fires) / len(fires)) if fires else 0.0,
        "n_days": len(days),
        "days_pos": sum(1 for m in day_means if m > 0),
        "t_day": tstat(day_means),
        "fires": fires,
        "mechanism": mech,
    }


PATHS_DB = ROOT / "polybot" / "db" / "window_paths.db"



def ladder_recalibrate(days: int = 1, write: bool = False):
    """REPORT-ONLY: the trailing tape's dip-depth CDF (min winner-ask while
    max-tier locked), as quantiles of the dip minima. It never writes the
    ladder.

    Rung prices are set by BREAK-EVEN economics, not by dip frequency: a
    resting buy held to resolution breaks even at exactly the price paid, so a
    0.20 rung needs 20% against a measured 77-96% win rate. A dip-quantile
    estimator measures only how deep panic happened to reach in the trailing
    day, so it drags the deep rungs shallow — the direction that was already
    measured wrong. `write` is retained for call-shape parity and ignored."""
    since = max(datetime.now(timezone.utc).timestamp() - days * 86400.0,
                TWAP_SWITCH_TS)
    labels = load_labels(since)
    if not labels:
        return {"n_dips": 0, "applied": False}
    lrec, _trec, books = load_windows(labels, since)
    mins = []
    for ep, lab in sorted(labels.items()):
        recs = sorted(lrec[ep])
        strike = lab["price_to_beat"]
        close = ep + 300
        t0 = close - 30.0
        if len(recs) < 3 or not strike:
            continue
        lock_t = lock_side = None
        for rx, p in recs:
            k = close - rx
            if k > 25 or k < 1:
                continue
            avg = running_avg(recs, t0, rx)
            if avg is None:
                continue
            w = (rx - t0) / 30.0
            disp = w * avg + (1 - w) * p - strike
            if abs(disp) >= twap_margin(TWAP_MARGIN_MAX, k):
                lock_t, lock_side = rx, (1 if disp >= 0 else 0)
                break
        if lock_t is None or lock_side != lab["resolved_up"]:
            continue
        # A locked winner's ask usually sits AT 1.0 — that is a real "no dip"
        # observation and must stay in the denominator (requiring a < 1.0 here
        # once measured depth-given-dip instead of dip-given-locked and sent
        # every rung to the floor of the clamps).
        mn = None
        for ts, a in books[ep][lock_side]:
            if lock_t <= ts <= close and 0.0 < a <= 1.0:
                mn = a if mn is None else min(mn, a)
        if mn is not None:
            mins.append(mn)
    dips = sorted(m for m in mins if m <= 0.985)
    if len(mins) < 150 or len(dips) < 8:
        # Small samples move prices on noise (a partial-tape day once shifted
        # every rung on 53 windows) — the seed ladder stands.
        return {"n_locked": len(mins), "n_dips": len(dips), "applied": False}
    def q(f):
        return round(dips[min(int(f * len(dips)), len(dips) - 1)], 2)
    return {"n_locked": len(mins), "n_dips": len(dips), "applied": False,
            "dip_q": [q(0.10), q(0.25), q(0.50), q(0.75), q(0.90)]}


def health_read(db_path=None, min_edge: float = 0.04, days: int = 1):
    """Nightly-ping sim read: the lock-dip replay over the trailing tape days.
    db_path is accepted for call-shape parity and unused (labels are read from
    both mode DBs). Trailing window stays SHORT (context line, not the
    verdict): each tape day is ~2.5 GB and the job must finish well inside
    the 23:45-23:59 ET wind-down. Shape: n_fills / net_per_sh."""
    since = max(datetime.now(timezone.utc).timestamp() - days * 86400.0,
                TWAP_SWITCH_TS)
    r = run_replay(since, min_edge, DEFAULT_RTT)
    if r is None:
        return {"n_fills": 0, "net_per_sh": 0.0, "n_days": 0}
    r.pop("fires", None)
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--rtt", type=float, default=DEFAULT_RTT)
    args = ap.parse_args()
    since = datetime.now(timezone.utc).timestamp() - args.days * 86400.0
    r = run_replay(since, args.min_edge, args.rtt)
    if r is None:
        print("no TWAP-era labels yet")
        return
    m = r["mechanism"]
    print(f"windows {r['n_windows']}  fills {r['n_fills']}  kills {r['kills']}  "
          f"lock-breaches {r['lock_breaches']}")
    print(f"EW net {r['net_per_sh'] * 100:+.2f} c/sh  win {r['win_rate']:.0%}  "
          f"days {r['n_days']} ({r['days_pos']} positive)  "
          f"t_day {r['t_day'] if r['t_day'] is not None else float('nan'):+.2f}")
    print(f"mechanism: strike {m['strike_exact'][0]}/{m['strike_exact'][1]} exact, "
          f"final {m['final_exact'][0]}/{m['final_exact'][1]} exact, "
          f"chain {m['chain'][0]}/{m['chain'][1]}  (worst ${m['worst']:.4f})")
    if m["worst"] > 0.005:
        print("🚨 MECHANISM MISMATCH — Polymarket changed the resolution rule again. "
              "Set late_window.sniper_enabled: false and verify by hand.")
    for f in r["fires"]:
        print(f"  {f['ep']}  k={f['k']:4.1f}s  {'UP' if f['side'] else 'DN'}  "
              f"ask {f['ask']:.3f}  {f['tier']:4s}  "
              f"{'WIN ' if f['win'] else 'MISS'}  {f['net'] * 100:+.1f}c")


if __name__ == "__main__":
    main()
