# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes the mathematical probability that BTC finishes above/below the opening strike price, compares that to the market's price, and trades when mispricing exceeds 10%. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the exit threshold.

## Key Architecture Decisions

- **4-layer probability model.** The bot computes P(Up) using four independent signal layers: (1) Student-t CDF with df=4 for fat-tailed Brownian motion: z = (BTC - strike) / (ATR * sqrt(time)), P = t.cdf(z, 4) — captures fat tails that the normal distribution misses, finding edge on underdog positions the market overprices. (2) Regime detection: 1-lag autocorrelation of recent returns. Trending regimes amplify probability, mean-reverting regimes dampen it (±3%). (3) Order flow: book imbalance + trade flow from CLOB WebSocket data. Informed buying/selling pressure leads price movement (±4%). (4) Indicator momentum: RSI, MACD, Stochastic, OBV, VWAP provide a small directional nudge (±4%). The CDF (Layer 1) drives all decisions — layers just nudge. The edge is: model probability - effective market price.
- **Active position management.** Hold to $1 resolution when the model is confident. Exit early (scalp) when holding_edge drops below a fee-aware threshold that accounts for exit costs and time urgency. Same probability model for entry AND exit. Not fixed take-profit/stop-loss — the math decides.
- **Single position at a time.** Full Kelly on the best edge, no capital dilution.
- **One trade per 5-min contract.** After any exit, that contract is blacklisted.
- **Kelly fraction = 0.15.** Conservative for binary outcomes where losses are total.
- **Minimum edge = 10%.** Only trade when model disagrees with market by 10%+. Tunable by learning pipeline (range 5-35%).
- **Signal layer weights.** Layer 1 (Student-t base) provides the core probability — the CDF drives all decisions. Layer 2 (regime) adjusts by ±3% max. Layer 3 (order flow) adjusts by ±4% max. Layer 4 (momentum/indicators) adjusts by ±4% max. Total max layer swing: ±11%. Deliberately small so the CDF must show a direction before layers can push past the 65% confidence gate.
- **Real-time WebSocket + Gamma API for prices.** Primary: CLOB WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) provides real-time book snapshots, price deltas, best bid/ask, last trades, and market resolution events. Trading loop is event-driven — reacts instantly to book changes instead of polling. HTTP fallback: `clob.polymarket.com/book?token_id=TOKEN` if WS disconnected. Gamma API (`gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}`) for contract discovery and `token_id_up`/`token_id_down`. Supplementary: `GET /spread` for liquidity check, `GET /midpoints` for quick price ref, `GET /last-trades-prices` for fill validation. Gamma `outcomePrices` are stale fallback only.
- **NegRisk execution pricing via GET /price.** The raw token book (`GET /book`) only shows direct token orders — in negRisk binary markets the CLOB cross-matches across complementary tokens, so the raw book shows asks at $0.99 on both sides while the real executable price is ~$0.50. **Always use `GET /price?token_id=X&side=BUY` for entry pricing and `side=SELL` for exit pricing.** This is what Polymarket's website shows. Never use raw book best ask/bid for edge calculation.
- **Outcomes are "Up"/"Down".** Contract fields: `price_up`, `price_down`.
- **Binance.US, not Binance.com.** HTTP 451 for US IPs on .com.
- **Strike = BTC price at 5-min window boundary.** Derived from candle buffer, not "first time bot sees the contract."
- **`--mode paper` CLI flag.** Paper mode uses persistent SQLite bankroll with real CLOB order book prices for realistic fill simulation. Fee rates fetched live from `GET /fee-rate?token_id=X` (crypto = 1.8%). Entry fees collected in shares (fewer shares received), exit fees in USDC — matching Polymarket's actual collection method. Prices snapped to market tick size. Min order size enforced from CLOB book. FOK fill semantics (100% fill or reject). Orders capped to 50% of available book depth. **Convex slippage model**: fills are penalized by `fill_pct * impact_factor * (1 + fill_pct)` where `fill_pct = order_size / book_depth`. Cost accelerates as the order walks through deeper price levels — at 50% depth the cost is 50% higher than a linear model, at 100% it is 2x. **Net-edge gate**: after Kelly sizing, estimated slippage is subtracted from edge; the trade is rejected if `net_edge < min_edge`. This prevents trades where execution cost eats the edge. **Price sum gate**: `price_up + price_down` must be in [0.98, 1.02] or the entry is skipped (stale/broken prices). Resolutions ($1/$0) have no slippage. Live mode on polymarket.com uses the py-clob-client SDK for EIP-712 signed CLOB orders. Requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env.

