"""Real-time Polymarket CLOB WebSocket feed.

Maintains live order book state per subscribed token_id.
Signals the trading loop via asyncio.Event on every book update
so it can react instantly instead of polling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections import deque
from typing import Any

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10  # seconds — Polymarket requires PING every 10s
HEARTBEAT_TIMEOUT = 25   # seconds — force reconnect if no PONG within 2.5× interval
RECONNECT_BASE = 1       # seconds — exponential backoff start
RECONNECT_MAX = 30       # seconds — backoff cap


class ClobWebSocket:
    def __init__(self, url: str = WS_URL) -> None:
        self.url: str = url

        # Live state — read by trading loop (no lock needed, single event loop)
        self.books: dict[str, dict[str, Any]] = {}              # token_id -> full book snapshot
        self.best_bid_ask: dict[str, dict[str, str]] = {}       # token_id -> {best_bid, best_ask, spread}
        self.last_trade: dict[str, dict[str, Any]] = {}         # token_id -> {price, size, side}
        self.trade_buffer: dict[str, deque[dict[str, Any]]] = {}     # token_id -> deque of recent trades
        self._price_samples: dict[str, deque[tuple[float, float]]] = {}  # token_id -> deque of (timestamp, midprice)
        self._trade_events: dict[str, asyncio.Event] = {}       # token_id -> Event fired on each trade

        # Events — trading loop awaits these
        self.book_updated: asyncio.Event = asyncio.Event()
        self.market_resolved: asyncio.Event = asyncio.Event()

        # Connection state
        self.connected: bool = False
        self._ws: Any = None
        self._subscribed_ids: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closing: bool = False
        self._last_pong_ts: float = 0.0

    async def start(self) -> None:
        """Launch the WebSocket connection as a background task."""
        self._closing = False
        self._task = asyncio.create_task(self._run_forever())

    async def close(self) -> None:
        """Cleanly shut down the WebSocket."""
        self._closing = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def subscribe(self, token_ids: list[str]) -> None:
        """Subscribe to token_ids. Safe to call before or after connection."""
        new_ids = [t for t in token_ids if t and t not in self._subscribed_ids]
        if not new_ids:
            return
        self._subscribed_ids.extend(new_ids)
        if self._ws and self.connected:
            msg = json.dumps({
                "operation": "subscribe",
                "assets_ids": new_ids,
                "level": 2,
                "custom_feature_enabled": True,
            })
            try:
                await self._ws.send(msg)
                logger.debug(f"WS subscribed to {len(new_ids)} tokens")
            except Exception as e:
                logger.warning(f"WS subscribe send failed: {e}")

    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from token_ids and clear their state."""
        ids_to_remove = [t for t in token_ids if t in self._subscribed_ids]
        if not ids_to_remove:
            return
        for t in ids_to_remove:
            self._subscribed_ids.remove(t)
            self.books.pop(t, None)
            self.best_bid_ask.pop(t, None)
            self.last_trade.pop(t, None)
            self.trade_buffer.pop(t, None)
        if self._ws and self.connected:
            msg = json.dumps({
                "operation": "unsubscribe",
                "assets_ids": ids_to_remove,
            })
            try:
                await self._ws.send(msg)
            except Exception:
                pass

    def get_book(self, token_id: str) -> dict[str, Any]:
        """Return latest book for token_id, or {} if not available."""
        return self.books.get(token_id, {})

    def get_trade_history(self, token_id: str) -> list[dict[str, Any]]:
        """Return list of recent trades for a token, oldest first."""
        return list(self.trade_buffer.get(token_id, []))

    def trades_since(self, token_id: str, since_ts: float) -> list[dict[str, Any]]:
        """Return trades for token_id with timestamp >= since_ts (epoch seconds).

        Used by LiveTrader to derive fill VWAP from WS trade events instead
        of issuing a second REST balance read. Each trade dict carries
        ``price`` (str), ``size`` (str), ``side`` (str), ``timestamp`` (float).
        """
        buf = self.trade_buffer.get(token_id)
        if not buf:
            return []
        return [t for t in buf if t.get("timestamp", 0.0) >= since_ts]

    # --- Internal ---

    async def _run_forever(self) -> None:
        """Connect and reconnect loop with exponential backoff."""
        # Wait for at least one subscription before opening the connection —
        # Polymarket's server closes idle unsubscribed connections within ~10s.
        while not self._closing and not self._subscribed_ids:
            await asyncio.sleep(0.5)
        if self._closing:
            return

        backoff = RECONNECT_BASE
        while not self._closing:
            try:
                async with websockets.connect(self.url, ping_interval=None, compression=None) as ws:
                    self._ws = ws
                    _sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None
                    if _sock is not None:
                        try: _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except Exception: pass
                    self.connected = True
                    backoff = RECONNECT_BASE
                    logger.debug("CLOB WebSocket connected")

                    # Send initial subscription
                    if self._subscribed_ids:
                        init_msg = json.dumps({
                            "assets_ids": self._subscribed_ids,
                            "type": "market",
                            "initial_dump": True,
                            "level": 2,
                            "custom_feature_enabled": True,
                        })
                        await ws.send(init_msg)

                    # Start heartbeat
                    self._heartbeat_task = asyncio.create_task(self._heartbeat(ws))

                    # Read messages
                    async for raw in ws:
                        if self._closing:
                            break
                        self._handle_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                self._ws = None
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                if not self._closing:
                    logger.warning(f"CLOB WS disconnected: {e} — reconnecting in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)

        self.connected = False
        self._ws = None

    async def _heartbeat(self, ws: Any) -> None:
        """Send PING every interval and detect dead connections via missing PONGs.

        Polymarket's server can stop responding while the TCP socket stays
        nominally open, leaving the reader stuck on a half-dead connection.
        Tracking _last_pong_ts and closing the socket when it goes stale forces
        _run_forever's reconnect path to kick in instead of waiting forever
        for data that won't arrive.
        """
        self._last_pong_ts = time.time()
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
                if time.time() - self._last_pong_ts > HEARTBEAT_TIMEOUT:
                    logger.warning(
                        "CLOB WS: no PONG in %.0fs — forcing reconnect",
                        time.time() - self._last_pong_ts,
                    )
                    await ws.close()
                    return
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    def _handle_message(self, raw: str) -> None:
        """Parse and dispatch a WebSocket message. Non-async for speed."""
        if raw == "PONG":
            self._last_pong_ts = time.time()
            return

        try:
            msg = json.loads(raw)
            # Polymarket can send arrays (e.g., batch book snapshots) — handle each element
            if isinstance(msg, list):
                for item in msg:
                    if isinstance(item, dict):
                        self._dispatch(item)
                return
        except json.JSONDecodeError:
            return

        if isinstance(msg, dict):
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route a single parsed message to the appropriate handler."""
        event_type = msg.get("event_type", "")

        if event_type == "book":
            self._on_book(msg)
        elif event_type == "price_change":
            self._on_price_change(msg)
        elif event_type == "best_bid_ask":
            self._on_best_bid_ask(msg)
        elif event_type == "last_trade_price":
            self._on_last_trade(msg)
        elif event_type == "market_resolved":
            self.market_resolved.set()
        elif event_type == "tick_size_change":
            logger.debug(f"Tick size changed: {msg.get('old_tick_size')} -> {msg.get('new_tick_size')} for {msg.get('asset_id')}")

    def _on_book(self, msg: dict[str, Any]) -> None:
        """Full book snapshot — replace entire book state."""
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return
        self.books[asset_id] = {
            "bids": msg.get("bids", []),
            "asks": msg.get("asks", []),
            "hash": msg.get("hash", ""),
            "timestamp": msg.get("timestamp", ""),
            "market": msg.get("market", ""),
            "ts": time.time(),
        }
        self.book_updated.set()

    def _on_price_change(self, msg: dict[str, Any]) -> None:
        """Delta update — update best_bid_ask from price_changes."""
        now = time.time()
        for change in msg.get("price_changes", []):
            asset_id = change.get("asset_id", "")
            if not asset_id:
                continue
            self.best_bid_ask[asset_id] = {
                "best_bid": change.get("best_bid", "0"),
                "best_ask": change.get("best_ask", "0"),
                "price": change.get("price", "0"),
                "size": change.get("size", "0"),
                "side": change.get("side", ""),
                "ts": now,
            }
            # Record price sample for velocity tracking
            try:
                bid = float(change.get("best_bid", "0"))
                ask = float(change.get("best_ask", "0"))
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    if asset_id not in self._price_samples:
                        self._price_samples[asset_id] = deque(maxlen=200)
                    self._price_samples[asset_id].append((now, mid))
            except (ValueError, TypeError):
                pass
        self.book_updated.set()

    def _on_best_bid_ask(self, msg: dict[str, Any]) -> None:
        """Explicit best bid/ask event (custom_feature_enabled)."""
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return
        self.best_bid_ask[asset_id] = {
            "best_bid": msg.get("best_bid", "0"),
            "best_ask": msg.get("best_ask", "0"),
            "spread": msg.get("spread", "0"),
            "ts": time.time(),
        }
        self.book_updated.set()

    def _on_last_trade(self, msg: dict[str, Any]) -> None:
        """Last trade price — for fill validation logging and trade flow signal."""
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return
        trade = {
            "price": msg.get("price", "0"),
            "size": msg.get("size", "0"),
            "side": msg.get("side", ""),
            "timestamp": time.time(),
        }
        self.last_trade[asset_id] = trade
        if asset_id not in self.trade_buffer:
            self.trade_buffer[asset_id] = deque(maxlen=100)
        self.trade_buffer[asset_id].append(trade)
        ev = self._trade_events.get(asset_id)
        if ev is not None:
            ev.set()

    def trade_event_for(self, token_id: str) -> asyncio.Event:
        """Return an asyncio.Event for ``token_id`` that fires on every trade
        message for that token. Lazily created — the caller must ``.clear()``
        before awaiting the next fire.
        """
        ev = self._trade_events.get(token_id)
        if ev is None:
            ev = asyncio.Event()
            self._trade_events[token_id] = ev
        return ev
