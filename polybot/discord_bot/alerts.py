from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import discord

ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

class AlertManager:
    def __init__(self, bot: Any, trade_channel_name: str, control_channel_name: str,
                 daily_channel_name: str = "polybot-daily") -> None:
        self.bot: Any = bot
        self.trade_channel_name: str = trade_channel_name
        self.control_channel_name: str = control_channel_name
        self.daily_channel_name: str = daily_channel_name
        self.session_id: str = uuid.uuid4().hex[:8]

    def _get_channel(self, name: str) -> Any:
        if not hasattr(self, '_channel_cache'):
            self._channel_cache: dict[str, Any] = {}
        if name not in self._channel_cache:
            for guild in self.bot.guilds:
                for channel in guild.text_channels:
                    if channel.name == name:
                        self._channel_cache[name] = channel
                        break
        return self._channel_cache.get(name)

    async def _send_to_channels(self, msg: str, channels: list[str]) -> None:
        for name in channels:
            channel = self._get_channel(name)
            if channel:
                try:
                    await channel.send(msg)
                except Exception as e:
                    logger.warning(f"Failed to send to #{name}: {e}")

    async def send_trade_opened(self, question: str, side: str, size: float, entry_price: float,
                                ev: float, exit_target: float,
                                model_prob: float = 0.0, market_price: float = 0.0,
                                fee: float = 0.0, flow: float = 0.0) -> None:
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        window = question.replace("Bitcoin Up or Down - ", "") if question else ""
        await channel.send(
            f"**OPEN {side}**  {window}\n"
            f"```\n"
            f"  Price     {entry_price:.3f}\n"
            f"  Size      ${size:.2f}\n"
            f"  Edge      {ev:+.0%}   (model {model_prob:.0%}  mkt {market_price:.0%})\n"
            f"  Flow      {flow:+.2f}\n"
            f"  Fee       ${fee:.2f}\n"
            f"```")

    async def send_trade_closed(self, question: str, exit_price: float, log_return: float,
                                hold_hours: float,
                                side: str = "", entry_price: float = 0.0, pnl: float = 0.0,
                                gain_pct: float = 0.0, reason: str = "",
                                fees: float = 0.0) -> None:
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        tag = "PROFIT" if pnl >= 0 else "LOSS"
        window = question.replace("Bitcoin Up or Down - ", "") if question else ""
        await channel.send(
            f"**CLOSE {tag} {side}**  {window}\n"
            f"```\n"
            f"  Entry     {entry_price:.3f}  ->  {exit_price:.3f}\n"
            f"  Return    {gain_pct:+.1%}   (${pnl:+.2f})\n"
            f"  Fees      ${fees:.2f}\n"
            f"  Reason    {reason}\n"
            f"```")

    async def send_pipeline_summary(self, summary: str) -> None:
        channel = self._get_channel(self.daily_channel_name)
        if not channel:
            return
        await channel.send(f"**Learning Pipeline Complete**\n{summary}")

    async def send_strategy_recommendation(self, recommendations: list[Any]) -> None:
        channel = self._get_channel(self.control_channel_name)
        if not channel:
            return
        lines = ["**Strategy Recommendation**\n"]
        for rec in recommendations:
            lines.append(f"`{rec.param}`: {rec.current_value} -> {rec.recommended_value}")
            lines.append(f"  Reason: {rec.reason}")
        msg = await channel.send("\n".join(lines))
        await msg.add_reaction("\u2705")
        await msg.add_reaction("\u274c")

    async def send_error(self, error_message: str) -> None:
        channel = self._get_channel(self.control_channel_name)
        if not channel:
            return
        await channel.send(f"**Error**\n```{error_message}```")

    async def send_session_banner(self, mode: str, bankroll: float) -> None:
        """Send a session start banner to both channels to mark a new bot run."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        banner = (
            f"\n{'━' * 38}\n"
            f"**POLYBOT SESSION STARTED**\n"
            f"`{now}` | Session `{self.session_id}`\n"
            f"Mode: `{mode}` | Bankroll: `${bankroll:,.2f}`\n"
            f"{'━' * 38}"
        )
        await self._send_to_channels(banner, [self.trade_channel_name, self.daily_channel_name])

    async def send_day_open(self, mode: str, bankroll: float) -> None:
        """Log start of trading day to trade and daily channels."""
        now = datetime.now(ET)
        msg = (
            f"\n{'─' * 38}\n"
            f"**TRADING DAY OPEN** — {now.strftime('%A, %B %d %Y')}\n"
            f"Mode: `{mode}` | Bankroll: `${bankroll:,.2f}`\n"
            f"{'─' * 38}"
        )
        await self._send_to_channels(msg, [self.trade_channel_name, self.daily_channel_name])

    async def send_day_close(self, bankroll: float, day_pnl: float, wins: int, losses: int,
                             fees: float = 0.0):
        """Log end of trading day to trade and daily channels."""
        now = datetime.now(ET)
        total = wins + losses
        wr = wins / total if total > 0 else 0
        msg = (
            f"\n{'─' * 38}\n"
            f"**TRADING DAY CLOSE** — {now.strftime('%A, %B %d %Y')}\n"
            f"Bankroll: `${bankroll:,.2f}` | Day P&L: `${day_pnl:+,.2f}`\n"
            f"Trades: `{total}` ({wins}W / {losses}L) | Win Rate: `{wr:.0%}`\n"
            f"Total Fees: `${fees:,.2f}`\n"
            f"{'─' * 38}"
        )
        await self._send_to_channels(msg, [self.trade_channel_name, self.daily_channel_name])

    async def send_circuit_breaker(self, event: str, breaker: Any) -> None:
        """Alert when circuit breaker state changes."""
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        if event == "streak_losses":
            await channel.send(
                f"**CIRCUIT BREAKER** — {breaker.consecutive_losses} consecutive losses. "
                f"Drawdown `{breaker.drawdown_pct:.1%}` | "
                f"Kelly at `{breaker.kelly_multiplier:.0%}`.")
        elif event == "streak_wins":
            await channel.send(
                f"**CIRCUIT BREAKER** — {breaker.consecutive_wins} consecutive wins. "
                f"Drawdown `{breaker.drawdown_pct:.1%}` | "
                f"Kelly at `{breaker.kelly_multiplier:.0%}`.")

    async def purge_channel(self, channel_name: str, limit: int = 200) -> int:
        """Delete up to `limit` messages from a channel. Returns count deleted, or -1 on error."""
        channel = self._get_channel(channel_name)
        if not channel:
            logger.warning(f"Channel #{channel_name} not found for purge")
            return -1
        try:
            deleted = await channel.purge(limit=limit)
            return len(deleted)
        except discord.Forbidden:
            logger.error(f"Missing 'Manage Messages' permission for #{channel_name}")
            return -1
        except Exception as e:
            logger.error(f"Failed to purge #{channel_name}: {e}")
            return -1

    async def send_daily_report(self, outcomes: list[dict[str, Any]], analysis: dict[str, Any],
                                recommendations: dict[str, Any], config_changes: dict[str, Any]) -> None:
        """Post end-of-day report to the daily channel."""
        channel = self._get_channel(self.daily_channel_name)
        if not channel:
            logger.warning(f"Channel #{self.daily_channel_name} not found for daily report")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)

        # Filter today's trades
        todays = []
        for o in outcomes:
            ts = o.get("timestamp", "")
            if ts.startswith(today):
                todays.append(o)

        if not todays:
            await channel.send(f"**Daily Report — {today}**\nNo trades today.")
            return

        # --- P&L ---
        total_pnl = 0.0
        total_fees = 0.0
        wins, losses = 0, 0
        best_trade, worst_trade = None, None
        best_pnl, worst_pnl = -999, 999

        for o in todays:
            # Use recorded pnl/fees if available (fee-adjusted), else fallback to raw calc
            pnl = o.get("pnl", 0)
            fees = o.get("fees", 0)
            if pnl == 0 and fees == 0:
                entry = o.get("entry_price", 0)
                exit_p = o.get("exit_price", 0)
                size = o.get("size", 0)
                snap = o.get("indicator_snapshot", {})
                ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
                size = ctx.get("size", size) or size
                if entry > 0 and size > 0:
                    pnl = (size / entry) * exit_p - size
            total_pnl += pnl
            total_fees += fees

            if o.get("correct", False):
                wins += 1
            else:
                losses += 1

            if pnl > best_pnl:
                best_pnl = pnl
                best_trade = o
            if pnl < worst_pnl:
                worst_pnl = pnl
                worst_trade = o

        total = wins + losses
        win_rate = wins / total if total > 0 else 0

        # --- Win rate by hour (EST) ---
        hour_buckets: dict[int, list[bool]] = {}
        for o in todays:
            ts = o.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                et_hour = dt.astimezone(ET).hour
                hour_buckets.setdefault(et_hour, []).append(o.get("correct", False))
            except (ValueError, AttributeError):
                pass

        hour_lines = []
        for h in sorted(hour_buckets):
            bucket = hour_buckets[h]
            h_wins = sum(bucket)
            h_total = len(bucket)
            h_wr = h_wins / h_total if h_total > 0 else 0
            bar = "+" * h_wins + "-" * (h_total - h_wins)
            hour_lines.append(f"  {h:2d}:00  {bar:<12s} {h_wr:5.0%}  ({h_wins}W/{h_total - h_wins}L)")

        # --- Edge breakdown ---
        edge_buckets = {"10-20%": [0, 0], "20-30%": [0, 0], "30%+": [0, 0]}
        for o in todays:
            snap = o.get("indicator_snapshot", {})
            ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
            edge = ctx.get("edge", 0)
            won = o.get("correct", False)
            if edge < 0.20:
                edge_buckets["10-20%"][0] += 1
                edge_buckets["10-20%"][1] += int(won)
            elif edge < 0.30:
                edge_buckets["20-30%"][0] += 1
                edge_buckets["20-30%"][1] += int(won)
            else:
                edge_buckets["30%+"][0] += 1
                edge_buckets["30%+"][1] += int(won)

        edge_lines = []
        for bucket, (count, w) in edge_buckets.items():
            if count > 0:
                edge_lines.append(f"  {bucket:<8s} {w}/{count} wins  ({w/count:.0%})")

        # --- Side breakdown ---
        up_w = sum(1 for o in todays if o.get("side") == "Up" and o.get("correct"))
        up_t = sum(1 for o in todays if o.get("side") == "Up")
        dn_w = sum(1 for o in todays if o.get("side") == "Down" and o.get("correct"))
        dn_t = sum(1 for o in todays if o.get("side") == "Down")

        # --- Avg edge ---
        edges = []
        for o in todays:
            snap = o.get("indicator_snapshot", {})
            ctx = snap.get("trade_context", {}) if isinstance(snap, dict) else {}
            e = ctx.get("edge", 0)
            if e > 0:
                edges.append(e)
        avg_edge = sum(edges) / len(edges) if edges else 0

        # --- Build message ---
        msg = (
            f"\n{'=' * 42}\n"
            f"**POLYBOT DAILY REPORT — {today}**\n"
            f"{'=' * 42}\n\n"
        )

        # P&L section
        pnl_emoji = "+" if total_pnl >= 0 else ""
        msg += (
            f"**P&L**\n"
            f"```\n"
            f"  Total P&L:    ${total_pnl:+,.2f}\n"
            f"  Total Fees:   ${total_fees:,.2f}\n"
            f"  Trades:       {total}  ({wins}W / {losses}L)\n"
            f"  Win Rate:     {win_rate:.1%}\n"
            f"  Avg Edge:     {avg_edge:.1%}\n"
        )
        if best_trade:
            msg += f"  Best Trade:   ${best_pnl:+,.2f}  ({best_trade.get('side', '')})\n"
        if worst_trade:
            msg += f"  Worst Trade:  ${worst_pnl:+,.2f}  ({worst_trade.get('side', '')})\n"
        msg += f"```\n\n"

        # Side breakdown
        if up_t > 0 or dn_t > 0:
            msg += f"**By Side**\n```\n"
            if up_t > 0:
                msg += f"  Up:    {up_w}/{up_t} wins  ({up_w/up_t:.0%})\n"
            if dn_t > 0:
                msg += f"  Down:  {dn_w}/{dn_t} wins  ({dn_w/dn_t:.0%})\n"
            msg += f"```\n\n"

        # Hourly breakdown
        if hour_lines:
            msg += f"**Win Rate by Hour (ET)**\n```\n"
            msg += "\n".join(hour_lines)
            msg += f"\n```\n\n"

        # Edge breakdown
        if edge_lines:
            msg += f"**Win Rate by Edge**\n```\n"
            msg += "\n".join(edge_lines)
            msg += f"\n```\n\n"

        # Config changes
        if config_changes:
            msg += f"**Config Changes for Tomorrow**\n```\n"
            for param, change in config_changes.items():
                msg += f"  {param}: {change['old']} -> {change['new']}\n"
            msg += f"```\n\n"
        else:
            msg += f"**Config Changes:** None — keeping current settings\n\n"

        # Claude's analysis
        findings = recommendations.get("key_findings", [])
        if findings:
            msg += f"**Claude's Key Findings**\n"
            for f in findings[:5]:
                msg += f"  - {f}\n"
            msg += "\n"

        warnings = recommendations.get("risk_warnings", [])
        if warnings:
            msg += f"**Risk Warnings**\n"
            for w in warnings[:3]:
                msg += f"  - {w}\n"
            msg += "\n"

        reasoning = recommendations.get("reasoning", "")
        if reasoning:
            msg += f"**Analysis Summary**\n{reasoning[:500]}\n"

        msg += f"\n{'=' * 42}"

        # Discord has 2000 char limit — split if needed
        if len(msg) <= 2000:
            await channel.send(msg)
        else:
            chunks = [msg[i:i+1990] for i in range(0, len(msg), 1990)]
            for chunk in chunks:
                await channel.send(chunk)
