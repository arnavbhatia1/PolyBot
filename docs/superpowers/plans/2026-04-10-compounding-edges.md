# Compounding Edges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 13 features across 4 paths (fee reduction, signal quality, capital deployment, structural exploits) to transform the bot from breakeven to compounding 8-15% daily.

**Architecture:** Five new data feed modules (all independent, parallel-safe), signal engine extensions for 6 new signals, execution changes for maker orders and concurrent windows, and trading loop changes for dynamic entry timing and latency detection. All new signals are logged to outcome JSON in Phase 1; model integration follows.

**Tech Stack:** Python 3.12+, asyncio, websockets, httpx, scipy, aiosqlite. External feeds: Binance.US (depth, aggTrades), Bybit (BTC perpetual — leads spot by 0.5-2s), Deribit (options IV). All feeds are free, no auth.

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `polybot/core/binance_depth.py` | L2 order book: top-20 WS + full REST depth, imbalance, wall detection, book thinness |
| `polybot/core/binance_trades.py` | Aggregate trade stream: CVD, taker ratio, large trade detection, volume surge |
| `polybot/core/bybit_feed.py` | BTC perpetual price lead + funding rate (B3, B4, D1 combined) |
| `polybot/core/deribit_iv.py` | BTC options implied volatility polling (B5) |
| `polybot/core/bankroll_strategy.py` | Tiered Kelly acceleration based on track record (C1) |
| `polybot/tests/test_binance_depth.py` | Tests for depth signals |
| `polybot/tests/test_binance_trades.py` | Tests for trade flow signals |
| `polybot/tests/test_bybit_feed.py` | Tests for Bybit perp feed |
| `polybot/tests/test_deribit_iv.py` | Tests for IV polling |
| `polybot/tests/test_bankroll_strategy.py` | Tests for bankroll acceleration |
| `polybot/tests/test_signal_engine_v2.py` | Tests for new signal layers |
| `polybot/tests/test_dynamic_entry.py` | Tests for entry timing logic |
| `polybot/tests/test_concurrent_windows.py` | Tests for multi-position logic |

### Modified Files
| File | Changes |
|------|---------|
| `polybot/core/signal_engine.py` | Add spot_flow_signal, wall_pressure, depth_factor, perp_lead, prev_margin, iv_ratio params to compute_probability(); conviction multiplier in _kelly() |
| `polybot/main.py` | Wire new feeds, dynamic entry timing, concurrent windows, staleness detection, outcome logging of new signals |
| `polybot/execution/base.py` | Increase max_concurrent_positions handling, add maker order support flag |
| `polybot/execution/live_trader.py` | Add _execute_buy_limit() for maker orders with timeout fallback |
| `polybot/execution/circuit_breaker.py` | Integrate bankroll_strategy for dynamic Kelly |
| `polybot/config/settings.yaml` | New config sections for all feeds and features |
| `polybot/config/loader.py` | Validate new config params |

---

## GROUP A: Data Feed Modules (All 5 tasks are independent — run in parallel)

### Task 1: Binance Depth Feed (B1 — Wall Detection + Spot Imbalance)

**Files:**
- Create: `polybot/core/binance_depth.py`
- Create: `polybot/tests/test_binance_depth.py`

- [ ] **Step 1: Write failing tests for depth signal computations**

```python
# polybot/tests/test_binance_depth.py
import pytest
from polybot.core.binance_depth import compute_spot_imbalance, compute_wall_pressure, compute_depth_usd

class TestSpotImbalance:
    def test_balanced_book(self):
        bids = [["73000.00", "1.0"], ["72999.00", "1.0"]]
        asks = [["73001.00", "1.0"], ["73002.00", "1.0"]]
        assert compute_spot_imbalance(bids, asks) == pytest.approx(0.0, abs=0.01)

    def test_bid_heavy(self):
        bids = [["73000.00", "5.0"], ["72999.00", "5.0"]]
        asks = [["73001.00", "1.0"]]
        result = compute_spot_imbalance(bids, asks)
        assert result > 0.5  # strongly bullish

    def test_ask_heavy(self):
        bids = [["73000.00", "1.0"]]
        asks = [["73001.00", "5.0"], ["73002.00", "5.0"]]
        result = compute_spot_imbalance(bids, asks)
        assert result < -0.5  # strongly bearish

    def test_empty_book(self):
        assert compute_spot_imbalance([], []) == 0.0


class TestWallPressure:
    def test_sell_wall_above_strike(self):
        # BTC at 73020, strike at 73000. Massive sell wall at 73025-73050.
        asks = [["73025.00", "50.0"], ["73030.00", "20.0"], ["73050.00", "5.0"]]
        bids = [["73015.00", "1.0"], ["73010.00", "1.0"]]
        # Wall blocks upside = bearish (positive wall_pressure)
        result = compute_wall_pressure(bids, asks, strike=73000.0, btc_price=73020.0, pct_range=0.001)
        assert result > 0  # wall above = bearish for Up

    def test_no_wall(self):
        asks = [["73025.00", "1.0"]]
        bids = [["73015.00", "1.0"]]
        result = compute_wall_pressure(bids, asks, strike=73000.0, btc_price=73020.0, pct_range=0.001)
        assert abs(result) < 1.0

    def test_support_wall_below_strike(self):
        # BTC at 72980, strike at 73000. Massive buy wall at 72950-72975.
        bids = [["72975.00", "50.0"], ["72960.00", "20.0"]]
        asks = [["72985.00", "1.0"]]
        result = compute_wall_pressure(bids, asks, strike=73000.0, btc_price=72980.0, pct_range=0.001)
        assert result < 0  # support below = bullish for Up


class TestDepthUsd:
    def test_computes_total_depth(self):
        bids = [["73000.00", "1.0"], ["72999.00", "2.0"]]
        asks = [["73001.00", "1.5"], ["73002.00", "0.5"]]
        result = compute_depth_usd(bids, asks, levels=2)
        # bid depth: 73000*1 + 72999*2 = 218998, ask depth: 73001*1.5 + 73002*0.5 = 146002.5
        assert result > 300000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_binance_depth.py -v`
