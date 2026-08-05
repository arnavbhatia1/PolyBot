"""SQLite database models for positions, trade history, and bankroll.

Per-mode SQLite database (polybot_paper.db / polybot_live.db). All async via aiosqlite.
Bankroll is the single source of truth for capital — never reconstruct it from trades.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

_ET = ZoneInfo("America/New_York")

class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path: str = db_path
        self.conn: aiosqlite.Connection | None = None
        # One connection, many coroutines: any other task's commit mid-transaction
        # persists the half-done write (SQLite commits the CONNECTION's open
        # transaction). Every commit-bearing method serializes through this lock.
        self._write_lock = asyncio.Lock()
        # Hot-read mirror (see _rebuild_hot_mirror): position_id -> (market_id, status, size)
        self._pos_mirror: dict[int, tuple[str, str, float]] = {}
        self._bankroll_mirror: float | None = None

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
        # Additive migrations — columns only ever appended (schema is immutable truth)
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
            # The true link to positions: an implicit t.id = p.id join only holds
            # while both AUTOINCREMENT sequences run in lockstep — any drift
            # silently mispairs rows. NULL rows are read via COALESCE to t.id.
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
        await self._rebuild_hot_mirror()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ── Hot-read mirror ───────────────────────────────────────────────────────
    # The fire path must never await the DB: these sync peeks serve the exact
    # preflight semantics from memory. Every status/bankroll writer below runs
    # under _write_lock in this single-writer process, so the mirror IS the DB.

    async def _rebuild_hot_mirror(self) -> None:
        cur = await self.conn.execute(
            "SELECT id, market_id, status, size FROM positions "
            "WHERE status IN ('open', 'pending_resolution')"
        )
        rows = await cur.fetchall()
        self._pos_mirror = {r[0]: (r[1], r[2], float(r[3] or 0.0)) for r in rows}
        cur = await self.conn.execute("SELECT amount FROM bankroll WHERE id=1")
        row = await cur.fetchone()
        self._bankroll_mirror = float(row[0]) if row else 0.0

    def preflight_peek(self, market_id: str) -> tuple[bool, int, float, float] | None:
        """Sync (has_position_in_market, open_count, bankroll, deployed_usdc);
        None until the mirror is built (callers fall back to the DB query)."""
        if self._bankroll_mirror is None:
            return None
        has = False
        open_count = 0
        deployed = 0.0
        for mid, st, sz in self._pos_mirror.values():
            if st == "open":
                open_count += 1
            deployed += sz
            if mid == market_id:
                has = True
        return has, open_count, self._bankroll_mirror, deployed

    def has_open_or_pending_market(self, market_id: str) -> bool | None:
        """Sync has_position_for_market; None until the mirror is built."""
        if self._bankroll_mirror is None:
            return None
        return any(mid == market_id for mid, _st, _sz in self._pos_mirror.values())

    def open_or_pending_count(self) -> int | None:
        """Sync count of open/pending positions; None until the mirror is built."""
        if self._bankroll_mirror is None:
            return None
        return len(self._pos_mirror)

    def open_market_count(self) -> int | None:
        """Sync count of status='open' positions; None until the mirror is built."""
        if self._bankroll_mirror is None:
            return None
        return sum(1 for _mid, st, _sz in self._pos_mirror.values() if st == "open")

    def mirror_mark_closed(self, position_id: int) -> None:
        """Hook for the one status writer outside this class (reconcile's
        direct status-only UPDATE in live_trader)."""
        self._pos_mirror.pop(position_id, None)

    async def open_position_and_debit_bankroll(
        self,
        new_bankroll: float,
        **position_kwargs: Any,
    ) -> int:
        """Insert the position row AND update bankroll in one transaction.

        Both writes or neither — a crash between them must never leave a
        position with no bankroll debit (or vice versa). Takes open_position
        kwargs plus the post-debit bankroll value.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
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
                self._pos_mirror[pos_id] = (
                    position_kwargs["market_id"], "open",
                    float(position_kwargs["size"] or 0.0))
                self._bankroll_mirror = float(new_bankroll)
                return pos_id
            except BaseException:
                # BaseException: a Ctrl+C/cancel mid-transaction must roll back too —
                # the connection is shared, and any other coroutine's later commit
                # would persist the half-done write.
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
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE positions SET status='pending_resolution' WHERE id=?",
                (position_id,),
            )
            await self.conn.commit()
            if position_id in self._pos_mirror:
                mid, _st, sz = self._pos_mirror[position_id]
                self._pos_mirror[position_id] = (mid, "pending_resolution", sz)

    async def _close_position_and_history(
        self, position_id: int, exit_price: float,
        pnl: float, fees: float, exit_reason: str,
    ) -> None:
        """Mark position closed and write the trade_history row.

        Does NOT commit — callers wrap this with any bankroll update inside
        a single transaction.
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

        * new_bankroll: set absolute (resolve_position — paper computes
          bankroll + revenue, live reads on-chain balance).
        * bankroll_delta: credit relative.
        * Neither: position-only close, no bankroll write.

        Every write commits or none does — a crash can never leave a closed
        position with an unaccounted bankroll change (or vice versa).
        """
        if new_bankroll is not None and bankroll_delta is not None:
            raise ValueError("Pass at most one of new_bankroll / bankroll_delta")
        async with self._write_lock:
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
                self._pos_mirror.pop(position_id, None)
                if new_bankroll is not None:
                    self._bankroll_mirror = float(new_bankroll)
                elif bankroll_delta is not None and self._bankroll_mirror is not None:
                    self._bankroll_mirror += float(bankroll_delta)
            except BaseException:
                # Roll back on cancellation too, or a foreign commit persists the
                # half-done close (same rationale as open_position_and_debit_bankroll).
                await self.conn.rollback()
                raise

    async def sync_entry_booking(
        self, position_id: int, entry_price: float, shares_held: float,
    ) -> bool:
        """Fill-audit write path: sync a position's entry to chain truth.

        Returns False without writing when the trade already reached history
        (resolved before the audit ran) — the check and the UPDATE run under
        the write lock so a concurrent close can't slip between them.
        """
        async with self._write_lock:
            booked = await (await self.conn.execute(
                "SELECT 1 FROM trade_history WHERE position_id=?", (position_id,)
            )).fetchone()
            if booked:
                return False
            await self.conn.execute(
                "UPDATE positions SET entry_price=?, shares_held=? WHERE id=?",
                (entry_price, shares_held, position_id),
            )
            await self.conn.commit()
            return True

    async def has_position_for_market(self, market_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id=? AND status IN ('open', 'pending_resolution')",
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] > 0

    async def get_open_trade_preflight(self, market_id: str) -> tuple[bool, int, float, float]:
        """Return (has_position_in_market, open_count, bankroll, deployed_usdc) in one round trip.

        Atomic snapshot: 4 separate gathered queries could see inconsistent
        views of the positions table around a concurrent insert/update.
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
        """Return (wins, losses, modeled_fees, pnl_sum) for a trading day (ET date).

        The ET date converts to a UTC range so UTC-timestamped trades bucket
        into the Eastern day. modeled_fees is the day's MODELED fee buffer —
        the same quantity each OPEN ping shows, so the day-close sum matches.
        No fee is currently charged on-chain on this series; nothing here is money.
        """
        day_start_et = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_ET)
        day_end_et = day_start_et + timedelta(days=1)
        utc_start = day_start_et.astimezone(timezone.utc).isoformat()
        utc_end = day_end_et.astimezone(timezone.utc).isoformat()

        cursor = await self.conn.execute(
            "SELECT t.pnl, t.fees, t.exit_price, t.entry_price, t.size, p.fee_rate, "
            "p.shares_held "
            "FROM trade_history t "
            "LEFT JOIN positions p ON COALESCE(t.position_id, t.id) = p.id "
            "WHERE t.exit_timestamp >= ? AND t.exit_timestamp < ?",
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
            size_val = row[4] or 0.0
            rate = row[5] if row[5] is not None else 0.07  # DEFAULT_FEE_RATE (base.py)
            shares_val = row[6]
            # Entry buffer in USD: rate·size·(1−entry). Stored `fees` already
            # carries an entry component of size − shares_held·entry (zero for
            # chain-audited live fills, full buffer for paper) — swap it for the
            # modeled buffer so the entry fee counts exactly once either way.
            if entry_p and 0.0 < entry_p < 1.0:
                total_fees += rate * size_val * (1.0 - entry_p)
                if shares_val is not None:
                    stored_entry = max(0.0, size_val - shares_val * entry_p)
                    total_fees -= min(stored_entry, fee_val)
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
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
                (amount,),
            )
            await self.conn.commit()
            self._bankroll_mirror = float(amount)

    async def get_bankroll(self) -> float:
        cursor = await self.conn.execute("SELECT amount FROM bankroll WHERE id=1")
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def set_peak_bankroll(self, amount: float) -> None:
        async with self._write_lock:
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
