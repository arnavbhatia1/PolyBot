import logging
import uuid
from datetime import datetime, timezone

import discord

logger = logging.getLogger(__name__)

class AlertManager:
    def __init__(self, bot, trade_channel_name, control_channel_name):
        self.bot = bot
        self.trade_channel_name = trade_channel_name
        self.control_channel_name = control_channel_name
        self.session_id = uuid.uuid4().hex[:8]

    def _get_channel(self, name):
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if channel.name == name:
                    return channel
        return None

    async def send_trade_opened(self, question, side, size, entry_price, ev, exit_target):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        await channel.send(
            f"**OPEN {side}** @ `{entry_price:.3f}` | Size: `${size:.2f}` | Signal: `{ev:+.3f}`")

    async def send_trade_closed(self, question, exit_price, log_return, hold_hours,
                                side="", entry_price=0.0, pnl=0.0, gain_pct=0.0, reason=""):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        tag = "PROFIT" if pnl >= 0 else "LOSS"
        await channel.send(
            f"**CLOSE {tag} {side}** | `{entry_price:.3f}`->`{exit_price:.3f}` | "
            f"`{gain_pct:+.1%}` | `${pnl:+.2f}` | {reason}")

    async def send_pipeline_summary(self, summary):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        await channel.send(f"**Learning Pipeline Complete**\n{summary}")

    async def send_strategy_recommendation(self, recommendations):
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

    async def send_error(self, error_message):
        channel = self._get_channel(self.control_channel_name)
        if not channel:
            return
        await channel.send(f"**Error**\n```{error_message}```")

    async def send_session_banner(self, mode: str, bankroll: float):
        """Send a session start banner to both channels to mark a new bot run."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        banner = (
            f"\n{'━' * 38}\n"
            f"**POLYBOT SESSION STARTED**\n"
            f"`{now}` | Session `{self.session_id}`\n"
            f"Mode: `{mode}` | Bankroll: `${bankroll:,.2f}`\n"
            f"{'━' * 38}"
        )
        for name in [self.trade_channel_name, self.control_channel_name]:
            channel = self._get_channel(name)
            if channel:
                try:
                    await channel.send(banner)
                except Exception as e:
                    logger.warning(f"Failed to send session banner to #{name}: {e}")

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
