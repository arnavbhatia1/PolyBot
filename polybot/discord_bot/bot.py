"""Discord bot for PolyBot — commands and real-time trade alerts.

Commands: !status, !history, !pause, !resume, !clear, !session, !pipeline, !commands.
Alerts fire on trade open/close, circuit breaker events, and daily session banners.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from polybot.paths import PIPELINE_RUN_LOG_PATH, CALIBRATION_PARAMS_PATH

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def _slug_to_window(slug: str) -> str:
    """Convert btc-updown-5m-1776691500 to '9:25-9:30 ET'."""
    try:
        from datetime import timedelta
        ts = int(slug.rsplit("-", 1)[-1])
        start = datetime.fromtimestamp(ts, tz=_ET)
        end = start + timedelta(minutes=5)
        return f"{start.strftime('%I:%M').lstrip('0')}-{end.strftime('%I:%M ET').lstrip('0')}"
    except Exception:
        return slug


def create_bot(db: Any, trader: Any, scanner: Any, scheduler: Any,
               config: dict[str, Any]) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    bot.db = db
    bot.trader = trader
    bot.scanner = scanner
    bot.scheduler = scheduler
    bot.config = config
    bot.is_paused = False
    bot.ready_event = asyncio.Event()

    @bot.event
    async def on_ready():
        logger.debug(f"Discord bot connected as {bot.user}")
        bot.ready_event.set()

    @bot.event
    async def on_command_error(ctx, error):
        """Collapse noisy Discord tracebacks to single-line warnings.

        Unknown commands (user typos) and Discord API hiccups (5xx) shouldn't dump
        a 20-line stack trace — the bot's main trading loop is unaffected.
        """
        if isinstance(error, commands.CommandNotFound):
            logger.debug(f"Unknown Discord command: {ctx.message.content!r}")
            return
        if isinstance(error, commands.CommandInvokeError):
            root = error.original
            if isinstance(root, discord.HTTPException) and 500 <= root.status < 600:
                logger.warning(f"Discord API {root.status} on !{ctx.command}: transient, ignoring")
                return
            logger.error(f"!{ctx.command} failed: {type(root).__name__}: {root}")
            return
        logger.warning(f"Discord command error on !{ctx.command}: {type(error).__name__}: {error}")

    @bot.command(name="commands")
    async def commands_list(ctx):
        await ctx.send(
            "**PolyBot Commands**\n"
            "`!status` — Bankroll, P&L, open positions, performance, current window\n"
            "`!history [n]` — Last n closed trades (default 10)\n"
            "`!pause` / `!resume` — Pause or resume entries\n"
            "`!clear [trades|control|all]` — Purge channel messages\n"
            "`!session` — Re-send session banner\n"
            "`!pipeline` — Last run, adopted changes, calibrator state, next run"
        )

    @bot.command(name="status")
    async def status(ctx):
        bankroll = await bot.db.get_bankroll()
        open_positions = await bot.db.get_open_positions()
        today_et = datetime.now(_ET).strftime("%Y-%m-%d")
        day_wins, day_losses, _, pnl_24h = await bot.db.get_day_stats(today_et)

        # Lifetime performance
        trades = await bot.db.get_trade_history(limit=999999)
        pnls, gain_pcts = [], []
        today_gain_pcts = []
        for t in trades:
            size = t.get("size", 0)
            entry_p = t.get("entry_price", 0)
            stored = t.get("pnl")
            if stored is not None and stored != 0:
                pnl = stored
            elif entry_p > 0 and size > 0:
                pnl = (size / entry_p) * t.get("exit_price", 0) - size
            else:
                pnl = 0
            pnls.append(pnl)
            gp = pnl / size if size > 0 else 0
            gain_pcts.append(gp)
            # Bucket by ET calendar day. exit_timestamp is UTC, so convert to ET first —
            # a naive [:10] compares a UTC date to the ET date and mixes in the prior evening.
            exit_ts = t.get("exit_timestamp", "")
            if exit_ts:
                try:
                    et_day = datetime.fromisoformat(
                        exit_ts.replace("Z", "+00:00")).astimezone(_ET).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    et_day = ""
                if et_day == today_et:
                    today_gain_pcts.append(gp)

        total = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / total if total else 0
        total_pnl = sum(pnls)
        mean_r = sum(gain_pcts) / total if total else 0
        var_r = sum((r - mean_r) ** 2 for r in gain_pcts) / total if total > 1 else 1
        sharpe = mean_r / math.sqrt(var_r) if var_r > 0 else 0

        # Today's Sharpe
        day_total = day_wins + day_losses
        day_wr = day_wins / day_total if day_total else 0
        if len(today_gain_pcts) > 1:
            m = sum(today_gain_pcts) / len(today_gain_pcts)
            v = sum((r - m) ** 2 for r in today_gain_pcts) / len(today_gain_pcts)
            day_sharpe = m / math.sqrt(v) if v > 0 else 0
        else:
            day_sharpe = 0

        # Build message
        state = "PAUSED" if bot.is_paused else "ACTIVE"
        mode = bot.config.get("mode", "paper").upper()
        pnl_sign = "+" if pnl_24h >= 0 else ""
        total_sign = "+" if total_pnl >= 0 else ""

        lines = [
            f"**PolyBot** `{mode}` | `{state}`",
            f"Bankroll: `${bankroll:.2f}`",
            f"Today: `{pnl_sign}${pnl_24h:.2f}` | `{day_wr:.0%}` WR ({day_wins}W/{day_losses}L) | Sharpe: `{day_sharpe:.3f}`",
            f"All-time: `{total_sign}${total_pnl:.2f}` | WR: `{win_rate:.0%}` ({total} trades) | Sharpe: `{sharpe:.3f}`",
        ]

        if open_positions:
            lines.append(f"\n**Open Positions ({len(open_positions)})**")
            for pos in open_positions:
                lines.append(
                    f"  #{pos['id']} `{pos['side']}` @ `{pos['entry_price']:.3f}` | "
                    f"`${pos['size']:.2f}` | `{pos['status']}`"
                )
        else:
            lines.append("No open positions.")

        try:
            contract = await bot.scanner.find_active_contract()
            if contract:
                lines.append(
                    f"\n**Current Window** `{_slug_to_window(contract['slug'])}`  "
                    f"`{contract['seconds_remaining']:.0f}s` left  "
                    f"Up=`{contract['price_up']:.3f}` Dn=`{contract['price_down']:.3f}`"
                )
        except Exception:
            pass

        await ctx.send("\n".join(lines))

    @bot.command(name="history")
    async def history(ctx, n: int = 10):
        # Clamp to a sane range: negative n → SQLite LIMIT -1 (all rows); huge n →
        # a >2000-char message Discord rejects. Bound to [1, 50].
        n = max(1, min(50, n))
        trades = await bot.db.get_trade_history(limit=n)
        if not trades:
            await ctx.send("No trade history yet.")
            return

        lines = [f"**Last {len(trades)} Trades**"]
        lines.append("```")
        lines.append(f"{'Time':<12} {'Side':<5} {'Type':<6} {'Entry':<7} {'Exit':<7} {'Size':<7} {'PnL':<9} {'R%'}")
        lines.append("-" * 65)
        for t in trades:
            entry = t.get("entry_price", 0)
            exit_p = t.get("exit_price", 0)
            size = t.get("size", 0)
            pnl = t.get("pnl") or ((size / entry) * exit_p - size if entry > 0 and size > 0 else 0)
            gain_pct = pnl / size if size > 0 else 0
            exit_reason = t.get("exit_reason", "res")
            trade_type = "SCALP" if exit_reason == "scalp" else "HOLD"
            side = t.get("side", "?")
            # Format time from exit_timestamp
            exit_ts = t.get("exit_timestamp", "")
            try:
                dt = datetime.fromisoformat(exit_ts.replace("Z", "+00:00")).astimezone(_ET)
                time_str = dt.strftime("%I:%M %p").lstrip("0")
            except Exception:
                time_str = "?"
            pnl_str = f"${pnl:+.2f}"
            lines.append(
                f"{time_str:<12} {side:<5} {trade_type:<6} {entry:<7.3f} {exit_p:<7.3f} "
                f"${size:<6.0f} {pnl_str:<9} {gain_pct:+.0%}"
            )
        lines.append("```")
        await ctx.send("\n".join(lines))

    @bot.command(name="pause")
    async def pause(ctx):
        bot.is_paused = True
        await ctx.send("Trading **paused**.")

    @bot.command(name="resume")
    async def resume(ctx):
        bot.is_paused = False
        await ctx.send("Trading **resumed**.")

    @bot.command(name="clear")
    async def clear_channels(ctx, target: str = "all", confirm: str = ""):
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
            await ctx.send("Usage: `!clear [trades|control|all] confirm`")
            return
        # Confirmation guard. Purges Discord chat history only (never the DB /
        # memory / positions — verified), but it's irreversible message deletion,
        # so require an explicit `confirm` token.
        if confirm != "confirm":
            chan_list = ", ".join(f"#{name}" for _, name in targets)
            await ctx.send(
                f"`!clear {target}` will delete recent messages from {chan_list} "
                f"(chat only — no trade/financial data is touched). "
                f"Re-run `!clear {target} confirm` to proceed."
            )
            return
        results = []
        for label, name in targets:
            count = await am.purge_channel(name)
            results.append(f"#{name}: {count} messages cleared" if count >= 0 else f"#{name}: failed")
        await ctx.send("**Clear Complete**\n" + "\n".join(results))

    @bot.command(name="pipeline")
    async def pipeline_status(ctx):
        run_log_path = PIPELINE_RUN_LOG_PATH
        cal_path = CALIBRATION_PARAMS_PATH

        last_run = "no data"
        status_line = "no data"
        source_line = "?"
        baseline_line = "n/a"
        holdout_line = "skipped (insufficient data)"
        if run_log_path.exists():
            try:
                runs = json.loads(run_log_path.read_text())
                if runs:
                    latest = runs[-1]
                    dt = datetime.fromisoformat(latest["date"].replace("Z", "+00:00")).astimezone(_ET)
                    last_run = dt.strftime("%Y-%m-%d %H:%M ET")
                    source_line = latest.get("source", "?")
                    baseline_sharpe = latest.get("baseline_sharpe", 0.0)
                    n_baseline = latest.get("n_baseline_trades", 0)
                    if n_baseline:
                        baseline_line = f"{baseline_sharpe:+.3f} Sharpe on {n_baseline:,} trades"
                    else:
                        baseline_line = f"{baseline_sharpe:+.3f} Sharpe"
                    changes = latest.get("changes", [])
                    if isinstance(changes, list):
                        adopted = [c for c in changes if c.get("decision") == "adopted"]
                        rejected = [c for c in changes if c.get("decision") != "adopted"]
                        status_line = f"{len(adopted)} adopted / {len(rejected)} rejected"
                        h_changes = [c for c in changes if "holdout_candidate_sharpe" in c]
                        if h_changes:
                            h_base = h_changes[0].get("holdout_baseline_sharpe", 0.0)
                            improving = sum(1 for c in h_changes
                                            if c.get("holdout_candidate_sharpe", 0) >= c.get("holdout_baseline_sharpe", 0))
                            holdout_line = (f"baseline {h_base:+.3f} Sharpe, "
                                            f"{improving}/{len(h_changes)} non-degrading")
                    elif isinstance(changes, dict):
                        status_line = f"{len(changes)} adopted / 0 rejected"
                    else:
                        status_line = "no proposals"
            except Exception:
                pass

        cal_line = "unknown"
        if cal_path.exists():
            try:
                cal_data = json.loads(cal_path.read_text())
                if cal_data.get("type") == "isotonic":
                    n_samples = cal_data.get("n_samples", 0)
                    y_thr = cal_data.get("y_thresholds", [])
                    if y_thr:
                        cal_line = f"isotonic, n={n_samples}, span [{y_thr[0]:.3f}, {y_thr[-1]:.3f}]"
                    else:
                        cal_line = f"isotonic, n={n_samples}"
                else:
                    cal_line = "identity (no transform)"
            except Exception:
                pass

        # Pipeline runs 23:45 ET (run_polybot.ps1, CLAUDE.md §11/§16).
        now_et = datetime.now(_ET)
        next_run = now_et.replace(hour=23, minute=45, second=0, microsecond=0)
        if next_run <= now_et:
            next_run = next_run + timedelta(days=1)
        delta = next_run - now_et
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        next_str = f"{next_run.strftime('%Y-%m-%d %H:%M ET')} (in {hours}h {mins}m)"

        await ctx.send(
            f"**Pipeline**\n"
            f"```\n"
            f"  Last run     {last_run}\n"
            f"  Status       {status_line}\n"
            f"  Source       {source_line}\n"
            f"  Baseline     {baseline_line}\n"
            f"  Calibrator   {cal_line}\n"
            f"  Holdout      {holdout_line}\n"
            f"  Next run     {next_str}\n"
            f"```"
        )

    @bot.command(name="session")
    async def session_banner(ctx):
        am = getattr(bot, 'alert_manager', None)
        if not am:
            await ctx.send("Alert manager not available.")
            return
        bankroll = await bot.db.get_bankroll()
        await am.send_session_banner(mode=bot.config.get("mode", "paper"), bankroll=bankroll)
        await ctx.send("Session banner sent.")

    return bot
