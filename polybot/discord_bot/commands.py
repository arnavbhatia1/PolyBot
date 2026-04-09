# polybot/discord_bot/commands.py
from __future__ import annotations

from typing import Any


def format_status(mode: str, is_paused: bool, open_positions: int, bankroll: float,
                  pnl_24h: float) -> str:
    state = "PAUSED" if is_paused else "ACTIVE"
    pnl_sign = "+" if pnl_24h >= 0 else ""
    return (f"**PolyBot Status**\nMode: `{mode}` | State: `{state}`\n"
            f"Open Positions: `{open_positions}`\n"
            f"Bankroll: `${bankroll:.2f}`\n"
            f"24h P&L: `{pnl_sign}${pnl_24h:.2f}`")

def format_positions(positions: list[dict[str, Any]],
                     current_prices: dict[str, float] | None = None) -> str:
    if not positions:
        return "No open positions."
    lines = ["**Open Positions**\n"]
    for pos in positions:
        lines.append(f"**#{pos['id']}** {pos['question']}\n"
                     f"  Side: `{pos['side']}` | Entry: `{pos['entry_price']:.2f}` | "
                     f"Size: `${pos['size']:.2f}`\n"
                     f"  Target: `{pos['exit_target']:.2f}` | Stop: `{pos['stop_loss']:.2f}`")
    return "\n".join(lines)

def format_performance(sharpe_ratio: float, win_rate: float, total_pnl: float,
                       avg_hold_hours: float, total_trades: int, best_trade: float,
                       worst_trade: float) -> str:
    pnl_sign = "+" if total_pnl >= 0 else ""
    return (f"**Performance**\nSharpe Ratio: `{sharpe_ratio:.2f}`\n"
            f"Win Rate: `{win_rate:.0%}` ({total_trades} trades)\n"
            f"Total P&L: `{pnl_sign}${total_pnl:.2f}`\n"
            f"Avg Hold Time: `{avg_hold_hours:.1f}h`\n"
            f"Best Trade: `+${best_trade:.2f}` | Worst: `${worst_trade:.2f}`")
