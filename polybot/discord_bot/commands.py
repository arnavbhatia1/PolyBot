# polybot/discord_bot/commands.py

def format_status(mode, is_paused, open_positions, bankroll, pnl_24h):
    state = "PAUSED" if is_paused else "ACTIVE"
    pnl_sign = "+" if pnl_24h >= 0 else ""
    return (f"**PolyBot Status**\nMode: `{mode}` | State: `{state}`\n"
            f"Open Positions: `{open_positions}`\n"
            f"Bankroll: `${bankroll:.2f}`\n"
            f"24h P&L: `{pnl_sign}${pnl_24h:.2f}`")

def format_positions(positions, current_prices=None):
    if not positions:
        return "No open positions."
    lines = ["**Open Positions**\n"]
    for pos in positions:
        lines.append(f"**#{pos['id']}** {pos['question']}\n"
                     f"  Side: `{pos['side']}` | Entry: `{pos['entry_price']:.2f}` | "
                     f"Size: `${pos['size']:.2f}`\n"
                     f"  Target: `{pos['exit_target']:.2f}` | Stop: `{pos['stop_loss']:.2f}`")
    return "\n".join(lines)

def format_performance(sharpe_ratio, win_rate, total_pnl, avg_hold_hours, total_trades, best_trade, worst_trade):
    pnl_sign = "+" if total_pnl >= 0 else ""
    return (f"**Performance**\nSharpe Ratio: `{sharpe_ratio:.2f}`\n"
            f"Win Rate: `{win_rate:.0%}` ({total_trades} trades)\n"
            f"Total P&L: `{pnl_sign}${total_pnl:.2f}`\n"
            f"Avg Hold Time: `{avg_hold_hours:.1f}h`\n"
            f"Best Trade: `+${best_trade:.2f}` | Worst: `${worst_trade:.2f}`")