## Project Structure

```
polybot/
  main.py                    # Entry point, trading loop (hold to resolution)
  config/settings.yaml       # ALL tunable parameters
  core/
    binance_feed.py          # WebSocket + candle buffer
    clob_ws.py               # Real-time CLOB WebSocket feed (order books, trades, resolution)
    market_scanner.py        # Gamma API discovery + CLOB HTTP helpers (spread, midpoints, volume)
    signal_engine.py         # Probability model: P(Up) from BTC vs strike + time + vol
    order_flow.py          # Book imbalance + trade flow signal from CLOB data
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all 7, manages weight versions
  execution/
    base.py                  # BaseTrader ABC, TradeResult, FillResult, fee functions
    paper_trader.py          # PaperTrader(BaseTrader) — instant simulated fills
    live_trader.py           # LiveTrader(BaseTrader) — FOK market orders via py-clob-client SDK
    circuit_breaker.py       # Streak-based Kelly reduction (3 losses → half Kelly, 2 wins → restore)
  agents/
    scheduler.py             # Daily learning pipeline
    outcome_reviewer.py      # Logs resolved trades
    counterfactual_tracker.py # Tracks "what if I held?" for scalped positions
    bias_detector.py         # Per-indicator accuracy + counterfactual analysis
    ta_evolver.py            # Recommends weight adjustments
    weight_optimizer.py      # Versions and auto-adopts weights
  brain/
    claude_client.py         # analyze_strategy() for daily pipeline + analyze_market() legacy
  memory/
    outcomes/                # One JSON per trade
    counterfactuals/         # One JSON per scalped trade — what if held to resolution?
    weights/                 # Versioned weight configs
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py                   # Commands: status, positions, history, performance, clear, session, pause/resume
    commands.py              # Formatting helpers
    alerts.py                # Trade alerts, session banners, channel purging
  db/models.py               # SQLite: positions, trade_history, bankroll
  math_engine/
    decision_table.py        # Legacy — not used in main trading loop
    returns.py               # Log returns, gain_pct (arithmetic returns for binary), Sharpe ratio
```

## Config

`polybot/config/settings.yaml`:
- `circuit_breaker.losses_to_reduce:` — 3 (consecutive losses before halving Kelly)
- `circuit_breaker.wins_to_restore:` — 2 (wins at half Kelly before restoring full)
- `math.kelly_fraction:` — 0.15 (fraction of full Kelly)
- `signal.entry_threshold:` — 0.10 (minimum 10% edge to trade)
- `signal.exit_edge_threshold:` — -0.10 (exit when holding edge drops below -10%)
- `signal.min_model_probability:` — 0.65 (skip coin-flip trades — model must be ≥65% confident)
- `signal.momentum_weight:` — 0.04 (max ±4% indicator adjustment, Layer 4)
- `signal.regime_weight:` — 0.03 (max ±3% regime adjustment, Layer 2)
- `signal.flow_weight:` — 0.04 (max ±4% order flow adjustment, Layer 3)
- `signal.student_t_df:` — 4 (degrees of freedom for fat-tailed CDF)
- `signal.weights:` — per-indicator weights for momentum calculation
- `execution.max_concurrent_positions:` — 1 (single position, full focus)
- `execution.max_bankroll_deployed:` — 0.80
- `execution.max_book_fill_pct:` — 0.50 (cap order to 50% of available ask depth — realistic fills)
- `execution.slippage_impact_pct:` — 0.03 (base impact factor for convex slippage model — see below)
- `market.entry_window_seconds:` — 300 (full 5-min window)
- `market.min_time_remaining_seconds:` — 20 (tunable by learning pipeline)
- `market.clob_ws_url:` — `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `market.max_spread:` — 0.10 (skip entry if bid-ask spread > 10%)
- `schedule.trading_start_hour:` — 12 (8 AM EST in UTC)
- `schedule.trading_end_hour:` — 20, `trading_end_minute:` — 30 (4:30 PM EST in UTC)
- `agents.daily_pipeline_hour:` — 20, `daily_pipeline_minute:` — 45 (4:45 PM EST)
- `discord.daily_channel_name:` — "polybot-daily" (end-of-day reports)

## Running

```bash
python -m polybot.main --mode paper   # Paper trading (persistent bankroll across sessions)
python -m polybot.main --mode live    # Live trading (real USDC on Polymarket)
python -m polybot.main                # Defaults to mode in settings.yaml
python -m pytest polybot/tests/       # 334 tests
```

## How the Probability Model Works

```
Strike = BTC price at 5-min window open (from Binance candle at the CONTRACT's
window boundary, derived from slug — not current time). Polymarket resolves using
Chainlink BTC/USD oracle (eventMetadata.priceToBeat/finalPrice from Gamma API),
which can differ from Binance by $20-200. Entry uses Binance (Chainlink not
available during active windows). Resolution always uses Gamma/Chainlink data.
Distance = current BTC price - strike
Vol = ATR (average true range from 1-min candles, period=7)
Time = minutes remaining in the window

