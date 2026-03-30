import json
import logging
from pathlib import Path
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
    "rsi": {"period": 14, "overbought": 70, "oversold": 30},
    "macd": {"fast": 12, "slow": 26, "signal_period": 9},
    "stochastic": {"k_period": 14, "d_smoothing": 3, "overbought": 80, "oversold": 20},
    "ema": {"fast_period": 9, "slow_period": 21, "chop_threshold": 0.001},
    "obv": {"slope_period": 5},
    "atr": {"period": 14, "low_pct": 25, "high_pct": 90, "history": 100},
}

class IndicatorEngine:
    def __init__(self, weights_dir: str, active_version: str = "weights_v001",
                 params: dict | None = None):
        self.weights_dir = Path(weights_dir)
        self.active_version = active_version
        self.params = params or DEFAULT_PARAMS
        self._weights = self._load_weights()

    def _load_weights(self) -> dict:
        path = self.weights_dir / f"{self.active_version}.json"
        if path.exists():
            return json.loads(path.read_text())
        return {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20, "entry_threshold": 0.60}

    def get_weights(self) -> dict:
        return self._weights.copy()

    def set_active_version(self, version: str):
        self.active_version = version
        self._weights = self._load_weights()

    def compute_all(self, buffer: CandleBuffer) -> dict:
        closes = buffer.get_closes()
        highs = buffer.get_highs()
        lows = buffer.get_lows()
        volumes = buffer.get_volumes()
        p = self.params
        return {
            "rsi": compute_rsi_signal(closes, **p["rsi"]),
            "macd": compute_macd_signal(closes, **p["macd"]),
            "stochastic": compute_stochastic_signal(highs, lows, closes, **p["stochastic"]),
            "ema": compute_ema_signal(closes, **p["ema"]),
            "obv": compute_obv_signal(closes, volumes, **p["obv"]),
            "vwap": compute_vwap_signal(highs, lows, closes, volumes),
            "atr": compute_atr_gate(highs, lows, closes, **p["atr"]),
        }

    def compute_score(self, indicators: dict) -> float:
        w = self._weights
        score = (indicators["rsi"]["score"] * w.get("rsi", 0.20) +
                 indicators["macd"]["score"] * w.get("macd", 0.25) +
                 indicators["stochastic"]["score"] * w.get("stochastic", 0.20) +
                 indicators["obv"]["score"] * w.get("obv", 0.15) +
                 indicators["vwap"]["score"] * w.get("vwap", 0.20))
        return max(-1.0, min(1.0, score))

    def get_snapshot(self, indicators: dict) -> dict:
        return {"rsi": indicators["rsi"], "macd": indicators["macd"],
                "stochastic": indicators["stochastic"], "ema": indicators["ema"],
                "obv": indicators["obv"], "vwap": indicators["vwap"],
                "atr": indicators["atr"], "weights": self.get_weights()}
