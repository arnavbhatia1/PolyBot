Step 1: polybot/core/kraken_feed.py
python

Copy
"""
Kraken BTC/USD WebSocket feed.
Kraken is a Chainlink oracle data source and a legitimate US-accessible
price discovery venue. Used as secondary price + Chainlink approximation input.
No auth required.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

KRAKEN_WS_URL = "wss://ws.kraken.com"
KRAKEN_PAIR = "XBT/USD"  # Kraken uses XBT not BTC


@dataclass
class KrakenTick:
    bid: float
    ask: float
    last: float
    vwap_24h: float
    volume_24h: float
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < 10.0


class KrakenFeed:
    """
    WebSocket subscriber for Kraken BTC/USD ticker.
    Reconnects automatically on disconnect.
    Thread-safe latest tick access via self.latest.
    """

    def __init__(self):
        self.latest: Optional[KrakenTick] = None
        self._running = False
        self._reconnect_delay = 2.0
        self._max_reconnect_delay = 60.0

    @property
    def price(self) -> Optional[float]:
        """Last trade price if fresh, else None."""
        if self.latest and self.latest.is_fresh:
            return self.latest.last
        return None

    @property
    def mid(self) -> Optional[float]:
        """Mid price if fresh, else None."""
        if self.latest and self.latest.is_fresh:
            return self.latest.mid
        return None

    async def start(self):
        """Start feed. Call as asyncio task. Runs until stop() called."""
        self._running = True
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect()
                delay = self._reconnect_delay  # reset on success
            except Exception as e:
                logger.warning(f"KrakenFeed error: {e}. Reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def stop(self):
        self._running = False

    async def _connect(self):
        async with websockets.connect(
            KRAKEN_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("KrakenFeed connected")

            # Subscribe to ticker
            await ws.send(json.dumps({
                "event": "subscribe",
                "pair": [KRAKEN_PAIR],
                "subscription": {"name": "ticker"}
            }))

            async for raw in ws:
                if not self._running:
                    break
                await self._handle(raw)

    async def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Kraken sends: [channelID, {ticker_data}, "ticker", "XBT/USD"]
        if not isinstance(msg, list) or len(msg) < 4:
            return
        if msg[-1] != KRAKEN_PAIR or msg[-2] != "ticker":
            return

        data = msg[1]
        try:
            self.latest = KrakenTick(
                bid=float(data["b"][0]),        # best bid
                ask=float(data["a"][0]),        # best ask
                last=float(data["c"][0]),       # last trade price
                vwap_24h=float(data["p"][1]),   # 24h VWAP
                volume_24h=float(data["v"][1]), # 24h volume
            )
        except (KeyError, IndexError, ValueError) as e:
            logger.debug(f"KrakenFeed parse error: {e}")
Step 2: polybot/core/bitstamp_feed.py
python

Copy
"""
Bitstamp BTC/USD WebSocket feed.
Bitstamp is a Chainlink oracle data source. Lower volume than Coinbase/Kraken
but included for accurate Chainlink median approximation.
No auth required.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

BITSTAMP_WS_URL = "wss://ws.bitstamp.net"


@dataclass
class BitstampTick:
    price: float
    buy_price: float    # best bid
    sell_price: float   # best ask
    volume_24h: float
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.buy_price + self.sell_price) / 2.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < 15.0  # Bitstamp slightly slower, 15s threshold


