import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta
from polybot.core.websocket_monitor import ExitMonitor

FIXED_NOW = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def positions():
    return [{"id": 1, "market_id": "market_123", "entry_price": 0.55,
             "exit_target": 0.68, "stop_loss": 0.47, "claude_probability": 0.72,
             "entry_timestamp": "2026-03-30T00:00:00+00:00"}]


@pytest.fixture
def monitor():
    return ExitMonitor(time_stop_hours=24, time_stop_min_gain=0.02)


def test_check_exit_take_profit(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        action = monitor.check_exit(positions[0], current_price=0.70)
    assert action == "take_profit"


def test_check_exit_stop_loss(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        action = monitor.check_exit(positions[0], current_price=0.45)
    assert action == "stop_loss"


def test_check_exit_hold(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        action = monitor.check_exit(positions[0], current_price=0.60)
    assert action == "hold"


def test_check_exit_time_stop(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        old_position = positions[0].copy()
        old_position["entry_timestamp"] = "2026-03-28T00:00:00+00:00"
        action = monitor.check_exit(old_position, current_price=0.56)
    assert action == "time_stop"


def test_check_exit_time_stop_not_triggered_with_gain(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        old_position = positions[0].copy()
        old_position["entry_timestamp"] = "2026-03-28T00:00:00+00:00"
        action = monitor.check_exit(old_position, current_price=0.60)
    assert action == "hold"
