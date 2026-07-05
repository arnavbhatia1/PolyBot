"""One-shot correction for live trade #1 (2026-07-04, btc-updown-5m-1783212000).

The boot recovery on 93a04b3e^ booked the first live sniper win as
`reconcile_recovery_unknown` at entry price (pnl -0.08) because the CLOB book
for the hours-old market was gone. Gamma's pinned prices (Up=0, Down=1,
closed) prove the Down position resolved at $1. Bankroll is already correct
(synced from chain) — this fixes only the trade record.

Run with the bot STOPPED: python scripts/correct_trade1_win.py
Idempotent (guards on the wrong exit_reason). Delete this script after use.
"""
import sqlite3

SHARES = 7.5552782608695646
SIZE = 6.99                      # entry fee included in size
PNL = round(SHARES * 1.0 - SIZE, 4)          # +0.5653; exit fee at $1.00 is 0
ENTRY_FEE = round(0.07 * SHARES * 0.92 * 0.08, 4)  # rate*shares*p*(1-p) = 0.0389

db = sqlite3.connect("polybot/db/polybot_live.db")
cur = db.execute(
    "UPDATE trade_history SET exit_price=1.0, pnl=?, fees=?, "
    "exit_reason='reconcile_recovery_gamma_win' "
    "WHERE id=1 AND exit_reason='reconcile_recovery_unknown'",
    (PNL, ENTRY_FEE),
)
db.execute("UPDATE positions SET exit_price=1.0 WHERE id=1 AND status='closed'")
db.commit()
print(f"trade_history rows corrected: {cur.rowcount} (1 = done, 0 = already corrected)")
print(f"trade 1 now: WIN, exit 1.00, pnl +{PNL}, fees {ENTRY_FEE}")
