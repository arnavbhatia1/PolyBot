"""SQLite database models for positions, trade history, and bankroll.

Per-mode SQLite database (polybot_paper.db / polybot_live.db). All async via aiosqlite.
Bankroll is the single source of truth for capital — never reconstruct it from trades.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

_ET = ZoneInfo("America/New_York")

class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path: str = db_path
        self.conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                signal_score REAL NOT NULL,
                entry_timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_timestamp TEXT,
                indicator_snapshot TEXT,
                fee_rate REAL,
                shares_held REAL
            );

            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                size REAL NOT NULL,
                exit_timestamp TEXT NOT NULL,
                exit_reason TEXT NOT NULL DEFAULT 'resolution'
            );

            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS peak_bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount REAL NOT NULL
            );
        """)
        # Migrate existing DBs: add missing columns to positions and trade_history
        cursor = await self.conn.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "fee_rate" not in cols:
            await self.conn.execute("ALTER TABLE positions ADD COLUMN fee_rate REAL")
        if "shares_held" not in cols:
            await self.conn.execute("ALTER TABLE positions ADD COLUMN shares_held REAL")
        cursor = await self.conn.execute("PRAGMA table_info(trade_history)")
        th_cols = {row[1] for row in await cursor.fetchall()}
        if "pnl" not in th_cols:
            await self.conn.execute("ALTER TABLE trade_history ADD COLUMN pnl REAL DEFAULT 0")
        if "fees" not in th_cols:
            await self.conn.execute("ALTER TABLE trade_history ADD COLUMN fees REAL DEFAULT 0")
        if "exit_reason" not in th_cols:
            await self.conn.execute("ALTER TABLE trade_history ADD COLUMN exit_reason TEXT NOT NULL DEFAULT 'resolution'")
        if "position_id" not in th_cols:
            # The true link to positions. The historical implicit join (t.id = p.id)
            # only held while both AUTOINCREMENT sequences happened to run in
            # lockstep — any drift (unclosed positions, a ledger reset) silently
            # mispairs rows. Legacy rows keep NULL; readers COALESCE to t.id.
            await self.conn.execute("ALTER TABLE trade_history ADD COLUMN position_id INTEGER")
        # Hot-path indexes — get_open_positions / has_position_for_market run every tick.
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_market_status "
            "ON positions(market_id, status)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_history_exit_ts "
            "ON trade_history(exit_timestamp)"
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def open_position_and_debit_bankroll(
        self,
        new_bankroll: float,
        **position_kwargs: Any,
    ) -> int:
        """Insert the position row AND update bankroll in a single SQLite transaction.

        Either both writes happen or neither — a process crash between them can no
        longer leave the DB with a position record but no bankroll debit (or vice
        versa). Pass the same kwargs you'd pass to ``open_position``, plus the new
        bankroll value to set after the debit.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = await self.conn.execute(
                """INSERT INTO positions
                (market_id, question, side, entry_price, size, signal_score,
                 entry_timestamp, status, indicator_snapshot,
                 fee_rate, shares_held)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (position_kwargs["market_id"], position_kwargs["question"],
                 position_kwargs["side"], position_kwargs["entry_price"],
                 position_kwargs["size"], position_kwargs["signal_score"],
                 now,
                 position_kwargs.get("indicator_snapshot", ""),
                 position_kwargs.get("fee_rate"), position_kwargs.get("shares_held")),
            )
            pos_id = cursor.lastrowid
            await self.conn.execute(
                "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
                (new_bankroll,),
            )
            await self.conn.commit()
            return pos_id
        except BaseException:
            # BaseException: a Ctrl+C/cancel landing mid-transaction must roll
            # back too — the connection is shared, and a later commit from any
            # other coroutine would otherwise persist the half-done write.
            await self.conn.rollback()
            raise

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """Returns positions that need management: both 'open' (active) and 'pending_resolution' (expired, awaiting Gamma)."""
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'pending_resolution')"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_pending_resolution(self, position_id: int) -> None:
        """Mark an expired position as pending resolution — doesn't count against max_concurrent_positions."""
        await self.conn.execute(
            "UPDATE positions SET status='pending_resolution' WHERE id=?",
            (position_id,),
        )
        await self.conn.commit()

    async def _close_position_and_history(
        self, position_id: int, exit_price: float,
        pnl: float, fees: float, exit_reason: str,
    ) -> None:
        """Mark position closed and write the trade_history row.

        Pure inner step shared by every close path — does NOT commit. Callers
        wrap this together with any bankroll update inside a single transaction.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_timestamp=? WHERE id=?",
            (exit_price, now, position_id),
        )
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        )
        pos = dict(await cursor.fetchone())
        await self.conn.execute(
            """INSERT INTO trade_history
            (side, entry_price, exit_price, size,
             exit_timestamp, pnl, fees, exit_reason, position_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["side"], pos["entry_price"], exit_price, pos["size"],
             now, pnl, fees, exit_reason, position_id),
        )

    async def close_position(
        self, position_id: int, exit_price: float,
        pnl: float = 0.0, fees: float = 0.0, exit_reason: str = "resolution",
        new_bankroll: float | None = None, bankroll_delta: float | None = None,
    ) -> None:
        """Close a position atomically. Pass at most one of new_bankroll / bankroll_delta.

        * new_bankroll: set absolute (used by resolve_position — paper computes
          bankroll + revenue, live reads on-chain balance).
        * bankroll_delta: credit relative (mirror of open_position_and_debit_bankroll).
        * Neither: position-only close, no bankroll write.

        Either every write commits or none does — a crash can never leave a
        closed position with an unaccounted bankroll change (or vice versa).
        """
        if new_bankroll is not None and bankroll_delta is not None:
            raise ValueError("Pass at most one of new_bankroll / bankroll_delta")
        try:
            await self._close_position_and_history(
                position_id, exit_price, pnl, fees, exit_reason,
            )
            if new_bankroll is not None:
                await self.conn.execute(
                    "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
                    (new_bankroll,),
                )
            elif bankroll_delta is not None:
                await self.conn.execute(
                    "UPDATE bankroll SET amount = amount + ? WHERE id = 1",
                    (bankroll_delta,),
                )
            await self.conn.commit()
        except BaseException:
            # Same rationale as open_position_and_debit_bankroll: roll back on
            # cancellation too, or a foreign commit persists the half-done close.
            await self.conn.rollback()
            raise

    async def has_position_for_market(self, market_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id=? AND status IN ('open', 'pending_resolution')",
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] > 0

    async def get_open_trade_preflight(self, market_id: str) -> tuple[bool, int, float, float]:
        """Return (has_position_in_market, open_count, bankroll, deployed_usdc) in one round trip.
        Atomic snapshot — eliminates the race window where 4 separate gathered queries could
        see inconsistent views of the positions table after a concurrent insert/update.
        """
        cursor = await self.conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM positions WHERE market_id=? AND status IN ('open','pending_resolution')),"
            "  (SELECT COUNT(*) FROM positions WHERE status='open'),"
            "  (SELECT amount FROM bankroll WHERE id=1),"
            "  (SELECT COALESCE(SUM(size), 0) FROM positions WHERE status IN ('open','pending_resolution'))",
            (market_id,),
        )
        row = await cursor.fetchone()
        return (row[0] or 0) > 0, int(row[1] or 0), float(row[2] or 0.0), float(row[3] or 0.0)

    async def get_open_position_count(self) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        )
        row = await cursor.fetchone()
        return row[0]

    async def get_trade_history(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM trade_history ORDER BY exit_timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_day_stats(self, date_str: str) -> tuple[int, int, float, float]:
        """Return (wins, losses, fees, pnl_sum) for a given trading day (ET date string).

        Converts the ET date to a UTC range so trades timestamped in UTC are
        correctly bucketed into the Eastern trading day.
        """
        day_start_et = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_ET)
        day_end_et = day_start_et + timedelta(days=1)
        utc_start = day_start_et.astimezone(timezone.utc).isoformat()
        utc_end = day_end_et.astimezone(timezone.utc).isoformat()

        cursor = await self.conn.execute(
            "SELECT pnl, fees, exit_price, entry_price FROM trade_history "
            "WHERE exit_timestamp >= ? AND exit_timestamp < ?",
            (utc_start, utc_end),
        )
        rows = await cursor.fetchall()
        wins = losses = 0
        total_fees = 0.0
        total_pnl = 0.0
        for row in rows:
            pnl_val = row[0]
            fee_val = row[1] or 0.0
            exit_p = row[2]
            entry_p = row[3]
            total_fees += fee_val
            total_pnl += (pnl_val or 0.0)
            # Use stored pnl when available; fall back to price comparison for old rows
            if pnl_val is not None and pnl_val != 0:
                win = pnl_val > 0
            else:
                win = exit_p > entry_p
            if win:
                wins += 1
            else:
                losses += 1
        return wins, losses, total_fees, total_pnl

    async def set_bankroll(self, amount: float) -> None:
        await self.conn.execute(
            "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
            (amount,),
        )
        await self.conn.commit()

    async def get_bankroll(self) -> float:
        cursor = await self.conn.execute("SELECT amount FROM bankroll WHERE id=1")
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def set_peak_bankroll(self, amount: float) -> None:
        await self.conn.execute(
            "INSERT INTO peak_bankroll (id, amount) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
            (amount,),
        )
        await self.conn.commit()

    async def get_peak_bankroll(self) -> float | None:
        cursor = await self.conn.execute("SELECT amount FROM peak_bankroll WHERE id=1")
        row = await cursor.fetchone()
        return row[0] if row else None