class BitstampFeed:
    """
    WebSocket subscriber for Bitstamp BTC/USD live trades + order book.
    Reconnects automatically on disconnect.
    """

    def __init__(self):
        self.latest: Optional[BitstampTick] = None
        self._running = False
        self._reconnect_delay = 2.0
        self._max_reconnect_delay = 60.0

    @property
    def price(self) -> Optional[float]:
        if self.latest and self.latest.is_fresh:
            return self.latest.price
        return None

    async def start(self):
        self._running = True
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect()
                delay = self._reconnect_delay
            except Exception as e:
                logger.warning(f"BitstampFeed error: {e}. Reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def stop(self):
        self._running = False

    async def _connect(self):
        async with websockets.connect(
            BITSTAMP_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            logger.info("BitstampFeed connected")

            # Subscribe to live trades channel
            await ws.send(json.dumps({
                "event": "bts:subscribe",
                "data": {"channel": "live_trades_btcusd"}
            }))

            # Also subscribe to order book for bid/ask
            await ws.send(json.dumps({
                "event": "bts:subscribe",
                "data": {"channel": "order_book_btcusd"}
            }))

            async for raw in ws:
                if not self._running:
                    break
                await self._handle(raw)

    async def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        event = msg.get("event", "")
        channel = msg.get("channel", "")
        data = msg.get("data", {})

        if event == "trade" and "live_trades" in channel:
            try:
                price = float(data["price"])
                if self.latest:
                    self.latest.price = price
                    self.latest.timestamp = time.time()
                else:
                    self.latest = BitstampTick(
                        price=price,
                        buy_price=price,    # fallback until order book arrives
                        sell_price=price,
                        volume_24h=float(data.get("amount", 0)),
                    )
            except (KeyError, ValueError) as e:
                logger.debug(f"BitstampFeed trade parse error: {e}")

        elif "order_book" in channel and data:
            try:
                bid = float(data["bids"][0][0])
                ask = float(data["asks"][0][0])
                if self.latest:
                    self.latest.buy_price = bid
                    self.latest.sell_price = ask
                    self.latest.timestamp = time.time()
            except (KeyError, IndexError, ValueError) as e:
                logger.debug(f"BitstampFeed book parse error: {e}")
Step 3: polybot/core/chainlink_monitor.py
python

Copy
"""
Direct Chainlink BTC/USD oracle monitor on Polygon.

Subscribes to on-chain AnswerUpdated events from the Chainlink aggregator
contract. Fires a callback the moment a new price is confirmed on-chain.

Why this matters:
- Polymarket resolves BTC contracts using this exact oracle
- Event subscription gives price ~0-2 seconds after on-chain finalization
- Polling chainlink_feed.py via HTTP can lag by 5-30 seconds
- Knowing the EXACT resolution price the moment it's finalized is
  the most accurate possible resolution prediction

Chainlink BTC/USD on Polygon:
  Proxy:      0xc907E116054Ad103354f2D350FD2514433D57F6F
  Heartbeat:  3600 seconds (1 hour)
  Deviation:  0.5%
  Decimals:   8

Requires POLYGON_RPC_WS in .env (free tier from Alchemy or public node).
Falls back to HTTP polling if WebSocket unavailable.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Chainlink BTC/USD Aggregator on Polygon Mainnet
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6F"
CHAINLINK_DECIMALS = 8
CHAINLINK_DEVIATION_THRESHOLD = 0.005   # 0.5% triggers update
CHAINLINK_HEARTBEAT_S = 3600            # 1 hour max staleness

# keccak256("AnswerUpdated(int256,uint256,uint256)")
ANSWER_UPDATED_TOPIC = (
    "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
)

# AggregatorV3Interface ABI — minimal subset needed
AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",           "type": "uint80"},
            {"name": "answer",            "type": "int256"},
            {"name": "startedAt",         "type": "uint256"},
            {"name": "updatedAt",         "type": "uint256"},
            {"name": "answeredInRound",   "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "current",   "type": "int256"},
            {"indexed": True,  "name": "roundId",   "type": "uint256"},
            {"indexed": False, "name": "updatedAt", "type": "uint256"},
        ],
        "name": "AnswerUpdated",
        "type": "event",
    },
]

# Free public Polygon RPC (no key needed, rate-limited but sufficient)
PUBLIC_POLYGON_WS   = "wss://polygon-bor-rpc.publicnode.com"
PUBLIC_POLYGON_HTTP = "https://polygon-rpc.com"


@dataclass
class ChainlinkUpdate:
    price: float        # BTC/USD price (decoded, human-readable)
    round_id: int
    updated_at: int     # Unix timestamp from on-chain
    received_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        """How old the on-chain update itself is."""
        return time.time() - self.updated_at

    @property
    def staleness_fraction(self) -> float:
        """0.0 = just updated, 1.0 = at heartbeat limit."""
        return min(self.age_seconds / CHAINLINK_HEARTBEAT_S, 1.0)


class ChainlinkMonitor:
    """
    Monitors Chainlink BTC/USD on Polygon for live price updates.

    Two modes:
      1. WebSocket event subscription (preferred) — fires on each AnswerUpdated
      2. HTTP polling fallback — polls latestRoundData every 10s

    Usage:
        monitor = ChainlinkMonitor()
        monitor.on_update = my_callback  # called with ChainlinkUpdate
        asyncio.create_task(monitor.start())
    """

    def __init__(self):
        self.latest: Optional[ChainlinkUpdate] = None
        self.on_update: Optional[Callable[[ChainlinkUpdate], None]] = None
        self._running = False

        # RPC endpoints — prefer env vars, fall back to public nodes
        self._ws_url   = os.getenv("POLYGON_RPC_WS",   PUBLIC_POLYGON_WS)
        self._http_url = os.getenv("POLYGON_RPC_HTTP", PUBLIC_POLYGON_HTTP)

    @property
    def price(self) -> Optional[float]:
        """Latest Chainlink price if available."""
        return self.latest.price if self.latest else None

    @property
    def is_stale(self) -> bool:
        """True if Chainlink hasn't updated within the heartbeat window."""
        if not self.latest:
            return True
        return self.latest.age_seconds > CHAINLINK_HEARTBEAT_S

    def seconds_since_update(self) -> float:
        if not self.latest:
            return float("inf")
        return self.latest.age_seconds

    def predicted_update_price(self, synthetic_price: float) -> dict:
        """
        Given current synthetic price, predict if Chainlink will update.

        Returns dict with:
            will_update: bool
            deviation_pct: float
            gap_usd: float (signed — positive = synthetic above Chainlink)
            direction: str
            trigger_distance_usd: float (0 if already triggered)
        """
        if not self.latest:
            return {
                "will_update": False,
                "deviation_pct": 0.0,
                "gap_usd": 0.0,
                "direction": "unknown",
                "trigger_distance_usd": None,
            }

        gap = synthetic_price - self.latest.price
        deviation_pct = abs(gap) / self.latest.price
        will_update = deviation_pct >= CHAINLINK_DEVIATION_THRESHOLD
        trigger_distance = (
            0.0
            if will_update
            else max(
                0.0,
                (CHAINLINK_DEVIATION_THRESHOLD - deviation_pct) * self.latest.price,
            )
        )

        return {
            "will_update": will_update,
            "deviation_pct": deviation_pct,
            "gap_usd": gap,
            "direction": "above" if gap > 0 else "below",
            "trigger_distance_usd": trigger_distance,
        }

    async def start(self):
        """Start monitor. Fetches initial price, then runs WS + poll in parallel."""
        self._running = True
        await self._poll_once()  # seed before WS connects
        await asyncio.gather(
            self._ws_loop(),
            self._poll_loop(),
            return_exceptions=True,
        )

    async def stop(self):
        self._running = False

    # ------------------------------------------------------------------ #
    #  WebSocket event subscription                                        #
    # ------------------------------------------------------------------ #

    async def _ws_loop(self):
        """Subscribe to AnswerUpdated events via eth_subscribe."""
        delay = 2.0
        while self._running:
            try:
                await self._ws_connect()
                delay = 2.0
            except Exception as e:
                logger.warning(
                    f"ChainlinkMonitor WS error: {e}. Retry in {delay}s"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def _ws_connect(self):
        try:
            import websockets as ws_lib
        except ImportError:
            logger.error("websockets not installed — ChainlinkMonitor WS disabled")
            return

        async with ws_lib.connect(self._ws_url, ping_interval=20) as ws:
            logger.info(f"ChainlinkMonitor WS connected: {self._ws_url}")

            # Subscribe to logs from the aggregator contract
            # CORRECTION: use ws.send(json.dumps(...)) — websockets has no send_json()
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": [
                    "logs",
                    {
                        "address": CHAINLINK_BTC_USD_POLYGON,
                        "topics": [ANSWER_UPDATED_TOPIC],
                    },
                ],
            }))

            resp = await ws.recv()
            sub_response = json.loads(resp) if isinstance(resp, str) else resp
            sub_id = sub_response.get("result")
            logger.info(f"ChainlinkMonitor subscribed, id={sub_id}")

            async for raw in ws:
                if not self._running:
                    break
                msg = json.loads(raw) if isinstance(raw, str) else raw
                await self._handle_log(msg)

    async def _handle_log(self, msg: dict):
        """Parse AnswerUpdated log and fire callback."""
        params = msg.get("params", {})
        result = params.get("result", {})
        if not result:
            return

        topics = result.get("topics", [])
        data   = result.get("data", "0x")

        # Need: topics[0]=sig, topics[1]=current(indexed int256),
        #        topics[2]=roundId(indexed uint256), data=updatedAt(non-indexed)
        if len(topics) < 3:
            return

        try:
            # topic[1] = indexed int256 current (raw price units)
            raw_price = int(topics[1], 16)
            # Two's complement for int256 (BTC prices always positive, but be safe)
            if raw_price >= 2**255:
                raw_price -= 2**256

            price_usd = raw_price / (10 ** CHAINLINK_DECIMALS)

            # data contains ABI-encoded updatedAt (single uint256, 32 bytes)
            updated_at = int(data, 16) if data and data != "0x" else int(time.time())

            # round_id from topic[2]
            round_id = int(topics[2], 16)

            update = ChainlinkUpdate(
                price=price_usd,
                round_id=round_id,
                updated_at=updated_at,
            )

            self.latest = update
            logger.info(
                f"ChainlinkMonitor: new price ${price_usd:,.2f} "
                f"round={round_id} on-chain age={update.age_seconds:.1f}s"
            )

            if self.on_update:
                self.on_update(update)

        except Exception as e:
            logger.debug(f"ChainlinkMonitor log parse error: {e}")

    # ------------------------------------------------------------------ #
    #  HTTP polling fallback                                               #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self):
        """Poll latestRoundData every 10s as fallback / sanity check."""
        while self._running:
            await asyncio.sleep(10)
            try:
                await self._poll_once()
            except Exception as e:
                logger.debug(f"ChainlinkMonitor poll error: {e}")

    async def _poll_once(self):
        """Single HTTP call to latestRoundData."""
        try:
            from web3 import Web3
        except ImportError:
            logger.warning(
                "web3 not installed — ChainlinkMonitor HTTP polling disabled. "
                "Run: pip install 'web3>=6.0.0'"
            )
            return

        try:
            w3 = Web3(Web3.HTTPProvider(self._http_url))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(CHAINLINK_BTC_USD_POLYGON),
                abi=AGGREGATOR_ABI,
            )
            (round_id, answer, _started_at, updated_at, _answered_in) = (
                contract.functions.latestRoundData().call()
            )

            price_usd = answer / (10 ** CHAINLINK_DECIMALS)
            update = ChainlinkUpdate(
                price=price_usd,
                round_id=round_id,
                updated_at=updated_at,
            )

            # Only fire callback if this is genuinely a new round
            if not self.latest or round_id > self.latest.round_id:
                self.latest = update
                logger.debug(
                    f"ChainlinkMonitor poll: ${price_usd:,.2f} "
                    f"round={round_id} age={update.age_seconds:.0f}s"
                )
                if self.on_update:
                    self.on_update(update)
            else:
                # Refresh received_at even if round didn't change
                self.latest = update

        except Exception as e:
            logger.debug(f"ChainlinkMonitor._poll_once error: {e}")
