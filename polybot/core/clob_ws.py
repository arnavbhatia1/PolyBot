"""Real-time Polymarket CLOB WebSocket feed.

Maintains live order book state per subscribed token_id.
Signals the trading loop via asyncio.Event on every book update
so it can react instantly instead of polling.
"""
import asyncio
import json
import logging
import time

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10  # seconds — Polymarket requires PING every 10s
RECONNECT_BASE = 1       # seconds — exponential backoff start
RECONNECT_MAX = 30       # seconds — backoff cap


class ClobWebSocket:
    def __init__(self, url: str = WS_URL):
        self.url = url

        # Live state — read by trading loop (no lock needed, single event loop)
        self.books: dict[str, dict] = {}              # token_id -> full book snapshot
        self.best_bid_ask: dict[str, dict] = {}       # token_id -> {best_bid, best_ask, spread}
        self.last_trade: dict[str, dict] = {}         # token_id -> {price, size, side}

        # Events — trading loop awaits these
        self.book_updated = asyncio.Event()
        self.market_resolved = asyncio.Event()

        # Connection state
        self.connected = False
        self._ws = None
        self._subscribed_ids: list[str] = []
        self._task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closing = False

    async def start(self):
        """Launch the WebSocket connection as a background task."""
        self._closing = False
        self._task = asyncio.create_task(self._run_forever())

    async def close(self):
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

    async def subscribe(self, token_ids: list[str]):
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
                logger.info(f"WS subscribed to {len(new_ids)} tokens")
            except Exception as e:
                logger.warning(f"WS subscribe send failed: {e}")

    async def unsubscribe(self, token_ids: list[str]):
        """Unsubscribe from token_ids and clear their state."""
        ids_to_remove = [t for t in token_ids if t in self._subscribed_ids]
        if not ids_to_remove:
            return
        for t in ids_to_remove:
            self._subscribed_ids.remove(t)
            self.books.pop(t, None)
            self.best_bid_ask.pop(t, None)
            self.last_trade.pop(t, None)
        if self._ws and self.connected:
            msg = json.dumps({
                "operation": "unsubscribe",
                "assets_ids": ids_to_remove,
            })
            try:
                await self._ws.send(msg)
            except Exception:
                pass

    def get_book(self, token_id: str) -> dict:
        """Return latest book for token_id, or {} if not available."""
        return self.books.get(token_id, {})

    # --- Internal ---

    async def _run_forever(self):
        """Connect and reconnect loop with exponential backoff."""
        backoff = RECONNECT_BASE
        while not self._closing:
            try:
                async with websockets.connect(self.url, ping_interval=None) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = RECONNECT_BASE
                    logger.info("CLOB WebSocket connected")

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

    async def _heartbeat(self, ws):
        """Send PING every 10s to keep connection alive."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    def _handle_message(self, raw: str):
        """Parse and dispatch a WebSocket message. Non-async for speed."""
        if raw == "PONG":
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

    def _dispatch(self, msg: dict):
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
            logger.info(f"Tick size changed: {msg.get('old_tick_size')} -> {msg.get('new_tick_size')} for {msg.get('asset_id')}")

    def _on_book(self, msg: dict):
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
        }
        self.book_updated.set()

    def _on_price_change(self, msg: dict):
        """Delta update — update best_bid_ask from price_changes."""
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
            }
        self.book_updated.set()

    def _on_best_bid_ask(self, msg: dict):
        """Explicit best bid/ask event (custom_feature_enabled)."""
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return
        self.best_bid_ask[asset_id] = {
            "best_bid": msg.get("best_bid", "0"),
            "best_ask": msg.get("best_ask", "0"),
            "spread": msg.get("spread", "0"),
        }
        self.book_updated.set()

    def _on_last_trade(self, msg: dict):
        """Last trade price — for fill validation logging."""
        asset_id = msg.get("asset_id", "")
        if not asset_id:
            return
        self.last_trade[asset_id] = {
            "price": msg.get("price", "0"),
            "size": msg.get("size", "0"),
            "side": msg.get("side", ""),
        }
