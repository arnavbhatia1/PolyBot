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


def test_shutdown_watchdog_is_armed_before_anything_that_can_hang():
    """A wedged aiosqlite worker hangs shutdown at db.close(); a watchdog armed
    after that call never exists. Static guard on the arming order."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    try_node = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.Try) and n.finalbody)

    def _calls(attr):
        return [n.lineno for stmt in try_node.finalbody for n in ast.walk(stmt)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == attr]

    armed = _calls("start")
    assert armed, "no shutdown watchdog armed in the finally block"
    assert _calls("Timer"), "the watchdog is not a threading.Timer"
    awaits = [n.lineno for stmt in try_node.finalbody for n in ast.walk(stmt)
              if isinstance(n, ast.Await)]
    assert awaits, "no awaits in the finally — test is stale"
    assert min(awaits) > armed[0], \
        "shutdown awaits something before the watchdog is armed"