LAYER 1 — Fat-tailed base (Student-t CDF, df=4):
  z = distance / (vol * sqrt(time))
  P(Up) = t.cdf(z, df=4)
  
  Why Student-t: BTC 1-min returns have kurtosis ~6-8 (normal=3).
  Normal CDF underestimates reversal probability. When BTC is $50 above
  strike with 2 min left, normal says 85% Up, Student-t says ~78%.
  The difference is edge on the underdog side.

LAYER 2 — Regime detection:
  autocorr = 1-lag autocorrelation of last 10 1-min returns
  If autocorr > 0 (trending): amplify P away from 0.5 (max ±3%)
  If autocorr < 0 (reverting): dampen P toward 0.5

LAYER 3 — Order flow:
  book_imbalance = (bid_depth - ask_depth) / total_depth (across both Up/Down books)
  trade_flow = net_buy_volume / total_volume (from WebSocket last_trade_price events)
  flow_signal = 0.6 * book_imbalance + 0.4 * trade_flow
  Adjustment: flow_signal * 0.04 (max ±4%)
  
  Why: Order flow LEADS price. Informed traders accumulate before CLOB reprices.

LAYER 4 — Indicator momentum:
  Weighted RSI/MACD/Stochastic/OBV/VWAP score * 0.04 (max ±4%)

NEGRISK EXECUTION PRICING:
  price_up = GET /price?token_id=UP&side=BUY   (cross-matched, not raw book)
  price_down = GET /price?token_id=DOWN&side=BUY
  sell_price = GET /price?token_id=TOKEN&side=SELL (for scalp exits)

ENTRY:
  ATR gate: skip if volatility too quiet or too volatile
  Edge = P(Up) - effective_price_up    [or P(Down) - effective_price_down]
  If model_prob < 65%: SKIP (coin-flip filter)
  If edge >= 10%: TRADE, size = Kelly(probability, market_price) * 0.15, capped to 50% of book depth
  Net-edge gate: net_edge = edge - (price * convex_slippage); if net_edge < min_edge: SKIP
  Entry fill: /price quote + convex slippage: fill_pct * impact * (1 + fill_pct)
  If edge < 10%: SKIP

WHILE HOLDING (active position management):
  holding_edge = model_prob_for_our_side - current_market_price_for_our_side
  Fee-aware threshold: base_threshold - exit_fee_cost + time_urgency_bonus
  Time urgency: near expiry (<2 min), threshold relaxes by up to +5%
  If holding_edge ≤ effective_threshold: EXIT (scalp)
  If holding_edge > effective_threshold: HOLD

RESOLUTION (same as before — contract expired):
  Winning side pays $1.00/share, losing side pays $0.00/share.
```

## External APIs

### Binance.US — Live BTC Price Data
All market data comes from Binance.US (not .com — HTTP 451 for US IPs).

| Endpoint | Usage | Auth |
|----------|-------|------|
| `wss://stream.binance.us:9443/ws/btcusdt@kline_1m` | Real-time 1-min candle WebSocket. Drives the entire probability model: BTC price, strike derivation, ATR, all 7 indicators. | None |
| `GET https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=200` | REST backfill on startup to populate candle buffer before WS starts streaming. | None |

### Polymarket CLOB API — Order Book & Market Data
Base URL: `https://clob.polymarket.com` — all endpoints are public, no auth required.

