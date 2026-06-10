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

    async def _safe_send(self, channel: Any, msg: str) -> None:
        try:
            await channel.send(msg)
        except Exception as e:
            logger.warning("Discord send failed (#%s): %s", getattr(channel, 'name', '?'), e)

    async def _send_to_channels(self, msg: str, channels: list[str]) -> None:
        for name in channels:
            channel = self._get_channel(name)
            if channel:
                await self._safe_send(channel, msg)

    async def send_trade_opened(self, question: str, side: str, size: float, entry_price: float,
                                ev: float,
                                model_prob: float = 0.0, market_price: float = 0.0,
                                fee: float = 0.0, flow: float = 0.0,
                                bankroll: float = 0.0) -> None:
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        window = question.replace("Bitcoin Up or Down - ", "") if question else ""
        bankroll_str = f"  Bankroll  ${bankroll:,.2f}\n" if bankroll > 0 else ""
        await self._safe_send(channel,
            f"**OPEN {side}**  {window}\n"
            f"```\n"
            f"  Price     {entry_price:.3f}  |  ${size:.2f}\n"
            f"  Edge      {ev:+.0%}  (model {model_prob:.0%} vs mkt {market_price:.0%})\n"
            f"  Fee       ${fee:.2f}\n"
            f"{bankroll_str}```")

    async def send_trade_closed(self, question: str, exit_price: float,
                                side: str = "", entry_price: float = 0.0, pnl: float = 0.0,
                                gain_pct: float = 0.0, reason: str = "",
                                fees: float = 0.0, bankroll: float = 0.0,
                                day_wins: int = 0, day_losses: int = 0) -> None:
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return

        # Build header: SCALP WIN UP / RESOLUTION LOSS DOWN / ORPHANED UP
        r = reason.lower()
        if r.startswith("scalp"):
            exit_type = "SCALP"
        elif "orphan" in r:
            exit_type = "ORPHANED"
        else:
            exit_type = "RESOLVED"
        result_word = ("WIN" if pnl >= 0 else "LOSS") if exit_type != "ORPHANED" else ""
        header = " ".join(filter(None, [exit_type, result_word, side.upper()]))

        window = question.replace("Bitcoin Up or Down - ", "") if question else ""

        body = f"  Price    {entry_price:.3f} \u2192 {exit_price:.3f}\n"
        if exit_type != "ORPHANED":
            pnl_label = "Gain" if pnl >= 0 else "Loss"
            body += f"  {pnl_label:<8} {gain_pct:+.1%}  (${pnl:+.2f})\n"
        if bankroll > 0:
            body += f"  Day      {day_wins}W/{day_losses}L  |  ${bankroll:,.2f}\n"

        await self._safe_send(channel,
            f"**{header}**  |  {window}\n"
            f"```\n{body}```")

    async def send_error(self, error_message: str) -> None:
        channel = self._get_channel(self.control_channel_name)
        if not channel:
            return
        await self._safe_send(channel, f"**Error**\n```{error_message}```")

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
            await self._safe_send(channel,
                f"**CIRCUIT BREAKER** — {breaker.consecutive_losses} consecutive losses. "
                f"Drawdown `{breaker.drawdown_pct:.1%}` | "
                f"Kelly at `{breaker.kelly_multiplier:.0%}`.")
        elif event == "streak_wins":
            await self._safe_send(channel,
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
                                recommendations: dict[str, Any], config_changes: dict[str, Any],
                                pipeline_info: dict[str, Any] | None = None) -> None:
        """Post end-of-day report: P&L, Sharpe, side/exit breakdown, pipeline summary, current config."""
        channel = self._get_channel(self.daily_channel_name)
        if not channel:
            return

        pipeline_info = pipeline_info or {}

        # Pipeline runs at 11:45 PM ET — the trading day is today ET.
        # If run manually before noon, use yesterday ET instead.
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

        # Real trades only — ghosts (rejected trades) carry a gain_pct but no pnl.
        todays = [o for o in outcomes
                  if _et_date(o) == trading_et_date and not o.get("is_ghost")]
        date_str = trading_et_date.strftime("%Y-%m-%d")

        if not todays:
            await self._safe_send(channel, f"**Daily Report — {date_str} ET**\nNo trades today.")
            return

        total_pnl = sum(o.get("pnl", 0) for o in todays)
        total_fees = sum(o.get("fees", 0) for o in todays)
        wins = sum(1 for o in todays if o.get("correct"))
        total = len(todays)
        losses = total - wins
        wr = wins / total if total > 0 else 0

        # Sharpe — per-trade arithmetic return
        gain_pcts = [o.get("gain_pct", 0.0) for o in todays]
        avg_ret = sum(gain_pcts) / len(gain_pcts)
        variance = sum((r - avg_ret) ** 2 for r in gain_pcts) / len(gain_pcts)
        std_ret = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = avg_ret / std_ret if std_ret > 0 else 0.0

        def _side_stats(trades: list) -> tuple[int, int, float]:
            w = sum(1 for t in trades if t.get("correct"))
            return w, len(trades), sum(t.get("pnl", 0) for t in trades)

        up_trades = [o for o in todays if o.get("side", "").upper() == "UP"]
        dn_trades = [o for o in todays if o.get("side", "").upper() == "DOWN"]
        scalp_trades = [o for o in todays if o.get("exit_reason") == "scalp"]
        res_trades = [o for o in todays if o.get("exit_reason") == "resolution"]
        high_edge = [o for o in todays
                     if o.get("indicator_snapshot", {}).get("trade_context", {}).get("edge", 0) >= 0.08]
        low_edge = [o for o in todays
                    if 0.04 <= o.get("indicator_snapshot", {}).get("trade_context", {}).get("edge", 0) < 0.08]

        at = pipeline_info.get("all_time", {})
        summary_block = pipeline_info.get("summary_block", "")

        def _row(label: str, trades: list, show_pnl: bool = True) -> str | None:
            if not trades:
                return None
            w, n, p = _side_stats(trades)
            base = f"{label:<7}{w}/{n} ({w/n:.0%})"
            if show_pnl:
                base += f" ${p:+,.0f}"
            return base

        today_lines = [
            f"TODAY  ({date_str} ET)",
            f"  P&L     ${total_pnl:+,.2f}   (fees ${total_fees:.2f})",
            f"  Trades  {total}   {wins}W / {losses}L ({wr:.0%})",
            f"  Sharpe  {sharpe:+.3f}",
            "",
        ]
        side_rows = [r for r in (_row("UP", up_trades), _row("DOWN", dn_trades)) if r]
        for i, r in enumerate(side_rows):
            prefix = "  Side    " if i == 0 else "          "
            today_lines.append(f"{prefix}{r}")
        exit_rows = [r for r in (_row("Scalp", scalp_trades), _row("Hold", res_trades)) if r]
        for i, r in enumerate(exit_rows):
            prefix = "  Exit    " if i == 0 else "          "
            today_lines.append(f"{prefix}{r}")
        edge_rows = [r for r in (_row("4-8%", low_edge, show_pnl=False),
                                 _row("≥8%", high_edge, show_pnl=False)) if r]
        for i, r in enumerate(edge_rows):
            prefix = "  Edge    " if i == 0 else "          "
            today_lines.append(f"{prefix}{r}")

        at_lines: list[str] = []
        if at:
            at_lines = [
                "",
                "ALL-TIME",
                f"  P&L     ${at.get('total_pnl', 0):+,.2f}",
                f"  Trades  {at.get('total_trades', 0):,}   WR {at.get('win_rate', 0):.0%}",
                f"  Sharpe  {at.get('sharpe', 0):+.3f}",
            ]

        body = "\n".join(today_lines + at_lines)
        if summary_block:
            body = body + "\n\n" + summary_block

        merged = f"**Daily Report — {date_str} ET**\n```\n{body}\n```"

        if len(merged) <= 2000:
            await self._safe_send(channel, merged)
        else:
            part1 = f"**Daily Report — {date_str} ET**\n```\n" + "\n".join(today_lines + at_lines) + "\n```"
            await self._safe_send(channel, part1[:2000])
            if summary_block:
                await self._safe_send(channel, f"```\n{summary_block}\n```"[:2000])
