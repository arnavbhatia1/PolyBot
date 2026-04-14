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
                                fee: float = 0.0, flow: float = 0.0,
                                bankroll: float = 0.0) -> None:
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        window = question.replace("Bitcoin Up or Down - ", "") if question else ""
        bankroll_str = f"  Bankroll  ${bankroll:,.2f}\n" if bankroll > 0 else ""
        await channel.send(
            f"**OPEN {side}**  {window}\n"
            f"```\n"
            f"  Price     {entry_price:.3f}  |  ${size:.2f}\n"
            f"  Edge      {ev:+.0%}  (model {model_prob:.0%} vs mkt {market_price:.0%})\n"
            f"  Fee       ${fee:.2f}\n"
            f"{bankroll_str}```")

    async def send_trade_closed(self, question: str, exit_price: float, log_return: float,
                                hold_hours: float,
                                side: str = "", entry_price: float = 0.0, pnl: float = 0.0,
                                gain_pct: float = 0.0, reason: str = "",
                                fees: float = 0.0, bankroll: float = 0.0,
                                day_wins: int = 0, day_losses: int = 0) -> None:
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        tag = "WIN" if pnl >= 0 else "LOSS"
        window = question.replace("Bitcoin Up or Down - ", "") if question else ""
        day_line = ""
        if bankroll > 0:
            total = day_wins + day_losses
            day_line = f"  Day       {day_wins}W/{day_losses}L  |  ${bankroll:,.2f}\n"
        await channel.send(
            f"**{tag} {side}**  {window}  |  {reason}\n"
            f"```\n"
            f"  {entry_price:.3f} -> {exit_price:.3f}  |  {gain_pct:+.1%}  (${pnl:+.2f})\n"
            f"{day_line}```")

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
        """Post end-of-day report: P&L, Sharpe, side/exit breakdown, edge calibration, findings."""
        channel = self._get_channel(self.daily_channel_name)
        if not channel:
            return

        # Pipeline runs at 12:05 AM ET — the trading day was yesterday ET.
        # If run manually after noon, use today ET instead.
        et_now = datetime.now(ET)
        if et_now.hour < 12:
            trading_et_date = (et_now - timedelta(days=1)).date()
        else:
            trading_et_date = et_now.date()

        def _et_date(o: dict) -> Any:
            ts = o.get("timestamp", "")
            if not ts:
                return None
            try:
                dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt_utc.astimezone(ET).date()
            except Exception:
                return None

        todays = [o for o in outcomes if _et_date(o) == trading_et_date]
        date_str = trading_et_date.strftime("%Y-%m-%d")

        if not todays:
            await channel.send(f"**Daily Report — {date_str} ET**\nNo trades today.")
            return

        # Core stats
        total_pnl = sum(o.get("pnl", 0) for o in todays)
        total_fees = sum(o.get("fees", 0) for o in todays)
        wins = sum(1 for o in todays if o.get("correct"))
        total = len(todays)
        losses = total - wins
        wr = wins / total if total > 0 else 0

        # Sharpe — per-trade arithmetic return, consistent with bias_detector / weight_optimizer
        gain_pcts = [o.get("gain_pct", 0.0) for o in todays]
        avg_ret = sum(gain_pcts) / len(gain_pcts)
        variance = sum((r - avg_ret) ** 2 for r in gain_pcts) / len(gain_pcts)
        std_ret = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = avg_ret / std_ret if std_ret > 0 else 0.0

        # Side breakdown helper
        def _side_stats(trades: list) -> tuple[int, int, float]:
            w = sum(1 for t in trades if t.get("correct"))
            return w, len(trades), sum(t.get("pnl", 0) for t in trades)

        up_trades = [o for o in todays if o.get("side", "").upper() == "UP"]
        dn_trades = [o for o in todays if o.get("side", "").upper() == "DOWN"]

        # Exit reason breakdown
        scalp_trades = [o for o in todays if o.get("exit_reason") == "scalp"]
        res_trades = [o for o in todays if o.get("exit_reason") == "resolution"]

        # Edge calibration
        high_edge = [o for o in todays
                     if o.get("indicator_snapshot", {}).get("trade_context", {}).get("edge", 0) >= 0.08]
        low_edge = [o for o in todays
                    if 0.04 <= o.get("indicator_snapshot", {}).get("trade_context", {}).get("edge", 0) < 0.08]

        def _side_line(label: str, trades: list) -> str:
            if not trades:
                return ""
            w, n, p = _side_stats(trades)
            return f"  {label:<6} {w:>3}/{n:<3}  {w/n:.0%}  ${p:+.2f}\n"

        def _exit_line(label: str, trades: list) -> str:
            if not trades:
                return ""
            w, n, p = _side_stats(trades)
            return f"  {label:<12} {w:>3}/{n:<3}  {w/n:.0%}  ${p:+.2f}\n"

        def _edge_line(label: str, trades: list) -> str:
            if not trades:
                return ""
            w, n, _ = _side_stats(trades)
            return f"  {label:<16} {w:>3}/{n:<3}  {w/n:.0%}\n"

        # --- Message 1: core performance ---
        msg1 = (
            f"**DAILY REPORT — {date_str} ET**\n"
            f"```\n"
            f"  P&L      ${total_pnl:+,.2f}  (fees ${total_fees:.2f})\n"
            f"  Trades   {total}  ({wins}W/{losses}L)  WR {wr:.0%}\n"
            f"  Sharpe   {sharpe:+.3f}  (per-trade)\n"
            f"```\n"
            f"**By Side**\n```\n"
            f"{_side_line('UP', up_trades)}"
            f"{_side_line('DOWN', dn_trades)}"
            f"```\n"
            f"**By Exit**\n```\n"
            f"{_exit_line('Scalp', scalp_trades)}"
            f"{_exit_line('Resolution', res_trades)}"
            f"```\n"
        )
        if high_edge or low_edge:
            msg1 += (
                f"**Edge Calibration**\n```\n"
                f"{_edge_line('edge ≥8%', high_edge)}"
                f"{_edge_line('edge 4–8%', low_edge)}"
                f"```\n"
            )
        await channel.send(msg1[:2000])

        # --- Message 2: config changes + pipeline findings ---
        msg2 = ""
        if config_changes:
            msg2 += "**Config Changes**\n```\n"
            for param, change in config_changes.items():
                msg2 += f"  {param}: {change['old']} -> {change['new']}\n"
            msg2 += "```\n"

        findings = recommendations.get("key_findings", [])
        warnings = recommendations.get("risk_warnings", [])
        if findings or warnings:
            msg2 += "**Pipeline Findings**\n"
            for f in findings[:4]:
                msg2 += f"- {f}\n"
            for w in warnings[:2]:
                msg2 += f"**Warning:** {w}\n"

        if msg2:
            await channel.send(msg2[:2000])
