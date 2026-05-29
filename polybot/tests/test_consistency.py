"""Consistency guard tests — catch the doc/registry/handler drift this pass found.

1. settings.yaml [P]/[M] ownership tags must match param_registry (caught the
   deep_loss_hold_threshold [P]->[M] mistag).
2. Every documented Discord command must resolve to a registered handler, and the
   in-bot help text must list only real commands (caught the 4 phantom commands).
"""
import re
from unittest.mock import MagicMock

from polybot.paths import POLYBOT_DIR
from polybot.config.param_registry import BY_NAME, MANUAL_ONLY_PARAMS, is_manual_only

_SETTINGS = POLYBOT_DIR / "config" / "settings.yaml"
# e.g.  `  min_edge: 0.04   # [P] ...`  ->  ("min_edge", "P")
_TAG_RE = re.compile(r"^\s*([A-Za-z_]\w*):\s*\S.*#\s*\[([PM])\]")


def test_settings_yaml_ownership_tags_match_registry():
    """A key tagged [P] must be pipeline-tunable; [M] must be manual-only. Only keys
    the registry actually knows are checked (dict/special/infra keys are skipped)."""
    checked = []
    for line in _SETTINGS.read_text(encoding="utf-8").splitlines():
        m = _TAG_RE.match(line)
        if not m:
            continue
        key, tag = m.group(1), m.group(2)
        known = key in BY_NAME or key in MANUAL_ONLY_PARAMS
        if not known:
            continue  # weights/consensus_dead_zone/infra etc. — not a scalar tunable/manual param
        assert (tag == "M") == is_manual_only(key), (
            f"settings.yaml tags '{key}' as [{tag}] but is_manual_only({key})={is_manual_only(key)}"
        )
        checked.append(key)
    # Guard against the regex silently matching nothing (which would make this vacuous).
    assert len(checked) >= 10, f"only checked {checked} — tag format may have changed"


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
