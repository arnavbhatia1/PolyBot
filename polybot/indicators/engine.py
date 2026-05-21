from __future__ import annotations

import logging
import math
from typing import Any

from polybot.feeds.binance_feed import CandleBuffer
from polybot.indicators.rsi import compute_rsi_signal
from polybot.indicators.macd import compute_macd_signal
from polybot.indicators.stochastic import compute_stochastic_signal
from polybot.indicators.ema import compute_ema_signal
from polybot.indicators.obv import compute_obv_signal
from polybot.indicators.vwap import compute_vwap_signal
from polybot.indicators.atr import compute_atr_gate

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "rsi": {"period": 5, "overbought": 70, "oversold": 30},
    "macd": {"fast": 5, "slow": 13, "signal_period": 4},
    "stochastic": {"k_period": 5, "d_smoothing": 2, "overbought": 80, "oversold": 20},
    "ema": {"fast_period": 3, "slow_period": 8, "chop_threshold": 0.001},
    "obv": {"slope_period": 3},
    "atr": {"period": 7, "low_pct": 5, "history": 100},
}

DEFAULT_WEIGHTS = {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}


class IndicatorNormalizer:
    """Exponentially-weighted running mean/variance per indicator.

    Normalizes raw indicator scores to zero-mean, unit-variance before
    weighted aggregation. Ensures configured weights reflect actual
    contribution, not variance dominance.
    """

    def __init__(self, alpha: float = 0.02, warmup: int = 50) -> None:
        self.alpha: float = alpha
        self.warmup: int = warmup
        self._stats: dict[str, dict] = {}

    def normalize(self, name: str, raw_score: float) -> float:
        stats = self._stats.setdefault(name, {"mean": 0.0, "var": 1.0, "count": 0})
        stats["count"] += 1

        if stats["count"] == 1:
            stats["mean"] = raw_score
            stats["var"] = 1.0
        else:
            delta = raw_score - stats["mean"]
            stats["mean"] += self.alpha * delta
            stats["var"] = (1 - self.alpha) * stats["var"] + self.alpha * delta * delta

        if stats["count"] < self.warmup:
            return 0.0  # neutral during warmup, no adjustment

        std = max(math.sqrt(stats["var"]), 1e-6)
        z = (raw_score - stats["mean"]) / std
        return max(-3.0, min(3.0, z))


class IndicatorEngine:
    """Computes RSI/MACD/Stoch/EMA/OBV/VWAP/ATR each tick from the candle buffer.

    The L4 indicator weights are mutated in-place by the pipeline when a
    `weights` change is adopted; settings.yaml is the persistence source of
    truth (saved by the scheduler in the same step). No version A/B framework.
    """

    def __init__(self, weights: dict[str, float] | None = None,
                 params: dict[str, dict[str, Any]] | None = None) -> None:
        self.params: dict[str, dict[str, Any]] = params or DEFAULT_PARAMS
        self._weights: dict[str, float] = dict(weights) if weights else dict(DEFAULT_WEIGHTS)
        self.normalizer: IndicatorNormalizer = IndicatorNormalizer()
        # Cache key is buffer.version (bumps on add AND update_current). The
        # previous timestamp-based key only invalidated on `add` (once per minute),
        # silently returning stale indicators for intra-minute price changes.
        self._cache_version: int = -1
        self._cached: dict[str, dict[str, Any]] = {}

    def get_weights(self) -> dict[str, float]:
        return self._weights.copy()

    def set_weights(self, weights: dict[str, float]) -> None:
        """In-place update of L4 indicator weights."""
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
        for ind_name in ("rsi", "macd", "stochastic", "obv", "vwap"):
            if ind_name in result and "score" in result[ind_name]:
                result[ind_name]["norm_score"] = self.normalizer.normalize(
                    ind_name, result[ind_name]["score"])
        self._cache_version = v
        self._cached = result
        return result

    def get_snapshot(self, indicators: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {"rsi": indicators["rsi"], "macd": indicators["macd"],
                "stochastic": indicators["stochastic"], "ema": indicators["ema"],
                "obv": indicators["obv"], "vwap": indicators["vwap"],
                "atr": indicators["atr"], "weights": self.get_weights()}
