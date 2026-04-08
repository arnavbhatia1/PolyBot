# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes the mathematical probability that BTC finishes above/below the opening strike price, compares that to the market's price, and trades when mispricing exceeds 10%. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the exit threshold.

## Key Architecture Decisions

- **4-layer probability model.** The bot computes P(Up) using four independent signal layers: (1) Student-t CDF with df=4 for fat-tailed Brownian motion: z = (BTC - strike) / (ATR * sqrt(time)), P = t.cdf(z, 4) — captures fat tails that the normal distribution misses, finding edge on underdog positions the market overprices. (2) Regime detection: 1-lag autocorrelation of recent returns. Trending regimes amplify probability, mean-reverting regimes dampen it (±5%). (3) Order flow: book imbalance + trade flow from CLOB WebSocket data. Informed buying/selling pressure leads price movement (±6%). (4) Indicator momentum: RSI, MACD, Stochastic, OBV, VWAP provide a small directional nudge (±4%). The edge is: model probability - effective market price.
- **Active position management.** Hold to $1 resolution when the model is confident. Exit early (scalp) when holding_edge drops below a fee-aware threshold that accounts for exit costs and time urgency. Same probability model for entry AND exit. Not fixed take-profit/stop-loss — the math decides.
- **Single position at a time.** Full Kelly on the best edge, no capital dilution.
- **One trade per 5-min contract.** After any exit, that contract is blacklisted.
- **Kelly fraction = 0.15.** Conservative for binary outcomes where losses are total.
- **Minimum edge = 10%.** Only trade when model disagrees with market by 10%+. Tunable by learning pipeline (range 5-35%).
- **Signal layer weights.** Layer 1 (Student-t base) provides the core probability. Layer 2 (regime) adjusts by ±5% max. Layer 3 (order flow) adjusts by ±6% max. Layer 4 (momentum/indicators) adjusts by ±4% max — deliberately the weakest signal since indicators alone should not trigger trades.
- **Real-time WebSocket + Gamma API for prices.** Primary: CLOB WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) provides real-time book snapshots, price deltas, best bid/ask, last trades, and market resolution events. Trading loop is event-driven — reacts instantly to book changes instead of polling. HTTP fallback: `clob.polymarket.com/book?token_id=TOKEN` if WS disconnected. Gamma API (`gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}`) for contract discovery and `token_id_up`/`token_id_down`. Supplementary: `GET /spread` for liquidity check, `GET /midpoints` for quick price ref, `GET /last-trades-prices` for fill validation. Gamma `outcomePrices` are stale fallback only.
- **NegRisk execution pricing via GET /price.** The raw token book (`GET /book`) only shows direct token orders — in negRisk binary markets the CLOB cross-matches across complementary tokens, so the raw book shows asks at $0.99 on both sides while the real executable price is ~$0.50. **Always use `GET /price?token_id=X&side=BUY` for entry pricing and `side=SELL` for exit pricing.** This is what Polymarket's website shows. Never use raw book best ask/bid for edge calculation.
- **Outcomes are "Up"/"Down".** Contract fields: `price_up`, `price_down`.
- **Binance.US, not Binance.com.** HTTP 451 for US IPs on .com.
- **Strike = BTC price at 5-min window boundary.** Derived from candle buffer, not "first time bot sees the contract."
- **`--mode paper` CLI flag.** Paper mode uses persistent SQLite bankroll with real CLOB order book prices for realistic fill simulation. Fee rates fetched live from `GET /fee-rate?token_id=X` (crypto = 7.2%, sports = 3%, etc.). Entry fees collected in shares (fewer shares received), exit fees in USDC — matching Polymarket's actual collection method. Prices snapped to market tick size. Min order size enforced from CLOB book. FOK fill semantics (100% fill or reject). Orders scaled down to available CLOB depth when book is thin. Live mode on polymarket.com (EIP-712 signed CLOB orders) is future work.

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
    base.py                  # TradeResult dataclass
    paper_trader.py          # Simulated trades (paper mode)
    live_trader.py           # Stub — polymarket.com live trading is future work (EIP-712)
  agents/
    scheduler.py             # Daily learning pipeline
    outcome_reviewer.py      # Logs resolved trades
    bias_detector.py         # Per-indicator accuracy
    ta_evolver.py            # Recommends weight adjustments
    weight_optimizer.py      # Versions and auto-adopts weights
  brain/
    claude_client.py         # analyze_strategy() for daily pipeline + analyze_market() legacy
  memory/
    outcomes/                # One JSON per trade
    weights/                 # Versioned weight configs
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py                   # Commands: status, positions, history, performance, clear, session, pause/resume
    commands.py              # Formatting helpers
    alerts.py                # Trade alerts, session banners, channel purging
  db/models.py               # SQLite: positions, trade_history, bankroll
  math_engine/
    decision_table.py        # Legacy — not used in main trading loop
    returns.py               # Log returns, Sharpe ratio
```

## Config

`polybot/config/settings.yaml`:
- `math.kelly_fraction:` — 0.15 (fraction of full Kelly)
- `signal.entry_threshold:` — 0.10 (minimum 10% edge to trade)
- `signal.exit_edge_threshold:` — -0.10 (exit when holding edge drops below -10%)
- `signal.min_model_probability:` — 0.65 (skip coin-flip trades — model must be ≥65% confident)
- `signal.momentum_weight:` — 0.04 (max ±4% indicator adjustment, Layer 4 — weakest signal)
- `signal.regime_weight:` — 0.05 (max ±5% regime adjustment, Layer 2)
- `signal.flow_weight:` — 0.06 (max ±6% order flow adjustment, Layer 3)
- `signal.student_t_df:` — 4 (degrees of freedom for fat-tailed CDF)
- `signal.weights:` — per-indicator weights for momentum calculation
- `execution.max_concurrent_positions:` — 1 (single position, full focus)
- `execution.max_bankroll_deployed:` — 0.80
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
python -m pytest polybot/tests/       # 249 tests
```

