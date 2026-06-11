"""One-shot historical backfill for the Phase 4 wallet-fingerprinting tables.

Window list + resolution labels come from local memory records (no API calls):
  - counterfactuals/ scalp records: context_at_scalp.chainlink_price_to_beat +
    chainlink_final_price  ->  resolved_up = final >= ptb
  - outcomes/ exit_reason == 'resolution': Up won iff (side == 'Up') == correct
  - window_labels in the per-mode DB (recorder output): resolved_up directly
Per-trade JSON and rollup_*.json arrays are both walked; windows deduped by
market_id and bounded to the last --days (default 10).

Each window's taker tape is then fetched from the data-api (conditionId via
Gamma), throttled to ~3 concurrent windows with ~0.15s spacing between HTTP
requests. Rows are shape-identical to wallets.py's nightly job (same insert,
same PK, scored with wallets._won), and every ingested window is marked in
wallet_ingest_log so the nightly job never re-fetches it. Failed windows are
NOT marked, so re-running resumes where it left off.

Per-trade rows land in the gitignored tape DB (wallets.TAPE_DB_PATH); only the
small wallet_stats aggregate is written to the per-mode DB (live — the bot holds
it open: WAL + busy_timeout, short transactions), via wallets.rebuild_stats —
the same code path the nightly job uses.

Usage:
    python scripts/backfill_wallets.py [--limit N] [--days 3] [--db PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import aiosqlite  # noqa: E402
import httpx  # noqa: E402

from polybot import wallets  # noqa: E402
from polybot.paths import MEMORY_DIR  # noqa: E402

GAMMA_API = "https://gamma-api.polymarket.com"
DEFAULT_DB = REPO_ROOT / "polybot" / "db" / "polybot_paper.db"

CONCURRENCY = 3          # windows in flight
MIN_REQUEST_SPACING = 0.15  # seconds between HTTP request starts, global
PAGE_LIMIT = 500
MAX_OFFSET = 3000        # data-api 400s past ~offset 3000 (hard ceiling)
RETRIES = 3


class _DB:
    """Duck-typed shim for wallets.* helpers (they expect db.conn)."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn


