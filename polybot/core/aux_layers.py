"""Shared L3b (spot-flow) and L3e (liquidation) signal helpers.

Live (`main.py`) and replay (`agents/scheduler.py`) call these so the model
computation is identical across paths — eliminates backtest-vs-live drift.
"""
from __future__ import annotations

import math


_COINBASE_CVD_SCALE = 30.0       # ≈ typical 60s Coinbase BTC volume
_LIQ_USD_SCALE = 50_000.0        # cascade-scale net liquidation USD/min
_TAKER_MIN_N = 20                # min trades in window for taker ratio to count


def compute_spot_flow_signal(cvd_60s: float | None,
                              taker_60s: float | None = None,
                              taker_n: int = 0) -> float:
    """L3b spot-venue flow signal ∈ [-1, 1]. Returns 0 when CVD is None (feed cold).

    cvd_60s in BTC units (signed). taker_60s in [0, 1]; counted only when
    taker_n ≥ _TAKER_MIN_N.
    """
    if cvd_60s is None:
        return 0.0
    cvd_comp = math.tanh(cvd_60s / _COINBASE_CVD_SCALE) * 0.8
    taker_comp = (
        (taker_60s - 0.5) * 2.0 * 0.2
        if (taker_60s is not None and taker_n >= _TAKER_MIN_N)
        else 0.0
    )
    return max(-1.0, min(1.0, cvd_comp + taker_comp))


def compute_liquidation_signal(bybit_long_usd_min: float | None,
                                bybit_short_usd_min: float | None,
                                binance_long_usd_min: float | None,
                                binance_short_usd_min: float | None) -> float:
    """L3e direct-stream liquidation pressure ∈ [-1, 1].

    Net (short_liq − long_liq) USD/min, tanh-saturated at the cascade scale.
    Sign: short liquidation → price-up event (+); long liquidation → price-down (−).
    Returns 0 when no streams have emitted (all inputs None or zero).
    """
    long_usd = (bybit_long_usd_min or 0.0) + (binance_long_usd_min or 0.0)
    short_usd = (bybit_short_usd_min or 0.0) + (binance_short_usd_min or 0.0)
    if long_usd == 0.0 and short_usd == 0.0:
        return 0.0
    return math.tanh((short_usd - long_usd) / _LIQ_USD_SCALE)
