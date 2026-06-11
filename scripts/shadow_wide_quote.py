"""Phase 6 kill-bar evaluation: opportunistic wide-quote maker sim (shadow only).

From the 1 Hz window_paths stream + the CLOB tape, detect the stale-touch
liquidity deserts tasks/todo.md Phase 6 targets and simulate quoting into them.

EPISODE (per window): a side's touch (best bid, best ask — both non-null; empty
books are excluded, conservative) unchanged for >= T seconds (default 30,
--T) while |Coinbase move since that stale run began| >= 0.5%. The episode
opens at the first sample meeting both conditions and ends at the first touch
change on EITHER side (the book woke up -> cancel), a >2.5 s recorder gap, or
window end. One quote set per episode, frozen at the open.

QUOTES: anchored to the crude Coinbase-implied fair value
    f = clip(0.5 + (coinbase - strike) / (4 * sigma_room), 0.05, 0.95)
    sigma_room = max(strike * 0.001, 30)
(a deliberately rough +-2*sigma_room linearization of the remaining-window
distribution — the kill bar measures fill economics, not f's calibration).
We BUY both sides: bid_up = f - w, bid_down = (1 - f) - w. A both-sides fill
costs (f - w) + (1 - f - w) = 1 - 2w, so w >= 0.02 (--w, floor enforced)
guarantees >= 4c gross per $1 pair.

FILLS (conservative): a SELL-side tape print on that side's token strictly
BELOW our bid fills it (at our bid; at-the-level prints never count). One fill
per side per episode. Cancel-on-move: no fill counted if Coinbase (nearest
1 Hz sample <= the print) moved > 0.3% toward that bid since the episode
opened — the cancel-replace we'd have done live.

P&L: paired fills pay $1 at resolution (fee-free redemption) -> +2w per share.
Unpaired fills mark at resolution via window_labels ($1 the winner, $0 the
loser); unlabeled windows are counted but excluded from $.

Token attribution: window_paths stores no token ids, so each tape token with
prints inside a window is voted Up/Down by print-price proximity to the
recorded book mids (votes where the mids sit < 4c apart are skipped; >= 3
votes and >= 60% majority required).

KILL BAR (tasks/todo.md Phase 6): positive net EV over >= 3 distinct ET days
of recorded shadow.

  python scripts/shadow_wide_quote.py [--db polybot/db/polybot_paper.db]
                                      [--T 30] [--w 0.02] [--shares 10]
  python scripts/shadow_wide_quote.py --selftest
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.paths import MEMORY_DIR  # noqa: E402

RECORDINGS = MEMORY_DIR / "recordings"
ET = ZoneInfo("America/New_York")

MOVE_ACTIVATE = 0.005      # |Coinbase move| that marks a stale touch as an episode
MOVE_CANCEL = 0.003        # adverse move past which a print no longer fills us
W_MIN = 0.02               # pair cost 1 - 2w <= 0.96 -> >= 4c gross per pair
GAP_BREAK_S = 2.5          # recorder gap: reset runs, end episodes
CLASSIFY_MIN_VOTES = 3
CLASSIFY_MAJORITY = 0.60
MID_SEP_MIN = 0.04         # skip votes when book mids are indistinguishable
NEAREST_ROW_TOL_S = 3.0
KILL_BAR_DAYS = 3

# window_paths row tuple layout used throughout
_TS, _BU, _AU, _BD, _AD, _CB, _STRIKE = range(7)


def _et_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d")


def open_db_with_table(db: Path, table: str) -> tuple[sqlite3.Connection, Path] | None:
    """Read-only connection to whichever DB holds `table`: the given trading DB
    or the recorder's sidecar window_paths.db beside it (the 1 Hz stream can be
    split out of the live DB to keep lock contention off the hot path)."""
    for cand in (db, db.with_name("window_paths.db")):
        if not cand.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{cand.resolve().as_posix()}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            continue
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone():
                return conn, cand
        except sqlite3.OperationalError:
            pass
        conn.close()
    return None


# ── loading ───────────────────────────────────────────────────────────────────

def load_windows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    windows: dict[str, list[tuple]] = defaultdict(list)
    cur = conn.execute(
        "SELECT window_id, ts, bid_up, ask_up, bid_down, ask_down,"
        " coinbase_price, strike FROM window_paths ORDER BY ts")
    for wid, *row in cur:
        windows[wid].append(tuple(row))
    return dict(windows)


def load_labels(conn: sqlite3.Connection) -> dict[str, int]:
    return {wid: ru for wid, ru in
            conn.execute("SELECT window_id, resolved_up FROM window_labels")}


def load_tape(tape_dir: Path = RECORDINGS) -> dict[str, list[tuple]]:
    """token -> [(ts, price, size, side)] sorted."""
    tape: dict[str, list[tuple]] = defaultdict(list)
    for fp in sorted(tape_dir.glob("tape_*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
                tape[t["token"]].append((float(t["ts"]), float(t["price"]),
                                         float(t.get("size") or 0),
                                         (t.get("side") or "").upper()))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
    for buf in tape.values():
        buf.sort()
    return dict(tape)


# ── episode detection ─────────────────────────────────────────────────────────

def fair_anchor(coinbase: float, strike: float) -> float:
    sigma_room = max(strike * 0.001, 30.0)
    return min(0.95, max(0.05, 0.5 + (coinbase - strike) / (4.0 * sigma_room)))


def detect_episodes(rows: list[tuple], T: float, w: float) -> list[dict]:
    """Maximal stale-touch quoting episodes in one window's 1 Hz rows."""
    eps: list[dict] = []
    n = len(rows)
    run_up = run_dn = 0   # index where the current touch run began (per side)
    ep: dict | None = None
    for i in range(n):
        ts, bu, au, bd, ad, cb, strike = rows[i]
        if i > 0:
            prev = rows[i - 1]
            gap = ts - prev[_TS] > GAP_BREAK_S
            if gap or bu is None or au is None or (bu, au) != (prev[_BU], prev[_AU]):
                run_up = i
            if gap or bd is None or ad is None or (bd, ad) != (prev[_BD], prev[_AD]):
                run_dn = i
            if ep is not None and (run_up == i or run_dn == i):
                # touch updated (or gap): cancel — cut at the last stale sample
                ep["i1"], ep["t1"] = i - 1, prev[_TS]
                eps.append(ep)
                ep = None
        if ep is None and cb is not None and strike is not None:
            for rs in (run_up, run_dn):
                cb0r = rows[rs][_CB]
                if (cb0r and ts - rows[rs][_TS] >= T
                        and abs(cb / cb0r - 1.0) >= MOVE_ACTIVATE):
                    f = fair_anchor(cb, strike)
                    ep = {"i0": i, "t0": ts, "cb0": cb, "f": f,
                          "q_up": round(f - w, 4), "q_dn": round((1.0 - f) - w, 4)}
                    break
    if ep is not None:
        ep["i1"], ep["t1"] = n - 1, rows[n - 1][_TS] + 1.0
        eps.append(ep)
    return eps