| Endpoint | Usage | Cached | File |
|----------|-------|--------|------|
| `WSS wss://ws-subscriptions-clob.polymarket.com/ws/market` | **Primary price source.** Real-time order book snapshots, price deltas, best bid/ask, last trades, market resolution events. Subscribed with `custom_feature_enabled: true`, `level: 2`, `initial_dump: true`. PING heartbeat every 10s. Handles both dict and array message formats. | In-memory (live) | `core/clob_ws.py` |
| `GET /book?token_id=TOKEN` | Full order book (bids + asks). HTTP fallback when WebSocket is disconnected or book not yet received for a token. Also provides `min_order_size` and `tick_size` in response. | 2s per token | `core/market_scanner.py` |
| `GET /fee-rate?token_id=TOKEN` | Taker fee rate in basis points (e.g., 720 = 7.2% for crypto). Used to compute realistic entry fees (in shares) and exit fees (in USDC). Formula: `fee = feeRate × shares × p × (1-p)`. | 1 hour | `core/market_scanner.py` |
| `GET /tick-size?token_id=TOKEN` | Minimum price increment (e.g., "0.01"). All VWAP fill prices are snapped to tick grid via `snap_to_tick()`. | 1 hour | `core/market_scanner.py` |
| `GET /price?token_id=TOKEN&side=BUY\|SELL` | **Primary execution price.** Returns the real negRisk cross-matched price accounting for complementary token matching. Raw book shows asks at $0.99 but `/price` shows the true executable price (~$0.50). Used for ALL entry and exit pricing. | No cache | `core/market_scanner.py` |
| `GET /spread?token_id=TOKEN` | Bid-ask spread as a string (e.g., "0.04"). Used as entry filter — skip if spread > `max_spread` (default 10%). Checked from WS `best_bid_ask` first, HTTP fallback. | No cache | `core/market_scanner.py` |
| `GET /midpoints?token_ids=T1,T2` | Midpoint price (avg of best bid/ask) per token. Lightweight hold evaluation reference. | No cache | `core/market_scanner.py` |
| `GET /last-trades-prices?token_ids=T1,T2` | Last trade price + side per token (max 500). Logged after fills to validate paper VWAP against actual market trades. | No cache | `core/market_scanner.py` |

### Polymarket Gamma API — Contract Discovery
Base URL: `https://gamma-api.polymarket.com` — public, no auth.

| Endpoint | Usage | Cached | File |
|----------|-------|--------|------|
| `GET /events?slug=btc-updown-5m-{window_ts}` | Discovers active 5-min BTC Up/Down contracts by deterministic slug. Returns event with markets array containing `conditionId`, `outcomes`, `outcomePrices`, `clobTokenIds`, `endDate`, `closed`, `negRisk`. | 5s | `core/market_scanner.py` |

Used for: contract discovery (finding current/next window), resolution detection (`closed=true`, `outcomePrices` near $1/$0), `token_id_up`/`token_id_down` extraction, `seconds_remaining` computation from `endDate`. **Note:** `outcomePrices` from Gamma are stale — never use for edge calculation, only for resolution confirmation and Gamma-only fallback.

### Polymarket Data API — Volume
Base URL: `https://data-api.polymarket.com` — public, no auth.

| Endpoint | Usage | Cached | File |
|----------|-------|--------|------|
| `GET /live-volume?id=EVENT_ID` | Total live trading volume for an event. Used as entry filter to skip dead markets with no recent activity. | No cache | `core/market_scanner.py` |

### Anthropic Claude API — Learning Pipeline
Used by `brain/claude_client.py` via the Anthropic Python SDK.

| Endpoint | Usage | When |
|----------|-------|------|
| `POST /v1/messages` (via SDK) | TAEvolver sends bias analysis + last 75 trades + current config. Claude returns structured JSON: recommended weight adjustments, momentum_weight, min_edge, kelly_fraction, reasoning, findings, risk warnings. Response validated server-side. | Daily at 4:45 PM ET |

Model: `claude-sonnet-4-6`. Falls back to local math if API fails.

### Discord API — Alerts & Control
Used by `discord_bot/bot.py` via discord.py library.

| Feature | Usage |
|---------|-------|
| Trade alerts | Open/close notifications with entry/exit price, PnL, fees, edge |
| Day open/close banners | Session start bankroll, end-of-day P&L summary |
| Commands | `!status`, `!positions`, `!history`, `!performance`, `!pause`, `!resume` |
| Daily reports | Learning pipeline findings posted to `polybot-daily` channel |

