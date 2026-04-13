from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from polybot.core.binance_feed import CandleBuffer
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
    "atr": {"period": 7, "low_pct": 5, "high_pct": 95, "history": 100},
}

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
    def __init__(self, weights_dir: str, active_version: str = "weights_v001",
                 params: dict[str, dict[str, Any]] | None = None) -> None:
        self.weights_dir: Path = Path(weights_dir)
        self.active_version: str = active_version
        self.params: dict[str, dict[str, Any]] = params or DEFAULT_PARAMS
        self._weights: dict[str, float] = self._load_weights()
        self.normalizer: IndicatorNormalizer = IndicatorNormalizer()

    def _load_weights(self) -> dict[str, float]:
        path = self.weights_dir / f"{self.active_version}.json"
        if path.exists():
            return json.loads(path.read_text())
        return {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20, "entry_threshold": 0.60}

    def get_weights(self) -> dict[str, float]:
        return self._weights.copy()

    def set_active_version(self, version: str) -> None:
        self.active_version = version
        self._weights = self._load_weights()

    def compute_all(self, buffer: CandleBuffer) -> dict[str, dict[str, Any]]:
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
        return result

    def compute_score(self, indicators: dict[str, dict[str, Any]]) -> float:
        w = self._weights
        score = (indicators["rsi"]["score"] * w.get("rsi", 0.20) +
                 indicators["macd"]["score"] * w.get("macd", 0.25) +
                 indicators["stochastic"]["score"] * w.get("stochastic", 0.20) +
                 indicators["obv"]["score"] * w.get("obv", 0.15) +
                 indicators["vwap"]["score"] * w.get("vwap", 0.20))
        return max(-1.0, min(1.0, score))

    def get_snapshot(self, indicators: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {"rsi": indicators["rsi"], "macd": indicators["macd"],
                "stochastic": indicators["stochastic"], "ema": indicators["ema"],
                "obv": indicators["obv"], "vwap": indicators["vwap"],
                "atr": indicators["atr"], "weights": self.get_weights()}
