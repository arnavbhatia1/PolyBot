"""Coinbase Exchange real-time BTC price feed.

Public WebSocket ticker channel (fires on every trade). Provides:
  - latest BTC-USD trade price (fastest US-venue feed)
  - best bid / best ask
  - per-trade CVD + taker ratio (aggressor side × size)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from polybot.feeds._json import loads as _loads
from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-feed.exchange.coinbase.com"
RECONNECT_BASE = 1
RECONNECT_MAX = 30


@dataclass
class CoinbaseState:
    price: float = 0.0
    updated_at: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0

    @property
    def age_seconds(self) -> float:
        if self.updated_at == 0:
            return float("inf")
        return time.time() - self.updated_at


class CoinbaseFeed:
    """Streams Coinbase BTC-USD ticker; tracks price + spot-venue CVD/taker."""

    def __init__(self, ws_url: str = WS_URL, product_id: str = "BTC-USD",
                 trade_buffer_s: float = 300.0) -> None:
        self.ws_url = ws_url
        self.product_id = product_id
        self.state = CoinbaseState()
        self.staleness = StalenessTracker("coinbase")
        # Set on every fresh ticker so a latency-sensitive consumer (the late-window
        # sniper loop) can wake the instant Coinbase moves — the CLOB book lags it,
        # so waiting on book updates alone would miss the stale-book window. The
        # waiter clears it after waking.
        self.price_event: asyncio.Event = asyncio.Event()

        # Per-trade flow: (ts, signed_size). +size = buyer aggressor, -size = seller aggressor.
        self._trade_buffer_s = trade_buffer_s
        self._trades: deque[tuple[float, float]] = deque()
        # 1s-bucketed (ts, price) history for realized_vol.
        self._prices: deque[tuple[float, float]] = deque()
        self._last_price_sample: float = 0.0
        # When the current contiguous trade window began (reset on every
        # (re)connect, since the deque is cleared). Window-based reads must not
        # trust a window the buffer doesn't span yet — a fresh reconnect would
        # otherwise read a truncated window as genuinely flat flow.
        self._window_start: float = 0.0

        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connect_ws())

    async def stop(self) -> None:
        self._running = False
        # Cancel before the first await — stop() runs under a shutdown timeout.
        if self._task:
            self._task.cancel()
        if self._ws:
            await self._ws.close()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def covers(self, window_s: float) -> bool:
        """True iff the trade buffer continuously spans the last window_s seconds
        (i.e., no reconnect cleared it mid-window). Consumers stamp None instead
        of reading a truncated window as a real near-zero."""
        return self._window_start > 0 and (time.time() - self._window_start) >= window_s

    def get_cvd(self, window_s: float = 60.0) -> float:
        """Signed cumulative volume delta over the last window_s seconds."""
        cutoff = time.time() - window_s
        total = 0.0
        for ts, sz in reversed(self._trades):
            if ts < cutoff:
                break
            total += sz
        return total

    def realized_vol(self, window_s: float = 60.0) -> float:
        """Sample stdev of log returns over the 1s-bucketed price history in the
        window. 0.0 when fewer than 3 samples; gate on covers(window_s) to avoid
        reading a reconnect-truncated window as genuinely quiet."""
        cutoff = time.time() - window_s
        closes = [p for ts, p in self._prices if ts >= cutoff and p > 0]
        if len(closes) < 3:
            return 0.0
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / max(1, len(rets) - 1)
        return math.sqrt(var)

    def cb_move(self, window_s: float = 2.0) -> float | None:
        """Signed Coinbase price change over exactly the last ``window_s`` — the live
        form of the offline late-window ``cb_move`` signal. The 1s-bucketed history is
        spaced >=1s, so the latest bucket at/before (now - window_s) can sit up to ~1s
        too far back; we therefore INTERPOLATE the ``then`` price at exactly
        (now - window_s) between the two buckets bracketing it, so the effective lookback
        stays == window_s. Without this, a sustained move is measured over ~window_s+1s
        and OVERSTATES the move, firing on sub-threshold moves the harness scored as
        non-fires. ``now`` is the freshest un-bucketed tick (state.price), so a recent
        spike is still captured. None if the buffer doesn't continuously span the window
        (reconnect) so a truncated buffer can't read as a flat move.
        """
        cur = self.state.price
        if cur <= 0 or not self.covers(window_s):
            return None
        cutoff = time.time() - window_s
        j = nxt = None             # j = last bucket at/before cutoff; nxt = first after
        for ts, p in self._prices:
            if p <= 0:
                continue
            if ts <= cutoff:
                j = (ts, p)
            else:
                nxt = (ts, p)
                break
        if j is None:              # nothing older than the cutoff yet
            return None
        if nxt is None:            # cutoff is past the newest bucket — use it directly
            then = j[1]
        else:                      # interpolate the price at exactly `cutoff`
            span = nxt[0] - j[0]
            frac = 0.0 if span <= 0 else (cutoff - j[0]) / span
            then = j[1] + (nxt[1] - j[1]) * frac
        return cur - then

    def get_taker_ratio(self, window_s: float = 60.0, min_trades: int = 20) -> tuple[float, int]:
        """(buy_fraction, n) over window. Returns (0.5, n) when n < min_trades."""
        cutoff = time.time() - window_s
        buy = total = 0.0
        n = 0
        for ts, sz in reversed(self._trades):
            if ts < cutoff:
                break
            n += 1
            total += abs(sz)
            if sz > 0:
                buy += sz
        if n < min_trades or total <= 0:
            return 0.5, n
        return buy / total, n

    def _prune_trades(self, now: float) -> None:
        cutoff = now - self._trade_buffer_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    async def _connect_ws(self) -> None:
        import websockets

        backoff = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=60, compression=None,
                ) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "coinbase")
                    self.staleness.reset()
                    self.staleness.mark_connected()
                    self._trades.clear()
                    self._prices.clear()
                    self._last_price_sample = 0.0
                    self._window_start = time.time()

                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": [self.product_id],
                        "channels": ["ticker", "heartbeat"],
                    }))

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
                        except asyncio.TimeoutError:
                            logger.warning("Coinbase WS idle >25s, forcing reconnect")
                            break
                        try:
                            data = _loads(msg)
                        except ValueError:
                            continue
                        self._handle_message(data)
                        backoff = RECONNECT_BASE   # healthy DATA — safe to reset (an
                                                   # accept-then-drop server must keep escalating)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                self.staleness.mark_disconnected()
                logger.warning("Coinbase WS error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "heartbeat":
            self.staleness.observe()
            return
        if kind != "ticker" or data.get("product_id") != self.product_id:
            return

        now = time.time()
        self.staleness.observe(now)

        try:
            price = float(data["price"])
        except (KeyError, ValueError, TypeError):
            return
        # float() happily parses "NaN"/"Infinity"; reject non-finite prints so a
        # bad tick can't flow into L1's z = (btc - strike) / vol_scaled.
        if not math.isfinite(price):
            return

        self.state.price = price
        self.state.updated_at = now
        self.price_event.set()

        if now - self._last_price_sample >= 1.0:
            self._prices.append((now, price))
            self._last_price_sample = now
            cutoff = now - self._trade_buffer_s
            while self._prices and self._prices[0][0] < cutoff:
                self._prices.popleft()

        bid = data.get("best_bid")
        ask = data.get("best_ask")
        try:
            if bid is not None:
                self.state.best_bid = float(bid)
            if ask is not None:
                self.state.best_ask = float(ask)
        except (ValueError, TypeError):
            pass

        # Per-trade flow: ticker.side is the aggressor (taker) side.
        side = data.get("side")
        last_size = data.get("last_size")
        if side and last_size is not None:
            try:
                sz = float(last_size)
            except (ValueError, TypeError):
                sz = 0.0
            if sz > 0:
                signed = sz if side == "buy" else -sz
                self._trades.append((now, signed))
                self._prune_trades(now)