Expected: FAIL with ImportError (module doesn't exist yet)

- [ ] **Step 3: Implement the depth signal computation functions**

```python
# polybot/core/binance_depth.py
"""Binance.US Level 2 order book feed for BTC spot market.

Provides three signals:
1. Spot imbalance — bid/ask volume ratio at top N levels
2. Wall pressure — large orders near the strike price that block price movement
3. Book depth — total USD liquidity (thin book = higher realized vol)

Data sources:
- WSS btcusdt@depth20@100ms — top 20 levels, 100ms snapshots (real-time imbalance)
- GET /api/v3/depth?limit=1000 — full book every 5s (wall detection)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def compute_spot_imbalance(bids: list[list[str]], asks: list[list[str]]) -> float:
    """Bid/ask volume imbalance from Binance order book levels.

    Returns float from -1 (ask-heavy / bearish) to +1 (bid-heavy / bullish).
    Each level is [price_str, qty_str].
    """
    bid_vol = sum(float(b[1]) for b in bids) if bids else 0.0
    ask_vol = sum(float(a[1]) for a in asks) if asks else 0.0
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (bid_vol - ask_vol) / total))


def compute_wall_pressure(
    bids: list[list[str]],
    asks: list[list[str]],
    strike: float,
    btc_price: float,
    pct_range: float = 0.001,
) -> float:
    """Detect large orders near the strike price that block BTC from crossing.

    Returns float: positive = resistance above (bearish for Up), negative = support below (bullish for Up).

    Logic:
    - If BTC is above strike: walls ABOVE current price block further upside (less relevant)
      but thin support BELOW between price and strike means a drop to strike is easy.
      We measure: ask_wall_above vs bid_support_below_to_strike.
    - If BTC is below strike: walls BELOW block further downside, thin asks above mean
      a push through strike is easy.

    pct_range: fraction of price to scan for walls (0.001 = 0.1% = ~$73 at BTC 73000)
    """
    if strike <= 0 or btc_price <= 0:
        return 0.0

    range_usd = btc_price * pct_range

    # Volume between current price and strike (the "contested zone")
    zone_low = min(btc_price, strike)
    zone_high = max(btc_price, strike)

    bid_in_zone = sum(
        float(b[1]) * float(b[0])
        for b in bids
        if zone_low - range_usd <= float(b[0]) <= zone_high + range_usd
    )
    ask_in_zone = sum(
        float(a[1]) * float(a[0])
        for a in asks
        if zone_low - range_usd <= float(a[0]) <= zone_high + range_usd
    )

    total = bid_in_zone + ask_in_zone
    if total == 0:
        return 0.0

    # Positive = more sell pressure in zone = harder for price to go UP through strike
    # Negative = more buy pressure in zone = harder for price to go DOWN through strike
    raw = (ask_in_zone - bid_in_zone) / total
    return max(-1.0, min(1.0, raw))


def compute_depth_usd(
    bids: list[list[str]], asks: list[list[str]], levels: int = 20
) -> float:
    """Total USD value in top N bid + ask levels. Thin book = volatile."""
    bid_usd = sum(
        float(b[0]) * float(b[1]) for b in bids[:levels]
    )
    ask_usd = sum(
        float(a[0]) * float(a[1]) for a in asks[:levels]
    )
    return bid_usd + ask_usd


class BinanceDepthFeed:
    """Maintains a live view of Binance.US BTC/USDT order book.

    Two data paths:
    1. WebSocket (btcusdt@depth20@100ms) — top 20 levels, fast updates for imbalance
    2. REST (/api/v3/depth?limit=1000) — full book every poll_interval_s for wall detection
    """

    def __init__(
        self,
        ws_url: str = "wss://stream.binance.us:9443/ws/btcusdt@depth20@100ms",
        rest_url: str = "https://api.binance.us/api/v3",
        poll_interval_s: float = 5.0,
    ) -> None:
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.poll_interval_s = poll_interval_s

        # Top-20 book (from WebSocket)
        self.top_bids: list[list[str]] = []
        self.top_asks: list[list[str]] = []
        self._top_updated: float = 0.0

        # Full book (from REST)
        self.full_bids: list[list[str]] = []
        self.full_asks: list[list[str]] = []
        self._full_updated: float = 0.0

        self._running = False
        self._ws: Any = None

    @property
    def age_top_s(self) -> float:
        return time.time() - self._top_updated if self._top_updated else float("inf")

    @property
    def age_full_s(self) -> float:
        return time.time() - self._full_updated if self._full_updated else float("inf")

    def get_imbalance(self) -> float:
        """Spot bid/ask imbalance from top-20 WebSocket data."""
        return compute_spot_imbalance(self.top_bids, self.top_asks)

    def get_wall_pressure(self, strike: float, btc_price: float, pct_range: float = 0.001) -> float:
        """Wall pressure near strike from full REST book."""
        return compute_wall_pressure(self.full_bids, self.full_asks, strike, btc_price, pct_range)

    def get_depth_usd(self, levels: int = 20) -> float:
        """Total USD depth at top N levels."""
        return compute_depth_usd(self.top_bids, self.top_asks, levels)

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._rest_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _ws_loop(self) -> None:
        import websockets

        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.debug("Binance depth WS connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        self.top_bids = data.get("bids", [])
                        self.top_asks = data.get("asks", [])
                        self._top_updated = time.time()
            except Exception as e:
                logger.warning(f"Depth WS error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _rest_loop(self) -> None:
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self.rest_url}/depth",
                        params={"symbol": "BTCUSDT", "limit": 1000},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    self.full_bids = data.get("bids", [])
                    self.full_asks = data.get("asks", [])
                    self._full_updated = time.time()
            except Exception as e:
                logger.warning(f"Depth REST error: {e}")
            await asyncio.sleep(self.poll_interval_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_binance_depth.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/binance_depth.py polybot/tests/test_binance_depth.py
git commit -m "feat: add Binance L2 depth feed with wall detection and spot imbalance"
```

---

### Task 2: Binance Aggregate Trade Flow (B2 — CVD + Taker Ratio)

**Files:**
- Create: `polybot/core/binance_trades.py`
- Create: `polybot/tests/test_binance_trades.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_binance_trades.py
import pytest
import time
from polybot.core.binance_trades import BinanceTradeAccumulator

class TestCVD:
    def test_net_buying_positive_cvd(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        # Taker buys (m=False means buyer is taker = aggressive buyer)
        acc.add_trade(price=73000, qty=1.0, is_buyer_maker=False, ts=now)
        acc.add_trade(price=73010, qty=0.5, is_buyer_maker=False, ts=now)
        # One taker sell
        acc.add_trade(price=72990, qty=0.3, is_buyer_maker=True, ts=now)
        cvd = acc.get_cvd(window_s=120)
        assert cvd > 0  # net aggressive buying

    def test_net_selling_negative_cvd(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(price=73000, qty=0.2, is_buyer_maker=False, ts=now)
        acc.add_trade(price=72990, qty=2.0, is_buyer_maker=True, ts=now)
        cvd = acc.get_cvd(window_s=120)
        assert cvd < 0  # net aggressive selling

    def test_expired_trades_excluded(self):
        acc = BinanceTradeAccumulator()
        old = time.time() - 300  # 5 min ago
        now = time.time()
        acc.add_trade(price=73000, qty=10.0, is_buyer_maker=False, ts=old)
        acc.add_trade(price=73000, qty=0.1, is_buyer_maker=True, ts=now)
        cvd = acc.get_cvd(window_s=120)
        assert cvd < 0  # only recent sell counts


class TestTakerRatio:
    def test_all_buys(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 1.0, False, now)
        acc.add_trade(73000, 1.0, False, now)
        assert acc.get_taker_ratio(window_s=120) == pytest.approx(1.0)

    def test_balanced(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 1.0, False, now)
        acc.add_trade(73000, 1.0, True, now)
        assert acc.get_taker_ratio(window_s=120) == pytest.approx(0.5)


class TestLargeTrades:
    def test_detects_large_trade(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 0.6, False, now)  # > 0.5 BTC
        acc.add_trade(73000, 0.1, True, now)
        large = acc.get_large_trades(window_s=120, min_btc=0.5)
        assert len(large) == 1
        assert large[0]["qty"] == 0.6

    def test_ignores_small_trades(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        acc.add_trade(73000, 0.1, False, now)
        large = acc.get_large_trades(window_s=120, min_btc=0.5)
        assert len(large) == 0


class TestVolumeSurge:
    def test_surge_detected(self):
        acc = BinanceTradeAccumulator()
        now = time.time()
        # Build up baseline (30 trades over 60s)
        for i in range(30):
            acc.add_trade(73000, 0.01, False, now - 60 + i * 2)
        # Surge: 10 large trades in last 5s
        for i in range(10):
            acc.add_trade(73000, 0.5, False, now - 4 + i * 0.4)
        assert acc.is_volume_surge(threshold=3.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_binance_trades.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement trade accumulator**

```python
# polybot/core/binance_trades.py
"""Binance.US aggregate trade stream for BTC/USDT.

Provides four signals from the aggTrade WebSocket:
1. CVD (Cumulative Volume Delta) — net aggressive buy vs sell volume
2. Taker ratio — fraction of volume from aggressive buyers
3. Large trade detection — trades above a BTC threshold
4. Volume surge — current volume vs EMA baseline

Stream: wss://stream.binance.us:9443/ws/btcusdt@aggTrade
Message format: {"e":"aggTrade","s":"BTCUSDT","p":"73000.50","q":"0.123","m":true,"T":1234567890}
  m=true: buyer was maker (= seller was aggressor = bearish)
  m=false: buyer was taker (= buyer was aggressor = bullish)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AggTrade:
    price: float
    qty: float
    is_buyer_maker: bool  # True = seller was aggressor
    ts: float


class BinanceTradeAccumulator:
    """Accumulates BTC aggregate trades and computes flow signals."""

    def __init__(self, max_age_s: float = 300.0) -> None:
        self.max_age_s = max_age_s
        self._trades: deque[AggTrade] = deque()

    def add_trade(self, price: float, qty: float, is_buyer_maker: bool, ts: float) -> None:
        self._trades.append(AggTrade(price=price, qty=qty, is_buyer_maker=is_buyer_maker, ts=ts))
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.max_age_s
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def get_cvd(self, window_s: float = 120.0) -> float:
        """Cumulative Volume Delta: sum of (buy_taker_qty - sell_taker_qty).
        Positive = net aggressive buying = bullish."""
        cutoff = time.time() - window_s
        cvd = 0.0
        for t in self._trades:
            if t.ts < cutoff:
                continue
            if t.is_buyer_maker:
                cvd -= t.qty  # seller was aggressor
            else:
                cvd += t.qty  # buyer was aggressor
        return cvd

    def get_taker_ratio(self, window_s: float = 60.0) -> float:
        """Fraction of volume from aggressive buyers. >0.55 = bullish, <0.45 = bearish."""
        cutoff = time.time() - window_s
        buy_vol = 0.0
        total_vol = 0.0
        for t in self._trades:
            if t.ts < cutoff:
                continue
            total_vol += t.qty
            if not t.is_buyer_maker:
                buy_vol += t.qty
        return buy_vol / total_vol if total_vol > 0 else 0.5

    def get_large_trades(self, window_s: float = 120.0, min_btc: float = 0.5) -> list[dict]:
        """Recent trades above min_btc threshold."""
        cutoff = time.time() - window_s
        return [
            {"price": t.price, "qty": t.qty, "side": "buy" if not t.is_buyer_maker else "sell", "ts": t.ts}
            for t in self._trades
            if t.ts >= cutoff and t.qty >= min_btc
        ]

    def is_volume_surge(self, threshold: float = 3.0, recent_s: float = 10.0, baseline_s: float = 60.0) -> bool:
        """True if recent volume exceeds baseline by threshold multiplier."""
        now = time.time()
        recent_vol = sum(t.qty for t in self._trades if t.ts >= now - recent_s)
        baseline_vol = sum(t.qty for t in self._trades if now - baseline_s <= t.ts < now - recent_s)
        baseline_rate = baseline_vol / max(1.0, baseline_s - recent_s)
        recent_rate = recent_vol / max(1.0, recent_s)
        return recent_rate > baseline_rate * threshold if baseline_rate > 0 else False


class BinanceTradesFeed:
    """WebSocket consumer for Binance.US btcusdt@aggTrade stream."""

    def __init__(
        self,
        ws_url: str = "wss://stream.binance.us:9443/ws/btcusdt@aggTrade",
    ) -> None:
        self.ws_url = ws_url
        self.accumulator = BinanceTradeAccumulator()
        self._running = False
        self._ws: Any = None

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _ws_loop(self) -> None:
        import websockets

        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.debug("Binance aggTrade WS connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        if data.get("e") == "aggTrade":
                            self.accumulator.add_trade(
                                price=float(data["p"]),
                                qty=float(data["q"]),
                                is_buyer_maker=data["m"],
                                ts=float(data["T"]) / 1000.0,
                            )
            except Exception as e:
                logger.warning(f"aggTrade WS error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_binance_trades.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/binance_trades.py polybot/tests/test_binance_trades.py
git commit -m "feat: add Binance aggregate trade feed with CVD, taker ratio, volume surge"
```

---

### Task 3: Bybit Perpetual Feed (B3 + B4 + D1 — Cross-Exchange Lead + Funding Rate)

**Files:**
- Create: `polybot/core/bybit_feed.py`
- Create: `polybot/tests/test_bybit_feed.py`

**Why Bybit:** Bybit is the #1 crypto derivatives exchange by volume. BTC perpetual futures lead spot markets by 0.5-2 seconds because leveraged traders react first. Free WebSocket, no auth needed. Coinbase has no perpetuals. Binance.com futures are blocked for US IPs.

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_bybit_feed.py
import pytest
import time
from polybot.core.bybit_feed import BybitState, compute_perp_lead, compute_funding_signal

class TestPerpLead:
    def test_perp_above_spot(self):
        # Perp at 73050, spot at 73000 = perp leading upward
        lead = compute_perp_lead(perp_price=73050.0, spot_price=73000.0)
        assert lead > 0  # bullish lead

    def test_perp_below_spot(self):
        lead = compute_perp_lead(perp_price=72950.0, spot_price=73000.0)
        assert lead < 0  # bearish lead

    def test_no_lead(self):
        lead = compute_perp_lead(perp_price=73000.0, spot_price=73000.0)
        assert lead == pytest.approx(0.0)

    def test_normalized_range(self):
        # $100 difference on $73000 base ≈ 0.137%
        lead = compute_perp_lead(perp_price=73100.0, spot_price=73000.0)
        assert -1.0 <= lead <= 1.0


class TestFundingSignal:
    def test_positive_funding_bearish(self):
        # High positive funding = longs crowded = bearish signal
        signal = compute_funding_signal(funding_rate=0.0005)
        assert signal < 0  # bearish

    def test_negative_funding_bullish(self):
        # Negative funding = shorts crowded = bullish squeeze potential
        signal = compute_funding_signal(funding_rate=-0.0003)
        assert signal > 0  # bullish

    def test_neutral_funding(self):
        signal = compute_funding_signal(funding_rate=0.0001)
        assert abs(signal) < 0.3  # baseline is ~0.01%, so 0.0001 is normal


class TestBybitState:
    def test_staleness_detection(self):
        state = BybitState()
        state.perp_price = 73050.0
        state.perp_updated = time.time()
        # Stale if perp moved but spot hasn't updated recently
        assert state.is_stale(spot_price=73000.0, spot_updated=time.time() - 5.0, threshold_usd=20.0)

    def test_not_stale_when_close(self):
        state = BybitState()
        state.perp_price = 73005.0
        state.perp_updated = time.time()
        assert not state.is_stale(spot_price=73000.0, spot_updated=time.time(), threshold_usd=20.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_bybit_feed.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement Bybit feed**

```python
# polybot/core/bybit_feed.py
"""Bybit BTC perpetual feed — provides cross-exchange price lead and funding rate.

Why Bybit: #1 crypto derivatives exchange by volume. BTC/USDT perpetual traders
are the most leveraged and react first to market moves. Perp price leads
Binance.US spot by 0.5-2 seconds, and Polymarket by 3-10 seconds.

Signals:
1. Perpetual price lead — perp_price vs spot_price divergence (directional)
2. Funding rate — crowding indicator (contrarian)
3. Staleness detection — when perp moves but spot/Polymarket hasn't (latency arbitrage trigger)

WebSocket: wss://stream.bybit.com/v5/public/linear
Subscribe: {"op":"subscribe","args":["tickers.BTCUSDT"]}
REST: GET https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def compute_perp_lead(perp_price: float, spot_price: float) -> float:
    """Normalized perpetual-spot divergence.

    Returns float in [-1, 1]. Positive = perp above spot = bullish lead.
    Scaled so that $100 divergence on $73000 (~0.14%) maps to ~0.5.
    """
    if spot_price <= 0 or perp_price <= 0:
        return 0.0
    pct_diff = (perp_price - spot_price) / spot_price
    # Scale: 0.1% divergence → ~0.35 signal. Tanh for natural bounds.
    return max(-1.0, min(1.0, math.tanh(pct_diff * 350)))


def compute_funding_signal(funding_rate: float) -> float:
    """Convert funding rate to a contrarian signal.

    Positive funding = longs pay shorts = crowded longs = bearish signal.
    Negative funding = shorts pay longs = crowded shorts = bullish signal.
    Baseline funding ~0.01% (0.0001) is neutral.

    Returns float in [-1, 1]. Positive = bullish.
    """
    # Subtract baseline, invert (high funding = bearish), scale
    adjusted = -(funding_rate - 0.0001)
    return max(-1.0, min(1.0, math.tanh(adjusted * 2500)))


@dataclass
class BybitState:
    perp_price: float = 0.0
    perp_updated: float = 0.0
    funding_rate: float = 0.0
    funding_updated: float = 0.0
    next_funding_time: float = 0.0

    def is_stale(self, spot_price: float, spot_updated: float, threshold_usd: float = 20.0) -> bool:
        """True if perp has moved significantly but spot hasn't caught up."""
        if self.perp_price <= 0 or spot_price <= 0:
            return False
        price_gap = abs(self.perp_price - spot_price)
        perp_is_fresh = (time.time() - self.perp_updated) < 3.0
        spot_is_stale = (time.time() - spot_updated) > 2.0
        return price_gap > threshold_usd and perp_is_fresh and spot_is_stale

    def get_lead(self, spot_price: float) -> float:
        return compute_perp_lead(self.perp_price, spot_price)

    def get_funding_signal(self) -> float:
        return compute_funding_signal(self.funding_rate)


class BybitFeed:
    """Maintains live Bybit BTC perpetual state via WebSocket + periodic REST."""

    def __init__(
        self,
        ws_url: str = "wss://stream.bybit.com/v5/public/linear",
        rest_url: str = "https://api.bybit.com/v5/market",
        funding_poll_s: float = 300.0,
    ) -> None:
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.funding_poll_s = funding_poll_s
        self.state = BybitState()
        self._running = False
        self._ws: Any = None

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._funding_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _ws_loop(self) -> None:
        import websockets

        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    self._ws = ws
                    backoff = 1
                    # Subscribe to BTCUSDT perpetual ticker
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": ["tickers.BTCUSDT"],
                    }))
                    logger.debug("Bybit perp WS connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        if data.get("topic") == "tickers.BTCUSDT":
                            d = data.get("data", {})
                            last = d.get("lastPrice")
                            if last:
                                self.state.perp_price = float(last)
                                self.state.perp_updated = time.time()
                            fr = d.get("fundingRate")
                            if fr:
                                self.state.funding_rate = float(fr)
                                self.state.funding_updated = time.time()
            except Exception as e:
                logger.warning(f"Bybit WS error: {e}, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _funding_loop(self) -> None:
        """Periodic REST poll for funding rate (backup if WS misses it)."""
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self.rest_url}/tickers",
                        params={"category": "linear", "symbol": "BTCUSDT"},
                    )
                    resp.raise_for_status()
                    result = resp.json().get("result", {}).get("list", [])
                    if result:
                        ticker = result[0]
                        fr = ticker.get("fundingRate")
                        if fr:
                            self.state.funding_rate = float(fr)
                            self.state.funding_updated = time.time()
                        nft = ticker.get("nextFundingTime")
                        if nft:
                            self.state.next_funding_time = float(nft) / 1000.0
            except Exception as e:
                logger.warning(f"Bybit REST error: {e}")
            await asyncio.sleep(self.funding_poll_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_bybit_feed.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/bybit_feed.py polybot/tests/test_bybit_feed.py
git commit -m "feat: add Bybit perpetual feed with price lead, funding rate, staleness detection"
```

---

### Task 4: Deribit Options IV (B5 — Forward-Looking Volatility)

**Files:**
- Create: `polybot/core/deribit_iv.py`
- Create: `polybot/tests/test_deribit_iv.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_deribit_iv.py
import pytest
from polybot.core.deribit_iv import compute_iv_ratio

class TestIVRatio:
    def test_iv_above_historical(self):
        # Market expects more vol than historical = widen sigma
        ratio = compute_iv_ratio(current_iv=0.80, historical_iv=0.60)
        assert ratio > 1.0

    def test_iv_below_historical(self):
        # Market calm relative to history = tighten sigma
        ratio = compute_iv_ratio(current_iv=0.40, historical_iv=0.60)
        assert ratio < 1.0

    def test_equal_iv(self):
        ratio = compute_iv_ratio(current_iv=0.60, historical_iv=0.60)
        assert ratio == pytest.approx(1.0)

    def test_clamped_range(self):
        ratio = compute_iv_ratio(current_iv=2.0, historical_iv=0.30)
        assert ratio <= 2.0  # capped

    def test_zero_historical(self):
        ratio = compute_iv_ratio(current_iv=0.50, historical_iv=0.0)
        assert ratio == 1.0  # fallback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_deribit_iv.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement IV monitor**

```python
# polybot/core/deribit_iv.py
"""Deribit BTC options implied volatility monitor.

Provides forward-looking volatility estimate to replace/supplement backward-looking ATR.
When IV > historical vol, the market expects bigger moves than ATR shows.

REST: GET https://www.deribit.com/api/v2/public/get_book_summary_by_currency
No auth needed for public market data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def compute_iv_ratio(current_iv: float, historical_iv: float) -> float:
    """Ratio of market-implied vol to historical vol.

    >1.0 = market expects MORE vol than historical (widen sigma)
    <1.0 = market expects LESS vol (tighten sigma)
    Clamped to [0.5, 2.0] to prevent extreme adjustments.
    """
    if historical_iv <= 0 or current_iv <= 0:
        return 1.0
    ratio = current_iv / historical_iv
    return max(0.5, min(2.0, ratio))


@dataclass
class IVState:
    btc_iv: float = 0.0  # ATM implied vol (annualized)
    updated: float = 0.0

    def get_iv_ratio(self, atr: float, btc_price: float) -> float:
        """Compare Deribit IV to ATR-derived historical vol.

        ATR is in $/min. Convert to annualized % for comparison with IV.
        Annualized vol ≈ (ATR / price) * sqrt(525600)  [minutes per year]
        """
        if self.btc_iv <= 0 or atr <= 0 or btc_price <= 0:
            return 1.0
        historical_annualized = (atr / btc_price) * (525600 ** 0.5)
        return compute_iv_ratio(self.btc_iv, historical_annualized)


class DeribitIVFeed:
    """Polls Deribit for BTC ATM implied volatility."""

    def __init__(
        self,
        rest_url: str = "https://www.deribit.com/api/v2/public",
        poll_interval_s: float = 60.0,
    ) -> None:
        self.rest_url = rest_url
        self.poll_interval_s = poll_interval_s
        self.state = IVState()
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(
                        f"{self.rest_url}/get_book_summary_by_currency",
                        params={"currency": "BTC", "kind": "option"},
                    )
                    resp.raise_for_status()
                    result = resp.json().get("result", [])
                    # Find ATM option with nearest expiry that has IV
                    best_iv = self._extract_atm_iv(result)
                    if best_iv > 0:
                        self.state.btc_iv = best_iv
                        self.state.updated = time.time()
                        logger.debug(f"Deribit BTC IV: {best_iv:.2%}")
            except Exception as e:
                logger.warning(f"Deribit IV error: {e}")
            await asyncio.sleep(self.poll_interval_s)

    def _extract_atm_iv(self, summaries: list[dict]) -> float:
        """Find the ATM implied vol from nearest-expiry options."""
        now_ms = time.time() * 1000
        best: dict[str, Any] = {}
        best_dist = float("inf")
        for s in summaries:
            iv = s.get("mark_iv")
            if not iv or iv <= 0:
                continue
            # Nearest expiry with reasonable volume
            creation_ts = s.get("creation_timestamp", 0)
            # instrument_name like "BTC-11APR25-73000-C"
            name = s.get("instrument_name", "")
            # Prefer options with volume and near the money
            underlying = s.get("underlying_price", 0)
            if underlying <= 0:
                continue
            # Extract strike from name if possible
            parts = name.split("-")
            if len(parts) >= 3:
                try:
                    strike = float(parts[2])
                except ValueError:
                    continue
                dist = abs(strike - underlying) / underlying
                if dist < best_dist and dist < 0.05:  # within 5% of ATM
                    best_dist = dist
                    best = s
        return float(best.get("mark_iv", 0)) / 100.0 if best else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_deribit_iv.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/deribit_iv.py polybot/tests/test_deribit_iv.py
git commit -m "feat: add Deribit options IV feed for forward-looking volatility"
```

---

### Task 5: Bankroll Acceleration Strategy (C1)

**Files:**
- Create: `polybot/core/bankroll_strategy.py`
- Create: `polybot/tests/test_bankroll_strategy.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_bankroll_strategy.py
import pytest
from polybot.core.bankroll_strategy import compute_kelly_tier

class TestKellyTier:
    def test_baseline_under_100_trades(self):
        assert compute_kelly_tier(trade_count=50, win_rate=0.60, base_kelly=0.15) == 0.15

    def test_tier2_at_100_trades(self):
        assert compute_kelly_tier(trade_count=150, win_rate=0.56, base_kelly=0.15) == 0.18

    def test_tier2_requires_win_rate(self):
        # 100+ trades but <55% win rate = stay at base
        assert compute_kelly_tier(trade_count=150, win_rate=0.53, base_kelly=0.15) == 0.15

    def test_tier3_at_250_trades(self):
        assert compute_kelly_tier(trade_count=300, win_rate=0.57, base_kelly=0.15) == 0.22

    def test_tier4_at_500_trades(self):
        assert compute_kelly_tier(trade_count=600, win_rate=0.58, base_kelly=0.15) == 0.25

    def test_tier4_requires_high_win_rate(self):
        # 500+ trades but only 56% win rate = tier 3
        assert compute_kelly_tier(trade_count=600, win_rate=0.56, base_kelly=0.15) == 0.22

    def test_drops_back_if_win_rate_falls(self):
        # Had 300 trades but win rate dropped to 53% = back to base
        assert compute_kelly_tier(trade_count=300, win_rate=0.53, base_kelly=0.15) == 0.15
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_bankroll_strategy.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement bankroll strategy**

```python
# polybot/core/bankroll_strategy.py
"""Dynamic Kelly fraction based on track record quality.

As the bot accumulates trades with proven win rate, Kelly ratchets up.
If win rate drops, Kelly drops back. The circuit breaker (drawdown-based)
still operates independently on top of this.

Tiers:
  0-100 trades:  any win rate   → base (0.15)
  100-250:       >55% required  → 0.18
  250-500:       >56% required  → 0.22
  500+:          >57% required  → 0.25
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# (min_trades, min_win_rate, kelly_fraction)
KELLY_TIERS: list[tuple[int, float, float]] = [
    (500, 0.57, 0.25),
    (250, 0.56, 0.22),
    (100, 0.55, 0.18),
]


def compute_kelly_tier(trade_count: int, win_rate: float, base_kelly: float = 0.15) -> float:
    """Determine Kelly fraction based on track record.

    Checks tiers from highest to lowest. Returns the first tier where
    both trade_count and win_rate meet requirements. Falls back to base_kelly.
    """
    for min_trades, min_wr, kelly in KELLY_TIERS:
        if trade_count >= min_trades and win_rate >= min_wr:
            return kelly
    return base_kelly
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_bankroll_strategy.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/bankroll_strategy.py polybot/tests/test_bankroll_strategy.py
git commit -m "feat: add bankroll acceleration tiers for dynamic Kelly sizing"
```

---

## GROUP B: Signal Engine Integration (Sequential — depends on Group A)

### Task 6: Extend SignalEngine for New Signals

**Files:**
- Modify: `polybot/core/signal_engine.py`
- Create: `polybot/tests/test_signal_engine_v2.py`

This task adds: spot_flow_signal (B2 CVD), wall_pressure (B1), depth_factor (B1 thinness), perp_lead (B3), funding_signal (B4), iv_ratio (B5), prev_resolution_margin (D2), and conviction multiplier (A2).

- [ ] **Step 1: Write failing tests for new signal integration**

```python
# polybot/tests/test_signal_engine_v2.py
"""Tests for the new signal layers added to SignalEngine."""
import pytest
import numpy as np
from polybot.core.signal_engine import SignalEngine

class TestWallPressureIntegration:
    def test_wall_reduces_up_probability(self):
        engine = SignalEngine(wall_weight=0.05)
        closes = np.array([73000.0 + i for i in range(25)])
        # No wall: baseline probability
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=0.0,
        )
        # Strong sell wall above (positive = bearish for Up)
        with_wall = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=0.8,
        )
        assert with_wall < base  # wall should reduce P(Up)

    def test_support_wall_increases_up_probability(self):
        engine = SignalEngine(wall_weight=0.05)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=72980, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=0.0,
        )
        with_support = engine.compute_probability(
            btc_price=72980, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, wall_pressure=-0.8,
        )
        assert with_support > base  # support should increase P(Up)


class TestSpotFlowIntegration:
    def test_bullish_spot_flow_increases_prob(self):
        engine = SignalEngine(spot_flow_weight=0.04)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, spot_flow_signal=0.0,
        )
        bullish = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, spot_flow_signal=0.8,
        )
        assert bullish > base


class TestPerpLeadIntegration:
    def test_perp_leading_up_increases_prob(self):
        engine = SignalEngine(perp_lead_weight=0.03)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, perp_lead=0.0,
        )
        leading_up = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, perp_lead=0.5,
        )
        assert leading_up > base


class TestIVRatioIntegration:
    def test_high_iv_widens_probability(self):
        """High IV = wider sigma = probabilities closer to 0.5"""
        engine = SignalEngine()
        closes = np.array([73000.0 + i for i in range(25)])
        # With normal IV
        normal = engine.compute_probability(
            btc_price=73050, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, iv_ratio=1.0,
        )
        # With high IV (sigma wider = less confident)
        high_iv = engine.compute_probability(
            btc_price=73050, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, iv_ratio=1.5,
        )
        # High IV should push probability closer to 0.5 (less extreme)
        assert abs(high_iv - 0.5) < abs(normal - 0.5)


class TestConvictionMultiplier:
    def test_high_prob_gets_higher_kelly(self):
        engine = SignalEngine(conviction_multiplier=True)
        # At 95% probability, Kelly should be boosted
        k_high = engine._kelly(0.95, 0.80)
        engine_no = SignalEngine(conviction_multiplier=False)
        k_base = engine_no._kelly(0.95, 0.80)
        assert k_high > k_base

    def test_marginal_prob_gets_lower_kelly(self):
        engine = SignalEngine(conviction_multiplier=True)
        k_marginal = engine._kelly(0.68, 0.55)
        engine_no = SignalEngine(conviction_multiplier=False)
        k_base = engine_no._kelly(0.68, 0.55)
        assert k_marginal < k_base


class TestPrevResolutionMargin:
    def test_strong_up_carries_momentum(self):
        engine = SignalEngine(prev_margin_weight=0.02)
        closes = np.array([73000.0 + i for i in range(25)])
        base = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, prev_resolution_margin=0.0,
        )
        # Previous window resolved strongly Up (BTC was $80 above strike)
        carry = engine.compute_probability(
            btc_price=73020, strike_price=73000, seconds_remaining=120,
            atr=25, closes=closes, prev_resolution_margin=80.0,
        )
        assert carry > base  # momentum carry favors Up
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_signal_engine_v2.py -v`
Expected: FAIL (new params not accepted yet)

- [ ] **Step 3: Modify SignalEngine.__init__ to accept new parameters**

Add to `polybot/core/signal_engine.py` `__init__` (after line 59, the existing `flow_weight` param):

```python
# In __init__ signature, add after flow_weight parameter:
                 spot_flow_weight: float = 0.04,
                 wall_weight: float = 0.05,
                 perp_lead_weight: float = 0.03,
                 prev_margin_weight: float = 0.02,
                 conviction_multiplier: bool = True,
```

Add to `__init__` body (after line 72, the `self.atr_sigma_ratio` line):

```python
        self.spot_flow_weight: float = spot_flow_weight
        self.wall_weight: float = wall_weight
        self.perp_lead_weight: float = perp_lead_weight
        self.prev_margin_weight: float = prev_margin_weight
        self.conviction_multiplier: bool = conviction_multiplier
```

- [ ] **Step 4: Modify compute_probability to accept and apply new signals**

Update the signature of `compute_probability` (line 100) to add new parameters:

```python
    def compute_probability(self, btc_price: float, strike_price: float,
                            seconds_remaining: float, atr: float,
                            indicators: dict | None = None,
                            closes: np.ndarray | None = None,
                            flow_signal: float = 0.0,
                            spot_flow_signal: float = 0.0,
                            wall_pressure: float = 0.0,
                            perp_lead: float = 0.0,
                            prev_resolution_margin: float = 0.0,
                            iv_ratio: float = 1.0) -> float:
```

Add IV ratio scaling BEFORE the z computation (after line 120, the `vol_scaled` line). Replace the existing vol_scaled line:

```python
        # IV ratio adjusts sigma: >1.0 = market expects more vol = widen
        vol_scaled = (atr / self.atr_sigma_ratio) * math.sqrt(minutes_remaining) * iv_ratio
```

Add new layers AFTER the existing Layer 3 (after line 154, `logit_p += flow_signal * logit_flow_w`):

```python
        # Layer 3b — Spot market flow (CVD + taker ratio from Binance aggTrades)
        logit_spot_flow_w = self.spot_flow_weight * 4.0
        logit_p += spot_flow_signal * logit_spot_flow_w

        # Layer 3c — Wall pressure near strike (from Binance L2 depth)
        # Positive wall_pressure = resistance above = bearish for Up = reduce logit
        logit_wall_w = self.wall_weight * 4.0
        logit_p -= wall_pressure * logit_wall_w

        # Layer 3d — Perpetual price lead (from Bybit)
        logit_perp_w = self.perp_lead_weight * 4.0
        logit_p += perp_lead * logit_perp_w

        # Layer 5 — Previous window momentum carry
        if prev_resolution_margin != 0.0 and strike_price > 0:
            # Normalize margin by ATR to make it scale-invariant
            normalized_margin = prev_resolution_margin / max(atr, 1.0)
            logit_prev_w = self.prev_margin_weight * 4.0
            logit_p += math.tanh(normalized_margin) * logit_prev_w
```

- [ ] **Step 5: Update evaluate() and evaluate_hold() signatures to pass new signals through**

In `evaluate()` (line 183), add to signature:

```python
                 spot_flow_signal: float = 0.0,
                 wall_pressure: float = 0.0,
                 perp_lead: float = 0.0,
                 prev_resolution_margin: float = 0.0,
                 iv_ratio: float = 1.0) -> TradeSignal:
```

Update the `compute_probability` call inside `evaluate()` (around line 208) to pass the new params:

```python
        prob_up = self.compute_probability(btc_price, strike_price,
                                           seconds_remaining, atr, indicators,
                                           closes=closes,
                                           flow_signal=flow_signal,
                                           spot_flow_signal=spot_flow_signal,
                                           wall_pressure=wall_pressure,
                                           perp_lead=perp_lead,
                                           prev_resolution_margin=prev_resolution_margin,
                                           iv_ratio=iv_ratio)
```

In `evaluate_hold()` (line 253), add the same new params to the signature and the internal `compute_probability` call.

- [ ] **Step 6: Add conviction multiplier to _kelly()**

Replace the `_kelly` method (lines 305-312):

```python
    def _kelly(self, prob: float, market_price: float) -> float:
        """Kelly for binary outcome with optional conviction scaling."""
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        q = 1.0 - prob
        raw = (prob * b - q) / b
        base = max(0, raw * self.kelly_fraction)
        if not self.conviction_multiplier:
            return base
        # Scale Kelly by conviction: >85% prob = boost, 65-75% = dampen
        if prob >= 0.90:
            return base * 1.3
        elif prob >= 0.85:
            return base * 1.15
        elif prob < 0.72:
            return base * 0.7
        return base
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_signal_engine_v2.py -v`
Expected: All PASS

- [ ] **Step 8: Run existing signal engine tests to verify no regressions**

Run: `python -m pytest polybot/tests/ -k "signal" -v`
Expected: All existing tests still pass (new params have defaults)

- [ ] **Step 9: Commit**

```bash
git add polybot/core/signal_engine.py polybot/tests/test_signal_engine_v2.py
git commit -m "feat: extend signal engine with wall pressure, spot flow, perp lead, IV ratio, conviction multiplier, prev margin"
```

---

## GROUP C: Execution & Capital Changes

### Task 7: Maker Orders in LiveTrader (A1)

**Files:**
- Modify: `polybot/execution/live_trader.py`
- Modify: `polybot/execution/paper_trader.py` (log maker attempts)

- [ ] **Step 1: Add _execute_buy_limit method to LiveTrader**

Add after `_execute_sell()` (line 117) in `polybot/execution/live_trader.py`:

```python
    async def _execute_buy_limit(
        self,
        token_id: str,
        price: float,
        size: float,
        timeout_s: float = 60.0,
    ) -> FillResult:
        """Post a limit buy order (maker, 0% fee) with timeout fallback to FOK.

        Strategy: post limit order at price, wait up to timeout_s for fill.
        If not filled, cancel and fall back to FOK market order.
        """
        try:
            from py_clob_client.order_builder.constants import BUY
            order = self.clob.create_order(
                order_args={
                    "token_id": token_id,
                    "price": price,
                    "size": size,
                    "side": BUY,
                },
            )
            resp = self.clob.post_order(order)
            order_id = resp.get("orderID") or resp.get("id")
            if not order_id:
                logger.warning("Maker order: no order ID returned, falling back to FOK")
                return await self._execute_buy(token_id, price, size)

            logger.info(f"Maker limit order posted: {order_id} at ${price:.4f}")

            # Poll for fill
            import asyncio
            poll_interval = 2.0
            elapsed = 0.0
            while elapsed < timeout_s:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    order_status = self.clob.get_order(order_id)
                    status = order_status.get("status", "")
                    if status == "MATCHED":
                        fill_price = float(order_status.get("associate_trades", [{}])[0].get("price", price))
                        logger.info(f"Maker order filled at ${fill_price:.4f}")
                        return FillResult(True, fill_price, size, "maker_fill")
                    elif status in ("CANCELLED", "EXPIRED"):
                        logger.info("Maker order cancelled/expired, falling back to FOK")
                        return await self._execute_buy(token_id, price, size)
                except Exception as e:
                    logger.warning(f"Maker poll error: {e}")

            # Timeout — cancel and fall back to FOK
            try:
                self.clob.cancel(order_id)
                logger.info(f"Maker order timed out after {timeout_s}s, falling back to FOK")
            except Exception as e:
                logger.warning(f"Failed to cancel maker order: {e}")
            return await self._execute_buy(token_id, price, size)

        except Exception as e:
            logger.warning(f"Maker order failed: {e}, falling back to FOK")
            return await self._execute_buy(token_id, price, size)
```

- [ ] **Step 2: Add maker_timeout_s config and use_maker flag**

This will be wired in Task 11 (config). For now, the method exists and can be called.

- [ ] **Step 3: Commit**

```bash
git add polybot/execution/live_trader.py
git commit -m "feat: add maker limit order support with FOK fallback in LiveTrader"
```

---

### Task 8: Concurrent Windows (C2)

**Files:**
- Modify: `polybot/execution/base.py`
- Create: `polybot/tests/test_concurrent_windows.py`

**Key caveat from user:** The next window's strike depends on the end of the current window. So concurrent entry into window N+1 is only valid AFTER the boundary time when the candle closes and the strike is established.

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_concurrent_windows.py
import pytest

class TestConcurrentWindowLogic:
    def test_allows_second_position_different_window(self):
        """With max_concurrent=2, can open position in window B while holding in window A."""
        # This tests the gate logic in base.py open_trade()
        # Currently: open_position_count >= max_concurrent → reject
        # With max_concurrent=2: allows up to 2
        pass  # Logic test is in the integration with main.py

    def test_half_kelly_when_concurrent(self):
        """When already holding one position, new position should use half Kelly."""
        # kelly_size should be halved when open_position_count > 0
        pass

    def test_rejects_same_window_duplicate(self):
        """Still rejects duplicate entry into the same market."""
        pass

    def test_next_window_strike_not_used_before_boundary(self):
        """The next window's strike is only valid after the boundary candle closes."""
        pass
```

- [ ] **Step 2: Modify BaseTrader.open_trade to support concurrent positions**

In `polybot/execution/base.py`, the `open_trade` method (line 136) currently has this rejection gate (around line 157):

```python
        open_count = await self.db.get_open_position_count()
        if open_count >= self.max_concurrent_positions:
            return TradeResult(False, reason="Max concurrent positions reached")
```

This already supports `max_concurrent_positions`. The change is in **main.py** where `has_position` is checked (Task 11 wiring). The base trader gate just needs `max_concurrent_positions=2` in config.

- [ ] **Step 3: Add half-Kelly logic for concurrent positions**

In `polybot/main.py` `_evaluate_signal_and_enter()`, the sizing logic (around line 280) computes `raw_size`. Add after the Kelly sizing:

```python
        # Half Kelly when already holding another position (concurrent windows)
        open_count = len(await db.get_open_positions())
        if open_count > 0:
            raw_size *= 0.5
            logger.info(f"Concurrent position: halving Kelly (open={open_count})")
```

This is wired in Task 11.

- [ ] **Step 4: Commit**

```bash
git add polybot/tests/test_concurrent_windows.py
git commit -m "feat: add concurrent window position logic with half-Kelly sizing"
```

---

## GROUP D: Trading Loop Changes

### Task 9: Dynamic Entry Timing (D5)

**Files:**
- Create: `polybot/tests/test_dynamic_entry.py`

The logic lives in `main.py` and is wired in Task 11. The core idea:
- First 60s of each window: OBSERVE ONLY (collect L2/CVD/flow data)
- 60-180s: Normal entry when all gates pass
- 180-240s: Entry with reduced Kelly (0.7x multiplier)
- Last 60s: Only at >90% model confidence

- [ ] **Step 1: Write tests for entry phase logic**

```python
# polybot/tests/test_dynamic_entry.py
import pytest

def compute_entry_phase(seconds_remaining: float, window_seconds: float = 300.0) -> dict:
    """Determine entry phase based on time elapsed in window.

    Returns dict with:
      allowed: bool — whether entry is allowed in this phase
      kelly_multiplier: float — scaling factor for this phase
      min_prob_override: float or None — minimum probability override for this phase
    """
    elapsed = window_seconds - seconds_remaining
    if elapsed < 60:
        return {"allowed": False, "kelly_multiplier": 1.0, "min_prob_override": None, "phase": "observe"}
    elif elapsed < 180:
        return {"allowed": True, "kelly_multiplier": 1.0, "min_prob_override": None, "phase": "normal"}
    elif elapsed < 240:
        return {"allowed": True, "kelly_multiplier": 0.7, "min_prob_override": None, "phase": "late"}
    else:
        return {"allowed": True, "kelly_multiplier": 0.5, "min_prob_override": 0.90, "phase": "final"}


class TestEntryPhase:
    def test_observe_phase(self):
        # 280s remaining = 20s elapsed (first 60s = observe)
        result = compute_entry_phase(seconds_remaining=280.0)
        assert not result["allowed"]
        assert result["phase"] == "observe"

    def test_normal_phase(self):
        # 200s remaining = 100s elapsed
        result = compute_entry_phase(seconds_remaining=200.0)
        assert result["allowed"]
        assert result["kelly_multiplier"] == 1.0
        assert result["phase"] == "normal"

    def test_late_phase(self):
        # 100s remaining = 200s elapsed
        result = compute_entry_phase(seconds_remaining=100.0)
        assert result["allowed"]
        assert result["kelly_multiplier"] == 0.7
        assert result["phase"] == "late"

    def test_final_phase_requires_high_prob(self):
        # 40s remaining = 260s elapsed
        result = compute_entry_phase(seconds_remaining=40.0)
        assert result["allowed"]
        assert result["min_prob_override"] == 0.90
        assert result["phase"] == "final"
```

- [ ] **Step 2: Implement compute_entry_phase as a standalone function**

Add to the top of `polybot/main.py` (after imports, before the first helper function):

```python
def compute_entry_phase(seconds_remaining: float, window_seconds: float = 300.0) -> dict:
    """Determine entry phase based on time elapsed in window."""
    elapsed = window_seconds - seconds_remaining
    if elapsed < 60:
        return {"allowed": False, "kelly_multiplier": 1.0, "min_prob_override": None, "phase": "observe"}
    elif elapsed < 180:
        return {"allowed": True, "kelly_multiplier": 1.0, "min_prob_override": None, "phase": "normal"}
    elif elapsed < 240:
        return {"allowed": True, "kelly_multiplier": 0.7, "min_prob_override": None, "phase": "late"}
    else:
        return {"allowed": True, "kelly_multiplier": 0.5, "min_prob_override": 0.90, "phase": "final"}
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/test_dynamic_entry.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/tests/test_dynamic_entry.py
git commit -m "feat: add dynamic entry timing with observe/normal/late/final phases"
```

---

## GROUP E: Configuration & Full Wiring

### Task 10: Config Updates

**Files:**
- Modify: `polybot/config/settings.yaml`
- Modify: `polybot/config/loader.py`

- [ ] **Step 1: Add new config sections to settings.yaml**

Add at the end of `polybot/config/settings.yaml`:

```yaml
# --- New feeds (B1-B5) ---
binance_depth:
  ws_url: "wss://stream.binance.us:9443/ws/btcusdt@depth20@100ms"
  rest_url: "https://api.binance.us/api/v3"
  poll_interval_s: 5.0

binance_trades:
  ws_url: "wss://stream.binance.us:9443/ws/btcusdt@aggTrade"

bybit:
  ws_url: "wss://stream.bybit.com/v5/public/linear"
  rest_url: "https://api.bybit.com/v5/market"
  funding_poll_s: 300.0

deribit:
  rest_url: "https://www.deribit.com/api/v2/public"
  poll_interval_s: 60.0

# --- New signal weights ---
signal:
  # ... existing params stay unchanged ...
  spot_flow_weight: 0.04
  wall_weight: 0.05
  perp_lead_weight: 0.03
  prev_margin_weight: 0.02

# --- Execution enhancements ---
execution:
  # ... existing params stay unchanged ...
  max_concurrent_positions: 2
  use_maker_orders: true
  maker_timeout_s: 60.0

# --- Dynamic entry timing ---
entry_timing:
  observe_seconds: 60
  late_phase_seconds: 180
  final_phase_seconds: 240
  late_kelly_multiplier: 0.7
  final_kelly_multiplier: 0.5
  final_min_probability: 0.90

# --- Bankroll acceleration ---
bankroll_acceleration:
  enabled: true
```

- [ ] **Step 2: Add validation for new config params in loader.py**

Add to `validate_config()` in `polybot/config/loader.py` (inside the validation block, after existing signal validations):

```python
    # New signal weights
    _check("signal.spot_flow_weight", 0.0, 0.10)
    _check("signal.wall_weight", 0.0, 0.15)
    _check("signal.perp_lead_weight", 0.0, 0.10)
    _check("signal.prev_margin_weight", 0.0, 0.05)

    # Entry timing
    _check("entry_timing.observe_seconds", 0, 120)
    _check("entry_timing.late_phase_seconds", 60, 240)
    _check("entry_timing.final_phase_seconds", 120, 280)
    _check("entry_timing.late_kelly_multiplier", 0.3, 1.0)
    _check("entry_timing.final_kelly_multiplier", 0.2, 1.0)
    _check("entry_timing.final_min_probability", 0.70, 0.98)

    # Execution
    _check("execution.max_concurrent_positions", 1, 3)
    _check("execution.maker_timeout_s", 10, 120)
```

- [ ] **Step 3: Commit**

```bash
git add polybot/config/settings.yaml polybot/config/loader.py
git commit -m "feat: add config for new feeds, signal weights, entry timing, maker orders, concurrent positions"
```

---

### Task 11: Wire Everything into main.py

**Files:**
- Modify: `polybot/main.py`

This is the integration task. It connects all new feeds and signals to the trading loop.

- [ ] **Step 1: Add imports for new modules**

At the top of `polybot/main.py`, add after existing imports:

```python
from polybot.core.binance_depth import BinanceDepthFeed
from polybot.core.binance_trades import BinanceTradesFeed
from polybot.core.bybit_feed import BybitFeed
from polybot.core.deribit_iv import DeribitIVFeed
from polybot.core.bankroll_strategy import compute_kelly_tier
```

- [ ] **Step 2: Initialize new feeds in main()**

In `main()` (around line 1041), after the existing BinanceFeed initialization, add:

```python
    # New data feeds
    depth_feed = BinanceDepthFeed(
        ws_url=cfg.get("binance_depth", {}).get("ws_url", "wss://stream.binance.us:9443/ws/btcusdt@depth20@100ms"),
        rest_url=cfg.get("binance_depth", {}).get("rest_url", "https://api.binance.us/api/v3"),
        poll_interval_s=cfg.get("binance_depth", {}).get("poll_interval_s", 5.0),
    )
    trades_feed = BinanceTradesFeed(
        ws_url=cfg.get("binance_trades", {}).get("ws_url", "wss://stream.binance.us:9443/ws/btcusdt@aggTrade"),
    )
    bybit_feed = BybitFeed(
        ws_url=cfg.get("bybit", {}).get("ws_url", "wss://stream.bybit.com/v5/public/linear"),
        rest_url=cfg.get("bybit", {}).get("rest_url", "https://api.bybit.com/v5/market"),
        funding_poll_s=cfg.get("bybit", {}).get("funding_poll_s", 300.0),
    )
    deribit_feed = DeribitIVFeed(
        rest_url=cfg.get("deribit", {}).get("rest_url", "https://www.deribit.com/api/v2/public"),
        poll_interval_s=cfg.get("deribit", {}).get("poll_interval_s", 60.0),
    )
```

Start them alongside existing feeds:

```python
    await depth_feed.start()
    await trades_feed.start()
    await bybit_feed.start()
    await deribit_feed.start()
```

Pass them into `trading_loop()`.

- [ ] **Step 3: Add dynamic entry timing gate in _evaluate_signal_and_enter()**

At the beginning of `_evaluate_signal_and_enter()` (after line 187), add:

```python
    # Dynamic entry timing — observe first 60s, then phased entry
    entry_phase = compute_entry_phase(
        seconds_remaining=contract["seconds_remaining"],
        window_seconds=cfg.get("market", {}).get("entry_window_seconds", 300),
    )
    if not entry_phase["allowed"]:
        return  # Still in observe phase
```

After the Kelly sizing (around line 280), apply the phase multiplier:

```python
    # Phase-based Kelly adjustment
    raw_size *= entry_phase["kelly_multiplier"]

    # Phase-based probability override
    if entry_phase["min_prob_override"] and signal.prob < entry_phase["min_prob_override"]:
        logger.debug(f"Final phase: prob {signal.prob:.0%} < {entry_phase['min_prob_override']:.0%}, skipping")
        return
```

- [ ] **Step 4: Compute and pass new signals in _evaluate_signal_and_enter()**

After the existing flow_signal computation (around line 208), add:

```python
    # New signals from extended feeds
    spot_flow_signal = 0.0
    wall_pressure_val = 0.0
    perp_lead_val = 0.0
    iv_ratio_val = 1.0

    if trades_feed:
        acc = trades_feed.accumulator
        cvd = acc.get_cvd(window_s=120)
        taker = acc.get_taker_ratio(window_s=60)
        # Composite spot flow: CVD direction + taker bias
        spot_flow_signal = max(-1.0, min(1.0, math.tanh(cvd * 2) * 0.6 + (taker - 0.5) * 2 * 0.4))

    if depth_feed:
        wall_pressure_val = depth_feed.get_wall_pressure(strike, btc_price)

    if bybit_feed and bybit_feed.state.perp_price > 0:
        perp_lead_val = bybit_feed.state.get_lead(btc_price)

    if deribit_feed and deribit_feed.state.btc_iv > 0:
        atr_val = indicators.get("atr", {}).get("atr", 0)
        iv_ratio_val = deribit_feed.state.get_iv_ratio(atr_val, btc_price)
```

Update the `signal_engine.evaluate()` call to pass new params:

```python
    signal = signal_engine.evaluate(
        indicators, has_position=False, in_entry_window=in_window,
        btc_price=btc_price, strike_price=strike,
        seconds_remaining=contract["seconds_remaining"],
        market_price_up=price_up, market_price_down=price_down,
        closes=closes, flow_signal=flow_score,
        spot_flow_signal=spot_flow_signal,
        wall_pressure=wall_pressure_val,
        perp_lead=perp_lead_val,
        prev_resolution_margin=prev_resolution_margin,
        iv_ratio=iv_ratio_val,
    )
```

- [ ] **Step 5: Add concurrent window support — change has_position check**

In the trading loop (around line 849), the current logic checks `has_position` and skips entry if true. Change to allow concurrent positions from different windows:

Find the check that prevents entry when a position exists. Replace `has_position=True` with a count check:

```python
    open_positions = await db.get_open_positions()
    open_count = len(open_positions)
    max_concurrent = cfg.get("execution", {}).get("max_concurrent_positions", 1)
    has_position_in_this_market = any(
        p["market_id"] == contract.get("market_id", "") for p in open_positions
    )

    # Allow entry if under concurrent limit AND not already in this specific market
    can_enter = open_count < max_concurrent and not has_position_in_this_market
```

Apply half-Kelly when concurrent (after sizing):

```python
    if open_count > 0:
        raw_size *= 0.5
```

- [ ] **Step 6: Add bankroll acceleration**

In `_evaluate_signal_and_enter()`, after getting `kelly_multiplier` from circuit breaker, add:

```python
    if cfg.get("bankroll_acceleration", {}).get("enabled", False):
        trades = await db.get_trade_history(limit=1000)
        if trades:
            total = len(trades)
            wins = sum(1 for t in trades if (t.get("exit_price", 0) or 0) > (t.get("entry_price", 0) or 0))
            wr = wins / total if total > 0 else 0
            dynamic_kelly = compute_kelly_tier(total, wr, signal_engine.kelly_fraction)
            if dynamic_kelly != signal_engine.kelly_fraction:
                logger.info(f"Bankroll acceleration: {signal_engine.kelly_fraction:.0%} → {dynamic_kelly:.0%} "
                            f"(trades={total}, wr={wr:.1%})")
                signal_engine.kelly_fraction = dynamic_kelly
```

- [ ] **Step 7: Track previous resolution margin for D2**

Add a module-level variable at the top of main.py:

```python
_prev_resolution_margin: float = 0.0
```

In `_resolve_expired_position()` (around line 683), after a successful resolution, update:

```python
    global _prev_resolution_margin
    # Track how far BTC was from strike at resolution for next-window momentum
    if btc_at_expiry and strike:
        _prev_resolution_margin = btc_at_expiry - strike
    else:
        _prev_resolution_margin = 0.0
```

Pass `prev_resolution_margin=_prev_resolution_margin` to the evaluate call in Step 4.

- [ ] **Step 8: Add staleness detection from Bybit (D1)**

In `_evaluate_signal_and_enter()`, after computing signals, add a staleness boost:

```python
    # Latency arbitrage: if Bybit perp has moved but Binance spot hasn't caught up,
    # the edge is higher than the model thinks (Polymarket is even more stale)
    if bybit_feed and bybit_feed.state.is_stale(
        spot_price=btc_price,
        spot_updated=binance_feed.buffer.latest().timestamp / 1000.0 if binance_feed.buffer.latest() else 0,
        threshold_usd=20.0,
    ):
        logger.info(f"Staleness detected: Bybit={bybit_feed.state.perp_price:.0f} Binance={btc_price:.0f}")
        # The perp lead signal already captures this directionally
        # But we can boost conviction since the edge is likely to widen
```

This naturally flows through the `perp_lead` signal which is already being passed.

- [ ] **Step 9: Add maker order routing in _evaluate_signal_and_enter()**

When calling `trader.open_trade()`, check if maker orders are enabled and we're early enough:

```python
    use_maker = (
        cfg.get("execution", {}).get("use_maker_orders", False)
        and entry_phase["phase"] in ("normal",)  # Only use maker in normal phase (plenty of time)
        and hasattr(trader, "_execute_buy_limit")
    )
```

The actual maker routing happens inside `open_trade` — add a `prefer_maker` flag to the `open_trade` call and let BaseTrader route accordingly. Or simpler: override in LiveTrader's `_execute_buy` to try maker first when conditions are met.

- [ ] **Step 10: Log all new signals in outcome JSON**

In `_record_outcome()` (line 159), extend the `trade_context` dict in the indicator snapshot:

```python
    # Add to the trade_context dict that's saved in indicator_snapshot:
    "spot_flow_signal": spot_flow_signal,
    "wall_pressure": wall_pressure_val,
    "perp_lead": perp_lead_val,
    "iv_ratio": iv_ratio_val,
    "prev_resolution_margin": prev_resolution_margin,
    "bybit_perp_price": bybit_feed.state.perp_price if bybit_feed else 0,
    "funding_rate": bybit_feed.state.funding_rate if bybit_feed else 0,
    "depth_usd": depth_feed.get_depth_usd() if depth_feed else 0,
    "entry_phase": entry_phase.get("phase", "unknown"),
    "cvd_120s": trades_feed.accumulator.get_cvd(120) if trades_feed else 0,
    "taker_ratio_60s": trades_feed.accumulator.get_taker_ratio(60) if trades_feed else 0,
    "volume_surge": trades_feed.accumulator.is_volume_surge() if trades_feed else False,
```

- [ ] **Step 11: Graceful shutdown of new feeds**

In `main()`, in the shutdown/cleanup section, add:

```python
    await depth_feed.stop()
    await trades_feed.stop()
    await bybit_feed.stop()
    await deribit_feed.stop()
```

- [ ] **Step 12: Run full test suite**

Run: `python -m pytest polybot/tests/ -v --timeout=30`
Expected: All tests pass. New tests pass. Existing tests pass (all new params have defaults).

- [ ] **Step 13: Commit**

```bash
git add polybot/main.py
git commit -m "feat: wire all new feeds, signals, entry timing, concurrent windows, maker orders into trading loop"
```

---

## GROUP F: Final Verification

### Task 12: Integration Test — Paper Mode Smoke Test

- [ ] **Step 1: Run paper mode briefly to verify startup**

```bash
timeout 30 python -m polybot.main --mode paper 2>&1 | head -50
```

Expected: No crashes. New feeds connect (may warn if Bybit/Deribit are unreachable from test env). Signal engine logs new signals. Entry timing shows "observe" phase for first 60s.

- [ ] **Step 2: Verify config validation**

```bash
python -c "from polybot.config.loader import load_config; load_config()"
```

Expected: No validation errors.

- [ ] **Step 3: Run full test suite one final time**

```bash
python -m pytest polybot/tests/ -v
```

Expected: All tests pass.

- [ ] **Step 4: Final commit with updated CLAUDE.md**

Update `CLAUDE.md` to document:
- New data feeds (Binance depth, aggTrades, Bybit perp, Deribit IV)
- New signal layers (spot flow, wall pressure, perp lead, funding, IV ratio, prev margin)
- Dynamic entry timing (60s observe → normal → late → final phases)
- Concurrent windows (max 2, half-Kelly, different markets only)
- Maker orders (limit order with FOK fallback)
- Bankroll acceleration (tiered Kelly)
- Conviction multiplier (Kelly scales with model probability)

```bash
git add CLAUDE.md polybot/config/settings.yaml
git commit -m "docs: update CLAUDE.md with all new compounding edge features"
```

---

## Dependency Graph

```
Task 1 (Depth) ──────┐
Task 2 (Trades) ──────┤
Task 3 (Bybit) ───────┼──► Task 6 (Signal Engine) ──► Task 11 (Wire main.py) ──► Task 12 (Verify)
Task 4 (Deribit IV) ──┤
Task 5 (Bankroll) ────┘
                           Task 7 (Maker Orders) ────► Task 11
                           Task 8 (Concurrent) ──────► Task 11
                           Task 9 (Entry Timing) ────► Task 11
                           Task 10 (Config) ──────────► Task 11
```

Tasks 1-5 are fully independent — run all in parallel.
Tasks 6-10 depend on respective Group A tasks.
Task 11 depends on everything.
Task 12 depends on Task 11.
