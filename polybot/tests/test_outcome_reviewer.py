import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from polybot.agents.outcome_reviewer import OutcomeReviewer

_ET = ZoneInfo("America/New_York")

@pytest.fixture
def outcomes_dir(tmp_path):
    return tmp_path / "outcomes"

@pytest.fixture
def reviewer(outcomes_dir):
    return OutcomeReviewer(outcomes_dir=str(outcomes_dir))

def test_record_outcome_creates_file(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="market_123", question="BTC Up?",
        side="Up", signal_score=0.72, profitable=True, entry_price=0.55,
        exit_price=0.68, log_return=0.212)
    files = list(Path(outcomes_dir).glob("*.json"))
    assert len(files) == 1

def test_record_outcome_content(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="market_123", question="BTC Up?",
        side="Up", signal_score=0.72, profitable=True, entry_price=0.55,
        exit_price=0.68, log_return=0.212,
        indicator_snapshot={"rsi": {"score": 0.3}, "macd": {"score": 0.5}})
    files = list(Path(outcomes_dir).glob("*.json"))
    data = json.loads(files[0].read_text())
    assert data["signal_score"] == 0.72
    assert data["correct"] is True
    assert data["indicator_snapshot"]["macd"]["score"] == 0.5

def test_profitable_trade_marked_correct(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="m1", question="Q?",
        side="Up", signal_score=0.5, profitable=True, entry_price=0.55,
        exit_price=0.68, log_return=0.2)
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["correct"] is True

def test_losing_trade_marked_incorrect(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="m1", question="Q?",
        side="Up", signal_score=0.5, profitable=False, entry_price=0.55,
        exit_price=0.40, log_return=-0.3)
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["correct"] is False

def test_load_all_outcomes(reviewer, outcomes_dir):
    for i in range(3):
        reviewer.record_outcome(position_id=i, market_id=f"market_{i}", question="Q?",
            side="Up", signal_score=0.7, profitable=True, entry_price=0.55,
            exit_price=0.68, log_return=0.2)
    outcomes = reviewer.load_all_outcomes()
    assert len(outcomes) == 3

def test_exit_reason_recorded(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=1, market_id="m1", question="Q?",
        side="Up", signal_score=0.7, profitable=True, entry_price=0.50,
        exit_price=0.80, log_return=0.47, exit_reason="scalp")
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["exit_reason"] == "scalp"

def test_exit_reason_defaults_to_resolution(reviewer, outcomes_dir):
    reviewer.record_outcome(position_id=2, market_id="m2", question="Q?",
        side="Down", signal_score=0.6, profitable=False, entry_price=0.55,
        exit_price=0.0, log_return=-10.0)
    data = json.loads(list(Path(outcomes_dir).glob("*.json"))[0].read_text())
    assert data["exit_reason"] == "resolution"

def _write_outcome(outcomes_dir, position_id, market_id, ts_iso, name=None):
    outcomes_dir.mkdir(parents=True, exist_ok=True)
    record = {"position_id": position_id, "exit_timestamp": ts_iso, "timestamp": ts_iso}
    if market_id is not None:
        record["market_id"] = market_id
    fname = name or f"{position_id}_{market_id}_{ts_iso[:10]}.json"
    (outcomes_dir / fname).write_text(json.dumps(record))

def test_rollup_skips_current_et_day(reviewer, outcomes_dir):
    yesterday_noon_et = (datetime.now(_ET) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    y_iso = yesterday_noon_et.astimezone(timezone.utc).isoformat()
    t_iso = datetime.now(timezone.utc).isoformat()
    _write_outcome(outcomes_dir, 1, "m_old", y_iso)
    _write_outcome(outcomes_dir, 2, "m_today", t_iso)

    rolled = reviewer.rollup_old_outcomes()

    assert rolled == 1
    names = {p.name for p in outcomes_dir.glob("*.json")}
    assert f"rollup_{yesterday_noon_et.strftime('%Y-%m-%d')}.json" in names
    assert any("m_today" in n for n in names)  # today's file untouched
    assert not any("m_old" in n and not n.startswith("rollup_") for n in names)

def test_load_all_dedups_by_position_and_market(reviewer, outcomes_dir):
    ts = "2026-06-01T12:00:00+00:00"
    _write_outcome(outcomes_dir, 1, "m1", ts, name="a.json")
    _write_outcome(outcomes_dir, 1, "m1", ts, name="b.json")  # duplicate
    _write_outcome(outcomes_dir, 1, "m2", ts, name="c.json")  # paper/live id collision
    outcomes = reviewer.load_all_outcomes()
    assert len(outcomes) == 2
    assert {o["market_id"] for o in outcomes} == {"m1", "m2"}

def test_load_all_dedups_legacy_records_by_position_id(reviewer, outcomes_dir):
    ts = "2026-06-01T12:00:00+00:00"
    _write_outcome(outcomes_dir, 7, None, ts, name="a.json")
    _write_outcome(outcomes_dir, 7, None, ts, name="b.json")
    assert len(reviewer.load_all_outcomes()) == 1
