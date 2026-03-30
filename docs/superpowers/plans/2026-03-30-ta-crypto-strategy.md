# TA Crypto Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve PolyBot into a 5-min BTC micro-trader using 7 technical indicators with gates + weighted scoring and a TA-focused learning pipeline.

**Architecture:** Binance WebSocket feeds real-time BTC candles into an in-memory buffer. Every 1 second, 7 indicators are computed, hard gates filter bad conditions, weighted scoring produces a signal, and if strong enough a trade is placed on the active Polymarket 5-min BTC contract. Learning agents tune all parameters daily.

**Tech Stack:** Python 3.11+, asyncio, websockets, numpy, httpx, py-clob-client, anthropic, discord.py, aiosqlite

---

## File Map

```
polybot/
  config/settings.yaml                  # MODIFY: add TA config sections
  requirements.txt                      # MODIFY: add websockets
  core/
    binance_feed.py                     # NEW: WebSocket + candle buffer
    market_scanner.py                   # NEW: 5-min BTC contract finder
    signal_engine.py                    # NEW: gates + weighted scoring
  indicators/
    __init__.py                         # NEW
    ema.py                              # NEW
    rsi.py                              # NEW
    macd.py                             # NEW
    stochastic.py                       # NEW
    obv.py                              # NEW
    vwap.py                             # NEW
    atr.py                              # NEW
    engine.py                           # NEW: combines all, manages weights
  agents/
    ta_evolver.py                       # NEW: replaces strategy_evolver
    weight_optimizer.py                 # NEW: replaces prompt_optimizer
    outcome_reviewer.py                 # MODIFY: add indicator snapshots
    bias_detector.py                    # MODIFY: indicator-level biases
    scheduler.py                        # MODIFY: wire new agents
  memory/
    weights/weights_v001.json           # NEW: initial weights
    weight_scores.json                  # NEW: Sharpe per weight version
  db/models.py                          # MODIFY: add indicator_snapshot column
  main.py                               # MODIFY: new trading loop
  discord_bot/commands.py               # MODIFY: TA-specific status
  tests/
    test_ema.py                         # NEW
    test_rsi.py                         # NEW
    test_macd.py                        # NEW
    test_stochastic.py                  # NEW
    test_obv.py                         # NEW
    test_vwap.py                        # NEW
    test_atr.py                         # NEW
    test_indicator_engine.py            # NEW
    test_binance_feed.py                # NEW
    test_signal_engine.py               # NEW
    test_market_scanner.py              # NEW
    test_ta_evolver.py                  # NEW
    test_weight_optimizer.py            # NEW
    test_ta_integration.py              # NEW
```

---

## Task 1: Update Config & Dependencies

**Files:**
- Modify: `polybot/config/settings.yaml`
- Modify: `requirements.txt`

- [ ] **Step 1: Add websockets to requirements.txt**

Add after the `pyyaml` line:
```
websockets>=13.0
```

- [ ] **Step 2: Add TA config sections to settings.yaml**

Add after the existing `database:` section:

```yaml
# TA Strategy Configuration
binance:
  symbol: "btcusdt"
  ws_url: "wss://stream.binance.com:9443/ws"
  rest_url: "https://api.binance.com/api/v3"
  candle_buffer_size: 200

indicators:
  rsi:
    period: 14
    overbought: 70
    oversold: 30
  macd:
    fast_period: 12
    slow_period: 26
    signal_period: 9
  stochastic:
    k_period: 14
    d_smoothing: 3
    overbought: 80
    oversold: 20
  ema:
    fast_period: 9
    slow_period: 21
    chop_threshold: 0.001  # % difference below which EMAs are "chopping"
  obv:
    slope_period: 5
  vwap:
    session_minutes: 5
  atr:
    period: 14
    low_percentile: 25
    high_percentile: 90
    history_periods: 100

signal:
  entry_threshold: 0.60
  weights:
    rsi: 0.20
    macd: 0.25
    stochastic: 0.20
    obv: 0.15
    vwap: 0.20
  active_weights_version: "weights_v001"

market:
  contract_type: "btc_5min"
  entry_window_seconds: 120  # 2 minutes
  min_time_remaining_seconds: 30  # don't enter in last 30s
  scan_cache_seconds: 5
```

- [ ] **Step 3: Create initial weights file**

```json
// polybot/memory/weights/weights_v001.json
{
  "rsi": 0.20,
  "macd": 0.25,
  "stochastic": 0.20,
  "obv": 0.15,
  "vwap": 0.20,
  "entry_threshold": 0.60,
  "version": "weights_v001"
}
```

- [ ] **Step 4: Create weight_scores.json**

```json
// polybot/memory/weight_scores.json
{
  "weights_v001": {"sharpe": 0.0, "total_trades": 0, "win_rate": 0.0}
}
```

- [ ] **Step 5: Install websockets and commit**

```bash
pip install websockets
git add polybot/config/settings.yaml requirements.txt polybot/memory/weights/ polybot/memory/weight_scores.json
git commit -m "feat: add TA strategy config, initial weights, and websockets dependency"
```

---

## Task 2: Candle Store & Binance Feed

**Files:**
- Create: `polybot/core/binance_feed.py`
- Create: `polybot/tests/test_binance_feed.py`

- [ ] **Step 1: Write tests**

```python
# polybot/tests/test_binance_feed.py
import pytest
import numpy as np
from polybot.core.binance_feed import Candle, CandleBuffer

def _make_candle(timestamp=1000, open=50000, high=50100, low=49900, close=50050, volume=10.0):
    return Candle(timestamp=timestamp, open=open, high=high, low=low, close=close, volume=volume)

def test_candle_creation():
    c = _make_candle()
    assert c.close == 50050
    assert c.volume == 10.0

def test_buffer_add_candle():
    buf = CandleBuffer(max_size=5)
    buf.add(_make_candle(timestamp=1000))
    assert len(buf) == 1

def test_buffer_max_size():
    buf = CandleBuffer(max_size=3)
    for i in range(5):
        buf.add(_make_candle(timestamp=i * 60000))
    assert len(buf) == 3

def test_buffer_get_closes():
    buf = CandleBuffer(max_size=10)
    for i in range(5):
        buf.add(_make_candle(timestamp=i * 60000, close=100 + i))
    closes = buf.get_closes()
    assert len(closes) == 5
    assert closes[-1] == 104

def test_buffer_get_highs_lows():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(high=100, low=90))
    buf.add(_make_candle(high=110, low=85))
    highs = buf.get_highs()
    lows = buf.get_lows()
    assert highs[-1] == 110
    assert lows[-1] == 85

def test_buffer_get_volumes():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(volume=5.0))
    buf.add(_make_candle(volume=10.0))
    vols = buf.get_volumes()
    assert vols[-1] == 10.0

def test_buffer_get_last_n():
    buf = CandleBuffer(max_size=10)
    for i in range(5):
        buf.add(_make_candle(timestamp=i * 60000, close=100 + i))
    last3 = buf.get_last_n(3)
    assert len(last3) == 3
    assert last3[-1].close == 104

def test_buffer_update_current_candle():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(timestamp=60000, close=100))
    buf.update_current(close=105, high=110, low=95, volume=20.0)
    assert buf.get_closes()[-1] == 105

def test_buffer_empty_returns_empty_arrays():
    buf = CandleBuffer(max_size=10)
    assert len(buf.get_closes()) == 0

def test_buffer_latest():
    buf = CandleBuffer(max_size=10)
    buf.add(_make_candle(close=999))
    assert buf.latest().close == 999

def test_buffer_latest_empty_returns_none():
    buf = CandleBuffer(max_size=10)
    assert buf.latest() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_binance_feed.py -v`
