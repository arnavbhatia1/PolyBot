#!/usr/bin/env python
"""Clean-slate the PAPER experiment so the data reflects the CURRENT bot.

WHY: the live bankroll carried a ~$800 offset (an old seed/top-up whose origin isn't
recoverable from the records) on top of real trade P&L, plus pre-fix contamination from
the old code. The trade rows themselves are clean (verified: no dupes, no nulls, pnl
reconciles to real $). So "cleaning" = a fresh start at a defined baseline, not deleting
junk. After this, bankroll == start + cum(real pnl) exactly — no offset, nothing to misread.

WHAT IT DOES (polybot_paper.db only):
  - BACKS UP the whole DB + archives the memory record dirs (outcomes/counterfactuals/
    ghost_outcomes) to backups/reset_<ts>/  (reversible — nothing is destroyed).
  - Clears trade_history + positions (the ledger).
  - Sets bankroll and peak_bankroll to --start (default 1000).
  - KEEPS window_labels (the sniper kill-bar harness needs them).
  - Does NOT touch window_paths.db (the recorder sensor / kill-bar corpus).

MUST be run with the trading bot STOPPED (it writes the DB on every trade; a concurrent
write would corrupt this). Run it before a fresh-baseline relaunch, then start the
bot — it boots on the clean baseline.

  python scripts/reset_paper_clean.py --dry-run          # preview, change nothing
  python scripts/reset_paper_clean.py --start 1000        # prompts, then resets
  python scripts/reset_paper_clean.py --start 1000 --yes  # no prompt
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "polybot" / "db" / "polybot_paper.db"
MEM = ROOT / "polybot" / "memory"
ARCHIVE_DIRS = ("outcomes", "counterfactuals", "ghost_outcomes")
KEEP_TABLES = ("window_labels",)                         # NOT cleared (kill-bar corpus)
CLEAR_TABLES = ("trade_history", "positions")            # the ledger


def summarize(db_path: Path) -> dict:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = {}
    for t in CLEAR_TABLES + KEEP_TABLES:
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = "(absent)"
    for t in ("bankroll", "peak_bankroll"):
        try:
            out[t] = c.execute(f"SELECT amount FROM {t} WHERE id=1").fetchone()[0]
        except (sqlite3.OperationalError, TypeError):
            out[t] = None
    c.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=1000.0, help="fresh paper bankroll (default 1000)")
    ap.add_argument("--dry-run", action="store_true", help="preview only, change nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: {DB} not found"); sys.exit(1)
    if args.start <= 0:
        print("ERROR: --start must be > 0"); sys.exit(1)

    before = summarize(DB)
    print(f"DB: {DB}")
    print("  BEFORE:", {k: (f"{v:.2f}" if isinstance(v, float) else v) for k, v in before.items()})
    print(f"\nPLAN: clear {CLEAR_TABLES}; set bankroll & peak_bankroll -> ${args.start:.2f}; "
          f"KEEP {KEEP_TABLES}; archive memory dirs {ARCHIVE_DIRS}.")
    if args.dry_run:
        print("\n--dry-run: nothing changed.")
        return

    print("\n*** The trading bot MUST be stopped (concurrent writes corrupt the DB). ***")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Aborted. No changes made."); return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "backups" / f"reset_{ts}"
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB, backup / DB.name)                      # full DB backup (reversible)
    for d in ARCHIVE_DIRS:
        src = MEM / d
        if src.exists():
            shutil.move(str(src), str(backup / d))          # archive memory records
            (MEM / d).mkdir(parents=True, exist_ok=True)    # leave an empty dir for the bot
    print(f"  backed up DB + archived memory -> {backup}")

    conn = sqlite3.connect(str(DB))
    try:
        cur = conn.cursor()
        for t in CLEAR_TABLES:
            try:
                cur.execute(f"DELETE FROM {t}")
            except sqlite3.OperationalError:
                pass
        for t in ("bankroll", "peak_bankroll"):
            cur.execute(f"INSERT INTO {t} (id, amount) VALUES (1, ?) "
                        f"ON CONFLICT(id) DO UPDATE SET amount=excluded.amount", (args.start,))
        conn.commit()
        cur.execute("VACUUM")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ERROR (rolled back): {e}\nRestore from {backup} if needed."); sys.exit(1)
    finally:
        conn.close()

    print("  AFTER :", {k: (f"{v:.2f}" if isinstance(v, float) else v) for k, v in summarize(DB).items()})
    print(f"\nDONE. Clean baseline ${args.start:.2f}. Backup: {backup}. Start the bot to begin fresh.")


if __name__ == "__main__":
    main()