# ── token attribution ─────────────────────────────────────────────────────────

def _nearest_row(ts_arr: list[float], t: float, tol: float = NEAREST_ROW_TOL_S) -> int | None:
    j = bisect.bisect_left(ts_arr, t)
    best = None
    for c in (j - 1, j):
        if 0 <= c < len(ts_arr) and abs(ts_arr[c] - t) <= tol:
            if best is None or abs(ts_arr[c] - t) < abs(ts_arr[best] - t):
                best = c
    return best


def classify_tokens(rows: list[tuple], tape: dict[str, list[tuple]],
                    wts: float) -> dict[str, str]:
    """{'up': token, 'down': token} by print-price proximity to the book mids."""
    ts_arr = [r[_TS] for r in rows]
    cands: dict[str, list[tuple[int, str]]] = {}
    for tok, prints in tape.items():
        lo = bisect.bisect_left(prints, (wts,))
        hi = bisect.bisect_left(prints, (wts + 300.0,))
        if lo >= hi:
            continue
        votes = {"up": 0, "down": 0}
        for ts, price, _sz, _side in prints[lo:hi][:200]:
            j = _nearest_row(ts_arr, ts)
            if j is None:
                continue
            _, bu, au, bd, ad, _cb, _k = rows[j]
            if None in (bu, au, bd, ad):
                continue
            mid_up, mid_dn = (bu + au) / 2.0, (bd + ad) / 2.0
            if abs(mid_up - mid_dn) < MID_SEP_MIN:
                continue
            if abs(price - mid_up) < abs(price - mid_dn):
                votes["up"] += 1
            elif abs(price - mid_dn) < abs(price - mid_up):
                votes["down"] += 1
        total = votes["up"] + votes["down"]
        if total >= CLASSIFY_MIN_VOTES:
            side = "up" if votes["up"] > votes["down"] else "down"
            if votes[side] / total >= CLASSIFY_MAJORITY:
                cands.setdefault(side, []).append((votes[side], tok))
    return {side: max(lst)[1] for side, lst in cands.items()}


