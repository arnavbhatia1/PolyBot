# polybot/tests/test_discord_commands.py
import pytest
from polybot.discord_bot.commands import format_status, format_positions, format_performance

def test_format_status():
    result = format_status(mode="paper", is_paused=False, open_positions=3, bankroll=95.50, pnl_24h=2.30)
    assert "paper" in result.lower()
    assert "3" in result
    assert "95.50" in result

def test_format_status_paused():
    result = format_status(mode="paper", is_paused=True, open_positions=0, bankroll=100.0, pnl_24h=0.0)
    assert "paused" in result.lower()

def test_format_positions_empty():
    result = format_positions([])
    assert "no open positions" in result.lower()

def test_format_positions_with_data():
    positions = [{"id": 1, "question": "Will BTC hit 100k?", "side": "YES",
                  "entry_price": 0.55, "size": 10.0, "exit_target": 0.68, "stop_loss": 0.47}]
    result = format_positions(positions, current_prices={"market_123": 0.60})
    assert "BTC" in result
    assert "0.55" in result

def test_format_performance():
    result = format_performance(sharpe_ratio=1.85, win_rate=0.72, total_pnl=15.30,
                                avg_hold_hours=8.5, total_trades=25, best_trade=5.20, worst_trade=-2.10)
    assert "1.85" in result
    assert "72" in result
    assert "15.30" in result