Step 4: polybot/core/synthetic_chainlink.py
python

Copy
"""
SyntheticChainlink: Weighted median of exchange feeds to approximate
what the Chainlink oracle will report before it updates on-chain.

Chainlink BTC/USD aggregates from: Coinbase, Kraken, Bitstamp, Binance,
Gemini, and others. This module replicates that aggregation using the
feeds we have access to, giving a prediction of the next Chainlink value
approximately 12-15 seconds before on-chain finalization.

Key behaviors:
  - Weighted median (not mean) — matches Chainlink's aggregation method
  - Staleness gating — excludes feeds older than their threshold
  - Resolution uncertainty scoring — high spread = uncertain resolution
  - Update prediction — will Chainlink update before window closes?
  - Strike computation — better strike estimate when Chainlink is stale

Integration points:
  - chainlink_monitor.py: provides on-chain updates via on_update callback
  - signal_engine.py: uses synthetic price when Chainlink is stale
  - main.py: SyntheticChainlink instance passed to signal_engine
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Chainlink oracle update parameters
DEVIATION_THRESHOLD = 0.005     # 0.5% triggers on-chain update
HEARTBEAT_SECONDS   = 3600      # 1 hour max staleness

# Feed staleness limits — how old a price can be before we exclude it
FEED_STALENESS_LIMITS: Dict[str, float] = {
    "coinbase":   5.0,   # Primary, fast
    "kraken":    10.0,   # Secondary
    "bitstamp":  15.0,   # Slower feed
    "binance_us": 10.0,  # Thin market, kept for coverage
}

# Relative weights for weighted-median calculation.
# Approximate Chainlink's volume-weighted source importance.
# Do NOT need to sum to 1.0.
FEED_WEIGHTS: Dict[str, float] = {
    "coinbase":   0.35,
    "kraken":     0.28,
    "bitstamp":   0.20,
    "binance_us": 0.17,
}


@dataclass
class SyntheticPrice:
    price: float                    # Weighted median price
    confidence: str                 # 'high' / 'medium' / 'low'
    active_feeds: list              # Which feeds contributed
    feed_spread_usd: float          # max - min across active feeds
    feed_spread_pct: float          # feed_spread_usd / price
    will_chainlink_update: bool     # Does synthetic deviate enough to trigger?
    deviation_from_chainlink: float # Fraction distance from last on-chain price
    gap_usd: float                  # Signed dollar gap (positive = above Chainlink)
    trigger_distance_usd: float     # Additional move needed to trigger update
    chainlink_age_s: float          # Seconds since last Chainlink on-chain update
    timestamp: float = field(default_factory=time.time)

    @property
    def resolution_uncertainty(self) -> str:
        """
        HIGH = Chainlink may not update before window close.
        LOW  = Chainlink is current or will update before close.
        """
        if self.feed_spread_usd > 50:
            return "HIGH"
        if self.chainlink_age_s > 1800 and not self.will_chainlink_update:
            return "HIGH"
        if len(self.active_feeds) < 2:
            return "HIGH"
        return "LOW"


class SyntheticChainlink:
    """
    Aggregates multiple exchange feeds into a Chainlink-approximating price.

    Usage:
        synth = SyntheticChainlink()
        synth.update_feed('coinbase', coinbase_feed.price)
        synth.update_feed('kraken', kraken_feed.price)
        synth.update_chainlink(chainlink_monitor.latest.price,
                               chainlink_monitor.latest.updated_at)

        result = synth.compute()
        print(result.price, result.will_chainlink_update)
    """

    def __init__(self):
        self._feeds: Dict[str, dict] = {
            name: {"price": None, "timestamp": 0.0}
            for name in FEED_WEIGHTS
        }
        self._chainlink_price: Optional[float] = None
        self._chainlink_updated_at: float = 0.0

    # ------------------------------------------------------------------ #
    #  Feed update methods                                                 #
    # ------------------------------------------------------------------ #

    def update_feed(self, feed_name: str, price: Optional[float]):
        """Update a single exchange feed price."""
        if feed_name not in self._feeds:
            return
        if price is None or price <= 0:
            return
        self._feeds[feed_name]["price"] = price
        self._feeds[feed_name]["timestamp"] = time.time()

    def update_chainlink(
        self,
        price: Optional[float],
        updated_at: Optional[float] = None,
    ):
        """Update the last known Chainlink on-chain price."""
        if price is None or price <= 0:
            return
        self._chainlink_price = price
        self._chainlink_updated_at = updated_at or time.time()

    # ------------------------------------------------------------------ #
    #  Core computation                                                    #
    # ------------------------------------------------------------------ #

    def compute(self) -> Optional[SyntheticPrice]:
        """
        Compute the synthetic Chainlink price from fresh feeds.
        Returns None if fewer than 2 feeds are fresh.
        """
        now = time.time()

        # Gather fresh feeds only
        fresh = []
        for name, limit in FEED_STALENESS_LIMITS.items():
            feed = self._feeds.get(name)
            if not feed:
                continue
            price = feed["price"]
            age = now - feed["timestamp"]
            if price and price > 0 and age <= limit:
                fresh.append({
                    "name":   name,
                    "price":  price,
                    "weight": FEED_WEIGHTS[name],
                    "age":    age,
                })

        if len(fresh) < 2:
            logger.debug(
                f"SyntheticChainlink: only {len(fresh)} fresh feeds — need >=2"
            )
            return None

        # Weighted median — sort by price, walk cumulative weight to midpoint
        fresh_sorted = sorted(fresh, key=lambda x: x["price"])
        total_weight = sum(f["weight"] for f in fresh_sorted)
        cumulative = 0.0
        synthetic_price = fresh_sorted[-1]["price"]  # fallback: highest
        for f in fresh_sorted:
            cumulative += f["weight"]
            if cumulative >= total_weight / 2.0:
                synthetic_price = f["price"]
                break

        prices = [f["price"] for f in fresh]
        spread_usd = max(prices) - min(prices)
        spread_pct = spread_usd / synthetic_price if synthetic_price > 0 else 0.0

        # Confidence tier
        if len(fresh) >= 3 and spread_usd < 20:
            confidence = "high"
        elif len(fresh) >= 2 and spread_usd < 50:
            confidence = "medium"
        else:
            confidence = "low"

        # Chainlink divergence analysis
        chainlink_age = (
            now - self._chainlink_updated_at
            if self._chainlink_updated_at > 0
            else float("inf")
        )

        if self._chainlink_price and self._chainlink_price > 0:
            gap_usd       = synthetic_price - self._chainlink_price
            deviation_pct = abs(gap_usd) / self._chainlink_price
            will_update   = deviation_pct >= DEVIATION_THRESHOLD
            trigger_distance = (
                0.0
                if will_update
                else max(
                    0.0,
                    (DEVIATION_THRESHOLD - deviation_pct) * self._chainlink_price,
                )
            )
        else:
            gap_usd = deviation_pct = trigger_distance = 0.0
            will_update = False

        return SyntheticPrice(
            price=synthetic_price,
            confidence=confidence,
            active_feeds=[f["name"] for f in fresh],
            feed_spread_usd=spread_usd,
            feed_spread_pct=spread_pct,
            will_chainlink_update=will_update,
            deviation_from_chainlink=deviation_pct,
            gap_usd=gap_usd,
            trigger_distance_usd=trigger_distance,
            chainlink_age_s=chainlink_age,
        )

    def best_strike_price(
        self,
        chainlink_price: Optional[float],
        chainlink_age_s: float,
    ) -> Tuple[Optional[float], str]:
        """
        Return the best available strike price and its source label.

        Priority:
          1. Chainlink on-chain if fresh (<30s) — exact match for resolution
          2. Synthetic if Chainlink stale — best approximation
          3. Chainlink even if stale — better than Coinbase alone
          4. None — caller must handle

        Returns: (price, source_label)
        """
        if chainlink_price and chainlink_age_s < 30:
            return chainlink_price, "chainlink_fresh"

        synthetic = self.compute()
        if synthetic and synthetic.confidence in ("high", "medium"):
            return synthetic.price, f"synthetic_{synthetic.confidence}"

        if chainlink_price:
            return chainlink_price, f"chainlink_stale_{chainlink_age_s:.0f}s"

        return None, "unavailable"

    def should_skip_trade(
        self,
        seconds_remaining: float,
    ) -> Tuple[bool, str]:
        """
        Gate: should we skip this trade due to oracle uncertainty?

        Skip conditions:
          1. Feed spread > $100 (feeds diverging — unusual event in progress)
          2. Fewer than 2 fresh feeds
          3. Chainlink very stale AND synthetic won't trigger AND <60s remaining
        """
        synthetic = self.compute()
        if synthetic is None:
            return True, "insufficient_feeds"

        if synthetic.feed_spread_usd > 100:
            return True, f"feed_divergence_{synthetic.feed_spread_usd:.0f}usd"

        if (
            synthetic.chainlink_age_s > 300
            and not synthetic.will_chainlink_update
            and seconds_remaining < 60
        ):
            return (
                True,
                f"stale_oracle_{synthetic.chainlink_age_s:.0f}s_"
                f"{seconds_remaining:.0f}s_remaining",
            )

        return False, "ok"
Step 5: polybot/core/perpetual_basis.py
python

Copy
"""
L3f: Perpetual basis signal.