# ── fill simulation ───────────────────────────────────────────────────────────

def simulate(windows: dict[str, list[tuple]], labels: dict[str, int],
             tape: dict[str, list[tuple]], T: float, w: float,
             shares: float) -> tuple[list[tuple[str, str]], list[dict], int]:
    """-> (episodes [(day, window_id)], fill/pnl events, fully-attributed windows)."""
    episodes_all: list[tuple[str, str]] = []
    events: list[dict] = []
    n_attr = 0
    for wid, rows in sorted(windows.items()):
        try:
            wts = float(int(wid.rsplit("-", 1)[-1]))
        except ValueError:
            continue
        rows = sorted(rows)
        eps = detect_episodes(rows, T, w)
        toks = classify_tokens(rows, tape, wts)
        if len(toks) == 2:
            n_attr += 1
        ts_arr = [r[_TS] for r in rows]
        for ep in eps:
            day = _et_day(ep["t0"])
            episodes_all.append((day, wid))
            fills: dict[str, float] = {}
            for side in ("up", "down"):
                tok = toks.get(side)
                if tok is None:
                    continue
                q = ep["q_up"] if side == "up" else ep["q_dn"]
                prints = tape.get(tok, [])
                lo = bisect.bisect_right(prints, (ep["t0"],))
                for pts, price, _sz, pside in prints[lo:]:
                    if pts > ep["t1"]:
                        break
                    if pts <= ep["t0"] or pside != "SELL" or price >= q:
                        continue
                    j = bisect.bisect_right(ts_arr, pts) - 1   # last sample <= print
                    cb_at = rows[j][_CB] if (j >= 0 and pts - ts_arr[j] <= NEAREST_ROW_TOL_S) else None
                    if cb_at is None:
                        continue                # can't verify the move: no fill
                    move = cb_at / ep["cb0"] - 1.0
                    if side == "up" and move < -MOVE_CANCEL:
                        continue                # Coinbase fell toward our Up bid: canceled
                    if side == "down" and move > MOVE_CANCEL:
                        continue                # Coinbase rose toward our Down bid: canceled
                    fills[side] = q
                    break
            if "up" in fills and "down" in fills:
                events.append(dict(day=day, window=wid, type="pair",
                                   pnl=(1.0 - fills["up"] - fills["down"]) * shares,
                                   adverse=False))
            elif fills:
                side, q = next(iter(fills.items()))
                ru = labels.get(wid)
                if ru is None:
                    events.append(dict(day=day, window=wid, type="unlabeled",
                                       side=side, pnl=None, adverse=False))
                else:
                    won = (side == "up") == bool(ru)
                    events.append(dict(day=day, window=wid, type="unpaired", side=side,
                                       pnl=((1.0 if won else 0.0) - q) * shares,
                                       adverse=not won))
    return episodes_all, events, n_attr


# ── reporting ─────────────────────────────────────────────────────────────────

def day_clustered_t(deltas: list[float]) -> float | None:
    n = len(deltas)
    if n < 2:
        return None
    mean = sum(deltas) / n
    sd = (sum((d - mean) ** 2 for d in deltas) / (n - 1)) ** 0.5
    return None if sd == 0 else mean / (sd / n ** 0.5)


