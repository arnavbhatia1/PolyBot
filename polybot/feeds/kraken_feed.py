"""Kraken BTC/USD WebSocket feed.

Kraken is a Chainlink oracle data source — one of the exchanges Chainlink
aggregates to compute its BTC/USD price. By tracking Kraken alongside
Coinbase, the bot has a better approximation of what Chainlink will report.

No authentication required. Uses XBT/USD (Kraken's BTC ticker symbol).
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.kraken.com"
KRAKEN_PAIR = "XBT/USD"
RECONNECT_BASE = 2
RECONNECT_MAX = 60


@dataclass
class KrakenState:
    """Live state from Kraken ticker."""
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    updated_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        if self.updated_at == 0:
            return float("inf")
        return time.time() - self.updated_at

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return self.ask - self.bid
        return 0.0


class KrakenFeed:
    """WebSocket consumer for Kraken BTC/USD (XBT/USD) ticker.

    Subscribes to the public ticker channel. Fires on every trade.
    Provides real-time last price and best bid/ask.
    Reconnects automatically with exponential backoff.
    """

    def __init__(self, ws_url: str = WS_URL) -> None:
        self.ws_url = ws_url
        self.state = KrakenState()
        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connect_ws())
        logger.debug("KrakenFeed starting for %s", KRAKEN_PAIR)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connect_ws(self) -> None:
        import websockets

        backoff = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=30, compression=None) as ws:
                    self._ws = ws
                    _sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None
                    if _sock is not None:
                        try: _sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except Exception: pass
                    backoff = RECONNECT_BASE

                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": [KRAKEN_PAIR],
                        "subscription": {"name": "ticker"},
                    }))
                    logger.debug("Kraken WebSocket connected, subscribed to %s", KRAKEN_PAIR)

                    # XBT/USD ticker fires on every trade; >30s silence is a dead stream.
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=90.0)
                        except asyncio.TimeoutError:
                            logger.warning("Kraken WS idle >90s, forcing reconnect")
                            break
                        self._handle_message(json.loads(msg))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Kraken WebSocket error: %s, reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle_message(self, msg: Any) -> None:
        """Parse Kraken ticker message.

        Kraken sends: [channelID, {ticker_data}, "ticker", "XBT/USD"]
        ticker_data keys: a=[ask,wholeLotVol,lotVol], b=[bid,...], c=[close,lotVol], ...
        """
        if not isinstance(msg, list) or len(msg) < 4:
            return
        if msg[-1] != KRAKEN_PAIR or msg[-2] != "ticker":
            return

        data = msg[1]
        now = time.time()
        try:
            self.state.price = float(data["c"][0])      # last trade price
            self.state.bid = float(data["b"][0])         # best bid
            self.state.ask = float(data["a"][0])         # best ask
            self.state.updated_at = now
        except (KeyError, IndexError, ValueError, TypeError) as e:
            logger.debug("KrakenFeed parse error: %s", e)
