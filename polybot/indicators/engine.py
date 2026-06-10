from __future__ import annotations

from typing import Any
from polybot.feeds.binance_feed import CandleBuffer
from polybot.indicators.rsi import compute_rsi_signal
from polybot.indicators.macd import compute_macd_signal
from polybot.indicators.stochastic import compute_stochastic_signal
from polybot.indicators.ema import compute_ema_signal
from polybot.indicators.obv import compute_obv_signal
from polybot.indicators.vwap import compute_vwap_signal
from polybot.indicators.atr import compute_atr_gate

DEFAULT_PARAMS = {
    "rsi": {"period": 5, "overbought": 70, "oversold": 30},
    "macd": {"fast": 5, "slow": 13, "signal_period": 4},
    "stochastic": {"k_period": 5, "d_smoothing": 2, "overbought": 80, "oversold": 20},
    "ema": {"fast_period": 3, "slow_period": 8, "chop_threshold": 0.001},
    "obv": {"slope_period": 3},
    "atr": {"period": 7, "low_pct": 5, "history": 100},
}

DEFAULT_WEIGHTS = {"rsi": 0.20, "macd": 0.30, "stochastic": 0.15, "obv": 0.15, "vwap": 0.20}


class IndicatorEngine:
    """Computes RSI/MACD/Stoch/EMA/OBV/VWAP/ATR each tick from the candle buffer.
    Indicator ``score`` fields are already bounded in [-1, 1] by their respective
    compute_*_signal functions — L4 reads them directly, no adaptive normalization.
    """

    def __init__(self, weights: dict[str, float] | None = None,
                 params: dict[str, dict[str, Any]] | None = None) -> None:
        self.params: dict[str, dict[str, Any]] = params or DEFAULT_PARAMS
        self._weights: dict[str, float] = dict(weights) if weights else dict(DEFAULT_WEIGHTS)
        self._cache_version: int = -1
        self._cached: dict[str, dict[str, Any]] = {}

    def get_weights(self) -> dict[str, float]:
        return self._weights.copy()

    def set_weights(self, weights: dict[str, float]) -> None:
        self._weights = {**self._weights, **weights}

    def compute_all(self, buffer: CandleBuffer, *, force: bool = False) -> dict[str, dict[str, Any]]:
        v = getattr(buffer, "version", -1)
        if not force and v == self._cache_version and self._cached:
            return self._cached
        closes = buffer.get_closes()
        highs = buffer.get_highs()
        lows = buffer.get_lows()
        volumes = buffer.get_volumes()
        p = self.params
        result = {
            "rsi": compute_rsi_signal(closes, **p["rsi"]),
            "macd": compute_macd_signal(closes, **p["macd"]),
            "stochastic": compute_stochastic_signal(highs, lows, closes, **p["stochastic"]),
            "ema": compute_ema_signal(closes, **p["ema"]),
            "obv": compute_obv_signal(closes, volumes, **p["obv"]),
            "vwap": compute_vwap_signal(highs, lows, closes, volumes),
            "atr": compute_atr_gate(highs, lows, closes, **p["atr"]),
        }
        self._cache_version = v
        self._cached = result
        return result

    def get_snapshot(self, indicators: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {"rsi": indicators["rsi"], "macd": indicators["macd"],
                "stochastic": indicators["stochastic"], "ema": indicators["ema"],
                "obv": indicators["obv"], "vwap": indicators["vwap"],
                "atr": indicators["atr"], "weights": self.get_weights()}