def report(episodes: list[tuple[str, str]], events: list[dict],
           coverage_days: set[str], T: float, w: float, shares: float) -> None:
    days = sorted(coverage_days | {d for d, _ in episodes})
    print(f"\nper-ET-day (T={T:.0f}s, w={w:.3f}, {shares:.0f} shares/quote):")
    print(f"{'day':>12} {'episodes':>9} {'fills':>6} {'pairs':>6} {'unpair':>7} "
          f"{'unlab':>6} {'net$':>9} {'adverse':>8}")
    daily_net = []
    for day in days:
        eps_d = sum(1 for d, _ in episodes if d == day)
        ev_d = [e for e in events if e["day"] == day]
        pairs = [e for e in ev_d if e["type"] == "pair"]
        unp = [e for e in ev_d if e["type"] == "unpaired"]
        unlab = [e for e in ev_d if e["type"] == "unlabeled"]
        net = sum(e["pnl"] for e in pairs + unp)
        adv = sum(e["adverse"] for e in unp)
        daily_net.append(net)
        print(f"{day:>12} {eps_d:>9} {2 * len(pairs) + len(unp) + len(unlab):>6} "
              f"{len(pairs):>6} {len(unp):>7} {len(unlab):>6} {net:>+9.2f} "
              f"{adv}/{len(unp):<6}")

    pairs = [e for e in events if e["type"] == "pair"]
    unp = [e for e in events if e["type"] == "unpaired"]
    unlab = [e for e in events if e["type"] == "unlabeled"]
    net_pairs = sum(e["pnl"] for e in pairs)
    net_unp = sum(e["pnl"] for e in unp)
    ep_with_fill = len({(e["day"], e["window"]) for e in events})
    n_days = len(days)
    print(f"\nepisodes: {len(episodes)} total"
          + (f" ({len(episodes) / n_days:.1f}/day)" if n_days else ""))
    print(f"fills: {2 * len(pairs) + len(unp) + len(unlab)}  "
          f"(pairs {len(pairs)}, unpaired {len(unp)}, unlabeled {len(unlab)})")
    if ep_with_fill:
        print(f"pair rate (episodes with any fill): {len(pairs)}/{ep_with_fill} "
              f"= {len(pairs) / ep_with_fill:.1%}")
    if unp:
        print(f"adverse-fill rate (unpaired resolved $0): "
              f"{sum(e['adverse'] for e in unp)}/{len(unp)} "
              f"= {sum(e['adverse'] for e in unp) / len(unp):.1%}")
    t = day_clustered_t(daily_net)
    print(f"net $: pairs {net_pairs:+.2f}  unpaired {net_unp:+.2f}  "
          f"TOTAL {net_pairs + net_unp:+.2f}  day-clustered t "
          + (f"{t:+.2f}" if t is not None else f"n/a ({n_days} day{'s' * (n_days != 1)})"))

    print(f"\nKILL BAR (Phase 6): positive net EV over >= {KILL_BAR_DAYS} distinct ET days")
    if n_days < KILL_BAR_DAYS:
        print(f"-> WAITING FOR DATA ({n_days}/{KILL_BAR_DAYS} shadow days)")
    elif net_pairs + net_unp > 0:
        print(f"-> PASS (net {net_pairs + net_unp:+.2f} over {n_days} days)")
    else:
        print(f"-> FAIL (net {net_pairs + net_unp:+.2f} over {n_days} days) — abort the sleeve")


# ── selftest ──────────────────────────────────────────────────────────────────

