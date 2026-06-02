#!/usr/bin/env python
"""Safely add funds to the PAPER bankroll without disturbing the experiment.

Why this is safe:
  - It ONLY changes the single `bankroll.amount` row.
  - It NEVER touches `trade_history` (your data), positions, or any record file.
  - It leaves `peak_bankroll` alone by default (the circuit-breaker tier keys off
    peak; changing it would shift sizing behaviour mid-test). Optionally bump it.
  - Every metric we measure (Sharpe, log-loss, win-rate) is per-trade RETURN
    (pnl/size), which is independent of the bankroll level — so a top-up does not
    distort any result. It only changes absolute $ bet size and keeps the bot
    above the $1 min-order floor so it can keep trading and logging.

IMPORTANT: stop the trading bot before running this (or run it at the daily
restart boundary). The bot reads/writes bankroll on every trade; topping up while
it's mid-resolution could be clobbered by an absolute-set close. With the bot
stopped, the next boot reads the new amount cleanly.

Usage:
  python topup_paper_bankroll.py --add 100        # add $100 to current bankroll
  python topup_paper_bankroll.py --target 100     # set bankroll to exactly $100
  python topup_paper_bankroll.py --add 100 --bump-peak   # also raise peak if exceeded
  python topup_paper_bankroll.py                  # show current state only (no change)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Script lives in scripts/; the repo root is one level up.
DB = Path(__file__).resolve().parent.parent / "polybot" / "db" / "polybot_paper.db"


def _get(cur, table):
    try:
        row = cur.execute(f"SELECT amount FROM {table} WHERE id=1").fetchone()
        return float(row[0]) if row else None
    except sqlite3.OperationalError:
        return None


def main():
    ap = argparse.ArgumentParser(description="Top up the paper bankroll safely.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--add", type=float, help="dollars to ADD to current bankroll")
    g.add_argument("--target", type=float, help="set bankroll to this exact amount")
    ap.add_argument("--bump-peak", action="store_true",
                    help="also raise peak_bankroll if the new bankroll exceeds it")
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: paper DB not found at {DB}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    bankroll = _get(cur, "bankroll")
    peak = _get(cur, "peak_bankroll")
    n_trades = cur.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
    print(f"DB: {DB}")
    print(f"  current bankroll : ${bankroll:.2f}" if bankroll is not None else "  current bankroll : (none)")
    print(f"  peak bankroll    : ${peak:.2f}" if peak is not None else "  peak bankroll    : (none)")
    print(f"  trade_history    : {n_trades} rows (UNTOUCHED)")

    if args.add is None and args.target is None:
        print("\nNo change requested. Pass --add X or --target Y to modify. (trade_history is never touched.)")
        return

    if bankroll is None:
        bankroll = 0.0
    new_amount = (bankroll + args.add) if args.add is not None else args.target
    if new_amount <= 0:
        print("ERROR: resulting bankroll must be > 0")
        sys.exit(1)

    print(f"\nWILL SET bankroll: ${bankroll:.2f} -> ${new_amount:.2f}")
    if peak is not None and new_amount > peak:
        if args.bump_peak:
            print(f"WILL ALSO raise peak: ${peak:.2f} -> ${new_amount:.2f} (circuit-breaker tier shifts up)")
        else:
            print(f"NOTE: new bankroll exceeds peak (${peak:.2f}); the bot will ratchet peak up on next read "
                  f"anyway. Leaving peak as-is (use --bump-peak to set it now).")
    resp = input("Proceed? [y/N] ").strip().lower()
    if resp != "y":
        print("Aborted. No changes made.")
        return

    try:
        cur.execute(
            "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount", (new_amount,))
        if args.bump_peak and peak is not None and new_amount > peak:
            cur.execute(
                "INSERT INTO peak_bankroll (id, amount) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount", (new_amount,))
        conn.commit()
        print(f"DONE. bankroll is now ${_get(cur, 'bankroll'):.2f}. "
              f"trade_history still {n_trades} rows. Restart the bot if it wasn't already stopped.")
    except Exception as e:
        conn.rollback()
        print(f"ERROR (rolled back, no change): {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
