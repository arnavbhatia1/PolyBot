"""Discord command authorization — DISCORD_ADMIN_IDS allowlist."""
from unittest.mock import MagicMock

from polybot.discord_bot.bot import create_bot, is_authorized, parse_admin_ids


def test_parse_admin_ids():
    assert parse_admin_ids(None) == frozenset()
    assert parse_admin_ids("") == frozenset()
    assert parse_admin_ids("123") == frozenset({123})
    assert parse_admin_ids("123, 456 ,789") == frozenset({123, 456, 789})
    # Non-numeric entries are dropped, valid ones kept.
    assert parse_admin_ids("abc,123") == frozenset({123})


def test_is_authorized_with_allowlist():
    admins = frozenset({111, 222})
    assert is_authorized(admins, 111)
    assert not is_authorized(admins, 999)


def test_is_authorized_unset_is_open():
    assert is_authorized(frozenset(), 999)


def test_create_bot_reads_admin_ids_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_ADMIN_IDS", "111,222")
    bot = create_bot(db=MagicMock(), trader=MagicMock(), scanner=MagicMock(),
                     scheduler=MagicMock(), config={"mode": "paper"})
    assert bot.admin_ids == frozenset({111, 222})

    monkeypatch.delenv("DISCORD_ADMIN_IDS")
    bot_open = create_bot(db=MagicMock(), trader=MagicMock(), scanner=MagicMock(),
                          scheduler=MagicMock(), config={"mode": "paper"})
    assert bot_open.admin_ids == frozenset()