class Throttle:
    """Global min-interval between HTTP request starts."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


def _window_ts(market_id: str) -> int | None:
    """btc-updown-5m-1781072700 -> 1781072700."""
    try:
        return int(market_id.rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return None


def _iter_records(directory: Path):
    """Yield record dicts from per-trade JSON and rollup_*.json arrays."""
    if not directory.is_dir():
        return
    for f in directory.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        yield from data if isinstance(data, list) else (data,)


def collect_labels(days: float) -> tuple[dict[str, int], dict[str, int]]:
    """{market_id: resolved_up} from local records, last `days` only.

    Chainlink-derived labels (counterfactual scalps) win conflicts — they are
    the same final >= ptb math the recorder uses on Gamma event_metadata.
    """
    cutoff = time.time() - days * 86400.0
    chainlink: dict[str, int] = {}
    outcome: dict[str, int] = {}
    conflicts = 0

    for r in _iter_records(MEMORY_DIR / "counterfactuals"):
        mid = r.get("market_id")
        ts = _window_ts(mid)
        if ts is None or ts < cutoff:
            continue
        cs = r.get("context_at_scalp") or {}
        ptb, final = cs.get("chainlink_price_to_beat"), cs.get("chainlink_final_price")
        if ptb is None or final is None:
            continue
        chainlink[mid] = 1 if float(final) >= float(ptb) else 0

    for r in _iter_records(MEMORY_DIR / "outcomes"):
        if r.get("exit_reason") != "resolution":
            continue
        mid = r.get("market_id")
        ts = _window_ts(mid)
        if ts is None or ts < cutoff:
            continue
        side, correct = r.get("side"), r.get("correct")
        if side not in ("Up", "Down") or not isinstance(correct, bool):
            continue
        outcome[mid] = 1 if ((side == "Up") == correct) else 0

    labels = dict(outcome)
    for mid, up in chainlink.items():
        if mid in outcome and outcome[mid] != up:
            conflicts += 1
        labels[mid] = up  # chainlink wins
    sources = {"counterfactual": len(chainlink), "outcome": len(outcome),
               "conflicts": conflicts}
    return labels, sources


async def labels_from_db(db: _DB, days: float) -> dict[str, int]:
    """window_labels rows (recorder output) — already authoritative."""
    cutoff = time.time() - days * 86400.0
    try:
        cur = await db.conn.execute(
            "SELECT window_id, resolved_up FROM window_labels")
        rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return {}
    out: dict[str, int] = {}
    for r in rows:
        ts = _window_ts(r["window_id"])
        if ts is not None and ts >= cutoff:
            out[r["window_id"]] = int(r["resolved_up"])
    return out


async def _get_json(client: httpx.AsyncClient, throttle: Throttle,
                    url: str, params: dict[str, Any]) -> Any:
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(RETRIES + 1):
        await throttle.wait()
        try:
            r = await client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                await asyncio.sleep(1.5 * (2 ** attempt) + random.random())
    raise last_err


async def fetch_window_tape(client: httpx.AsyncClient, throttle: Throttle,
                            window_id: str) -> list[dict[str, Any]] | None:
    """All taker trades for one window. None = Gamma has no conditionId
    (window still marked, nothing to fetch); raises on hard API failure."""
    data = await _get_json(client, throttle, f"{GAMMA_API}/events",
                           params={"slug": window_id})
    ev = data[0] if isinstance(data, list) and data else data
    markets = (ev or {}).get("markets") or []
    condition_id = (markets[0] or {}).get("conditionId", "") if markets else ""
    if not condition_id:
        return None
    trades: list[dict[str, Any]] = []
    offset = 0
    while True:
        try:
            page = await _get_json(client, throttle, f"{wallets.DATA_API}/trades",
                                   params={"market": condition_id,
                                           "limit": PAGE_LIMIT, "offset": offset})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and offset > 0:
                break  # data-api hard pagination ceiling — keep what we have
            raise
        if not isinstance(page, list) or not page:
            break
        trades.extend(page)
        if len(page) < PAGE_LIMIT or offset >= MAX_OFFSET:
            break
        offset += PAGE_LIMIT
    return trades


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="ingest at most N windows (dry run)")
    ap.add_argument("--days", type=float, default=3.0,
                    help="window lookback in days (default 3 — ~860 windows, "
                         "~2.5M tape rows; the nightly job extends from there)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="target SQLite DB")
    args = ap.parse_args()
    t0 = time.monotonic()

    conn = await aiosqlite.connect(args.db)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=15000")
    db = _DB(conn)
    tape = await wallets.open_tape_db()
    try:
        moved = await wallets.migrate_tape_out_of_main_db(db, tape)
        if moved:
            print(f"Migrated {moved} pre-split tape rows out of the per-mode DB")

        labels, sources = collect_labels(args.days)
        db_labels = await labels_from_db(db, args.days)
        for mid, up in db_labels.items():
            labels.setdefault(mid, up)
        print(f"Labeled windows (last {args.days:g}d): {len(labels)} "
              f"(counterfactual {sources['counterfactual']}, "
              f"outcome {sources['outcome']}, window_labels {len(db_labels)}, "
              f"label conflicts {sources['conflicts']})")

        cur = await tape.execute("SELECT window_id FROM wallet_ingest_log")
        done = {r["window_id"] for r in await cur.fetchall()}
        todo = sorted((m for m in labels if m not in done),
                      key=lambda m: _window_ts(m) or 0)
        skipped = len(labels) - len(todo)
        if args.limit is not None:
            todo = todo[:args.limit]
        print(f"To ingest: {len(todo)} windows ({skipped} already in ingest log)")

        throttle = Throttle(MIN_REQUEST_SPACING)
        sem = asyncio.Semaphore(CONCURRENCY)
        write_lock = asyncio.Lock()  # one window's batch per write transaction
        stats = {"windows": 0, "trades": 0, "failed": 0, "no_condition": 0}
        failures: list[str] = []

        async with httpx.AsyncClient(timeout=20.0) as client:

            async def ingest(window_id: str) -> None:
                async with sem:
                    try:
                        window_tape = await fetch_window_tape(client, throttle, window_id)
                    except Exception as e:
                        stats["failed"] += 1
                        if len(failures) < 10:
                            failures.append(f"{window_id}: {e!r}")
                        return
                    if window_tape is None:
                        stats["no_condition"] += 1
                        rows = []
                    else:
                        rows = wallets.score_rows(window_id, labels[window_id], window_tape)
                    async with write_lock:
                        if rows:
                            await tape.executemany(
                                "INSERT OR IGNORE INTO wallet_trades "
                                "VALUES (?,?,?,?,?,?,?,?)", rows)
                        await tape.execute(
                            "INSERT OR REPLACE INTO wallet_ingest_log "
                            "VALUES (?, ?, ?)",
                            (window_id, len(rows), time.time()))
                        await tape.commit()
                    stats["windows"] += 1
                    stats["trades"] += len(rows)
                    n = stats["windows"]
                    if n % 50 == 0:
                        rate = n / (time.monotonic() - t0)
                        eta = (len(todo) - n - stats["failed"]) / max(rate, 1e-9)
                        print(f"  {n}/{len(todo)} windows, "
                              f"{stats['trades']} trades, "
                              f"failed {stats['failed']}, "
                              f"ETA {eta / 60:.1f} min", flush=True)

            await asyncio.gather(*(ingest(w) for w in todo))

        counts = await wallets.rebuild_stats(db, tape)

        cur = await tape.execute("SELECT COUNT(*) n FROM wallet_trades")
        total_rows = (await cur.fetchone())["n"]
        cur = await tape.execute("SELECT COUNT(*) n FROM wallet_ingest_log")
        total_log = (await cur.fetchone())["n"]

        print()
        print("=== Backfill report ===")
        print(f"Windows ingested this run : {stats['windows']} "
              f"(no-conditionId {stats['no_condition']}, "
              f"failed {stats['failed']}, previously done {skipped})")
        print(f"Trades scored this run    : {stats['trades']}")
        print(f"wallet_trades total rows  : {total_rows}")
        print(f"wallet_ingest_log total   : {total_log}")
        print(f"Wallet classifications    : {counts}")
        if failures:
            print("Sample failures:")
            for f in failures:
                print(f"  {f}")

        for label, where, order in (
                ("Top-5 donor wallets by stake", "donor", "stake_usd DESC"),
                ("Top-5 sharp wallets by P&L", "sharp", "pnl_usd DESC")):
            cur = await db.conn.execute(f"""
                SELECT wallet, n_trades, n_won, stake_usd, pnl_usd,
                       pnl_usd / MAX(stake_usd, 1.0) AS markout
                FROM wallet_stats WHERE classification = ?
                ORDER BY {order} LIMIT 5""", (where,))
            print(f"\n{label}:")
            for r in await cur.fetchall():
                print(f"  {r['wallet']}  n={r['n_trades']} "
                      f"won={r['n_won']} stake=${r['stake_usd']:,.0f} "
                      f"pnl=${r['pnl_usd']:,.0f} markout={r['markout']:+.4f}")

        print(f"\nRuntime: {(time.monotonic() - t0) / 60:.1f} min")
    finally:
        await tape.close()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
