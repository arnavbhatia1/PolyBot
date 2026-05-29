"""Real-time Polymarket CLOB WebSocket feed.

Live order book + last-trade buffers per subscribed token_id. The trading loop
awaits ``book_updated`` so it reacts on every book delta instead of polling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

import websockets

from polybot.feeds._socket import enable_nodelay
from polybot.feeds._staleness import StalenessTracker
from polybot.feeds._json import loads as _loads

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10            # PING cadence (Polymarket spec)
HEARTBEAT_TIMEOUT = 25             # Reconnect if no PONG within 2.5× interval
RECONNECT_BASE = 1
RECONNECT_MAX = 30
TRADE_BUFFER_MAXLEN = 500          # ≥120s of trades at peak Polymarket BTC rate
FRESHNESS_S = 10.0                 # Default per-token freshness gate


class ClobWebSocket:
    def __init__(self, url: str = WS_URL) -> None:
        self.url: str = url

        # Live state per token_id
        self.books: dict[str, dict[str, Any]] = {}
        self.best_bid_ask: dict[str, dict[str, Any]] = {}
        self.last_trade: dict[str, dict[str, Any]] = {}
        self.trade_buffer: dict[str, deque[dict[str, Any]]] = {}
        self._trade_events: dict[str, asyncio.Event] = {}

        self.book_updated: asyncio.Event = asyncio.Event()
        self.market_resolved: asyncio.Event = asyncio.Event()

        self.connected: bool = False
        self._ws: Any = None
        self._subscribed_ids: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closing: bool = False
        self._last_pong_ts: float = 0.0
        self.staleness = StalenessTracker("clob_ws")

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run_forever())

    async def close(self) -> None:
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
            except Exception as e:
                logger.warning("WS subscribe send failed: %s", e)

    async def unsubscribe(self, token_ids: list[str]) -> None:
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
            try:
                await self._ws.send(json.dumps({
                    "operation": "unsubscribe", "assets_ids": ids_to_remove,
                }))
            except Exception:
                pass

    def get_book(self, token_id: str) -> dict[str, Any]:
        return self.books.get(token_id, {})

    def get_trade_history(self, token_id: str) -> list[dict[str, Any]]:
        return list(self.trade_buffer.get(token_id, []))

    def trades_since(self, token_id: str, since_ts: float) -> list[dict[str, Any]]:
        buf = self.trade_buffer.get(token_id)
        if not buf:
            return []
        return [t for t in buf if t.get("timestamp", 0.0) >= since_ts]

    def book_fresh(self, token_id: str, max_age_s: float = FRESHNESS_S) -> bool:
        """True iff the book snapshot for token_id was updated within max_age_s."""
        book = self.books.get(token_id)
        if not book:
            return False
        ts = float(book.get("ts", 0) or 0)
        return ts > 0 and (time.time() - ts) <= max_age_s

    def both_books_fresh(self, token_a: str, token_b: str, max_age_s: float = FRESHNESS_S) -> bool:
        """No-arb sanity gate companion: both sides current before checking price_sum."""
        return self.book_fresh(token_a, max_age_s) and self.book_fresh(token_b, max_age_s)

    def trade_event_for(self, token_id: str) -> asyncio.Event:
        ev = self._trade_events.get(token_id)
        if ev is None:
            ev = asyncio.Event()
            self._trade_events[token_id] = ev
        return ev

    # --- internals ---

    def _reset_per_token_state(self) -> None:
        """Clear all per-token rolling buffers. Called on every reconnect so
        windowed analytics (trade flow, VWAP) don't bridge the gap."""
        self.books.clear()
        self.best_bid_ask.clear()
        self.last_trade.clear()
        self.trade_buffer.clear()

    async def _run_forever(self) -> None:
        # Server closes idle un-subscribed connections within ~10s, so wait.
        while not self._closing and not self._subscribed_ids:
            await asyncio.sleep(0.5)
        if self._closing:
            return

        backoff = RECONNECT_BASE
        while not self._closing:
            try:
                async with websockets.connect(self.url, ping_interval=None, compression=None) as ws:
                    self._ws = ws
                    enable_nodelay(ws, "clob_ws")
                    self.connected = True
                    self.staleness.mark_connected()
                    backoff = RECONNECT_BASE
                    self.staleness.reset()
                    self._reset_per_token_state()

                    if self._subscribed_ids:
                        await ws.send(json.dumps({
                            "assets_ids": self._subscribed_ids,
                            "type": "market",
                            "initial_dump": True,
                            "level": 2,
                            "custom_feature_enabled": True,
                        }))

                    self._heartbeat_task = asyncio.create_task(self._heartbeat(ws))

                    async for raw in ws:
                        if self._closing:
                            break
                        self._handle_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                self.staleness.mark_disconnected()
                self._ws = None
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                if not self._closing:
                    logger.debug("CLOB WS disconnected: %s — reconnecting in %ds", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)

        self.connected = False
        self._ws = None

    async def _heartbeat(self, ws: Any) -> None:
        self._last_pong_ts = time.time()
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
                if time.time() - self._last_pong_ts > HEARTBEAT_TIMEOUT:
                    logger.warning("CLOB WS: no PONG in %.0fs — forcing reconnect",
                                   time.time() - self._last_pong_ts)
                    await ws.close()
                    return
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    def _handle_message(self, raw: str) -> None:
        if raw == "PONG":
            self._last_pong_ts = time.time()
            return
        try:
            msg = _loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        self.staleness.observe()
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._dispatch(item)
            return
        if isinstance(msg, dict):
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
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
            logger.debug("Tick size changed: %s -> %s for %s",
                         msg.get("old_tick_size"), msg.get("new_tick_size"), msg.get("asset_id"))

    def _on_book(self, msg: dict[str, Any]) -> None:
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
        self.book_updated.set()

    def _on_best_bid_ask(self, msg: dict[str, Any]) -> None:
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
        buf = self.trade_buffer.get(asset_id)
        if buf is None:
            buf = deque(maxlen=TRADE_BUFFER_MAXLEN)
            self.trade_buffer[asset_id] = buf
        buf.append(trade)
        ev = self._trade_events.get(asset_id)
        if ev is not None:
            ev.set()