Expected: FAIL

- [ ] **Step 3: Implement Candle and CandleBuffer**

```python
# polybot/core/binance_feed.py
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from collections import deque
import numpy as np
import httpx

logger = logging.getLogger(__name__)

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

class CandleBuffer:
    def __init__(self, max_size: int = 200):
        self.max_size = max_size
        self._candles: deque[Candle] = deque(maxlen=max_size)

    def __len__(self) -> int:
        return len(self._candles)

    def add(self, candle: Candle):
        self._candles.append(candle)

    def update_current(self, close: float, high: float, low: float, volume: float):
        if self._candles:
            c = self._candles[-1]
            c.close = close
            c.high = max(c.high, high)
            c.low = min(c.low, low)
            c.volume = volume

    def latest(self) -> Candle | None:
        return self._candles[-1] if self._candles else None

    def get_last_n(self, n: int) -> list[Candle]:
        items = list(self._candles)
        return items[-n:] if len(items) >= n else items

    def get_closes(self) -> np.ndarray:
        return np.array([c.close for c in self._candles], dtype=np.float64)

    def get_highs(self) -> np.ndarray:
        return np.array([c.high for c in self._candles], dtype=np.float64)

    def get_lows(self) -> np.ndarray:
        return np.array([c.low for c in self._candles], dtype=np.float64)

    def get_volumes(self) -> np.ndarray:
        return np.array([c.volume for c in self._candles], dtype=np.float64)

    def get_opens(self) -> np.ndarray:
        return np.array([c.open for c in self._candles], dtype=np.float64)

class BinanceFeed:
    def __init__(self, symbol: str = "btcusdt", buffer_size: int = 200,
                 ws_url: str = "wss://stream.binance.com:9443/ws",
                 rest_url: str = "https://api.binance.com/api/v3"):
        self.symbol = symbol
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.buffer = CandleBuffer(max_size=buffer_size)
        self._running = False
        self._ws = None

    async def backfill(self):
        url = f"{self.rest_url}/klines"
        params = {"symbol": self.symbol.upper(), "interval": "1m", "limit": self.buffer.max_size}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            klines = resp.json()
        for k in klines:
            self.buffer.add(Candle(
                timestamp=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
            ))
        logger.info(f"Backfilled {len(klines)} candles")

    async def _connect_ws(self):
        import websockets
        stream = f"{self.ws_url}/{self.symbol}@kline_1m"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(stream) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info(f"Binance WebSocket connected: {stream}")
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_kline(json.loads(msg))
            except Exception as e:
                logger.warning(f"WebSocket error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_kline(self, data: dict):
        k = data.get("k", {})
        if not k:
            return
        candle = Candle(
            timestamp=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
        )
        is_closed = k.get("x", False)
        if is_closed:
            self.buffer.add(candle)
        else:
            self.buffer.update_current(
                close=candle.close, high=candle.high,
                low=candle.low, volume=candle.volume,
            )

    async def start(self):
        self._running = True
        await self.backfill()
        asyncio.create_task(self._connect_ws())

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest polybot/tests/test_binance_feed.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/binance_feed.py polybot/tests/test_binance_feed.py
git commit -m "feat: Binance WebSocket price feed with rolling candle buffer"
```

---

## Task 3: EMA Indicator

**Files:**
- Create: `polybot/indicators/__init__.py`
- Create: `polybot/indicators/ema.py`
- Create: `polybot/tests/test_ema.py`

- [ ] **Step 1: Write tests**

```python
# polybot/tests/test_ema.py
import pytest
import numpy as np
from polybot.indicators.ema import compute_ema, compute_ema_signal

def test_ema_length_matches_input():
    closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    result = compute_ema(closes, period=3)
    assert len(result) == len(closes)

def test_ema_responds_to_recent_prices():
    closes = np.array([10.0] * 10 + [20.0])
    ema = compute_ema(closes, period=5)
    assert ema[-1] > 10.0  # Moved toward 20

def test_ema_fast_above_slow_bullish():
    # Uptrending data
    closes = np.array([float(i) for i in range(1, 30)])
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "bullish"
    assert signal["fast_ema"] > signal["slow_ema"]

def test_ema_fast_below_slow_bearish():
    # Downtrending data
    closes = np.array([float(30 - i) for i in range(30)])
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "bearish"

def test_ema_chop_detection():
    # Flat data = chopping
    closes = np.array([100.0] * 30)
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "chop"

def test_ema_needs_enough_data():
    closes = np.array([1.0, 2.0])
    signal = compute_ema_signal(closes, fast_period=9, slow_period=21, chop_threshold=0.001)
    assert signal["trend"] == "insufficient_data"
```

- [ ] **Step 2: Implement**

```python
# polybot/indicators/__init__.py
# (empty)
```

```python
# polybot/indicators/ema.py
import numpy as np

def compute_ema(closes: np.ndarray, period: int) -> np.ndarray:
    if len(closes) < period:
        return closes.copy()
    alpha = 2.0 / (period + 1)
    ema = np.empty_like(closes)
    ema[0] = closes[0]
    for i in range(1, len(closes)):
        ema[i] = alpha * closes[i] + (1 - alpha) * ema[i - 1]
    return ema

def compute_ema_signal(closes: np.ndarray, fast_period: int = 9, slow_period: int = 21,
                       chop_threshold: float = 0.001) -> dict:
    if len(closes) < slow_period + 1:
        return {"trend": "insufficient_data", "fast_ema": 0.0, "slow_ema": 0.0}
    fast = compute_ema(closes, fast_period)
    slow = compute_ema(closes, slow_period)
    fast_val = float(fast[-1])
    slow_val = float(slow[-1])
    mid = (fast_val + slow_val) / 2.0
    if mid == 0:
        return {"trend": "chop", "fast_ema": fast_val, "slow_ema": slow_val}
    diff_pct = abs(fast_val - slow_val) / mid
    if diff_pct < chop_threshold:
        trend = "chop"
    elif fast_val > slow_val:
        trend = "bullish"
    else:
        trend = "bearish"
    return {"trend": trend, "fast_ema": fast_val, "slow_ema": slow_val}
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/test_ema.py -v`
Expected: All 6 tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/indicators/ polybot/tests/test_ema.py
git commit -m "feat: EMA indicator with trend and chop detection"
```

---

## Task 4: RSI, MACD, Stochastic, OBV, VWAP, ATR Indicators

**Files:**
- Create: `polybot/indicators/rsi.py`, `polybot/indicators/macd.py`, `polybot/indicators/stochastic.py`, `polybot/indicators/obv.py`, `polybot/indicators/vwap.py`, `polybot/indicators/atr.py`
- Create: `polybot/tests/test_rsi.py`, `polybot/tests/test_macd.py`, `polybot/tests/test_stochastic.py`, `polybot/tests/test_obv.py`, `polybot/tests/test_vwap.py`, `polybot/tests/test_atr.py`

- [ ] **Step 1: Write all indicator tests**

```python
# polybot/tests/test_rsi.py
import pytest
import numpy as np
from polybot.indicators.rsi import compute_rsi, compute_rsi_signal