### WebSocket Message Types (CLOB)

The `ClobWebSocket` class (`core/clob_ws.py`) handles these event types:

| Event Type | Trigger | State Updated | Fires `book_updated`? |
|------------|---------|---------------|----------------------|
| `book` | Initial dump, after trades | `books[asset_id]` = full snapshot (bids, asks, hash) | Yes |
| `price_change` | Order placed/cancelled | `best_bid_ask[asset_id]` = {best_bid, best_ask, price, size, side} | Yes |
| `best_bid_ask` | Explicit BBA update (custom_feature) | `best_bid_ask[asset_id]` = {best_bid, best_ask, spread} | Yes |
| `last_trade_price` | Trade executed | `last_trade[asset_id]` = {price, size, side} | No |
| `market_resolved` | Contract resolved on-chain | Sets `market_resolved` event | No |
| `tick_size_change` | Market precision update | Logged (unusual mid-session) | No |

Messages can arrive as single JSON objects or JSON arrays (batch). Both formats are handled.

Subscription format:
```json
{
  "assets_ids": ["token_id_up", "token_id_down"],
  "type": "market",
  "initial_dump": true,
  "level": 2,
  "custom_feature_enabled": true
}
```

Dynamic subscribe/unsubscribe via `{"operation": "subscribe"/"unsubscribe", "assets_ids": [...]}`.

## Canonical Paper Trader Dataflow — DO NOT DEVIATE

This is the authoritative execution flow for the paper trader. The live trader MUST preserve the same dataflow shape — same gates, same ordering, same invariants. The only difference is what happens inside `open_trade()`, `close_trade()`, and `resolve_position()` (mock fill vs real CLOB order). Everything upstream and downstream is shared.

### Phase 1: Market Discovery

```
Binance WS (1-min candles)          Gamma API (contract discovery)
        │                                    │
        ▼                                    ▼
200-candle rolling buffer         btc-updown-5m-{window_ts}
        │                           │                    │
        ├── BTC price               ├── token_id_up      ├── token_id_down
        ├── Strike (open @ window)  ├── seconds_remaining └── conditionId
        └── ATR, indicators         │
                                    ▼
                            CLOB WS subscribe(token_up, token_down)
                                    │
                        ┌───────────┴───────────┐
                        ▼                       ▼
                   Book snapshots         Last trade prices
                   Best bid/ask           Resolution events
```

### Phase 2: Signal Generation

```
Candle buffer ──► indicator_engine.compute_all()
                        │
                        ├── RSI (0.20), MACD (0.25), Stochastic (0.20)
                        ├── OBV (0.15), VWAP (0.20)
                        └── weighted sum → momentum_score (±4% max)

CLOB books + trades ──► compute_flow_signal()
                        ├── book_imbalance (60%) + trade_flow (40%, 120s lookback)
                        └── flow_score (±4% max)

Candle closes ──► 1-lag autocorrelation (10 returns)
                        └── regime_adjustment (±3% max)

BTC price, strike, ATR, seconds_remaining
        │
        ▼
Student-t CDF:  z = (btc - strike) / (ATR × √minutes)
                P(Up) = t.cdf(z, df=4)
        │
        ▼
signal_engine.evaluate()
        ├── base_prob = Student-t CDF
        ├── + regime_adjustment
        ├── + flow_adjustment
        ├── + momentum_adjustment
        │         │
        │         ▼ final_prob → edge = |prob - market_price|
        │                        side = Up if prob > 0.5, else Down
        │                        kelly_size = (p*b - q)/b × 0.15
        ▼
   6 ENTRY GATES (all must pass, in order):
        ├── prob ≥ 0.65?              (confidence gate)
        ├── edge ≥ min_edge?          (mispricing gate)
        ├── spread ≤ 0.10?            (liquidity gate)
        ├── book depth ≥ $50?         (depth gate)
        ├── price_up + price_down ∈ [0.98, 1.02]?  (price sanity gate)
        └── seconds_remaining ≥ 20?   (timing gate)
```

### Phase 3: Sizing & Slippage

