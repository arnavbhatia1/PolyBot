from polybot.core.order_flow import book_imbalance, trade_flow, compute_flow_signal


def test_book_imbalance_bullish():
    """More bids on Up = bullish."""
    book_up = {"bids": [{"size": "500"}, {"size": "300"}], "asks": [{"size": "100"}]}
    book_down = {"bids": [{"size": "50"}], "asks": [{"size": "200"}]}
    result = book_imbalance(book_up, book_down)
    assert result > 0  # bullish


def test_book_imbalance_bearish():
    """More bids on Down = bearish."""
    book_up = {"bids": [{"size": "50"}], "asks": [{"size": "300"}]}
    book_down = {"bids": [{"size": "500"}], "asks": [{"size": "100"}]}
    result = book_imbalance(book_up, book_down)
    assert result < 0  # bearish


def test_book_imbalance_empty():
    result = book_imbalance({}, {})
    assert result == 0.0


def test_book_imbalance_range():
    """Result is always in [-1, 1]."""
    book = {"bids": [{"size": "99999"}], "asks": []}
    result = book_imbalance(book, {})
    assert -1.0 <= result <= 1.0


def test_trade_flow_buying():
    trades_up = [{"size": "100", "side": "BUY", "timestamp": 9999999999}]
    trades_down = [{"size": "10", "side": "SELL", "timestamp": 9999999999}]
    result = trade_flow(trades_up, trades_down)
    assert result > 0  # net buying Up


def test_trade_flow_empty():
    result = trade_flow([], [])
    assert result == 0.0


def test_trade_flow_lookback():
    """Old trades are excluded."""
    trades = [{"size": "100", "side": "BUY", "timestamp": 0}]  # very old
    result = trade_flow(trades, [], lookback_seconds=60)
    assert result == 0.0


def test_composite_signal():
    book_up = {"bids": [{"size": "500"}], "asks": [{"size": "100"}]}
    book_down = {"bids": [{"size": "50"}], "asks": [{"size": "200"}]}
    trades_up = [{"size": "50", "side": "BUY", "timestamp": 9999999999}]
    trades_down = []
    result = compute_flow_signal(book_up, book_down, trades_up, trades_down)
    assert "flow_score" in result
    assert "book_imbalance" in result
    assert "trade_flow" in result
    assert "trade_count" in result
    assert -1.0 <= result["flow_score"] <= 1.0