def test_rsi_range():
    closes = np.array([44.0, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
                       46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    rsi = compute_rsi(closes, period=14)
    assert 0 <= rsi <= 100

def test_rsi_overbought():
    closes = np.array([float(i) for i in range(50)])  # Strong uptrend
    rsi = compute_rsi(closes, period=14)
    assert rsi > 70

def test_rsi_oversold():
    closes = np.array([float(50 - i) for i in range(50)])  # Strong downtrend
    rsi = compute_rsi(closes, period=14)
    assert rsi < 30

def test_rsi_signal_score_bearish_when_overbought():
    closes = np.array([float(i) for i in range(50)])
    signal = compute_rsi_signal(closes, period=14, overbought=70, oversold=30)
    assert signal["score"] < 0  # Overbought = bearish signal

def test_rsi_signal_score_bullish_when_oversold():
    closes = np.array([float(50 - i) for i in range(50)])
    signal = compute_rsi_signal(closes, period=14, overbought=70, oversold=30)
    assert signal["score"] > 0  # Oversold = bullish signal

def test_rsi_insufficient_data():
    closes = np.array([1.0, 2.0])
    signal = compute_rsi_signal(closes, period=14, overbought=70, oversold=30)
    assert signal["score"] == 0.0
```

```python
# polybot/tests/test_macd.py
import pytest
import numpy as np
from polybot.indicators.macd import compute_macd, compute_macd_signal

def test_macd_returns_three_components():
    closes = np.array([float(i) for i in range(50)])
    macd_line, signal_line, histogram = compute_macd(closes, fast=12, slow=26, signal=9)
    assert isinstance(macd_line, float)
    assert isinstance(signal_line, float)
    assert isinstance(histogram, float)

def test_macd_bullish_crossover():
    closes = np.array([float(50 - i) for i in range(30)] + [float(20 + i * 2) for i in range(20)])
    signal = compute_macd_signal(closes, fast=12, slow=26, signal_period=9)
    assert signal["histogram"] != 0

def test_macd_signal_score_positive_when_bullish():
    closes = np.array([float(i) for i in range(50)])
    signal = compute_macd_signal(closes, fast=12, slow=26, signal_period=9)
    assert signal["score"] > 0

def test_macd_signal_score_negative_when_bearish():
    closes = np.array([float(50 - i) for i in range(50)])
    signal = compute_macd_signal(closes, fast=12, slow=26, signal_period=9)
    assert signal["score"] < 0

def test_macd_insufficient_data():
    closes = np.array([1.0, 2.0])
    signal = compute_macd_signal(closes, fast=12, slow=26, signal_period=9)
    assert signal["score"] == 0.0
```

```python
# polybot/tests/test_stochastic.py
import pytest
import numpy as np
from polybot.indicators.stochastic import compute_stochastic, compute_stochastic_signal

def test_stochastic_range():
    highs = np.array([float(50 + i % 5) for i in range(20)])
    lows = np.array([float(45 + i % 5) for i in range(20)])
    closes = np.array([float(47 + i % 5) for i in range(20)])
    k, d = compute_stochastic(highs, lows, closes, k_period=14, d_smoothing=3)
    assert 0 <= k <= 100
    assert 0 <= d <= 100

def test_stochastic_overbought_signal():
    highs = np.array([float(i) for i in range(50, 70)])
    lows = np.array([float(i - 1) for i in range(50, 70)])
    closes = np.array([float(i - 0.1) for i in range(50, 70)])  # Close near high = overbought
    signal = compute_stochastic_signal(highs, lows, closes, k_period=14, d_smoothing=3, overbought=80, oversold=20)
    assert signal["k"] > 80

def test_stochastic_signal_bearish_when_overbought():
    highs = np.array([float(i) for i in range(50, 70)])
    lows = np.array([float(i - 1) for i in range(50, 70)])
    closes = np.array([float(i - 0.1) for i in range(50, 70)])
    signal = compute_stochastic_signal(highs, lows, closes, k_period=14, d_smoothing=3, overbought=80, oversold=20)
    assert signal["score"] < 0

def test_stochastic_insufficient_data():
    signal = compute_stochastic_signal(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                                        k_period=14, d_smoothing=3, overbought=80, oversold=20)
    assert signal["score"] == 0.0
```

```python
# polybot/tests/test_obv.py
import pytest
import numpy as np
from polybot.indicators.obv import compute_obv, compute_obv_signal

def test_obv_increases_on_up_close():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    volumes = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
    obv = compute_obv(closes, volumes)
    assert obv[-1] > obv[0]

def test_obv_decreases_on_down_close():
    closes = np.array([14.0, 13.0, 12.0, 11.0, 10.0])
    volumes = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
    obv = compute_obv(closes, volumes)
    assert obv[-1] < obv[0]

def test_obv_signal_bullish_when_price_up_obv_up():
    closes = np.array([float(10 + i) for i in range(20)])
    volumes = np.array([100.0] * 20)
    signal = compute_obv_signal(closes, volumes, slope_period=5)
    assert signal["score"] > 0

def test_obv_signal_bearish_when_price_down_obv_down():
    closes = np.array([float(30 - i) for i in range(20)])
    volumes = np.array([100.0] * 20)
    signal = compute_obv_signal(closes, volumes, slope_period=5)
    assert signal["score"] < 0

def test_obv_insufficient_data():
    signal = compute_obv_signal(np.array([1.0]), np.array([1.0]), slope_period=5)
    assert signal["score"] == 0.0
```

```python
# polybot/tests/test_vwap.py
import pytest
import numpy as np
from polybot.indicators.vwap import compute_vwap, compute_vwap_signal

def test_vwap_basic():
    highs = np.array([101.0, 102.0, 103.0])
    lows = np.array([99.0, 98.0, 97.0])
    closes = np.array([100.0, 100.0, 100.0])
    volumes = np.array([10.0, 10.0, 10.0])
    vwap = compute_vwap(highs, lows, closes, volumes)
    assert vwap > 0

def test_vwap_signal_bullish_below_vwap():
    # Price well below VWAP = undervalued = bullish
    highs = np.array([100.0] * 10 + [90.0] * 5)
    lows = np.array([99.0] * 10 + [89.0] * 5)
    closes = np.array([100.0] * 10 + [89.5] * 5)
    volumes = np.array([100.0] * 15)
    signal = compute_vwap_signal(highs, lows, closes, volumes)
    assert signal["score"] > 0  # Below VWAP = bullish (undervalued)

def test_vwap_signal_bearish_above_vwap():
    highs = np.array([100.0] * 10 + [111.0] * 5)
    lows = np.array([99.0] * 10 + [110.0] * 5)
    closes = np.array([100.0] * 10 + [110.5] * 5)
    volumes = np.array([100.0] * 15)
    signal = compute_vwap_signal(highs, lows, closes, volumes)
    assert signal["score"] < 0  # Above VWAP = bearish (overextended)

def test_vwap_insufficient_data():
    signal = compute_vwap_signal(np.array([1.0]), np.array([1.0]), np.array([1.0]), np.array([1.0]))
    assert signal["score"] == 0.0
```

```python
# polybot/tests/test_atr.py
import pytest
import numpy as np
from polybot.indicators.atr import compute_atr, compute_atr_gate

def test_atr_positive():
    highs = np.array([float(100 + i % 3) for i in range(20)])
    lows = np.array([float(98 + i % 3) for i in range(20)])
    closes = np.array([float(99 + i % 3) for i in range(20)])
    atr = compute_atr(highs, lows, closes, period=14)
    assert atr > 0

def test_atr_gate_passes_normal_volatility():
    highs = np.array([float(100 + (i % 5)) for i in range(100)])
    lows = np.array([float(98 + (i % 5)) for i in range(100)])
    closes = np.array([float(99 + (i % 5)) for i in range(100)])
    result = compute_atr_gate(highs, lows, closes, period=14, low_pct=25, high_pct=90, history=100)
    assert isinstance(result["passes"], bool)
    assert "atr" in result

def test_atr_gate_fails_zero_volatility():
    highs = np.array([100.0] * 100)
    lows = np.array([100.0] * 100)
    closes = np.array([100.0] * 100)
    result = compute_atr_gate(highs, lows, closes, period=14, low_pct=25, high_pct=90, history=100)
    assert result["passes"] is False  # Too quiet

def test_atr_insufficient_data():
    result = compute_atr_gate(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                               period=14, low_pct=25, high_pct=90, history=100)
    assert result["passes"] is False
```

- [ ] **Step 2: Implement all 6 indicators**

```python
# polybot/indicators/rsi.py
import numpy as np

def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def compute_rsi_signal(closes: np.ndarray, period: int = 14,
                       overbought: float = 70, oversold: float = 30) -> dict:
    if len(closes) < period + 1:
        return {"rsi": 50.0, "score": 0.0}
    rsi = compute_rsi(closes, period)
    if rsi >= overbought:
        score = -((rsi - overbought) / (100 - overbought))
    elif rsi <= oversold:
        score = (oversold - rsi) / oversold
    else:
        mid = (overbought + oversold) / 2
        score = -(rsi - mid) / (overbought - mid) * 0.3
    return {"rsi": round(rsi, 2), "score": round(max(-1.0, min(1.0, score)), 4)}
```

```python
# polybot/indicators/macd.py
import numpy as np
from polybot.indicators.ema import compute_ema

def compute_macd(closes: np.ndarray, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> tuple[float, float, float]:
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    fast_ema = compute_ema(closes, fast)
    slow_ema = compute_ema(closes, slow)
    macd_line = fast_ema - slow_ema
    signal_line = compute_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1])

def compute_macd_signal(closes: np.ndarray, fast: int = 12, slow: int = 26,
                        signal_period: int = 9) -> dict:
    if len(closes) < slow + signal_period:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "score": 0.0}
    macd_val, sig_val, hist = compute_macd(closes, fast, slow, signal_period)
    price_range = float(np.max(closes[-slow:]) - np.min(closes[-slow:]))
    if price_range == 0:
        score = 0.0
    else:
        score = hist / price_range * 5.0
    score = max(-1.0, min(1.0, score))
    return {"macd": round(macd_val, 4), "signal": round(sig_val, 4),
            "histogram": round(hist, 4), "score": round(score, 4)}