def selftest() -> None:
    """Three synthetic 120 s windows, frozen books (stale the whole way),
    strike 60000 -> sigma_room 60 -> f saturates at the 0.05/0.95 clips.
      w1: Coinbase +0.6% at t+40 -> episode opens (f=0.95, bid_up 0.93,
          bid_down 0.03); SELL prints through both bids -> PAIR = +2w*10 = +$0.40.
      w2: Coinbase -0.6% -> f=0.05 (bid_down 0.93); only the Down bid fills,
          window resolves Up -> unpaired adverse = (0 - 0.93)*10 = -$9.30.
      w3: same as w1 but Coinbase then retraces -0.4% from the open (toward the
          Up bid) before the print -> cancel-on-move blocks the fill.
    """
    T, w, shares = 30.0, 0.02, 10.0
    w1, w2, w3 = (f"btc-updown-5m-{ts}" for ts in (1000000200, 1000000800, 1000001400))

    def rows_for(wts, bu, au, bd, ad, cb_fn):
        return [(float(wts + i), bu, au, bd, ad, cb_fn(i), 60000.0) for i in range(120)]

    windows = {
        w1: rows_for(1000000200, 0.60, 0.62, 0.38, 0.40,
                     lambda i: 60000.0 if i < 40 else 60360.0),
        w2: rows_for(1000000800, 0.70, 0.72, 0.28, 0.30,
                     lambda i: 60000.0 if i < 40 else 59640.0),
        w3: rows_for(1000001400, 0.60, 0.62, 0.38, 0.40,
                     lambda i: 60000.0 if i < 40 else (60360.0 if i < 60 else 60118.0)),
    }
    labels = {w1: 1, w2: 1, w3: 0}

    def cls_prints(wts, price, n=5):  # BUY prints: attribution votes, never fills
        return [(float(wts + 5 + k), price, 1.0, "BUY") for k in range(n)]

    tape = {
        "TU1": sorted(cls_prints(1000000200, 0.61) + [(1000000250.0, 0.92, 5.0, "SELL")]),
        "TD1": sorted(cls_prints(1000000200, 0.39) + [(1000000255.0, 0.02, 5.0, "SELL")]),
        "TU2": cls_prints(1000000800, 0.71),
        "TD2": sorted(cls_prints(1000000800, 0.29) + [(1000000850.0, 0.92, 5.0, "SELL")]),
        "TU3": sorted(cls_prints(1000001400, 0.61) + [(1000001470.0, 0.50, 5.0, "SELL")]),
        "TD3": cls_prints(1000001400, 0.39),
    }

    eps = detect_episodes(windows[w1], T, w)
    assert len(eps) == 1 and eps[0]["t0"] == 1000000240.0, eps
    assert eps[0]["q_up"] == 0.93 and eps[0]["q_dn"] == 0.03, eps  # f clipped to 0.95

    episodes, events, n_attr = simulate(windows, labels, tape, T, w, shares)
    assert len(episodes) == 3, episodes
    assert n_attr == 3, n_attr
    pairs = [e for e in events if e["type"] == "pair"]
    unp = [e for e in events if e["type"] == "unpaired"]
    assert len(pairs) == 1 and abs(pairs[0]["pnl"] - 0.40) < 1e-9, pairs
    assert len(unp) == 1 and abs(unp[0]["pnl"] - (-9.30)) < 1e-9 and unp[0]["adverse"], unp
    assert not [e for e in events if e["window"] == w3], \
        "cancel-on-move must block the w3 fill"
    net = sum(e["pnl"] for e in events if e["pnl"] is not None)
    assert abs(net - (-8.90)) < 1e-9, net

    report(episodes, events, {_et_day(r[0][_TS]) for r in windows.values()}, T, w, shares)
    print("\nhand-check: pair = (1 - 0.93 - 0.03) * 10 = +$0.40 ;"
          " unpaired Down @0.93 resolved $0 = -$9.30 ; net -$8.90 ; adverse 1/1")
    print("SELFTEST PASS")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 6 kill bar: wide-quote maker shadow sim")
    ap.add_argument("--db", default="polybot/db/polybot_paper.db")
    ap.add_argument("--T", type=float, default=30.0, help="stale-touch seconds (default 30)")
    ap.add_argument("--w", type=float, default=W_MIN,
                    help=f"half-width inside fair (floor {W_MIN}: >=4c gross per pair)")
    ap.add_argument("--shares", type=float, default=10.0, help="shares per quote side")
    ap.add_argument("--selftest", action="store_true",
                    help="run the in-memory fixture instead of real data")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    w = max(args.w, W_MIN)
    if w != args.w:
        print(f"--w raised to the {W_MIN} floor (pair gross must clear 4c)")

    db = Path(args.db)
    opened = open_db_with_table(db, "window_paths")
    if opened is None:
        print(f"WAITING FOR DATA — no window_paths table in {db} "
              "or its window_paths.db sidecar.")
        return
    conn, paths_src = opened
    try:
        windows = load_windows(conn)
    except sqlite3.OperationalError as e:
        print(f"WAITING FOR DATA — cannot read window_paths ({e}).")
        return
    finally:
        conn.close()
    labels, labels_src = {}, None
    opened = open_db_with_table(db, "window_labels")
    if opened is not None:
        conn, labels_src = opened
        try:
            labels = load_labels(conn)
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    print(f"window_paths source: {paths_src}   window_labels source: {labels_src}")
    if not windows:
        print("WAITING FOR DATA — window_paths is empty (recorder just started?).")
        return
    tape = load_tape()
    n_rows = sum(len(r) for r in windows.values())
    n_prints = sum(len(p) for p in tape.values())
    print(f"windows: {len(windows)} ({n_rows} rows)  labels: {len(labels)}  "
          f"tape: {len(tape)} tokens / {n_prints} prints")
    if not tape:
        print("WAITING FOR DATA — no tape files in "
              f"{RECORDINGS} (episodes can't fill without prints).")
        return

    episodes, events, n_attr = simulate(windows, labels, tape, args.T, w, args.shares)
    print(f"token attribution: {n_attr}/{len(windows)} windows fully attributed")
    if not episodes:
        print("no stale-touch episodes detected yet — book never sat still "
              f">= {args.T:.0f}s through a >= {MOVE_ACTIVATE:.1%} Coinbase move. "
              "Re-run as data accumulates (or lower --T to probe).")
        return
    coverage_days = {_et_day(rows[0][_TS]) for rows in windows.values() if rows}
    report(episodes, events, coverage_days, args.T, w, args.shares)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
