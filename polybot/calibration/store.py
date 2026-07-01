"""Calibration harness persistence — its OWN sqlite DB, isolated from the trading DBs.

Two tables: append-only `snapshots` (one row per market per snapshot pass) and
`resolutions` (one row per market once it settles). Analysis joins them. Follows the
repo aiosqlite conventions (WAL, busy_timeout, Row factory, atomic commit/rollback,
UTC-ISO timestamps). DB path is gitignored (local-only research artifact).
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from polybot.paths import POLYBOT_DIR

DEFAULT_DB_PATH = str(POLYBOT_DIR / "db" / "calibration.db")

_SNAPSHOT_COLS = [
    "taken_ts", "taken_epoch", "condition_id", "token0_id", "slug", "coin", "family",
    "pricing_kind", "strike", "side", "end_ts", "horizon_days", "lead_s",
    "pm_bid", "pm_ask", "pm_mid", "ask_depth_usd", "bid_depth_usd",
    "iv_implied", "iv_used", "spot", "fwd", "t_years",
]


class CalibrationStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                taken_ts TEXT NOT NULL,
                taken_epoch REAL NOT NULL,
                condition_id TEXT NOT NULL,
                token0_id TEXT NOT NULL,
                slug TEXT NOT NULL,
                coin TEXT,
                family TEXT,
                pricing_kind TEXT,
                strike REAL,
                side TEXT,
                end_ts REAL,
                horizon_days REAL,
                lead_s REAL,
                pm_bid REAL,
                pm_ask REAL,
                pm_mid REAL,
                ask_depth_usd REAL,
                bid_depth_usd REAL,
                iv_implied REAL,
                iv_used REAL,
                spot REAL,
                fwd REAL,
                t_years REAL
            );

            CREATE TABLE IF NOT EXISTS resolutions (
                condition_id TEXT PRIMARY KEY,
                slug TEXT,
                token0_id TEXT,
                outcome INTEGER NOT NULL,
                resolved_ts TEXT,
                labeled_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snap_condition ON snapshots(condition_id);
            CREATE INDEX IF NOT EXISTS idx_snap_end_ts ON snapshots(end_ts);
        """)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def insert_snapshots(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        placeholders = ",".join("?" for _ in _SNAPSHOT_COLS)
        sql = f"INSERT INTO snapshots ({','.join(_SNAPSHOT_COLS)}) VALUES ({placeholders})"
        try:
            await self.conn.executemany(
                sql, [tuple(r.get(c) for c in _SNAPSHOT_COLS) for r in rows])
            await self.conn.commit()
            return len(rows)
        except BaseException:
            await self.conn.rollback()
            raise

    async def conditions_needing_label(self, now_epoch: float, grace_s: float = 3600.0
                                       ) -> list[dict]:
        """Distinct snapshotted markets that have ended (+grace) and aren't yet resolved."""
        cur = await self.conn.execute(
            """SELECT condition_id, slug, token0_id, MAX(end_ts) AS end_ts
               FROM snapshots
               WHERE end_ts IS NOT NULL AND end_ts < ?
                 AND condition_id NOT IN (SELECT condition_id FROM resolutions)
               GROUP BY condition_id, slug, token0_id""",
            (now_epoch - grace_s,))
        return [dict(r) for r in await cur.fetchall()]

    async def upsert_resolution(self, condition_id: str, slug: str, token0_id: str,
                                outcome: int, resolved_ts: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self.conn.execute(
                """INSERT INTO resolutions (condition_id, slug, token0_id, outcome,
                       resolved_ts, labeled_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(condition_id) DO UPDATE SET outcome=excluded.outcome,
                       resolved_ts=excluded.resolved_ts, labeled_at=excluded.labeled_at""",
                (condition_id, slug, token0_id, int(outcome), resolved_ts, now))
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def join_rows(self) -> list[dict]:
        """Snapshots joined to their resolution outcome (for calibration analysis)."""
        cur = await self.conn.execute(
            """SELECT s.family, s.pricing_kind, s.coin, s.slug, s.strike, s.side,
                      s.lead_s, s.pm_ask, s.pm_bid, s.pm_mid, s.iv_implied, r.outcome
               FROM snapshots s JOIN resolutions r ON s.condition_id = r.condition_id""")
        return [dict(row) for row in await cur.fetchall()]

    async def latest_snapshots(self) -> list[dict]:
        """Most-recent snapshot per market (for the live IV cross-check)."""
        cur = await self.conn.execute(
            """SELECT s.* FROM snapshots s
               JOIN (SELECT condition_id, MAX(taken_epoch) AS mx
                     FROM snapshots GROUP BY condition_id) m
                 ON s.condition_id = m.condition_id AND s.taken_epoch = m.mx""")
        return [dict(row) for row in await cur.fetchall()]

    async def status(self) -> dict:
        async def scalar(sql):
            cur = await self.conn.execute(sql)
            return (await cur.fetchone())[0]
        return {
            "snapshots": await scalar("SELECT COUNT(*) FROM snapshots"),
            "markets_tracked": await scalar("SELECT COUNT(DISTINCT condition_id) FROM snapshots"),
            "resolutions": await scalar("SELECT COUNT(*) FROM resolutions"),
            "passes": await scalar("SELECT COUNT(DISTINCT taken_ts) FROM snapshots"),
        }