```

```python
# polybot/indicators/stochastic.py
import numpy as np

def compute_stochastic(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                       k_period: int = 14, d_smoothing: int = 3) -> tuple[float, float]:
    if len(closes) < k_period + d_smoothing:
        return 50.0, 50.0
    k_values = []
    for i in range(k_period - 1, len(closes)):
        h = np.max(highs[i - k_period + 1:i + 1])
        l = np.min(lows[i - k_period + 1:i + 1])
        if h == l:
            k_values.append(50.0)
        else:
            k_values.append(((closes[i] - l) / (h - l)) * 100)
    k_arr = np.array(k_values)
    d_val = float(np.mean(k_arr[-d_smoothing:])) if len(k_arr) >= d_smoothing else float(k_arr[-1])
    return float(k_arr[-1]), d_val

def compute_stochastic_signal(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                              k_period: int = 14, d_smoothing: int = 3,
                              overbought: float = 80, oversold: float = 20) -> dict:
    if len(closes) < k_period + d_smoothing:
        return {"k": 50.0, "d": 50.0, "score": 0.0}
    k, d = compute_stochastic(highs, lows, closes, k_period, d_smoothing)
    if k >= overbought:
        score = -((k - overbought) / (100 - overbought))
    elif k <= oversold:
        score = (oversold - k) / oversold
    else:
        score = 0.0
    if k > d and k < oversold + 10:
        score = max(score, 0.3)
    elif k < d and k > overbought - 10:
        score = min(score, -0.3)
    return {"k": round(k, 2), "d": round(d, 2), "score": round(max(-1.0, min(1.0, score)), 4)}
```

```python
# polybot/indicators/obv.py
import numpy as np

