"""Consistency guard tests — catch doc/handler drift.

Every documented Discord command must resolve to a registered handler, and the
in-bot help text must list only real commands (caught the 4 phantom commands).
"""
import re
from unittest.mock import MagicMock

from polybot.paths import POLYBOT_DIR


def test_discord_commands_resolve_and_help_text_is_accurate():
    """Every registered command is reachable; the `!commands` help text references
    only real commands (no phantom-documented commands)."""
    from polybot.discord_bot.bot import create_bot

    bot = create_bot(db=MagicMock(), trader=MagicMock(), scanner=MagicMock(),
                     scheduler=MagicMock(), config={"mode": "paper"})
    registered = {c.name for c in bot.commands}

    # The 8 commands the bot implements; discord.py also auto-registers a built-in `help`.
    custom = {"status", "history", "pause", "resume", "clear", "session", "pipeline", "commands"}
    assert custom <= registered, f"documented command(s) with no handler: {sorted(custom - registered)}"
    assert registered - custom <= {"help"}, f"undocumented command(s): {sorted(registered - custom - {'help'})}"

    # The in-bot help text / docstring must not advertise a command that has no handler.
    bot_src = (POLYBOT_DIR / "discord_bot" / "bot.py").read_text(encoding="utf-8")
    advertised = set(re.findall(r"`!(\w+)", bot_src))
    phantom = advertised - registered
    assert not phantom, f"help text advertises commands with no handler: {sorted(phantom)}"