```
bankroll (from SQLite)
        │
        ▼
raw_size = bankroll × kelly_size × breaker.kelly_multiplier
        │                                    │
        │                            (1.0 normal, 0.5 after 3 losses)
        ▼
CAP CHAIN (in order):
        ├── size < $1.00?             → REJECT
        ├── size > bankroll × 0.80?   → cap to 80%
        └── size > depth × 0.50?      → cap to 50% of ask depth
                │
                ▼
SLIPPAGE (convex model):
  slip = (size/depth) × 0.03 × (1 + size/depth)
        │
        ▼
NET EDGE CHECK:
  net_edge = gross_edge - (price × slip)
  net_edge < min_edge? → REJECT
```

### Phase 4: Execution (BaseTrader.open_trade — shared by paper and live)

```
CLOB REST: GET /price?token_id=X&side=BUY → exec_price
        │
        ▼
exec_price × (1 + slippage) → snap_to_tick(price, tick_size)
        │
        ▼
CLOB REST: GET /fee-rate?token_id=X → fee_rate (default 0.018)
        │
        ▼
FEE CALCULATION (entry — collected in SHARES):
  shares_ordered = size / price
  fee_shares = fee_rate × shares_ordered × price × (1-price) / price
  shares_received = shares_ordered - fee_shares
        │
        ▼
3 REJECTION GATES (in order):
  ├── duplicate market?                → reject
  ├── open positions ≥ max (1)?        → reject
  └── deployed + size > bankroll × 80%? → reject
        │ all pass
        ▼
DB: INSERT positions (entry_price, size, shares_held, fee_rate, snapshot)
DB: UPDATE bankroll = bankroll - size   ← USDC only, fee is in shares
        │
        ▼
TradeResult(success=True, position_id=N)
  → Discord alert + logger
```

### Phase 5: Position Management (while holding)

```
LOOP (every tick, ~1-2ms, event-driven from CLOB WS):
        │
        ▼
Gamma API: seconds_remaining? closed?
        │
        ├── EXPIRED (seconds_remaining ≤ 0 AND closed=True)
        │         │
        │         ▼ RESOLUTION PATH
        │   BTC vs strike → exit_price = $1.00 or $0.00
        │   trader.resolve_position(pos_id, exit_price)
        │     fee = rate × shares × p × (1-p) = $0 at extremes
        │     revenue = shares × exit_price
        │     DB: bankroll += revenue
        │     record_outcome(exit_reason="resolution")
        │
        └── STILL ACTIVE
                  │
                  ▼
        RE-EVALUATE (same 4-layer model, current data):
          signal_engine.evaluate_hold()
            ├── Recompute prob for held side
            ├── holding_edge = model_prob - current_market_price
            ├── effective_threshold = exit_threshold(-10%)
            │     - exit_fee_cost
            │     + time_urgency (ramps in last 120s)
            ▼
          holding_edge > effective_threshold?
            ├── YES → HOLD (do nothing, loop continues)
            └── NO  → SCALP EXIT
                        │
                        ▼
                  GET /price?side=SELL → exit price
                  apply slippage (convex, worse for seller)
                  snap_to_tick()
                        │
                        ▼
                  trader.close_trade(pos_id, exit_fill)
                    EXIT FEE (in USDC):
                      fee = rate × shares × price × (1-price)
                      revenue = shares × exit_price - fee_usdc
                    DB: bankroll += revenue
                    DB: positions.status = 'closed'
                    DB: INSERT trade_history
                    record_outcome(exit_reason="scalp")
                    counterfactual_tracker.watch(pos, scalp_context)
                        │
                        ▼ (contract still has time remaining)
                  Tracker watches until window expires (30s buffer)
                  Then: BTC vs strike → resolution_price ($0 or $1)
                  Computes hypothetical PnL if held vs actual scalp PnL
                  Writes JSON to memory/counterfactuals/
```

### Phase 6: Circuit Breaker Update

```
trade closed → gain_pct = pnl / size
        │
  ┌─────┴─────┐
  │ WIN       │ LOSS
  ▼           ▼
record_win()  record_loss()
  │             ├── consecutive_losses++
  │             └── if ≥ 3 AND not reduced:
  │                   reduced=True → kelly_multiplier=0.5
  │
  ├── consecutive_losses = 0
  └── if reduced AND wins_since_reduction ≥ 2:
        reduced=False → kelly_multiplier=1.0
```

### Phase 7: Outcome → Learning Pipeline

