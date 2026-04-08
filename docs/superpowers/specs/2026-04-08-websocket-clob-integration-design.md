# WebSocket CLOB + Lightweight API Integration

**Date:** 2026-04-08
**Goal:** Replace HTTP polling with WebSocket-driven real-time price data for fastest possible paper trade execution. Add lightweight HTTP endpoints for supplementary market intelligence.

## Problem

The trading loop polls `GET /book` via HTTP every 250ms per token — 8 round-trips/sec for Up+Down. This adds ~50-200ms latency per evaluation cycle and wastes rate limit budget. Price changes between polls are invisible.

## Architecture

### New Component: `core/clob_ws.py` — ClobWebSocket

Persistent WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`.

**Responsibilities:**
- Maintain real-time order book state per subscribed token_id
- Signal the trading loop on every book update via `asyncio.Event`
- Handle heartbeat (PING every 10s), reconnection with exponential backoff
- Dynamic subscribe/unsubscribe as 5-min contracts rotate

**Subscription config:**
```json
{
  "assets_ids": ["token_id_up", "token_id_down"],
  "type": "market",
  "initial_dump": true,
  "level": 2,
  "custom_feature_enabled": true
}
```

`custom_feature_enabled: true` gives us `best_bid_ask` and `market_resolved` events for free.

**In-memory state:**
```python
self.books: dict[str, dict]           # token_id -> latest full book
self.best_bid_ask: dict[str, dict]    # token_id -> {best_bid, best_ask, spread}
self.last_trade: dict[str, dict]      # token_id -> {price, size, side}
self.book_updated: asyncio.Event      # set on any book/price_change/best_bid_ask event
self.market_resolved: asyncio.Event   # set on market_resolved event
```

**Event handling:**
| Event Type | Action |
|------------|--------|
| `book` | Replace `self.books[asset_id]` with full snapshot, set `book_updated` |
| `price_change` | Update `best_bid_ask` from delta, set `book_updated` |
| `best_bid_ask` | Update `self.best_bid_ask[asset_id]`, set `book_updated` |
| `last_trade_price` | Update `self.last_trade[asset_id]` |
| `market_resolved` | Set `self.market_resolved` |
| `tick_size_change` | Log warning (mid-session tick change is unusual) |

**Reconnection:**
- On disconnect: exponential backoff starting at 1s, max 30s
- On reconnect: re-subscribe to all active token_ids with `initial_dump: true`
- Track `connected` boolean for fallback logic

**Public interface:**
```python
class ClobWebSocket:
    books: dict[str, dict]
    best_bid_ask: dict[str, dict]
    last_trade: dict[str, dict]
    book_updated: asyncio.Event
    market_resolved: asyncio.Event
    connected: bool

    async def start()                          # launch background task
    async def subscribe(token_ids: list[str])  # add tokens
    async def unsubscribe(token_ids: list[str])# remove tokens
    async def close()                          # clean shutdown
    def get_book(token_id: str) -> dict        # returns book or {}
```

### Modified: `core/market_scanner.py` — Lightweight HTTP Helpers

New static/async methods (all public, no auth):

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `get_spread(token_id, http)` | `GET /spread?token_id=X` | Quick liquidity check — returns spread as float |
| `get_midpoints(token_ids, http)` | `GET /midpoints?token_ids=X,Y` | Lightweight mid-price for hold eval |
| `get_last_trade_prices(token_ids, http)` | `GET /last-trades-prices?token_ids=X,Y` | Fill validation — compare VWAP vs actual trades |
| `get_live_volume(event_id, http)` | `GET data-api.polymarket.com/live-volume?id=X` | Entry filter — skip dead markets |

### Modified: `main.py` — Event-Driven Trading Loop

**Before (polling):**
```python
while True:
    await asyncio.sleep(0.25)
    book_up = await market_scanner.fetch_clob_book(token_up, http)
    book_down = await market_scanner.fetch_clob_book(token_down, http)
    # evaluate...
```

**After (event-driven):**
```python
while True:
    try:
        await asyncio.wait_for(clob_ws.book_updated.wait(), timeout=1.0)
        clob_ws.book_updated.clear()
    except asyncio.TimeoutError:
        pass  # housekeeping tick — contract discovery, day banners

    # Read books from WebSocket state (instant, no HTTP)
    book_up = clob_ws.get_book(contract["token_id_up"])
    book_down = clob_ws.get_book(contract["token_id_down"])

    # Fall back to HTTP if WebSocket disconnected or book stale
    if not book_up and clob_ws.connected:
        book_up = await market_scanner.fetch_clob_book(token_up, http)
    ...
```

**Contract rotation lifecycle:**
1. Contract discovered → `clob_ws.subscribe([token_up, token_down])`
2. Contract expires → `clob_ws.unsubscribe([token_up, token_down])`
3. New contract found → subscribe to new pair
4. `market_resolved` event → trigger resolution immediately (no polling delay)

**Supplementary HTTP calls in the loop:**
- **Entry filter:** `get_live_volume()` once per new contract. Skip if volume is 0.
- **Entry check:** `get_spread()` before placing order. Skip if spread > threshold.
- **Hold eval:** `get_midpoints()` as lightweight alternative when full book isn't needed for the model (the model still uses full book for the actual trade decision).
- **Post-trade logging:** `get_last_trade_prices()` after fills to log VWAP vs actual market.

## Fallback Strategy

WebSocket is primary. HTTP is fallback. The bot must never be unable to trade because the WebSocket is down.

| Scenario | Behavior |
|----------|----------|
| WS connected, book fresh | Use WS book (instant) |
| WS connected, no book for token | HTTP `fetch_clob_book()` |
| WS disconnected | HTTP `fetch_clob_book()` + reconnect in background |
| WS reconnecting | HTTP `fetch_clob_book()` (seamless) |

## Testing Strategy

- Unit tests for ClobWebSocket message parsing (mock WebSocket messages)
- Unit tests for new market_scanner HTTP helpers
- Integration test: mock WS server, verify book state updates + event signaling
- Verify fallback: disconnect WS, confirm HTTP takes over seamlessly

## Files Changed

| File | Change |
|------|--------|
| `core/clob_ws.py` | **New** — ClobWebSocket class |
| `core/market_scanner.py` | Add `get_spread`, `get_midpoints`, `get_last_trade_prices`, `get_live_volume` |
| `main.py` | Event-driven loop, WS lifecycle, supplementary HTTP calls |
| `config/settings.yaml` | Add `clob_ws_url`, `max_spread` settings |
| `tests/test_clob_ws.py` | **New** — WS message parsing and state management tests |
| `tests/test_market_scanner.py` | Tests for new HTTP helpers |
| `CLAUDE.md` | Document WebSocket architecture |