def compute_obv(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    if len(closes) < 2:
        return np.array([0.0])
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    return obv

def compute_obv_signal(closes: np.ndarray, volumes: np.ndarray,
                       slope_period: int = 5) -> dict:
    if len(closes) < slope_period + 1:
        return {"obv_slope": 0.0, "price_slope": 0.0, "score": 0.0}
    obv = compute_obv(closes, volumes)
    obv_slope = float(obv[-1] - obv[-slope_period]) / slope_period
    price_slope = float(closes[-1] - closes[-slope_period]) / slope_period
    if obv_slope == 0:
        score = 0.0
    elif (obv_slope > 0 and price_slope > 0):
        score = min(1.0, abs(obv_slope) / (abs(obv_slope) + 1))
    elif (obv_slope < 0 and price_slope < 0):
        score = -min(1.0, abs(obv_slope) / (abs(obv_slope) + 1))
    else:
        score = 0.0
    return {"obv_slope": round(obv_slope, 2), "price_slope": round(price_slope, 4),
            "score": round(score, 4)}
```

```python
# polybot/indicators/vwap.py
import numpy as np

def compute_vwap(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                 volumes: np.ndarray) -> float:
    if len(closes) < 2 or np.sum(volumes) == 0:
        return float(closes[-1]) if len(closes) > 0 else 0.0
    typical_price = (highs + lows + closes) / 3.0
    return float(np.sum(typical_price * volumes) / np.sum(volumes))

def compute_vwap_signal(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                        volumes: np.ndarray) -> dict:
    if len(closes) < 3:
        return {"vwap": 0.0, "deviation": 0.0, "score": 0.0}
    vwap = compute_vwap(highs, lows, closes, volumes)
    price = float(closes[-1])
    typical = (highs + lows + closes) / 3.0
    std = float(np.std(typical - vwap)) if len(typical) > 1 else 1.0
    if std == 0:
        std = 1.0
    deviation = (price - vwap) / std
    score = -deviation * 0.3
    score = max(-1.0, min(1.0, score))
    return {"vwap": round(vwap, 2), "deviation": round(deviation, 4),
            "score": round(score, 4)}
```

```python
# polybot/indicators/atr.py
import numpy as np

def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    return float(np.mean(tr[-period:]))

def compute_atr_gate(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                     period: int = 14, low_pct: int = 25, high_pct: int = 90,
                     history: int = 100) -> dict:
    if len(closes) < period + 2:
        return {"atr": 0.0, "passes": False, "reason": "insufficient_data"}
    atr_current = compute_atr(highs, lows, closes, period)
    n = min(history, len(closes) - period)
    atr_history = []
    for i in range(n):
        end = len(closes) - i
        if end < period + 1:
            break
        atr_history.append(compute_atr(highs[:end], lows[:end], closes[:end], period))
    if not atr_history:
        return {"atr": atr_current, "passes": False, "reason": "no_history"}
    low_thresh = float(np.percentile(atr_history, low_pct))
    high_thresh = float(np.percentile(atr_history, high_pct))
    if atr_current < low_thresh:
        return {"atr": round(atr_current, 2), "passes": False, "reason": "too_quiet"}
    if atr_current > high_thresh:
        return {"atr": round(atr_current, 2), "passes": False, "reason": "too_volatile"}
    return {"atr": round(atr_current, 2), "passes": True, "reason": "ok"}
```

- [ ] **Step 3: Run all indicator tests**

Run: `python -m pytest polybot/tests/test_rsi.py polybot/tests/test_macd.py polybot/tests/test_stochastic.py polybot/tests/test_obv.py polybot/tests/test_vwap.py polybot/tests/test_atr.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/indicators/ polybot/tests/test_rsi.py polybot/tests/test_macd.py polybot/tests/test_stochastic.py polybot/tests/test_obv.py polybot/tests/test_vwap.py polybot/tests/test_atr.py
git commit -m "feat: 6 technical indicators — RSI, MACD, Stochastic, OBV, VWAP, ATR"
```

---

## Task 5: Indicator Engine (Combines All 7)

**Files:**
- Create: `polybot/indicators/engine.py`
- Create: `polybot/tests/test_indicator_engine.py`

- [ ] **Step 1: Write tests**

```python
# polybot/tests/test_indicator_engine.py
import pytest
import json
import numpy as np
from pathlib import Path
from polybot.indicators.engine import IndicatorEngine
from polybot.core.binance_feed import Candle, CandleBuffer

def _make_trending_buffer(direction="up", size=50):
    buf = CandleBuffer(max_size=200)
    for i in range(size):
        if direction == "up":
            price = 50000 + i * 50
        else:
            price = 60000 - i * 50
        buf.add(Candle(timestamp=i * 60000, open=price - 10, high=price + 20,
                       low=price - 20, close=price, volume=100.0))
    return buf

@pytest.fixture
def weights_path(tmp_path):
    path = tmp_path / "weights_v001.json"
    path.write_text(json.dumps({
        "rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20,
        "entry_threshold": 0.60, "version": "weights_v001"
    }))
    return str(tmp_path)

@pytest.fixture
def engine(weights_path):
    return IndicatorEngine(weights_dir=weights_path, active_version="weights_v001")

def test_compute_all_returns_7_indicators(engine):
    buf = _make_trending_buffer("up", 50)
    result = engine.compute_all(buf)
    assert "rsi" in result
    assert "macd" in result
    assert "stochastic" in result
    assert "ema" in result
    assert "obv" in result
    assert "vwap" in result
    assert "atr" in result

def test_compute_score_returns_float(engine):
    buf = _make_trending_buffer("up", 50)
    indicators = engine.compute_all(buf)
    score = engine.compute_score(indicators)
    assert isinstance(score, float)
    assert -1.0 <= score <= 1.0

def test_uptrend_produces_positive_score(engine):
    buf = _make_trending_buffer("up", 50)
    indicators = engine.compute_all(buf)
    score = engine.compute_score(indicators)
    assert score > 0

def test_downtrend_produces_negative_score(engine):
    buf = _make_trending_buffer("down", 50)
    indicators = engine.compute_all(buf)
    score = engine.compute_score(indicators)
    assert score < 0

def test_get_snapshot_returns_serializable(engine):
    buf = _make_trending_buffer("up", 50)
    indicators = engine.compute_all(buf)
    snapshot = engine.get_snapshot(indicators)
    json.dumps(snapshot)  # Should not raise

def test_load_weights(engine):
    weights = engine.get_weights()
    assert weights["rsi"] == 0.20
    assert weights["macd"] == 0.25
```

- [ ] **Step 2: Implement**

```python
# polybot/indicators/engine.py
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
        return {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                "obv": 0.15, "vwap": 0.20, "entry_threshold": 0.60}

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
        score = (
            indicators["rsi"]["score"] * w.get("rsi", 0.20) +
            indicators["macd"]["score"] * w.get("macd", 0.25) +
            indicators["stochastic"]["score"] * w.get("stochastic", 0.20) +
            indicators["obv"]["score"] * w.get("obv", 0.15) +
            indicators["vwap"]["score"] * w.get("vwap", 0.20)
        )
        return max(-1.0, min(1.0, score))

    def get_snapshot(self, indicators: dict) -> dict:
        return {
            "rsi": indicators["rsi"],
            "macd": indicators["macd"],
            "stochastic": indicators["stochastic"],
            "ema": indicators["ema"],
            "obv": indicators["obv"],
            "vwap": indicators["vwap"],
            "atr": indicators["atr"],
            "weights": self.get_weights(),
        }
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/test_indicator_engine.py -v`
Expected: All 6 tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/indicators/engine.py polybot/tests/test_indicator_engine.py
git commit -m "feat: indicator engine combining all 7 indicators with weighted scoring"
```

---

## Task 6: Signal Engine (Gates + Scoring)

**Files:**
- Create: `polybot/core/signal_engine.py`
- Create: `polybot/tests/test_signal_engine.py`

- [ ] **Step 1: Write tests**

```python
# polybot/tests/test_signal_engine.py
import pytest
from polybot.core.signal_engine import SignalEngine, TradeSignal

def _make_indicators(atr_passes=True, ema_trend="bullish", rsi_score=0.5,
                     macd_score=0.5, stoch_score=0.5, obv_score=0.3, vwap_score=0.3):
    return {
        "atr": {"atr": 50.0, "passes": atr_passes, "reason": "ok"},
        "ema": {"trend": ema_trend, "fast_ema": 100.0, "slow_ema": 99.0},
        "rsi": {"rsi": 35.0, "score": rsi_score},
        "macd": {"macd": 0.1, "signal": 0.05, "histogram": 0.05, "score": macd_score},
        "stochastic": {"k": 25.0, "d": 30.0, "score": stoch_score},
        "obv": {"obv_slope": 100, "price_slope": 0.5, "score": obv_score},
        "vwap": {"vwap": 99.0, "deviation": -0.5, "score": vwap_score},
    }

@pytest.fixture
def engine():
    return SignalEngine(
        entry_threshold=0.60,
        weights={"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
    )

def test_strong_bullish_signal_returns_buy_yes(engine):
    indicators = _make_indicators(rsi_score=0.8, macd_score=0.9, stoch_score=0.7, obv_score=0.6, vwap_score=0.5)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True)
    assert signal.action == "BUY_YES"

def test_strong_bearish_signal_returns_buy_no(engine):
    indicators = _make_indicators(ema_trend="bearish", rsi_score=-0.8, macd_score=-0.9,
                                   stoch_score=-0.7, obv_score=-0.6, vwap_score=-0.5)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True)
    assert signal.action == "BUY_NO"

def test_weak_signal_returns_skip(engine):
    indicators = _make_indicators(rsi_score=0.1, macd_score=0.1, stoch_score=0.1, obv_score=0.1, vwap_score=0.1)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True)
    assert signal.action == "SKIP"

def test_atr_gate_blocks_trade(engine):
    indicators = _make_indicators(atr_passes=False, rsi_score=0.9, macd_score=0.9, stoch_score=0.9)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True)
    assert signal.action == "SKIP"
    assert "atr" in signal.reason.lower()

def test_ema_chop_blocks_trade(engine):
    indicators = _make_indicators(ema_trend="chop", rsi_score=0.9, macd_score=0.9, stoch_score=0.9)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True)
    assert signal.action == "SKIP"
    assert "chop" in signal.reason.lower()

def test_has_position_blocks_trade(engine):
    indicators = _make_indicators(rsi_score=0.9, macd_score=0.9, stoch_score=0.9)
    signal = engine.evaluate(indicators, has_position=True, in_entry_window=True)
    assert signal.action == "SKIP"

def test_outside_entry_window_blocks_trade(engine):
    indicators = _make_indicators(rsi_score=0.9, macd_score=0.9, stoch_score=0.9)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=False)
    assert signal.action == "SKIP"

def test_signal_includes_score(engine):
    indicators = _make_indicators(rsi_score=0.8, macd_score=0.9, stoch_score=0.7, obv_score=0.6, vwap_score=0.5)
    signal = engine.evaluate(indicators, has_position=False, in_entry_window=True)
    assert signal.score != 0
    assert abs(signal.score) >= 0.60
```

- [ ] **Step 2: Implement**

```python
# polybot/core/signal_engine.py
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    action: str  # "BUY_YES", "BUY_NO", "SKIP"
    score: float
    reason: str
    gate_results: dict

class SignalEngine:
    def __init__(self, entry_threshold: float = 0.60,
                 weights: dict | None = None):
        self.entry_threshold = entry_threshold
        self.weights = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                   "obv": 0.15, "vwap": 0.20}

    def _check_gates(self, indicators: dict, has_position: bool,
                     in_entry_window: bool) -> tuple[bool, str, dict]:
        gates = {}
        if not in_entry_window:
            gates["entry_window"] = False
            return False, "Outside entry window", gates
        gates["entry_window"] = True

        if has_position:
            gates["position"] = False
            return False, "Already have position", gates
        gates["position"] = True

        atr = indicators.get("atr", {})
        if not atr.get("passes", False):
            gates["atr"] = False
            return False, f"ATR gate failed: {atr.get('reason', 'unknown')}", gates
        gates["atr"] = True

        ema = indicators.get("ema", {})
        if ema.get("trend") in ("chop", "insufficient_data"):
            gates["ema"] = False
            return False, f"EMA chop detected — no clear trend", gates
        gates["ema"] = True

        return True, "all_passed", gates

    def _compute_score(self, indicators: dict) -> float:
        w = self.weights
        score = (
            indicators["rsi"]["score"] * w.get("rsi", 0.20) +
            indicators["macd"]["score"] * w.get("macd", 0.25) +
            indicators["stochastic"]["score"] * w.get("stochastic", 0.20) +
            indicators["obv"]["score"] * w.get("obv", 0.15) +
            indicators["vwap"]["score"] * w.get("vwap", 0.20)
        )
        return max(-1.0, min(1.0, score))

    def evaluate(self, indicators: dict, has_position: bool,
                 in_entry_window: bool) -> TradeSignal:
        passes, reason, gates = self._check_gates(indicators, has_position, in_entry_window)
        if not passes:
            return TradeSignal(action="SKIP", score=0.0, reason=reason, gate_results=gates)

        score = self._compute_score(indicators)

        if score >= self.entry_threshold:
            return TradeSignal(action="BUY_YES", score=score, reason="Strong bullish signal", gate_results=gates)
        elif score <= -self.entry_threshold:
            return TradeSignal(action="BUY_NO", score=score, reason="Strong bearish signal", gate_results=gates)
        else:
            return TradeSignal(action="SKIP", score=score,
                               reason=f"Signal {score:.2f} below threshold {self.entry_threshold}",
                               gate_results=gates)
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/test_signal_engine.py -v`
Expected: All 8 tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/core/signal_engine.py polybot/tests/test_signal_engine.py
git commit -m "feat: signal engine with hard gates and weighted scoring"
```

---

## Task 7: 5-min BTC Market Scanner

**Files:**
- Create: `polybot/core/market_scanner.py`
- Create: `polybot/tests/test_market_scanner.py`

- [ ] **Step 1: Write tests**

```python
# polybot/tests/test_market_scanner.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from polybot.core.market_scanner import BTCMarketScanner

SAMPLE_MARKET = {
    "condition_id": "0xbtc5min",
    "question": "Will BTC be above $65,000 at 12:05 UTC?",
    "tokens": [
        {"token_id": "tok_yes", "outcome": "Yes", "price": 0.55},
        {"token_id": "tok_no", "outcome": "No", "price": 0.45},
    ],
    "end_date_iso": "2026-03-30T12:05:00Z",
    "active": True,
    "closed": False,
    "category": "crypto",
}

def test_is_btc_5min_market():
    scanner = BTCMarketScanner()
    assert scanner.is_btc_5min_market(SAMPLE_MARKET) is True

def test_is_not_btc_5min_market():
    scanner = BTCMarketScanner()
    non_btc = SAMPLE_MARKET.copy()
    non_btc["question"] = "Will the election happen?"
    non_btc["category"] = "politics"
    assert scanner.is_btc_5min_market(non_btc) is False

def test_parse_contract_extracts_fields():
    scanner = BTCMarketScanner()
    contract = scanner.parse_contract(SAMPLE_MARKET)
    assert contract["condition_id"] == "0xbtc5min"
    assert contract["price_yes"] == 0.55
    assert contract["price_no"] == 0.45
    assert "token_id_yes" in contract
    assert "token_id_no" in contract

def test_in_entry_window_true():
    scanner = BTCMarketScanner(entry_window_seconds=120)
    assert scanner.in_entry_window(seconds_remaining=240) is True  # 4 min left, 1 min in

def test_in_entry_window_false_too_late():
    scanner = BTCMarketScanner(entry_window_seconds=120)
    assert scanner.in_entry_window(seconds_remaining=60) is False  # Only 1 min left
```

- [ ] **Step 2: Implement**

```python
# polybot/core/market_scanner.py
import logging
import time
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)

