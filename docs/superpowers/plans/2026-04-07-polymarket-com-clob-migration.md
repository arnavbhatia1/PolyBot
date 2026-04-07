# Polymarket.com CLOB Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace dead polymarket.us gateway with polymarket.com CLOB API for real order book prices, so paper trader simulates exactly what live trader will do.

**Architecture:** Each 5-min BTC binary contract has two tokens (Up/Down), each with its own order book on the CLOB at `clob.polymarket.com`. Entry fetches both books via `GET /book?token_id=X` (no auth, public). Paper trader fills against real ask depth (buying) or bid depth (selling). Gamma API remains for contract discovery only.

**Tech Stack:** Python 3, httpx (async HTTP), existing SQLite/aiosqlite persistence

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `polybot/core/market_scanner.py` | Modify | Replace `fetch_us_bbo`/`fetch_us_book` with `fetch_clob_book` and `fetch_clob_prices` |
| `polybot/main.py` | Modify | Wire CLOB prices into entry (line ~362) and hold evaluation (line ~303) |
| `polybot/execution/paper_trader.py` | Keep | No changes - already accepts price from main.py |
| `polybot/execution/polymarket_us.py` | Delete | Dead code - US platform has no crypto markets |
| `polybot/execution/live_trader.py` | Modify | Remove polymarket_us imports (live .com trader is future work) |
| `polybot/config/settings.yaml` | Modify | Add `clob_url`, remove old US/CLOB settings |
| `polybot/tests/test_market_scanner.py` | Modify | New tests for CLOB methods |
| `CLAUDE.md` | Modify | Document .com CLOB usage |

---

### Task 1: Replace US Gateway methods with CLOB API in market_scanner.py

**Files:**
- Modify: `polybot/core/market_scanner.py`
- Test: `polybot/tests/test_market_scanner.py`

- [ ] **Step 1: Write failing tests for `fetch_clob_book`**

```python
# In test_market_scanner.py, add at the end:

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

SAMPLE_CLOB_BOOK = {
    "market": "0xcondition123",
    "asset_id": "token_up_123",
    "bids": [
        {"price": "0.45", "size": "200"},
        {"price": "0.40", "size": "500"},
    ],
    "asks": [
        {"price": "0.55", "size": "150"},
        {"price": "0.60", "size": "300"},
    ],
    "last_trade_price": "0.50",
    "tick_size": "0.01",
}


@pytest.mark.asyncio
async def test_fetch_clob_book_returns_parsed_book():
    scanner = BTCMarketScanner()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = SAMPLE_CLOB_BOOK

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    book = await scanner.fetch_clob_book("token_up_123", http_client=mock_client)
    assert book["asks"][0] == {"price": "0.55", "size": "150"}
    assert book["bids"][0] == {"price": "0.45", "size": "200"}
    assert book["last_trade_price"] == "0.50"
    mock_client.get.assert_called_once()
    assert "token_id=token_up_123" in str(mock_client.get.call_args)


@pytest.mark.asyncio
async def test_fetch_clob_book_returns_empty_on_failure():
    scanner = BTCMarketScanner()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

    book = await scanner.fetch_clob_book("token_up_123", http_client=mock_client)
    assert book == {}


def test_clob_best_ask():
    book = SAMPLE_CLOB_BOOK
    price, depth = BTCMarketScanner.clob_best_ask(book)
    assert price == 0.55
    assert depth == 450.0  # 150 + 300


def test_clob_best_ask_empty():
    assert BTCMarketScanner.clob_best_ask({}) == (0.0, 0.0)
    assert BTCMarketScanner.clob_best_ask({"asks": []}) == (0.0, 0.0)


def test_clob_best_bid():
    book = SAMPLE_CLOB_BOOK
    price, depth = BTCMarketScanner.clob_best_bid(book)
    assert price == 0.45
    assert depth == 700.0  # 200 + 500


def test_clob_walk_asks():
    book = SAMPLE_CLOB_BOOK
    # 100 shares: all fill at 0.55 (first level has 150)
    vwap = BTCMarketScanner.clob_walk_asks(book, 100)
    assert vwap == 0.55

    # 200 shares: 150 @ 0.55 + 50 @ 0.60 = 82.5 + 30 = 112.5 / 200 = 0.5625
    vwap = BTCMarketScanner.clob_walk_asks(book, 200)
    assert abs(vwap - 0.5625) < 0.0001


def test_clob_walk_asks_insufficient():
    book = {"asks": [{"price": "0.55", "size": "10"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 100) == 0.0


def test_clob_walk_bids():
    book = SAMPLE_CLOB_BOOK
    # 100 shares: all fill at 0.45 (first level has 200)
    vwap = BTCMarketScanner.clob_walk_bids(book, 100)
    assert vwap == 0.45
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest polybot/tests/test_market_scanner.py -v -k "clob" 2>&1 | tail -20`
Expected: FAIL — methods don't exist yet