```
outcome JSON → polybot/memory/outcomes/
        │
        ▼ (daily at 4:45 PM ET)
BiasDetector: first 60% of outcomes (training set)
  → per-indicator accuracy, edge calibration, time/vol patterns
  → counterfactual analysis (if data exists): scalp accuracy, missed gains
        │
        ▼
TAEvolver: analysis + last 75 trades + config → Claude Sonnet
  → returns: {weights, min_edge, kelly_fraction, exit_threshold, hours, ...}
        │
        ▼
WeightOptimizer: backtest on last 40% (validation set)
  → new_sharpe ≥ old_sharpe × 1.03?
        ├── YES → hot-swap all params, persist to settings.yaml
        └── NO  → discard recommendations
```

### Paper → Live: What Changes, What Doesn't

**IDENTICAL (zero changes):** Signal engine, indicator engine, order flow, circuit breaker, outcome reviewer, learning pipeline, Discord alerts, DB schema, all entry gates, Kelly sizing, slippage estimation (for pre-trade net-edge gate). Both traders extend `BaseTrader` ABC — rejection gates, fee math, and DB operations are shared code in `base.py`, not duplicated.

**CHANGES (only 3 abstract methods differ between PaperTrader and LiveTrader):**
- `_execute_buy()`: instant fill → FOK market order via `create_market_order` + `post_order(FOK)` with exponential-backoff retry (3 attempts)
- `_execute_sell()`: instant fill → FOK market order via `create_market_order` + `post_order(FOK)` with retry
- `_resolve_bankroll()`: compute `shares * exit_price - fee` → fetch real USDC balance from Polymarket API (auto-credited on resolution)
- Bankroll: fetch real USDC balance on startup, reconcile on resolution
- Slippage: actual VWAP fill price replaces simulation (but convex model still used for pre-trade gate)

**INVARIANTS THAT MUST HOLD IN BOTH MODES (enforced by BaseTrader):**
- Entry fee collected in SHARES (fewer shares received, not extra USDC)
- Exit fee collected in USDC (subtracted from proceeds)
- Bankroll debited by `fill_size` USDC on entry, credited by `revenue` on exit
- All 3 open_trade rejection gates run BEFORE any exchange interaction
- TradeResult and FillResult are the contract boundaries — same shape regardless of mode

## Common Issues

- **No trades:** BTC is near the strike (no edge) or market is efficiently priced. This is correct behavior — no edge means no trade.
- **Binance 451:** Using .com instead of .us.
- **Wrong strike:** Strike for the probability model is derived from Binance candle buffer at the contract's window boundary (parsed from slug). Polymarket resolves using Chainlink oracle, which can differ from Binance by $20-200. Resolution always waits for Gamma API eventMetadata or closed+outcomePrices — never guesses from Binance. If buffer is empty on startup, first few windows may have no strike.
- **All trades losing:** Check if model is systematically miscalibrated. Lower kelly_fraction or raise min_edge.

## Learning Pipeline

Daily at 4:45 PM ET / 20:45 UTC (configurable via `agents.daily_pipeline_hour` and `daily_pipeline_minute`):

**Hold-out split:** Outcomes are sorted chronologically and split 60/40. The first 60% (older trades) are used for analysis and recommendations (steps 1-2). The last 40% (newer trades) are used for backtest validation (step 3). This prevents in-sample overfitting — Claude's recommendations are tested against data it hasn't seen.

**Minimum data requirement:** Claude is instructed to recommend NO CHANGES with fewer than 50 trades (was 20). Win rate variance at N=25 is ±13 percentage points — noise, not signal. The WeightOptimizer also requires at least 10 outcomes to run.

1. **BiasDetector** — Analyzes the **training set** (first 60% of outcomes):
   - Per-indicator accuracy (bullish/bearish breakdown, sample sizes)
   - Side analysis (Up vs Down win rate)
   - Edge calibration (do larger edges actually win more?)
   - Time patterns (win rate by seconds remaining at entry)
   - Volatility patterns (win rate by ATR regime)
   - Overall statistics (Sharpe, win rate, avg edge)

2. **TAEvolver** — Sends training-set analysis + trades + current config to Claude API:
   - System prompt defines Chief Quantitative Strategist role with full 4-layer model description
   - Trade data includes flow_score, exit_reason (scalp vs resolution) for each trade
   - Claude returns structured JSON: weight adjustments, all 4 layer weights (momentum, regime, flow, student_t_df), min_edge, kelly_fraction, reasoning, findings, risk warnings
   - Response validated server-side (weights sum to 1.0, all constraints enforced, new params clamped)
   - Falls back to local math if Claude API fails (resilient)

