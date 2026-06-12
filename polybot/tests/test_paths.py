"""Memory layout + gate-skip accumulator (current behavior)."""
import json

import polybot.paths as P


def test_rolling_state_lives_under_state_dir():
    for path in (P.GATE_STATS_PATH, P.GATE_STATS_CURRENT_PATH, P.ADVERSE_STATE_PATH,
                 P.FEED_STALENESS_PATH, P.FILL_STATS_PATH,
                 P.ORPHAN_POSITIONS_PATH, P.PREV_MARGIN_PATH,
                 P.PIPELINE_RUN_LOG_PATH):
        assert path.parent == P.STATE_DIR


def test_fold_gate_day_accumulates_across_days(tmp_path):
    acc = tmp_path / "gate_stats.json"
    P.fold_gate_day(acc, {"a": 3, "b": 1}, "20260527")
    P.fold_gate_day(acc, {"a": 2, "c": 5}, "20260528")
    d = json.loads(acc.read_text())
    assert d["days_accumulated"] == 2
    assert d["counts"] == {"a": 5, "b": 1, "c": 5}   # 3+2 / 1 / 5
    assert d["total_skips"] == 11
    assert d["first_day"] == "20260527" and d["last_day"] == "20260528"


def test_fold_gate_day_empty_is_noop(tmp_path):
    acc = tmp_path / "gate_stats.json"
    P.fold_gate_day(acc, {"a": 4}, "20260527")
    before = acc.read_text()
    assert P.fold_gate_day(acc, {}, "20260528") is None   # empty day folds nothing
    assert acc.read_text() == before                       # accumulator untouched
