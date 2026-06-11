"""E1/E2/E3 telemetry: aux latency fields, CLOB book aux, price-sum outlier log."""
import json
import time

import polybot.main as main
from polybot.main import _build_aux_signals, _clob_book_aux, _log_price_sum_outlier


class _FakeCoinbase:
    def __init__(self, price=71000.0, age=0.5, covers=True, rv=0.00042):
        class _S:
            pass
        self.state = _S()
        self.state.price = price
        self.state.age_seconds = age
        self._covers = covers
        self._rv = rv

    def covers(self, _w):
        return self._covers

    def get_cvd(self, _w):
        return 12.5

    def get_taker_ratio(self, _w):
        return 0.61, 250

    def realized_vol(self, _w):
        return self._rv


class _FakeTrades:
    def __init__(self, price=70990.0, age=0.4):
        class _A:
            pass
        self.accumulator = _A()
        self.accumulator.latest_price = price
        self.accumulator.latest_age_s = age


class TestBuildAuxSignals:
    def test_latency_fields_fresh(self):
        aux = _build_aux_signals(_FakeCoinbase(), _FakeTrades())
        assert aux["cross_venue_gap"] == 10.0
        assert aux["fast_realized_vol_60s"] == 0.00042

    def test_gap_none_when_binance_stale(self):
        aux = _build_aux_signals(_FakeCoinbase(), _FakeTrades(age=5.0))
        assert aux["cross_venue_gap"] is None
        assert aux["fast_realized_vol_60s"] == 0.00042  # coinbase-only, unaffected

    def test_gap_none_without_trades_feed(self):
        aux = _build_aux_signals(_FakeCoinbase(), None)
        assert aux["cross_venue_gap"] is None

    def test_vol_none_when_window_not_covered(self):
        aux = _build_aux_signals(_FakeCoinbase(covers=False), _FakeTrades())
        assert aux["fast_realized_vol_60s"] is None
        assert aux["coinbase_cvd_60s"] is None


def _book(asks, ts=None):
    b = {"asks": [{"price": str(p), "size": str(s)} for p, s in asks],
         "bids": []}
    if ts is not None:
        b["ts"] = ts
    return b


class _FakeClobWs:
    def __init__(self, books):
        self._books = books

    def get_book(self, token_id):
        return self._books.get(token_id, {})


class TestClobBookAux:
    def test_top5_depth_and_age_from_ws_books(self):
        now = time.time()
        ws = _FakeClobWs({
            "tu": _book([(0.50, 100), (0.51, 200), (0.52, 1), (0.53, 1), (0.54, 1), (0.99, 9999)], ts=now - 2.0),
            "td": _book([(0.52, 50)], ts=now - 1.0),
        })
        aux = _clob_book_aux(ws, "tu", "td", {}, {})
        # top-5 only: the 6th level (0.99 * 9999) must not count
        assert abs(aux["clob_depth_top5_up_usd"] - (0.50 * 100 + 0.51 * 200 + 0.52 + 0.53 + 0.54)) < 0.01
        assert abs(aux["clob_depth_top5_down_usd"] - 26.0) < 0.01
        assert 1.9 < aux["clob_book_age_s"] < 2.5  # max of the two sides

    def test_http_fallback_has_no_age(self):
        aux = _clob_book_aux(None, "tu", "td", _book([(0.50, 100)]), _book([(0.52, 50)]))
        assert aux["clob_depth_top5_up_usd"] == 50.0
        assert aux["clob_book_age_s"] is None

    def test_missing_side_is_none(self):
        aux = _clob_book_aux(None, "tu", "td", _book([(0.50, 100)]), {})
        assert aux["clob_depth_top5_up_usd"] == 50.0
        assert aux["clob_depth_top5_down_usd"] is None


class TestPriceSumOutlierLog:
    def test_writes_line_and_throttles(self, tmp_path, monkeypatch):
        path = tmp_path / "price_sum_outliers.jsonl"
        monkeypatch.setattr(main, "PRICE_SUM_OUTLIERS_PATH", path)
        main._last_price_sum_log.clear()
        _log_price_sum_outlier("btc-updown-5m-1", 0.40, 0.50, 120.0, 80.0)
        _log_price_sum_outlier("btc-updown-5m-1", 0.41, 0.50, 120.0, 80.0)  # throttled
        _log_price_sum_outlier("btc-updown-5m-2", 0.70, 0.40, 10.0, 20.0)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(lines) == 2
        assert lines[0]["sum"] == 0.9
        assert lines[0]["size_up"] == 120.0
        assert lines[1]["market"] == "btc-updown-5m-2"

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(main, "PRICE_SUM_OUTLIERS_PATH", None)  # .parent will explode inside
        main._last_price_sum_log.clear()
        _log_price_sum_outlier("m", 0.4, 0.5, 0.0, 0.0)  # swallowed
