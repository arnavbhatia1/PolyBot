from polybot.execution.correlation import concurrent_multiplier, estimate_correlation


def _pos(market_id: str, side: str, size: float = 18.0) -> dict:
    return {"market_id": market_id, "side": side, "size": size}


def test_empty_open_positions_returns_full_size():
    assert concurrent_multiplier("Up", "btc-updown-5m-1", []) == 1.0


def test_same_market_is_ignored_as_flip():
    # Flip in the same window is handled separately — not by the concurrent multiplier.
    assert estimate_correlation("Down", "mkt-1", _pos("mkt-1", "Up")) is None
    assert concurrent_multiplier("Down", "mkt-1", [_pos("mkt-1", "Up")]) == 1.0


def test_same_side_different_market_gets_deepest_discount():
    # Same-side concurrent → rho=0.75 → 0.35 multiplier
    mult = concurrent_multiplier("Up", "mkt-2", [_pos("mkt-1", "Up")])
    assert mult == 0.35


def test_opposite_side_different_market_gets_near_full_size():
    mult = concurrent_multiplier("Down", "mkt-2", [_pos("mkt-1", "Up")])
    assert mult == 0.90


def test_worst_correlation_wins_when_multiple_opens():
    opens = [_pos("mkt-1", "Up"), _pos("mkt-2", "Down")]
    assert concurrent_multiplier("Up", "mkt-3", opens) == 0.35


def test_case_insensitive_side_matching():
    assert estimate_correlation("up", "mkt-2", _pos("mkt-1", "UP")) == 0.75
    assert estimate_correlation("DOWN", "mkt-2", _pos("mkt-1", "up")) == -0.25