class BTCMarketScanner:
    CLOB_BASE_URL = "https://clob.polymarket.com"

    def __init__(self, entry_window_seconds: int = 120, min_time_remaining: int = 30,
                 cache_seconds: int = 5):
        self.entry_window_seconds = entry_window_seconds
        self.min_time_remaining = min_time_remaining
        self.cache_seconds = cache_seconds
        self._cached_contract = None
        self._cache_time = 0

    def is_btc_5min_market(self, market: dict) -> bool:
        question = market.get("question", "").lower()
        category = market.get("category", "").lower()
        is_btc = "btc" in question or "bitcoin" in question
        is_crypto = "crypto" in category
        is_short = any(term in question for term in ["5 min", "5min", "5-min", ":05", ":10", ":15", ":20", ":25", ":30", ":35", ":40", ":45", ":50", ":55", ":00"])
        return is_btc and (is_crypto or is_short)

    def parse_contract(self, market: dict) -> dict:
        tokens = market.get("tokens", [])
        price_yes = price_no = 0.0
        token_id_yes = token_id_no = ""
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            if outcome == "yes":
                price_yes = float(token.get("price", 0))
                token_id_yes = token.get("token_id", "")
            elif outcome == "no":
                price_no = float(token.get("price", 0))
                token_id_no = token.get("token_id", "")

        end_date_str = market.get("end_date_iso", "")
        seconds_remaining = 0
        if end_date_str:
            try:
                end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                seconds_remaining = max(0, (end - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                pass

        return {
            "condition_id": market.get("condition_id", ""),
            "question": market.get("question", ""),
            "price_yes": price_yes,
            "price_no": price_no,
            "token_id_yes": token_id_yes,
            "token_id_no": token_id_no,
            "seconds_remaining": seconds_remaining,
            "end_date": end_date_str,
        }

    def in_entry_window(self, seconds_remaining: float) -> bool:
        contract_duration = 300  # 5 minutes
        seconds_elapsed = contract_duration - seconds_remaining
        return (seconds_elapsed <= self.entry_window_seconds and
                seconds_remaining >= self.min_time_remaining)

    async def find_active_contract(self) -> dict | None:
        now = time.time()
        if self._cached_contract and (now - self._cache_time) < self.cache_seconds:
            contract = self._cached_contract
            end_str = contract.get("end_date", "")
            if end_str:
                try:
                    end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    remaining = (end - datetime.now(timezone.utc)).total_seconds()
                    if remaining > 0:
                        contract["seconds_remaining"] = remaining
                        return contract
                except ValueError:
                    pass
            self._cached_contract = None

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.CLOB_BASE_URL}/markets",
                                        params={"active": True, "closed": False, "limit": 100})
                resp.raise_for_status()
                data = resp.json()
                markets = data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return None

        for market in markets:
            if self.is_btc_5min_market(market):
                contract = self.parse_contract(market)
                if contract["seconds_remaining"] > self.min_time_remaining:
                    self._cached_contract = contract
                    self._cache_time = now
                    return contract
        return None
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/test_market_scanner.py -v`
Expected: All 5 tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/core/market_scanner.py polybot/tests/test_market_scanner.py
git commit -m "feat: 5-min BTC market scanner with entry window detection"
```