3. **WeightOptimizer** — Backtests recommendations against the **validation set** (last 40%):
   - Recomputes hypothetical edge with new weights/parameters
   - Auto-adopts if Sharpe improves >= 3%
   - Hot-swaps ALL params at runtime: indicator weights, momentum_weight, regime_weight, flow_weight, student_t_df, min_edge, kelly_fraction, min_model_probability, exit_edge_threshold, min_time_remaining, trading hours
   - **Persists all tuned parameters to settings.yaml** — values survive restarts
   - Discord alerts include Claude's key findings and reasoning

Outcome data enriched with `trade_context` in indicator_snapshot: btc_price, strike_price, seconds_remaining, market prices, model_probability, edge, momentum_score, ATR, size, flow_score, flow_book_imbalance, flow_trade_count. Each outcome also stores `gain_pct` (arithmetic return: pnl/size), `pnl`, and `fees`.

**Performance metrics use `gain_pct` (arithmetic returns), NOT `log_return`.** Log returns are mathematically broken for binary outcomes where exit_price=0 produces log(0)=-infinity. The `gain_pct` metric is bounded [-1, +inf) and gives an honest, positive Sharpe for profitable strategies. The `log_return` field is still stored for backward compatibility but is never used for Sharpe calculation.

## What NOT to Change

- Don't add fixed take-profit/stop-loss percentages — use the probability model for exit decisions (evaluate_hold).
- Don't increase momentum_weight above 0.10 — indicators alone should not trigger trades.
- Don't use normal CDF / logistic approximation — use Student-t CDF (fat tails). The normal distribution underestimates reversal probability for BTC.
- Don't remove complementary pricing — it's essential for seeing real underdog prices in negRisk binary markets.
- Don't increase flow_weight above 0.10 — order flow should nudge, not dominate. CDF drives decisions.
- Don't use `log_return` for Sharpe calculation — use `gain_pct` (arithmetic returns). Log returns are broken for binary outcomes (log(0) = -infinity).
- Don't use raw CLOB book asks/bids for entry/exit pricing — use `GET /price?token_id=X&side=BUY|SELL` for negRisk cross-matched execution prices. Raw book shows $0.99 on both sides; `/price` shows the real ~$0.50 price.
- Don't use Gamma API `outcomePrices` for edge calculation — they're stale/initial prices, not live order book.
- Don't hardcode fee rates — fetch from `GET /fee-rate?token_id=X`. Crypto is 0.072, not 0.05.
- Don't use polymarket.us for crypto — US platform has sports only. All crypto trading is on polymarket.com.
- Don't use Binance.com — use Binance.us.
- Don't allow multiple concurrent positions — one at a time, full Kelly.
- Don't bypass the circuit breaker — it exists to protect bankroll during losing streaks.
- Don't auto-delete the DB — bankroll persists across sessions in both modes. Never delete `polybot/db/polybot.db` between runs.
- Don't use limit orders in LiveTrader — FOK market orders for 5-min contract speed.
- Don't resolve positions by comparing Binance BTC price vs Binance strike — always wait for Gamma API `eventMetadata` or `closed` + `outcomePrices`. Binance and Chainlink (Polymarket's oracle) can disagree by $20-200, causing false WIN/LOSS.
- Don't compute entry strike from `int(now_ts // 300) * 300` — derive from the contract slug. The bot can find the next window's contract early, and current-time flooring gives the wrong window boundary.

## Baseline — LOCKED 2026-04-08

The core trading logic is FROZEN. Do not make structural changes to:
- `signal_engine.py` (4-layer probability model)
- `order_flow.py` (book imbalance + trade flow)
- Entry/exit/pricing logic in `main.py`
- `base.py` (BaseTrader ABC, fee math, shared gates/DB ops)
- `paper_trader.py` / `live_trader.py` (extend BaseTrader — only 3 abstract methods each)

Only the daily learning pipeline (4:45 PM) tunes parameters slowly. Any proposed "improvement" to frozen code requires explicit user approval. New features go in NEW files/modules.

## Always Update

Update this file and README.md with every behavioral change.