- [ ] **Step 3: Implement CLOB methods in market_scanner.py**

Replace the `US_GATEWAY` constant and all `fetch_us_*` methods with:

```python
# Replace US_GATEWAY line and all US Gateway / CLOB methods (lines ~23 and 108-201) with:

    CLOB_API = "https://clob.polymarket.com"

    # In __init__, replace _bbo_cache with:
    self._book_cache: dict[str, tuple[float, dict]] = {}  # token_id -> (timestamp, book)
    self._book_cache_seconds = 2

    # --- Polymarket.com CLOB (real order book, no auth required) ---

    async def fetch_clob_book(self, token_id: str, http_client=None) -> dict:
        """Fetch order book from polymarket.com CLOB.

        GET /book?token_id=X — no auth, 1500 req/10s limit.
        Returns full book: bids [{price, size}], asks [{price, size}],
        plus last_trade_price, tick_size, min_order_size.
        """
        if not token_id:
            return {}

        now = time.time()
        cached = self._book_cache.get(token_id)
        if cached and (now - cached[0]) < self._book_cache_seconds:
            return cached[1]

        try:
            url = f"{self.CLOB_API}/book"
            if http_client:
                resp = await http_client.get(url, params={"token_id": token_id})
            else:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(url, params={"token_id": token_id})
            resp.raise_for_status()
            book = resp.json()
            self._book_cache[token_id] = (now, book)
            return book
        except Exception as e:
            logger.debug(f"CLOB book fetch failed for {token_id[:20]}...: {e}")
            return {}

    @staticmethod
    def clob_best_ask(book: dict) -> tuple[float, float]:
        """Best ask price and total ask depth in shares. (0, 0) if empty."""
        asks = book.get("asks", [])
        if not asks:
            return 0.0, 0.0
        best = float(asks[0]["price"])
        depth = sum(float(a["size"]) for a in asks)
        return best, depth

    @staticmethod
    def clob_best_bid(book: dict) -> tuple[float, float]:
        """Best bid price and total bid depth in shares. (0, 0) if empty."""
        bids = book.get("bids", [])
        if not bids:
            return 0.0, 0.0
        best = float(bids[0]["price"])
        depth = sum(float(b["size"]) for b in bids)
        return best, depth

    @staticmethod
    def clob_walk_asks(book: dict, shares_needed: float) -> float:
        """Walk ask levels to compute VWAP fill price for buying.
        Returns 0.0 if book can't fill 90%+ of order."""
        asks = book.get("asks", [])
        if not asks or shares_needed <= 0:
            return 0.0
        filled = 0.0
        cost = 0.0
        for level in asks:
            price = float(level["price"])
            size = float(level["size"])
            take = min(size, shares_needed - filled)
            cost += take * price
            filled += take
            if filled >= shares_needed:
                break
        if filled < shares_needed * 0.90:
            return 0.0
        return cost / filled

    @staticmethod
    def clob_walk_bids(book: dict, shares_needed: float) -> float:
        """Walk bid levels to compute VWAP fill price for selling.
        Returns 0.0 if book can't fill 90%+ of order."""
        bids = book.get("bids", [])
        if not bids or shares_needed <= 0:
            return 0.0
        filled = 0.0
        revenue = 0.0
        for level in bids:
            price = float(level["price"])
            size = float(level["size"])
            take = min(size, shares_needed - filled)
            revenue += take * price
            filled += take
            if filled >= shares_needed:
                break
        if filled < shares_needed * 0.90:
            return 0.0
        return revenue / filled
```

