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
            "`!commands` — Show this message"
        )

    @bot.command(name="status")
    async def status(ctx):
        from polybot.discord_bot.commands import format_status
        bankroll = await bot.db.get_bankroll()
        positions = await bot.db.get_open_position_count()
        history = await bot.db.get_trade_history(limit=100)
        pnl_24h = sum(t.get("log_return", 0) for t in history[:10])
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

    return bot
