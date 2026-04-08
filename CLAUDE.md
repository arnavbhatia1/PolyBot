# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes the mathematical probability that BTC finishes above/below the opening strike price, compares that to the market's price, and trades when mispricing exceeds 10%. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the exit threshold.

## Key Architecture Decisions

- **Probability model, not indicators.** The bot computes P(Up) using Brownian motion: z = (BTC - strike) / (ATR * sqrt(time)), then P = logistic(1.7z). Indicators provide a small momentum nudge (±8%). The edge is: model probability - market price.
- **Active position management.** Hold to $1 resolution when the model is confident (holding_edge > 0). Exit early when the model says the market has moved past fair value (holding_edge ≤ -5%). Same Brownian motion model for entry AND exit. Not fixed take-profit/stop-loss — the math decides.
- **Single position at a time.** Full Kelly on the best edge, no capital dilution.
- **One trade per 5-min contract.** After any exit, that contract is blacklisted.
- **Kelly fraction = 0.15.** Conservative for binary outcomes where losses are total.
- **Minimum edge = 10%.** Only trade when model disagrees with market by 10%+. Tunable by learning pipeline (range 5-35%).
- **Momentum weight = 0.08.** Indicators nudge probability by max ±8%. This ensures indicators alone (without BTC movement from strike) cannot trigger a trade.
- **Real-time WebSocket + Gamma API for prices.** Primary: CLOB WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) provides real-time book snapshots, price deltas, best bid/ask, last trades, and market resolution events. Trading loop is event-driven — reacts instantly to book changes instead of polling. HTTP fallback: `clob.polymarket.com/book?token_id=TOKEN` if WS disconnected. Gamma API (`gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}`) for contract discovery and `token_id_up`/`token_id_down`. Supplementary: `GET /spread` for liquidity check, `GET /midpoints` for quick price ref, `GET /last-trades-prices` for fill validation. Gamma `outcomePrices` are stale fallback only.
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
- `signal.momentum_weight:` — 0.08 (max ±8% indicator adjustment to probability)
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
python -m pytest polybot/tests/       # 233 tests
```

## How the Probability Model Works

```
Strike = BTC price at 5-min window open (from candle buffer)
Distance = current BTC price - strike
Vol = ATR (average true range from 1-min candles)
Time = minutes remaining in the window

z = distance / (vol * sqrt(time))
P(Up) = 1 / (1 + exp(-1.7 * z))

Momentum nudge: P(Up) += indicator_score * 0.08

ENTRY:
  Edge = P(Up) - market_price_up    [or P(Down) - market_price_down]
  If model_prob < 65%: SKIP (coin-flip filter)
  If edge >= 10%: TRADE, size = Kelly(probability, market_price) * 0.15
  If edge < 10%: SKIP

WHILE HOLDING (active position management):
  holding_edge = model_prob_for_our_side - current_market_price_for_our_side
  If holding_edge > -10%: HOLD (model still supports the position)
  If holding_edge ≤ -10%: EXIT (market overpricing our side, take profit or cut loss)
  Same Brownian motion model — continuously re-evaluates.

RESOLUTION (contract expired, seconds_remaining <= 0):
  1. Prefer on-chain resolution from Gamma API (closed=true, outcomePrices near $1/$0)
  2. Fallback: determine winner from BTC price vs strike (binary outcome)
     If BTC >= strike: Up won. If BTC < strike: Down won.
  3. Orphaned positions (contract not findable after 10 min): same BTC fallback
  Winning side pays $1.00/share, losing side pays $0.00/share.
  Resolution fee is always $0 (fee formula: Θ × shares × p × (1-p) = 0 at p=0 or p=1).
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
   - System prompt defines Chief Quantitative Strategist role (quant research, trading, risk, crypto)
   - Claude returns structured JSON: weight adjustments, momentum_weight, min_edge, kelly_fraction, reasoning, findings, risk warnings
   - Response validated server-side (weights sum to 1.0, all constraints enforced)
   - Falls back to local math if Claude API fails (resilient)

3. **WeightOptimizer** — Backtests recommendations using trade_context edge data:
   - Recomputes hypothetical edge with new weights/parameters
   - Auto-adopts if Sharpe improves >= 3%
   - Hot-swaps indicator weights, momentum_weight, min_edge, kelly_fraction at runtime
   - **Persists all tuned parameters to settings.yaml** — values survive restarts
   - Discord alerts include Claude's key findings and reasoning

Outcome data enriched with `trade_context` in indicator_snapshot: btc_price, strike_price, seconds_remaining, market prices, model_probability, edge, momentum_score, ATR, size.

## What NOT to Change

- Don't add fixed take-profit/stop-loss percentages — use the probability model for exit decisions (evaluate_hold).
- Don't increase momentum_weight above 0.10 — indicators alone should not trigger trades.
- Don't use Gamma API `outcomePrices` for edge calculation — they're stale/initial prices, not live order book. Use `clob.polymarket.com/book?token_id=X` for real prices.
- Don't hardcode fee rates — fetch from `GET /fee-rate?token_id=X`. Crypto is 0.072, not 0.05.
- Don't use polymarket.us for crypto — US platform has sports only. All crypto trading is on polymarket.com.
- Don't use Binance.com — use Binance.us.
- Don't allow multiple concurrent positions — one at a time, full Kelly.
- Don't auto-delete the DB — bankroll persists across sessions in both modes. Never delete `polybot/db/polybot.db` between runs.
- Don't use limit orders in LiveTrader — FOK market orders for 5-min contract speed.

## Always Update

Update this file and README.md with every behavioral change.
