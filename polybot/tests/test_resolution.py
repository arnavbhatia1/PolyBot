"""Resolution exit-price decision — `_resolved_exit_price` (§8).

Binary payoff (winner 1.0 / loser 0.0). The Chainlink oracle (`event_metadata`)
is authoritative and preferred over the CLOB book. A *coherent* resolved book is
the fallback; an incoherent book (one side a stale/phantom print) is rejected so a
winning side can't mis-resolve to the wrong value — the caller keeps waiting and the
oracle/orphan path resolves it later.
"""
from polybot.main import _resolved_exit_price


def _meta(final, strike):
    return {"event_metadata": {"final_price": final, "price_to_beat": strike}}


def _book(closed, up, down):
    return {"closed": closed, "price_up": up, "price_down": down}


# --- Oracle branch (authoritative) ---

def test_oracle_up_wins():
    px, log = _resolved_exit_price(_meta(100.5, 100.0), "Up")
    assert px == 1.0 and log is not None


def test_oracle_up_wins_means_down_position_loses():
    assert _resolved_exit_price(_meta(100.5, 100.0), "Down")[0] == 0.0


def test_oracle_down_wins():
    assert _resolved_exit_price(_meta(99.5, 100.0), "Down")[0] == 1.0
    assert _resolved_exit_price(_meta(99.5, 100.0), "Up")[0] == 0.0


def test_oracle_tie_counts_as_up():
    # final == strike → Up wins (>= rule, matching Polymarket).
    assert _resolved_exit_price(_meta(100.0, 100.0), "Up")[0] == 1.0


# --- Coherent resolved book (fallback when no oracle) ---

def test_book_up_resolved_pays_binary_one_not_book_price():
    px, log = _resolved_exit_price(_book(True, 0.99, 0.02), "Up")
    assert px == 1.0 and log is None  # binary payoff, no oracle log


def test_book_down_winner_paid_one_not_raw_price_down():
    # price_up≈0.01 → Up lost, Down won. The winning Down is paid 1.0, NOT the raw
    # price_down. This is the regression the fix targets (old code returned price_down).
    assert _resolved_exit_price(_book(True, 0.01, 0.99), "Down")[0] == 1.0
    assert _resolved_exit_price(_book(True, 0.01, 0.99), "Up")[0] == 0.0


# --- Incoherent / unresolved → wait (None) ---

def test_incoherent_book_rejected():
    # Both sides high (sum 1.98) — a stale/phantom print. Must NOT fast-path: the old
    # code would have paid a Down position 0.99 even if Up actually won.
    assert _resolved_exit_price(_book(True, 0.99, 0.99), "Down") == (None, None)


def test_not_closed_book_waits():
    assert _resolved_exit_price(_book(False, 0.99, 0.02), "Up") == (None, None)


def test_mid_book_not_resolved():
    assert _resolved_exit_price(_book(True, 0.55, 0.46), "Up") == (None, None)


def test_empty_or_missing_waits():
    assert _resolved_exit_price({}, "Up") == (None, None)
    assert _resolved_exit_price(None, "Up") == (None, None)


# --- Oracle preferred over book; partial metadata falls through ---

def test_oracle_preferred_over_book():
    live = {"event_metadata": {"final_price": 100.5, "price_to_beat": 100.0},
            "closed": True, "price_up": 0.99, "price_down": 0.02}
    px, log = _resolved_exit_price(live, "Up")
    assert px == 1.0 and log is not None  # oracle log present → oracle path taken


def test_partial_metadata_falls_through_to_coherent_book():
    # final_price present but price_to_beat missing → unusable; coherent book resolves.
    live = {"event_metadata": {"final_price": 100.5},
            "closed": True, "price_up": 0.99, "price_down": 0.02}
    px, log = _resolved_exit_price(live, "Up")
    assert px == 1.0 and log is None  # fell through to book (no oracle log)