## How the Probability Model Works

```
Strike = BTC price at 5-min window open (from candle buffer)
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
  If autocorr > 0 (trending): amplify P away from 0.5 (max ±5%)
  If autocorr < 0 (reverting): dampen P toward 0.5

LAYER 3 — Order flow:
  book_imbalance = (bid_depth - ask_depth) / total_depth (across both Up/Down books)
  trade_flow = net_buy_volume / total_volume (from WebSocket last_trade_price events)
  flow_signal = 0.6 * book_imbalance + 0.4 * trade_flow
  Adjustment: flow_signal * 0.06 (max ±6%)
  
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
  If edge >= 10%: TRADE, size = Kelly(probability, market_price) * 0.15
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

## Common Issues

- **No trades:** BTC is near the strike (no edge) or market is efficiently priced. This is correct behavior — no edge means no trade.
- **Binance 451:** Using .com instead of .us.
- **Wrong strike:** Strike is derived from candle buffer at window boundary. If buffer is empty on startup, first few windows may have wrong strike.
- **All trades losing:** Check if model is systematically miscalibrated. Lower kelly_fraction or raise min_edge.

## Learning Pipeline

Daily at 4:45 PM ET / 20:45 UTC (configurable via `agents.daily_pipeline_hour` and `daily_pipeline_minute`):

1. **BiasDetector** — Rich multi-dimensional analysis of all outcomes:
   - Per-indicator accuracy (bullish/bearish breakdown, sample sizes)
   - Side analysis (Up vs Down win rate)
   - Edge calibration (do larger edges actually win more?)
   - Time patterns (win rate by seconds remaining at entry)
   - Volatility patterns (win rate by ATR regime)
   - Overall statistics (Sharpe, win rate, avg edge)

2. **TAEvolver** — Sends analysis + last 75 trades + current config to Claude API:
   - System prompt defines Chief Quantitative Strategist role with full 4-layer model description
   - Trade data includes flow_score, exit_reason (scalp vs resolution) for each trade
   - Claude returns structured JSON: weight adjustments, all 4 layer weights (momentum, regime, flow, student_t_df), min_edge, kelly_fraction, reasoning, findings, risk warnings
   - Response validated server-side (weights sum to 1.0, all constraints enforced, new params clamped)
   - Falls back to local math if Claude API fails (resilient)

3. **WeightOptimizer** — Backtests recommendations using trade_context edge data:
   - Recomputes hypothetical edge with new weights/parameters
   - Auto-adopts if Sharpe improves >= 3%
   - Hot-swaps ALL params at runtime: indicator weights, momentum_weight, regime_weight, flow_weight, student_t_df, min_edge, kelly_fraction, min_model_probability, exit_edge_threshold, min_time_remaining, trading hours
   - **Persists all tuned parameters to settings.yaml** — values survive restarts
   - Discord alerts include Claude's key findings and reasoning

Outcome data enriched with `trade_context` in indicator_snapshot: btc_price, strike_price, seconds_remaining, market prices, model_probability, edge, momentum_score, ATR, size, flow_score, flow_book_imbalance, flow_trade_count.

## What NOT to Change

- Don't add fixed take-profit/stop-loss percentages — use the probability model for exit decisions (evaluate_hold).
- Don't increase momentum_weight above 0.10 — indicators alone should not trigger trades.
- Don't use normal CDF / logistic approximation — use Student-t CDF (fat tails). The normal distribution underestimates reversal probability for BTC.
- Don't remove complementary pricing — it's essential for seeing real underdog prices in negRisk binary markets.
- Don't increase flow_weight above 0.10 — order flow should nudge, not dominate.
- Don't use raw CLOB book asks/bids for entry/exit pricing — use `GET /price?token_id=X&side=BUY|SELL` for negRisk cross-matched execution prices. Raw book shows $0.99 on both sides; `/price` shows the real ~$0.50 price.
- Don't use Gamma API `outcomePrices` for edge calculation — they're stale/initial prices, not live order book.
- Don't hardcode fee rates — fetch from `GET /fee-rate?token_id=X`. Crypto is 0.072, not 0.05.
- Don't use polymarket.us for crypto — US platform has sports only. All crypto trading is on polymarket.com.
- Don't use Binance.com — use Binance.us.
- Don't allow multiple concurrent positions — one at a time, full Kelly.
- Don't auto-delete the DB — bankroll persists across sessions in both modes. Never delete `polybot/db/polybot.db` between runs.
- Don't use limit orders in LiveTrader — FOK market orders for 5-min contract speed.

## Baseline — LOCKED 2026-04-08

The core trading logic is FROZEN. Do not make structural changes to:
- `signal_engine.py` (4-layer probability model)
- `order_flow.py` (book imbalance + trade flow)
- Entry/exit/pricing logic in `main.py`
- `paper_trader.py` (fee simulation)

Only the daily learning pipeline (4:45 PM) tunes parameters slowly. Any proposed "improvement" to frozen code requires explicit user approval. New features go in NEW files/modules.

## Always Update

Update this file and README.md with every behavioral change.
