"""Phase 1 kill-bar evaluation: would resting the exit SELL at mid have filled?

Joins every recorded scalp exit to (a) the window-path 1 Hz BBO stream (the
quote context at the exit moment) and (b) the CLOB tape (what actually printed
afterwards). A resting SELL at level L "fills" ONLY if a later BUY-side print
on the same token prints strictly through L within the timeout — the
conservative rule; queue position is unknowable, at-the-level prints don't
count.

KILL BAR (tasks/todo.md Phase 1): fill rate on ITM scalps (exit price >= 0.50)
must be >= 50%. Below that, the passive exit does not deploy.

Run after >= 3 days of tape (recorder ships with the gutted bot):
  python scripts/shadow_passive_exit.py [--timeout 10 20] [--level mid bid1]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polybot.execution.base import DEFAULT_FEE_RATE  # noqa: E402
from polybot.paths import MEMORY_DIR, OUTCOMES_DIR  # noqa: E402

RECORDINGS = MEMORY_DIR / "recordings"
TICK = 0.01


def load_tape() -> dict[str, list[tuple[float, float, str]]]:
    """token -> [(ts, price, side)] sorted."""
    tape: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for fp in sorted(RECORDINGS.glob("tape_*.jsonl")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
                tape[t["token"]].append(
                    (float(t["ts"]), float(t["price"]), (t.get("side") or "").upper()))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
    for buf in tape.values():
        buf.sort()
    return tape


def load_scalps() -> list[dict]:
    """Scalp exits with timestamp, token, side, exit fill, shares."""
    out = []
    for fp in sorted(OUTCOMES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for r in (data if isinstance(data, list) else [data]):
            if r.get("exit_reason") != "scalp" or not r.get("exit_timestamp"):
                continue
            ctx = (r.get("indicator_snapshot") or {}).get("trade_context", {}) or {}
            token = (ctx.get("token_id_up") if r.get("side") == "Up"
                     else ctx.get("token_id_down")) or ""
            if not token:
                continue
            from datetime import datetime
            ts = datetime.fromisoformat(
                r["exit_timestamp"].replace("Z", "+00:00")).timestamp()
            out.append({
                "pid": r.get("position_id"), "token": token, "ts": ts,
                "side": r.get("side"), "exit_price": float(r.get("exit_price") or 0),
                "size": float(r.get("size") or 0),
                "shares": float(r.get("size") or 0) / max(float(r.get("entry_price") or 1), 0.01),
                "day": r["exit_timestamp"][:10],
            })
    return out


async def load_bba_at(db_path: str, token_rows: list[dict]) -> None:
    """Attach bid/ask at exit moment from window_paths — read-only, from
    whichever DB holds the table: the trading DB or the recorder's sidecar
    window_paths.db beside it (the 1 Hz stream lives in the sidecar so the
    live bot's writes never contend with this read)."""
    import aiosqlite
    db = Path(db_path)
    for cand in (db, db.with_name("window_paths.db")):
        if not cand.exists():
            continue
        async with aiosqlite.connect(
                f"file:{cand.resolve().as_posix()}?mode=ro", uri=True) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='window_paths'")
            if not await cur.fetchone():
                continue
            conn.row_factory = aiosqlite.Row
            for r in token_rows:
                col_bid = "bid_up" if r["side"] == "Up" else "bid_down"
                col_ask = "ask_up" if r["side"] == "Up" else "ask_down"
                cur = await conn.execute(
                    f"SELECT {col_bid} AS b, {col_ask} AS a FROM window_paths "
                    f"WHERE ts BETWEEN ? AND ? AND {col_bid} IS NOT NULL "
                    f"ORDER BY ABS(ts - ?) LIMIT 1",
                    (r["ts"] - 3, r["ts"] + 3, r["ts"]))
                row = await cur.fetchone()
                r["bid"], r["ask"] = (row["b"], row["a"]) if row else (None, None)
            return
    raise SystemExit(
        f"window_paths table not found in {db} or {db.with_name('window_paths.db')}")


def simulate(scalps: list[dict], tape: dict, level: str, timeout_s: float) -> dict:
    filled, missed, uplift = [], [], []
    for s in scalps:
        if s["bid"] is None or s["ask"] is None:
            continue
        if level == "mid":
            L = round((s["bid"] + s["ask"]) / 2, 4)
        else:  # bid + 1 tick
            L = round(s["bid"] + TICK, 4)
        prints = tape.get(s["token"], [])
        fill = None
        for ts, price, side in prints:
            if ts <= s["ts"]:
                continue
            if ts > s["ts"] + timeout_s:
                break
            if side == "BUY" and price > L:   # strictly through
                fill = L
                break
        rec = dict(s, level=L)
        if fill is not None:
            # maker: no taker fee; uplift vs the actual FOK exit (which paid fee)
            taker_fee = DEFAULT_FEE_RATE * s["exit_price"] * (1 - s["exit_price"])
            uplift.append((fill - s["exit_price"] + taker_fee) * s["shares"])
            filled.append(rec)
        else:
            missed.append(rec)
    n = len(filled) + len(missed)
    itm = [r for r in filled + missed if r["exit_price"] >= 0.50]
    itm_filled = [r for r in filled if r["exit_price"] >= 0.50]
    return {
        "n": n, "fill_rate": len(filled) / n if n else 0.0,
        "itm_n": len(itm), "itm_fill_rate": len(itm_filled) / len(itm) if itm else 0.0,
        "uplift_usd": sum(uplift),
        "uplift_per_fill": st.mean(uplift) if uplift else 0.0,
        "days": len({r["day"] for r in filled + missed}),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="polybot/db/polybot_paper.db")
    ap.add_argument("--timeouts", nargs="*", type=float, default=[5, 10, 20])
    args = ap.parse_args()

    tape = load_tape()
    scalps = load_scalps()
    n_prints = sum(len(v) for v in tape.values())
    print(f"tape: {len(tape)} tokens, {n_prints} prints   scalps with token: {len(scalps)}")
    if not tape or not scalps:
        print("Not enough data yet — needs >=3 days of tape + window_paths recording.")
        return
    asyncio.run(load_bba_at(args.db, scalps))
    usable = [s for s in scalps if s.get("bid") is not None]
    print(f"scalps with BBO context: {len(usable)}")

    print(f"\n{'level':>6} {'timeout':>8} {'n':>5} {'fill%':>7} {'ITM n':>6} "
          f"{'ITM fill%':>10} {'uplift $':>9} {'$/fill':>8} {'days':>5}")
    for level in ("mid", "bid1"):
        for to in args.timeouts:
            r = simulate(usable, tape, level, to)
            bar = "PASS" if r["itm_fill_rate"] >= 0.50 else "fail"
            print(f"{level:>6} {to:>7.0f}s {r['n']:>5} {r['fill_rate']:>6.1%} "
                  f"{r['itm_n']:>6} {r['itm_fill_rate']:>9.1%} {r['uplift_usd']:>+9.2f} "
                  f"{r['uplift_per_fill']:>+8.3f} {r['days']:>5}  [{bar}]")
    print("\nKILL BAR: ITM fill rate >= 50% at any (level, timeout) over >= 3 days "
          "-> build the GTC two-stage exit. Otherwise it stays FOK.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
