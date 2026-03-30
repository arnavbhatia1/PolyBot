# polybot/discord_bot/alerts.py
import logging
import discord

logger = logging.getLogger(__name__)

class AlertManager:
    def __init__(self, bot, trade_channel_name, control_channel_name):
        self.bot = bot
        self.trade_channel_name = trade_channel_name
        self.control_channel_name = control_channel_name

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
        await channel.send(f"**Trade Opened**\n{question}\n"
            f"Side: `{side}` | Entry: `{entry_price:.2f}` | Size: `${size:.2f}`\n"
            f"EV: `{ev:.2%}` | Target: `{exit_target:.2f}`")

    async def send_trade_closed(self, question, exit_price, log_return, hold_hours):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        pnl_sign = "+" if log_return >= 0 else ""
        result = "profit" if log_return >= 0 else "loss"
        await channel.send(f"**Trade Closed ({result})**\n{question}\n"
            f"Exit: `{exit_price:.2f}` | P&L: `{pnl_sign}{log_return:.4f}` | Held: `{hold_hours:.1f}h`")

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
