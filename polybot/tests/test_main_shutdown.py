"""Ctrl+C (SIGINT) shutdown handler — clean repeat-press force-quit."""
import pytest

from polybot.main import _make_sigint_handler


def test_first_ctrl_c_raises_second_force_quits():
    """First press raises KeyboardInterrupt (lets main()'s finally tear down);
    a second, impatient press force-quits so interpreter exit can't hang on a
    lingering non-daemon thread join."""
    exits = []
    handler = _make_sigint_handler(force_quit=lambda code: exits.append(code))

    with pytest.raises(KeyboardInterrupt):
        handler()
    assert exits == []  # first press did NOT force-quit

    handler()  # second press
    assert exits == [130]  # force-quit with the conventional 128+SIGINT code


def test_third_press_still_force_quits():
    """Any press past the first force-quits (handler stays armed)."""
    exits = []
    handler = _make_sigint_handler(force_quit=lambda code: exits.append(code))
    with pytest.raises(KeyboardInterrupt):
        handler()
    handler()
    handler()
    assert exits == [130, 130]