The Bybit BTC/USDT perpetual price leads spot by 1-3 seconds during
directional moves. The basis (perp - spot) normalized by ATR is a
leading indicator for where spot price is heading.

Mechanism:
  - Strong move UP: Perpetual premium expands BEFORE spot catches up
  - Strong move DOWN: Perpetual discount before spot falls
  - Near zero basis: No leading signal

This is a WEAK signal (weight 0.03 default) — it nudges probability
in logit space but cannot override the CDF. Its value is timing:
it fires 1-3 seconds before Coinbase reflects the move.

Signal range: [-1.0, +1.0] via tanh normalization.
Positive = perp above spot = bullish pressure on spot.
Negative = perp below spot = bearish pressure on spot.
"""

import math
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# Basis is normalized by ATR to make it scale-invariant.
# When basis / ATR > 1.0, the perpetual is leading by a full ATR unit.
# tanh compresses this to [-1, +1].
BASIS_ATR_SCALE = 1.0    # divisor before tanh
HISTORY_WINDOW  = 20     # rolling window for basis history
MIN_BASIS_HISTORY = 5    # minimum samples before signal is valid


class PerpetualBasisSignal:
    """
    Computes the perpetual basis signal for Layer 3f.

    Feed it Bybit perp price and spot price on each update.
    Call compute(atr) to get the current signal value.

    Usage in signal_engine.py:
        basis_signal = self.perpetual_basis.compute(current_atr)
        if basis_signal is not None:
            logit_p += basis_signal * (basis_weight * logit_scale)
    """

    def __init__(self):
        self._perp_price: Optional[float] = None
        self._spot_price: Optional[float] = None
        self._perp_ts: float = 0.0
        self._spot_ts: float = 0.0
        self._basis_history: deque = deque(maxlen=HISTORY_WINDOW)

    def update_perp(self, price: float):
        """Call when Bybit perp price updates."""
        if price > 0:
            self._perp_price = price
            self._perp_ts = time.time()

    def update_spot(self, price: float):
        """Call when Coinbase (primary spot) price updates."""
        if price > 0:
            self._spot_price = price
            self._spot_ts = time.time()
            # Record current basis whenever spot updates
            if self._perp_price and self._is_fresh():
                self._basis_history.append(self._perp_price - self._spot_price)

    def _is_fresh(self) -> bool:
        """Both feeds must be < 5 seconds old."""
        now = time.time()
        return (
            now - self._perp_ts < 5.0
            and now - self._spot_ts < 5.0
        )

    def compute(self, atr: float) -> Optional[float]:
        """
        Returns basis signal in [-1.0, +1.0] or None if insufficient data.

        Args:
            atr: current ATR value for normalization (must be > 0)

        Returns:
            float in [-1, +1]: positive = perp above spot = bullish
            None: insufficient data or stale feeds
        """
        if not self._is_fresh():
            return None
        if len(self._basis_history) < MIN_BASIS_HISTORY:
            return None
        if atr <= 0:
            return None
        if self._perp_price is None or self._spot_price is None:
            return None

        current_basis = self._perp_price - self._spot_price

        # Normalize by ATR to make scale-invariant across vol regimes
        normalized = current_basis / (atr * BASIS_ATR_SCALE)

        # tanh compresses to [-1, +1]
        return math.tanh(normalized)

    @property
    def raw_basis_usd(self) -> Optional[float]:
        """Raw dollar basis (perp - spot) for logging."""
        if (
            self._perp_price is not None
            and self._spot_price is not None
            and self._is_fresh()
        ):
            return self._perp_price - self._spot_price
        return None

    @property
    def is_available(self) -> bool:
        """True if signal is ready to use."""
        return (
            self._is_fresh()
            and len(self._basis_history) >= MIN_BASIS_HISTORY
            and self._perp_price is not None
            and self._spot_price is not None
        )
Step 6: pip install
bash

Copy
pip install "web3>=6.0.0"
# websockets should already be >=11.0; verify:
pip install "websockets>=11.0"
Step 7: polybot/config/settings.yaml additions
Add these blocks to settings.yaml. Place them after the existing chainlink block:

yaml

Copy
# ── Synthetic Chainlink + Oracle Speed ────────────────────────────────
synthetic_chainlink:
  enabled: true
  min_feeds_required: 2           # skip compute() if fewer fresh feeds
  feed_spread_skip_usd: 100.0     # skip trade if feeds disagree >$100
  stale_chainlink_skip_seconds: 300  # flag HIGH uncertainty after 5min stale
  use_for_strike: true            # replace chainlink_feed.py strike with synthetic

# ── Kraken feed ────────────────────────────────────────────────────────
kraken:
  ws_url: "wss://ws.kraken.com"
  pair: "XBT/USD"
  staleness_limit_s: 10.0

# ── Bitstamp feed ──────────────────────────────────────────────────────
bitstamp:
  ws_url: "wss://ws.bitstamp.net"
  channel_trades: "live_trades_btcusd"
  channel_book: "order_book_btcusd"
  staleness_limit_s: 15.0

# ── Chainlink on-chain monitor ─────────────────────────────────────────
chainlink_monitor:
  enabled: true
  polygon_rpc_ws_env: "POLYGON_RPC_WS"    # env var name, not the value
  polygon_rpc_http_env: "POLYGON_RPC_HTTP"
  poll_interval_s: 10.0
  aggregator_address: "0xc907E116054Ad103354f2D350FD2514433D57F6F"
Also add basis_weight inside the existing signal: block:

yaml

Copy
signal:
  # ... existing keys ...
  basis_weight: 0.03    # L3f perpetual basis — range [0.00, 0.06]
Step 8: .env additions
bash

Copy
# Polygon RPC for Chainlink on-chain monitoring
# Option A — Public node, no key needed (start here):
POLYGON_RPC_WS=wss://polygon-bor-rpc.publicnode.com
POLYGON_RPC_HTTP=https://polygon-rpc.com

# Option B — Alchemy free tier (more reliable):
# POLYGON_RPC_WS=wss://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY
# POLYGON_RPC_HTTP=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# Option C — Infura free tier:
# POLYGON_RPC_WS=wss://polygon-mainnet.infura.io/ws/v3/YOUR_KEY
# POLYGON_RPC_HTTP=https://polygon-mainnet.infura.io/v3/YOUR_KEY
Step 9: signal_engine.py modifications
These are surgical additions — no existing logic changes.

python

Copy
# ── ADD to imports (top of file) ──────────────────────────────────────
from polybot.core.synthetic_chainlink import SyntheticChainlink
from polybot.core.perpetual_basis import PerpetualBasisSignal


# ── ADD to SignalEngine.__init__() parameters ─────────────────────────
# (append to existing parameter list)
def __init__(
    self,
    # ... all existing params unchanged ...
    synthetic_chainlink: SyntheticChainlink = None,
    perpetual_basis: PerpetualBasisSignal = None,
):
    # ... all existing init unchanged ...
    self.synthetic_chainlink = synthetic_chainlink
    self.perpetual_basis = perpetual_basis


# ── ADD get_best_strike() as a new method on SignalEngine ─────────────
def get_best_strike(
    self,
    chainlink_price: Optional[float],
    chainlink_age_s: float,
) -> tuple:
    """
    Get best available strike estimate.
    Delegates to SyntheticChainlink if available, else falls back
    to chainlink_price directly (existing behavior).

    Returns: (price, source_label)
    """
    if self.synthetic_chainlink:
        return self.synthetic_chainlink.best_strike_price(
            chainlink_price, chainlink_age_s
        )
    return chainlink_price, "chainlink_direct"


# ── ADD Layer 3f inside compute_probability() ─────────────────────────
# Insert AFTER Layer 3e (liquidation pressure), BEFORE Platt calibration.
# Find the comment "# ── LAYER 3e" and add immediately after its block:

# ── LAYER 3f: Perpetual basis (leading indicator) ─────────────────────
if self.perpetual_basis and self.perpetual_basis.is_available:
    basis_weight = self.cfg.get("signal.basis_weight", 0.03)
    basis_signal = self.perpetual_basis.compute(atr=current_atr)
    if basis_signal is not None:
        logit_p += basis_signal * (basis_weight * logit_scale)
        signal_contributions["L3f_basis"] = basis_signal * basis_weight
# ── END LAYER 3f ──────────────────────────────────────────────────────


# ── ADD to trade_context dict ─────────────────────────────────────────
# CORRECTION: compute once, not 4 separate times.
# Find where trade_context is built and add:

# Cache the computation — don't call compute() multiple times per trade
_synth = self.synthetic_chainlink.compute() if self.synthetic_chainlink else None

trade_context.update({
    "synthetic_chainlink_price":     _synth.price if _synth else None,
    "chainlink_will_update":         _synth.will_chainlink_update if _synth else None,
    "chainlink_deviation_pct":       _synth.deviation_from_chainlink if _synth else None,
    "feed_spread_usd":               _synth.feed_spread_usd if _synth else None,
    "oracle_resolution_uncertainty": _synth.resolution_uncertainty if _synth else "unknown",
    "perp_basis_usd": (
        self.perpetual_basis.raw_basis_usd
        if self.perpetual_basis else None
    ),
})
Step 10: main.py — _build_signal_engine() modifications
python

Copy
# ── ADD to imports ────────────────────────────────────────────────────
from polybot.core.kraken_feed import KrakenFeed
from polybot.core.bitstamp_feed import BitstampFeed
from polybot.core.chainlink_monitor import ChainlinkMonitor
from polybot.core.synthetic_chainlink import SyntheticChainlink
from polybot.core.perpetual_basis import PerpetualBasisSignal


# ── MODIFY _build_signal_engine() ────────────────────────────────────
# Add after existing feed instantiation, before engine construction:

def _build_signal_engine(cfg):
    # ... all existing feed construction unchanged ...

    # ── NEW: additional spot feeds for Chainlink approximation ────────
    kraken_feed   = KrakenFeed()
    bitstamp_feed = BitstampFeed()

    # ── NEW: Chainlink on-chain monitor ───────────────────────────────
    chainlink_monitor = ChainlinkMonitor()

    # ── NEW: Synthetic Chainlink aggregator ───────────────────────────
    synthetic_chainlink = SyntheticChainlink()

    # Wire ChainlinkMonitor → SyntheticChainlink
    def _on_chainlink_update(update):
        synthetic_chainlink.update_chainlink(update.price, update.updated_at)
        logger.info(
            f"Chainlink on-chain update: ${update.price:,.2f} "
            f"age={update.age_seconds:.1f}s"
        )

    chainlink_monitor.on_update = _on_chainlink_update

    # ── NEW: Perpetual basis signal ───────────────────────────────────
    perp_basis = PerpetualBasisSignal()

    # ── Wire price callbacks into aggregators ─────────────────────────
    # Find your existing Coinbase price-update handler and add:
    #   synthetic_chainlink.update_feed("coinbase", new_price)
    #   perp_basis.update_spot(new_price)
    #
    # Find your existing Binance.US price-update handler and add:
    #   synthetic_chainlink.update_feed("binance_us", new_price)
    #
    # Find your existing Bybit price-update handler and add:
    #   perp_basis.update_perp(new_price)
    #
    # The Kraken and Bitstamp feeds are polled via their .price property
    # in the main loop, OR hook into their internal _handle() to call:
    #   synthetic_chainlink.update_feed("kraken", kraken_feed.price)
    #   synthetic_chainlink.update_feed("bitstamp", bitstamp_feed.price)

    # ── Register new async tasks ──────────────────────────────────────
    # ADD these to your existing task list:
    new_tasks = [
        asyncio.create_task(kraken_feed.start(),   name="kraken_feed"),
        asyncio.create_task(bitstamp_feed.start(), name="bitstamp_feed"),
        asyncio.create_task(chainlink_monitor.start(), name="chainlink_monitor"),
    ]
    # tasks.extend(new_tasks)  — or however your task list is managed

    # ── Build signal engine with new components ───────────────────────
    engine = SignalEngine(
        cfg=cfg,
        # ... all existing params unchanged ...
        synthetic_chainlink=synthetic_chainlink,   # ADD
        perpetual_basis=perp_basis,                # ADD
    )

    return engine  # (and tasks, however your function signature works)
Note on hooking Kraken/Bitstamp into the update loop: The cleanest approach is to add a callback hook to each feed's _handle() method rather than polling .price in the main loop. Add this to KrakenFeed._handle():

python

Copy
# At the end of KrakenFeed._handle(), after self.latest = KrakenTick(...):
if self._on_price_update and self.latest:
    self._on_price_update(self.latest.last)
And in _build_signal_engine():

python

Copy
kraken_feed._on_price_update = lambda p: synthetic_chainlink.update_feed("kraken", p)
bitstamp_feed._on_price_update = lambda p: synthetic_chainlink.update_feed("bitstamp", p)
Add self._on_price_update = None to both feed __init__() methods.

Step 11–12: Verification
bash

Copy
# Run tests
python -m pytest polybot/tests/ -x

# Paper mode smoke test
python -m polybot.main --mode paper
Expected log lines within 30s of startup:


Copy
✓ KrakenFeed connected
✓ BitstampFeed connected
✓ ChainlinkMonitor WS connected: wss://polygon-bor-rpc.publicnode.com
  (OR if WS fails): ChainlinkMonitor poll: $XXXXX round=NNNN age=Xs
Expected in first trade's trade_context:

python

Copy
assert trade_context["synthetic_chainlink_price"] is not None  # e.g. 97432.50
assert trade_context["chainlink_will_update"] is not None      # True or False
assert trade_context["feed_spread_usd"] < 50                   # normal conditions
assert trade_context["oracle_resolution_uncertainty"] in ("HIGH", "LOW")
assert trade_context["perp_basis_usd"] is not None             # e.g. 12.50
Summary of Corrections Made
Spec Issue	Fix Applied
ws.send_json(...) in chainlink_monitor.py	→ await ws.send(json.dumps(...))
import math inside compute()	→ moved to module top
compute() called 4x in trade_context	→ computed once, cached as _synth
Missing None guard on _on_price_update callback	→ added if self._on_price_update check
tuple[float, str] return annotation (Python 3.9+ only)	→ from typing import Tuple + Tuple[Optional[float], str]