Also remove the old `walk_book_levels` static method and `fetch_us_bbo`/`fetch_us_book` methods entirely. Remove `US_GATEWAY` constant. Update `_bbo_cache` references to `_book_cache`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest polybot/tests/test_market_scanner.py -v 2>&1 | tail -30`
Expected: All new `clob_*` tests PASS. Old `walk_book` tests will fail (removed method).

- [ ] **Step 5: Fix old walk_book tests to use new method names**

Replace old `test_walk_book_*` tests:
```python
def test_walk_book_single_level():
    book = {"asks": [{"price": "0.55", "size": "100"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 50) == 0.55

def test_walk_book_multiple_levels():
    book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.60", "size": "100"}]}
    vwap = BTCMarketScanner.clob_walk_asks(book, 150)
    assert abs(vwap - 0.5667) < 0.001

def test_walk_book_insufficient_depth():
    book = {"asks": [{"price": "0.55", "size": "10"}]}
    assert BTCMarketScanner.clob_walk_asks(book, 100) == 0.0

def test_walk_book_empty():
    assert BTCMarketScanner.clob_walk_asks({}, 50) == 0.0
```

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest polybot/tests/ -x -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add polybot/core/market_scanner.py polybot/tests/test_market_scanner.py
git commit -m "feat: replace US gateway with polymarket.com CLOB API for real order book data"
```

---

### Task 2: Wire CLOB prices into main.py entry section

**Files:**
- Modify: `polybot/main.py` (entry section ~lines 362-470)

- [ ] **Step 1: Replace the entry price-fetching block**

Find the current entry section (after `if cid in traded_contracts: continue`) and replace the price-sourcing block with:

```python
            # Fetch real order books from polymarket.com CLOB (no auth, public)
            book_up = await market_scanner.fetch_clob_book(contract["token_id_up"], http_client)
            book_down = await market_scanner.fetch_clob_book(contract["token_id_down"], http_client)

            # Best ask = what you'd pay to buy each side
            ask_up, depth_up = market_scanner.clob_best_ask(book_up)
            ask_down, depth_down = market_scanner.clob_best_ask(book_down)

            if ask_up > 0 and ask_down > 0:
                price_up = ask_up
                price_down = ask_down
                price_source = "clob"
            elif ask_up > 0:
                price_up = ask_up
                price_down = max(0.01, 1.0 - float(book_up.get("bids", [{}])[0].get("price", "0.50")))
                price_source = "clob"
            elif ask_down > 0:
                price_down = ask_down
                price_up = max(0.01, 1.0 - float(book_down.get("bids", [{}])[0].get("price", "0.50")))
                price_source = "clob"
            else:
                # CLOB unavailable — fall back to Gamma (stale but better than nothing)
                price_up = contract["price_up"]
                price_down = contract["price_down"]
                price_source = "gamma"

            # Don't enter if market already decided (extreme prices)
            if price_up < 0.15 or price_up > 0.85:
                continue

            # Skip if no real depth to fill against
            if price_source == "clob":
                min_depth = market_scanner.min_book_depth_usd
                if (depth_up * ask_up < min_depth) and (depth_down * ask_down < min_depth):
                    logger.debug(f"Thin CLOB: Up ${depth_up * ask_up:.0f} Down ${depth_down * ask_down:.0f}")
                    continue
```

- [ ] **Step 2: Replace the fill simulation block (after Kelly sizing)**

Replace the old `fetch_us_book` / `walk_book_levels` block with:

```python
                # Simulate realistic fill by walking CLOB order book
                if price_source == "clob":
                    book = book_up if side == "Up" else book_down
                    shares_needed = size / price
                    vwap = market_scanner.clob_walk_asks(book, shares_needed)
                    if vwap > 0:
                        price = vwap
                    elif vwap == 0 and shares_needed > 0:
                        logger.info(f"SKIP: CLOB can't fill {shares_needed:.0f} shares of {side}")
                        continue
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest polybot/tests/ -x -q`
Expected: All pass (main.py has no direct unit tests, but integration tests should work)

- [ ] **Step 4: Commit**

```bash
git add polybot/main.py
git commit -m "feat: wire polymarket.com CLOB prices into entry evaluation and fill simulation"
```

---

### Task 3: Wire CLOB prices into hold evaluation

**Files:**
- Modify: `polybot/main.py` (hold evaluation ~lines 300-310)

- [ ] **Step 1: Replace hold price-fetching**

Replace the current `fetch_us_bbo` hold section with:

```python
                    # Fetch real CLOB price for hold evaluation
                    hold_token = contract_token_up if pos["side"] == "Up" else contract_token_down
                    # We can't easily get token_id from position alone, so use live contract data
                    if pos["side"] == "Up":
                        hold_book = await market_scanner.fetch_clob_book(
                            live.get("token_id_up", ""), http_client)
                        bid_price, _ = market_scanner.clob_best_bid(hold_book)
                    else:
                        hold_book = await market_scanner.fetch_clob_book(
                            live.get("token_id_down", ""), http_client)
                        bid_price, _ = market_scanner.clob_best_bid(hold_book)

                    gamma_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]
                    # Use CLOB bid if sane, else Gamma fallback
                    market_price = bid_price if 0.01 < bid_price < 0.99 else gamma_price
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest polybot/tests/ -x -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add polybot/main.py
git commit -m "feat: use CLOB bid price for hold/exit evaluation"
```

---

### Task 4: Clean up dead polymarket.us code

**Files:**
- Delete: `polybot/execution/polymarket_us.py`
- Modify: `polybot/execution/live_trader.py`
- Modify: `polybot/main.py`
- Delete tests: remove `polymarket_us` test references

- [ ] **Step 1: Stub out live_trader.py to remove polymarket_us dependency**

The live trader currently imports `PolymarketUSClient`. Since live trading on .com is future work, replace the import with a placeholder:

```python
# In live_trader.py, replace the PolymarketUSClient import and usage:
# Leave the class structure intact but make it clear it needs .com CLOB client (future work)

import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)


class LiveTrader:
    """Live trader — requires polymarket.com CLOB client (future implementation).

    Currently raises NotImplementedError. Paper mode works with CLOB prices.
    """

    def __init__(self, db: Database, **kwargs):
        self.db = db
        raise NotImplementedError(
            "Live trading on polymarket.com requires EIP-712 signed orders. "
            "Use paper mode for now. Live .com trader is future work."
        )
```

- [ ] **Step 2: Remove polymarket_us.py**

Delete `polybot/execution/polymarket_us.py`

- [ ] **Step 3: Update main.py — remove US client creation**

In main.py, remove the `from polybot.execution.polymarket_us import PolymarketUSClient` block and the US client creation in live mode. Replace with:

```python
    # Execution — route based on mode
    exec_cfg = config["execution"]
    if mode == "live":
        # polymarket.com live trading requires EIP-712 CLOB client (future work)
        logger.error("LIVE MODE not yet available for polymarket.com. Use --mode paper.")
        return
    else:
        trader = PaperTrader(db=db, max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
        logger.info("PAPER MODE — simulated trading against polymarket.com CLOB order book")
```

Also remove the `from polybot.execution.live_trader import LiveTrader` import at the top.

- [ ] **Step 4: Remove polymarket_us tests**

Delete or update any tests that import from `polymarket_us`.

Run: `python -m pytest polybot/tests/ -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove dead polymarket.us code, stub live trader for future .com work"
```

---

### Task 5: Update config and documentation

**Files:**
- Modify: `polybot/config/settings.yaml`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update settings.yaml**

Replace old market config:
```yaml
market:
  contract_type: "btc_5min"
  entry_window_seconds: 300
  min_time_remaining_seconds: 20
  scan_cache_seconds: 5
  clob_url: "https://clob.polymarket.com"  # Real order book API (public, no auth)
  min_book_depth_usd: 50  # Skip if ask depth < $50
```

- [ ] **Step 2: Update CLAUDE.md key architecture decisions**

Update the architecture section to reflect:
- polymarket.com CLOB replaces polymarket.us
- Gamma API for discovery, CLOB for prices
- Paper trader fills against real CLOB order book
- Live .com trader is future work (EIP-712 signing needed)
- Each token has its own order book (not a single Long/Short book)

- [ ] **Step 3: Commit**

```bash
git add polybot/config/settings.yaml CLAUDE.md
git commit -m "docs: update config and CLAUDE.md for polymarket.com CLOB migration"
```

---

### Task 6: End-to-end smoke test

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest polybot/tests/ -x -q`
Expected: All pass

- [ ] **Step 2: Manual smoke test — fetch a real CLOB book**

Run:
```bash
python -c "
import asyncio, httpx
async def test():
    import time
    ts = int(time.time() // 300) * 300
    # Get contract from Gamma
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f'https://gamma-api.polymarket.com/events', params={'slug': f'btc-updown-5m-{ts}'})
        data = r.json()
        if not data: print('No contract'); return
        import json
        e = data[0]
        m = e['markets'][0]
        tokens = json.loads(m['clobTokenIds'])
        print(f'Up token: {tokens[0][:30]}...')
        print(f'Down token: {tokens[1][:30]}...')
        # Fetch CLOB books
        for i, label in enumerate(['Up', 'Down']):
            r2 = await c.get('https://clob.polymarket.com/book', params={'token_id': tokens[i]})
            book = r2.json()
            asks = book.get('asks', [])
            bids = book.get('bids', [])
            best_ask = asks[0]['price'] if asks else 'NONE'
            best_bid = bids[0]['price'] if bids else 'NONE'
            print(f'{label}: best_ask={best_ask} best_bid={best_bid} levels={len(asks)}a/{len(bids)}b')
asyncio.run(test())
"
```

Expected: Real bid/ask prices from the CLOB. If spread is 0.01-0.99 (no market makers), that's the truth — paper trader correctly won't trade.

- [ ] **Step 3: Start paper mode and observe**

Run: `python -m polybot.main --mode paper`

Observe logs for:
- `src=clob` in trade entries (real CLOB prices used)
- `src=gamma` only when CLOB is down
- Realistic spread in logged prices (not stale 50/50)
- Trades only when real edge exists against real ask prices

- [ ] **Step 4: Commit any final fixes**

```bash
git add -A
git commit -m "test: verify polymarket.com CLOB integration end-to-end"
```