---

## Task 8: TA Evolver & Weight Optimizer Agents

**Files:**
- Create: `polybot/agents/ta_evolver.py`, `polybot/agents/weight_optimizer.py`
- Create: `polybot/tests/test_ta_evolver.py`, `polybot/tests/test_weight_optimizer.py`

- [ ] **Step 1: Write tests**

```python
# polybot/tests/test_ta_evolver.py
import pytest
from polybot.agents.ta_evolver import TAEvolver

@pytest.fixture
def evolver(tmp_path):
    return TAEvolver(strategy_log_path=str(tmp_path / "strategy_log.md"))

def test_analyze_outcomes_computes_stats(evolver):
    outcomes = [
        {"correct": True, "log_return": 0.05, "indicator_snapshot": {"rsi": {"score": 0.8}, "macd": {"score": 0.7},
         "stochastic": {"score": 0.5}, "obv": {"score": 0.3}, "vwap": {"score": 0.2}}},
        {"correct": False, "log_return": -0.1, "indicator_snapshot": {"rsi": {"score": 0.3}, "macd": {"score": 0.2},
         "stochastic": {"score": 0.1}, "obv": {"score": -0.1}, "vwap": {"score": 0.0}}},
    ]
    analysis = evolver.analyze(outcomes)
    assert "win_rate" in analysis
    assert analysis["total_trades"] == 2

def test_recommend_weight_adjustments(evolver):
    outcomes = [{"correct": True, "log_return": 0.05,
                 "indicator_snapshot": {"rsi": {"score": 0.8}, "macd": {"score": 0.9},
                  "stochastic": {"score": 0.5}, "obv": {"score": 0.1}, "vwap": {"score": 0.3}}} for _ in range(10)]
    outcomes += [{"correct": False, "log_return": -0.08,
                  "indicator_snapshot": {"rsi": {"score": 0.2}, "macd": {"score": 0.1},
                   "stochastic": {"score": 0.5}, "obv": {"score": 0.6}, "vwap": {"score": 0.4}}} for _ in range(5)]
    current_weights = {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}
    recs = evolver.recommend_weight_adjustments(outcomes, current_weights)
    assert isinstance(recs, dict)
    assert "rsi" in recs

def test_save_log(evolver, tmp_path):
    evolver.save_log({"win_rate": 0.65, "total_trades": 15}, {"rsi": 0.22, "macd": 0.28})
    assert (tmp_path / "strategy_log.md").exists()
```

```python
# polybot/tests/test_weight_optimizer.py
import json
import pytest
from pathlib import Path
from polybot.agents.weight_optimizer import WeightOptimizer

@pytest.fixture
def weights_dir(tmp_path):
    d = tmp_path / "weights"
    d.mkdir()
    (d / "weights_v001.json").write_text(json.dumps({
        "rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20,
        "entry_threshold": 0.60, "version": "weights_v001"}))
    return d

@pytest.fixture
def scores_path(tmp_path):
    path = tmp_path / "weight_scores.json"
    path.write_text(json.dumps({"weights_v001": {"sharpe": 1.2, "total_trades": 50, "win_rate": 0.65}}))
    return path

@pytest.fixture
def optimizer(weights_dir, scores_path):
    return WeightOptimizer(weights_dir=str(weights_dir), scores_path=str(scores_path), min_improvement=0.03)

def test_get_scores(optimizer):
    scores = optimizer.get_scores()
    assert "weights_v001" in scores

def test_get_best_version(optimizer):
    assert optimizer.get_best_version() == "weights_v001"

def test_save_weights(optimizer, weights_dir):
    new_weights = {"rsi": 0.22, "macd": 0.28, "stochastic": 0.18, "obv": 0.12, "vwap": 0.20,
                   "entry_threshold": 0.55, "version": "weights_v002"}
    optimizer.save_weights("weights_v002", new_weights)
    assert (weights_dir / "weights_v002.json").exists()

def test_record_score(optimizer, scores_path):
    optimizer.record_score("weights_v002", sharpe=1.5, total_trades=30, win_rate=0.70)
    scores = json.loads(scores_path.read_text())
    assert "weights_v002" in scores

def test_should_adopt(optimizer):
    assert optimizer.should_adopt(current_sharpe=1.2, candidate_sharpe=1.3) is True
    assert optimizer.should_adopt(current_sharpe=1.2, candidate_sharpe=1.21) is False

def test_get_next_version(optimizer):
    assert optimizer.get_next_version() == "weights_v002"
```

- [ ] **Step 2: Implement**

```python
# polybot/agents/ta_evolver.py
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

class TAEvolver:
    def __init__(self, strategy_log_path: str):
        self.strategy_log_path = Path(strategy_log_path)

    def analyze(self, outcomes: list[dict]) -> dict:
        if not outcomes:
            return {"win_rate": 0, "avg_log_return": 0, "total_trades": 0}
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = [o.get("log_return", 0) for o in outcomes]
        return {
            "win_rate": wins / len(outcomes),
            "avg_log_return": sum(returns) / len(returns),
            "total_trades": len(outcomes),
        }

    def recommend_weight_adjustments(self, outcomes: list[dict],
                                     current_weights: dict) -> dict:
        if len(outcomes) < 5:
            return current_weights.copy()
        indicator_names = ["rsi", "macd", "stochastic", "obv", "vwap"]
        win_scores = {name: [] for name in indicator_names}
        lose_scores = {name: [] for name in indicator_names}
        for o in outcomes:
            snap = o.get("indicator_snapshot", {})
            for name in indicator_names:
                score = snap.get(name, {}).get("score", 0)
                if o.get("correct"):
                    win_scores[name].append(abs(score))
                else:
                    lose_scores[name].append(abs(score))
        new_weights = {}
        for name in indicator_names:
            avg_win = sum(win_scores[name]) / len(win_scores[name]) if win_scores[name] else 0
            avg_lose = sum(lose_scores[name]) / len(lose_scores[name]) if lose_scores[name] else 0
            effectiveness = avg_win - avg_lose
            new_weights[name] = max(0.05, current_weights.get(name, 0.20) + effectiveness * 0.05)
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}
        return new_weights

    def save_log(self, analysis: dict, recommended_weights: dict):
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        entry = f"\n## {now}\n\n**Analysis:** {analysis}\n\n**Recommended Weights:** {recommended_weights}\n"
        existing = self.strategy_log_path.read_text() if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry)
```

