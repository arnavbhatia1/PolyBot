# polybot/discord_bot/bot.py
import logging
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

def create_bot(db, trader, scanner, scheduler, config):
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    bot.db = db
    bot.trader = trader
    bot.scanner = scanner
    bot.scheduler = scheduler
    bot.config = config
    bot.is_paused = False

    @bot.event
    async def on_ready():
        logger.info(f"Discord bot connected as {bot.user}")
        if hasattr(bot, 'alert_manager') and bot.alert_manager:
            await bot.alert_manager.purge_channel(bot.alert_manager.trade_channel_name)
            await bot.alert_manager.purge_channel(bot.alert_manager.control_channel_name)
            bankroll = getattr(bot, 'initial_bankroll', None) or await bot.db.get_bankroll()
            await bot.alert_manager.send_session_banner(
                mode=bot.config.get("mode", "paper"),
                bankroll=bankroll,
            )

    @bot.command(name="commands")
    async def commands_list(ctx):
        await ctx.send(
            "**PolyBot Commands**\n\n"
            "**Trading**\n"
            "`!status` — Mode, bankroll, open positions, 24h P&L\n"
            "`!positions` — All open positions with entry price, targets\n"
            "`!history [n]` — Last n closed trades (default 10)\n"
            "`!performance` — Sharpe ratio, win rate, total P&L\n"
            "`!pause` — Pause trading (keeps scanning)\n"
            "`!resume` — Resume trading\n"
            "`!mode` — Show current mode (paper/live)\n\n"
            "**Learning**\n"
            "`!agents` — Learning agent status and schedule\n"
            "`!lessons` — Top learnings from the memory system\n\n"
            "**Admin**\n"
            "`!clear [trades|control|all]` — Purge messages from channels\n"
            "`!session` — Re-send the session banner\n\n"
            "`!commands` — Show this message"
        )

    @bot.command(name="status")
    async def status(ctx):
        from polybot.discord_bot.commands import format_status
        bankroll = await bot.db.get_bankroll()
        positions = await bot.db.get_open_position_count()
        history = await bot.db.get_trade_history(limit=100)
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        pnl_24h = sum(
            t.get("size", 0) / t["entry_price"] * t.get("exit_price", 0) - t.get("size", 0)
            for t in history
            if t.get("exit_timestamp", "") >= cutoff and t.get("entry_price", 0) > 0
        )
        msg = format_status(mode=bot.config.get("mode", "paper"), is_paused=bot.is_paused,
                           open_positions=positions, bankroll=bankroll, pnl_24h=pnl_24h)
        await ctx.send(msg)

    @bot.command(name="positions")
    async def positions_cmd(ctx):
        from polybot.discord_bot.commands import format_positions
        positions = await bot.db.get_open_positions()
        await ctx.send(format_positions(positions))

    @bot.command(name="history")
    async def history(ctx, n: int = 10):
        trades = await bot.db.get_trade_history(limit=n)
        if not trades:
            await ctx.send("No trade history yet.")
            return
        lines = [f"**Last {len(trades)} Trades**\n"]
        for t in trades:
            pnl_sign = "+" if t["log_return"] >= 0 else ""
            lines.append(f"  {t['question'][:40]}... | {t['entry_price']:.2f} -> {t['exit_price']:.2f} | P&L: `{pnl_sign}{t['log_return']:.4f}`")
        await ctx.send("\n".join(lines))

    @bot.command(name="pause")
    async def pause(ctx):
        bot.is_paused = True
        await ctx.send("Trading **paused**.")

    @bot.command(name="resume")
    async def resume(ctx):
        bot.is_paused = False
        await ctx.send("Trading **resumed**.")

    @bot.command(name="mode")
    async def mode_cmd(ctx):
        await ctx.send(f"Current mode: `{bot.config.get('mode', 'paper')}`")

    @bot.command(name="lessons")
    async def lessons(ctx):
        import json
        from pathlib import Path
        lessons_path = Path("polybot/memory/lessons.json")
        if not lessons_path.exists():
            await ctx.send("No lessons recorded yet.")
            return
        data = json.loads(lessons_path.read_text())
        lines = ["**Lessons Learned**\n"]
        for key, value in list(data.items())[:10]:
            lines.append(f"  **{key}:** {value}")
        await ctx.send("\n".join(lines))

    @bot.command(name="agents")
    async def agents(ctx):
        await ctx.send(f"**Agent Status**\nOutcome Reviewer: runs every {bot.config.get('agents', {}).get('outcome_reviewer_interval_seconds', 3600)}s\n"
                       f"Daily Pipeline: runs at {bot.config.get('agents', {}).get('daily_pipeline_hour', 2)}:00 UTC")

    @bot.command(name="performance")
    async def performance(ctx):
        import math
        from polybot.discord_bot.commands import format_performance
        from datetime import datetime as dt
        trades = await bot.db.get_trade_history(limit=1000)
        if not trades:
            await ctx.send("No trades yet.")
            return
        total = len(trades)
        pnls = []
        log_returns = []
        hold_hours = []
        for t in trades:
            entry_p = t["entry_price"]
            pnl = t["size"] * (t["exit_price"] - entry_p) / entry_p if entry_p > 0 else 0
            pnls.append(pnl)
            log_returns.append(t["log_return"])
            try:
                entry_dt = dt.fromisoformat(t["entry_timestamp"])
                exit_dt = dt.fromisoformat(t["exit_timestamp"])
                hold_hours.append((exit_dt - entry_dt).total_seconds() / 3600)
            except Exception:
                pass
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / total
        total_pnl = sum(pnls)
        best = max(pnls)
        worst = min(pnls)
        avg_hold = sum(hold_hours) / len(hold_hours) if hold_hours else 0
        mean_r = sum(log_returns) / len(log_returns)
        var_r = sum((r - mean_r) ** 2 for r in log_returns) / len(log_returns) if total > 1 else 1
        std_r = math.sqrt(var_r) if var_r > 0 else 1
        sharpe = (mean_r / std_r) * math.sqrt(288) if std_r > 0 else 0  # 288 five-min periods/day
        await ctx.send(format_performance(sharpe, win_rate, total_pnl, avg_hold, total, best, worst))

    @bot.command(name="clear")
    async def clear_channels(ctx, target: str = "all"):
        """Purge messages from bot channels. Usage: !clear [trades|control|all]"""
        am = getattr(bot, 'alert_manager', None)
        if not am:
            await ctx.send("Alert manager not available.")
            return
        targets = []
        if target in ("all", "trades"):
            targets.append(("trades", am.trade_channel_name))
        if target in ("all", "control"):
            targets.append(("control", am.control_channel_name))
        if not targets:
            await ctx.send("Usage: `!clear [trades|control|all]`")
            return
        results = []
        for label, name in targets:
            count = await am.purge_channel(name)
            if count >= 0:
                results.append(f"#{name}: {count} messages cleared")
            else:
                results.append(f"#{name}: failed (check bot permissions)")
        await ctx.send("**Clear Complete**\n" + "\n".join(results))

    @bot.command(name="session")
    async def session_banner(ctx):
        """Re-send the session banner to mark a checkpoint."""
        am = getattr(bot, 'alert_manager', None)
        if not am:
            await ctx.send("Alert manager not available.")
            return
        bankroll = await bot.db.get_bankroll()
        await am.send_session_banner(
            mode=bot.config.get("mode", "paper"),
            bankroll=bankroll,
        )
        await ctx.send("Session banner sent.")

    return bot
