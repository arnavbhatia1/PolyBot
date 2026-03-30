import pytest
from polybot.math_engine.decision_table import DecisionTable

@pytest.fixture
def table():
    return DecisionTable(
        ev_threshold=0.05,
        kelly_fraction=0.25,
        entry_discount=0.85,
        exit_target=0.90,
        stop_loss_pct=0.15,
    )

def test_build_creates_entries_for_all_probabilities(table):
    table.build()
    assert len(table.table) == 99

def test_lookup_returns_decision_for_probability(table):
    table.build()
    decision = table.lookup(0.72)
    assert "max_buy_price" in decision
    assert "exit_price" in decision
    assert "kelly_fraction" in decision

def test_max_buy_price_is_probability_times_discount(table):
    table.build()
    decision = table.lookup(0.72)
    assert decision["max_buy_price"] == pytest.approx(0.72 * 0.85, abs=0.01)

def test_exit_price_is_probability_times_target(table):
    table.build()
    decision = table.lookup(0.72)
    assert decision["exit_price"] == pytest.approx(0.72 * 0.90, abs=0.01)

def test_should_buy_when_price_below_max(table):
    table.build()
    assert table.should_buy(probability=0.72, market_price=0.55) is True

def test_should_not_buy_when_price_above_max(table):
    table.build()
    assert table.should_buy(probability=0.72, market_price=0.65) is False

def test_should_exit_when_price_above_target(table):
    table.build()
    assert table.should_exit(probability=0.72, market_price=0.70) is True

def test_should_not_exit_when_price_below_target(table):
    table.build()
    assert table.should_exit(probability=0.72, market_price=0.55) is False

def test_should_stop_loss(table):
    table.build()
    assert table.should_stop_loss(entry_price=0.55, market_price=0.46) is True
    assert table.should_stop_loss(entry_price=0.55, market_price=0.50) is False

def test_calculate_ev(table):
    ev = table.calculate_ev(probability=0.72, market_price=0.55)
    assert ev == pytest.approx(0.17, abs=0.01)

def test_ev_filter_skips_low_edge(table):
    table.build()
    assert table.should_buy(probability=0.56, market_price=0.55) is False

def test_position_size_uses_quarter_kelly(table):
    table.build()
    size = table.position_size(probability=0.72, market_price=0.55, bankroll=100.0)
    assert size > 0
    assert size < 100.0

def test_lookup_rounds_to_nearest_cent(table):
    table.build()
    decision = table.lookup(0.723)
    assert decision is not None