```python
# polybot/agents/weight_optimizer.py
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

class WeightOptimizer:
    def __init__(self, weights_dir: str, scores_path: str, min_improvement: float = 0.03):
        self.weights_dir = Path(weights_dir)
        self.scores_path = Path(scores_path)
        self.min_improvement = min_improvement

    def get_scores(self) -> dict:
        if not self.scores_path.exists():
            return {}
        return json.loads(self.scores_path.read_text())

    def get_best_version(self) -> str:
        scores = self.get_scores()
        if not scores:
            return "weights_v001"
        return max(scores, key=lambda v: scores[v].get("sharpe", 0))

    def record_score(self, version: str, sharpe: float, total_trades: int, win_rate: float):
        scores = self.get_scores()
        scores[version] = {"sharpe": round(sharpe, 4), "total_trades": total_trades,
                           "win_rate": round(win_rate, 4)}
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(json.dumps(scores, indent=2))

    def save_weights(self, version: str, weights: dict):
        self.weights_dir.mkdir(parents=True, exist_ok=True)
        path = self.weights_dir / f"{version}.json"
        path.write_text(json.dumps(weights, indent=2))

    def should_adopt(self, current_sharpe: float, candidate_sharpe: float) -> bool:
        return (candidate_sharpe - current_sharpe) >= self.min_improvement

    def get_next_version(self) -> str:
        existing = list(self.weights_dir.glob("weights_v*.json"))
        if not existing:
            return "weights_v001"
        numbers = []
        for f in existing:
            match = re.search(r"v(\d+)", f.stem)
            if match:
                numbers.append(int(match.group(1)))
        return f"weights_v{max(numbers) + 1:03d}" if numbers else "weights_v001"
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/test_ta_evolver.py polybot/tests/test_weight_optimizer.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/agents/ta_evolver.py polybot/agents/weight_optimizer.py polybot/tests/test_ta_evolver.py polybot/tests/test_weight_optimizer.py
git commit -m "feat: TA evolver and weight optimizer agents"
```

---

## Task 9: Update Scheduler, Outcome Reviewer, DB, and Config

**Files:**
- Modify: `polybot/agents/scheduler.py` — wire ta_evolver and weight_optimizer
- Modify: `polybot/agents/outcome_reviewer.py` — add indicator_snapshot to records
- Modify: `polybot/db/models.py` — add indicator_snapshot column
- Modify: `polybot/tests/conftest.py` — add TA config to sample config

- [ ] **Step 1: Update DB schema — add indicator_snapshot to positions**

In `polybot/db/models.py`, add `indicator_snapshot TEXT` column to positions table (after `prompt_version`), and update `open_position` to accept and store it. Update `close_position` to include it in trade_history.

- [ ] **Step 2: Update outcome_reviewer.py — add indicator_snapshot param**

In `record_outcome`, add `indicator_snapshot: dict | None = None` parameter. Include it in the record JSON.

- [ ] **Step 3: Update scheduler.py — wire new agents**

Replace `strategy_evolver` with `ta_evolver` and `prompt_optimizer` with `weight_optimizer`. Update `_run_strategy_evolver` → `_run_ta_evolver` and `_run_prompt_optimizer` → `_run_weight_optimizer`. Same pipeline chain pattern.

- [ ] **Step 4: Update conftest.py — add TA sections to sample config**

Add `binance`, `indicators`, `signal`, and `market` sections matching the new settings.yaml structure.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest polybot/tests/ -v`
Expected: All existing + new tests PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/db/models.py polybot/agents/ polybot/tests/conftest.py
git commit -m "feat: wire TA agents into scheduler, add indicator snapshots to DB and outcomes"
```

---

## Task 10: Update main.py — New Trading Loop

**Files:**
- Modify: `polybot/main.py`

- [ ] **Step 1: Rewrite main.py with 1-second TA decision loop**

Replace the Claude-based trading loop with:
1. BinanceFeed starts (backfill + WebSocket)
2. BTCMarketScanner finds active contracts
3. IndicatorEngine computes all 7 indicators
4. SignalEngine evaluates gates + scoring
5. PaperTrader places trade if signal is strong
6. 1-second asyncio.sleep between cycles

Keep: Discord bot, agent scheduler, DB initialization, error recovery, logging.

Remove: Claude calls in trading loop, general market scanner, prompt builder in hot path.

- [ ] **Step 2: Run integration test**

Create `polybot/tests/test_ta_integration.py`:

```python
# polybot/tests/test_ta_integration.py
import pytest
import pytest_asyncio
import json
from pathlib import Path
from polybot.core.binance_feed import Candle, CandleBuffer
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine
from polybot.db.models import Database
from polybot.execution.paper_trader import PaperTrader

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()

@pytest.fixture
def weights_dir(tmp_path):
    d = tmp_path / "weights"
    d.mkdir()
    (d / "weights_v001.json").write_text(json.dumps({
        "rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20,
        "entry_threshold": 0.60, "version": "weights_v001"}))
    return str(d)

@pytest.mark.asyncio
async def test_full_ta_flow(db, weights_dir):
    """Trending BTC data → indicators → signal → paper trade."""
    buf = CandleBuffer(max_size=200)
    for i in range(60):
        price = 50000 + i * 50
        buf.add(Candle(timestamp=i * 60000, open=price - 10, high=price + 30,
                       low=price - 30, close=price, volume=100.0 + i))

    engine = IndicatorEngine(weights_dir=weights_dir, active_version="weights_v001")
    indicators = engine.compute_all(buf)

    signal_eng = SignalEngine(entry_threshold=0.60,
                              weights={"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                       "obv": 0.15, "vwap": 0.20})
    signal = signal_eng.evaluate(indicators, has_position=False, in_entry_window=True)

    if signal.action in ("BUY_YES", "BUY_NO"):
        trader = PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80,
                             max_concurrent_positions=5)
        side = "YES" if signal.action == "BUY_YES" else "NO"
        result = await trader.open_trade(
            market_id="0xbtc5min", question="BTC 5min Up?", side=side,
            price=0.55, size=5.0, claude_probability=abs(signal.score),
            claude_confidence="high", ev_at_entry=0.10,
            exit_target=0.90, stop_loss=0.40, prompt_version="ta_v001")
        assert result.success is True

    assert signal.score != 0  # Should have a non-zero signal from strong trend
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest polybot/tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/main.py polybot/tests/test_ta_integration.py
git commit -m "feat: 1-second TA decision loop with full indicator pipeline"
```

---

## Task 11: Final Verification & Cleanup

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest polybot/tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Verify file structure**

Run: `find polybot -type f -name "*.py" | grep -v __pycache__ | sort`
Expected: All planned files exist

- [ ] **Step 3: Remove unused old files (optional cleanup)**

Delete: `polybot/core/filters.py`, `polybot/core/scanner.py`, `polybot/core/websocket_monitor.py`
Delete: `polybot/agents/strategy_evolver.py`, `polybot/agents/prompt_optimizer.py`
Delete corresponding test files.

Only delete if all new tests pass. Keep old tests that still work (e.g. filters might still be imported somewhere).

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: final verification and cleanup — TA crypto strategy complete"
```
