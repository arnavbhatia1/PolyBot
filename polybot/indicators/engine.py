from __future__ import annotations

from typing import Any
from polybot.feeds.binance_feed import CandleBuffer
from polybot.indicators.atr import compute_atr_gate

DEFAULT_PARAMS = {
    "atr": {"period": 7, "low_pct": 5, "history": 100},
}


class IndicatorEngine:
    """ATR from the candle buffer — L1's vol input + the low-vol entry gate.

    The momentum-indicator committee this used to compute (RSI/MACD/Stoch/EMA/
    OBV/VWAP) was deleted with the entry-side prediction stack: nothing it fed
    survived the no-entry-edge verdict.
    """

    def __init__(self, params: dict[str, dict[str, Any]] | None = None) -> None:
        p = params or DEFAULT_PARAMS
        self.params: dict[str, dict[str, Any]] = {"atr": p.get("atr", DEFAULT_PARAMS["atr"])}
        self._cache_version: int = -1
        self._cached: dict[str, dict[str, Any]] = {}

    def compute_all(self, buffer: CandleBuffer, *, force: bool = False) -> dict[str, dict[str, Any]]:
        v = getattr(buffer, "version", -1)
        if not force and v == self._cache_version and self._cached:
            return self._cached
        result = {
            "atr": compute_atr_gate(buffer.get_highs(), buffer.get_lows(),
                                    buffer.get_closes(), **self.params["atr"]),
        }
        # Stamp the current 1-min candle's open-time so SignalEngine keeps one
        # rolling-ATR slot per candle (the forming candle keeps this timestamp for
        # the whole minute; only a new candle changes it).
        latest = buffer.latest() if hasattr(buffer, "latest") else None
        result["atr"]["candle_ts"] = latest.timestamp if latest is not None else None
        self._cache_version = v
        self._cached = result
        return result

    def get_snapshot(self, indicators: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {"atr": indicators["atr"]}
