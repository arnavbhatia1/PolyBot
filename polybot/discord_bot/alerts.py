from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
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

    async def send_health(self, message: str) -> None:
        """Nightly sniper-edge health report → daily channel (posted during the
        wind-down while the bot isn't trading)."""
        await self._send_to_channels(message, [self.daily_channel_name])

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
