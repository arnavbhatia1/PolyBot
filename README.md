# PolyBot

Automated 5-minute BTC Up/Down trader for Polymarket. Computes the mathematical probability that BTC finishes above/below the opening strike price using Brownian motion, compares that to the market's price, and trades when mispricing exceeds 10%. Hold to resolution — no scalping.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up secrets in .env
cp polybot/config/.env.example polybot/config/.env
# Edit .env with your keys (minimum: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN)

# Paper trading (simulated, persistent bankroll across sessions)
python -m polybot.main --mode paper

# Live trading (real USDC on Polymarket — future work, not yet implemented)
python -m polybot.main --mode live
```

## How It Works

```
Binance.US WebSocket (live BTC 1-min candles)
        |
  200-candle buffer + track strike price at 5-min window open
        |
  Probability model: z = (BTC - strike) / (ATR * sqrt(time))
                     P(Up) = 1 / (1 + exp(-1.7 * z))
        |
  Momentum nudge: P(Up) += indicator_score * 0.08  (max +/-8%)
        |
  Edge = model probability - market price (from live CLOB WebSocket)
        |
  Model confidence >= 65%? Edge >= 10%? --> Kelly size (0.15 fraction) --> Place trade
        |
  While holding: continuously re-evaluate with same model
  holding_edge = model_prob - market_price for our side
  If holding_edge > -10%: HOLD (ride to $1 resolution)
  If holding_edge <= -10%: EXIT (take profit or cut loss)
        |
  On resolution: binary outcome from BTC vs strike ($1 win / $0 loss)
  On early exit: VWAP from walking CLOB bid book
  --> Log outcome --> Learn
```

## Architecture

| Module | Purpose |
|--------|---------|
| `core/binance_feed.py` | Binance.US WebSocket price stream + rolling candle buffer |
| `core/clob_ws.py` | Real-time Polymarket CLOB WebSocket (order books, trades, resolution) |
| `core/market_scanner.py` | Gamma API contract discovery + CLOB HTTP helpers (spread, midpoints, volume, fees, tick size) |
| `core/signal_engine.py` | Probability model: BTC vs strike + time + vol + momentum nudge |
| `indicators/` | 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) |
| `indicators/engine.py` | Combines all 7, manages weight versions |
| `execution/paper_trader.py` | Realistic simulated trading — real CLOB prices, dynamic fees, FOK fills |
| `execution/live_trader.py` | Stub — polymarket.com live trading (EIP-712 signed CLOB orders) is future work |
| `agents/` | Self-learning pipeline (bias detector, TA evolver, weight optimizer) |
| `discord_bot/` | Commands, trade alerts, session management |
| `db/models.py` | SQLite for positions, trade history, bankroll |

## Data Sources

| Source | What | How |
|--------|------|-----|
| **Binance.US WebSocket** | Live BTC 1-min candles — drives probability model, ATR, all indicators | `wss://stream.binance.us:9443/ws/btcusdt@kline_1m` |
| **Polymarket CLOB WebSocket** | Real-time order books, price changes, last trades, resolution events | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| **Polymarket CLOB REST** | Fee rates, tick sizes, spread, midpoints, order book fallback | `https://clob.polymarket.com` |
| **Polymarket Gamma API** | Contract discovery (5-min BTC markets by deterministic slug) | `https://gamma-api.polymarket.com` |
| **Polymarket Data API** | Live volume per event (dead market filter) | `https://data-api.polymarket.com` |
| **Anthropic Claude API** | Daily learning pipeline — weight/parameter recommendations | `claude-sonnet-4-6` via SDK |

## Paper Trading Realism

Paper mode simulates live execution as closely as possible:

- **Real CLOB prices** — order books from WebSocket (not stale Gamma prices)
- **VWAP fill simulation** — walks ask levels on entry, bid levels on exit
- **Dynamic fee rates** — fetched live from `GET /fee-rate` per token (crypto = 7.2%)
- **Correct fee collection** — entry fees in shares (fewer shares received), exit fees in USDC
- **FOK fill semantics** — order must fill 100% or reject (scaled to available depth)
- **Tick size enforcement** — prices snapped to market tick via `GET /tick-size`
- **Min order size** — from CLOB book response (typically 5 shares)
- **Spread filter** — skip entry if bid-ask spread > 10%
- **Event-driven loop** — reacts instantly to WebSocket book changes (~1-2ms per cycle)

## Configuration

All parameters in `polybot/config/settings.yaml`:

- **Minimum edge** — 10% mispricing between model and market required to trade
- **Min model probability** — 0.65, skip coin-flip trades (model must be confident)
- **Momentum weight** — 0.08, indicators nudge base probability by max +/-8% (below min edge so indicators alone can't trigger trades)
- **Kelly fraction** — 0.15 (conservative for binary outcomes where losses are total)
- **Single position** — one trade at a time, full Kelly on the best edge
- **One trade per contract** — no re-entry after exit on same 5-min window
- **Active position management** — hold to $1 when model is confident, exit early when holding edge drops below -10%
- **Exit edge threshold** — -0.10 (same probability model for entry AND exit decisions)
- **Max spread** — 0.10 (skip illiquid markets)
- **Indicator weights** — RSI 0.20, MACD 0.25, Stochastic 0.20, OBV 0.15, VWAP 0.20
- All signal/entry params tunable by the learning pipeline (Claude recommends, optimizer backtests)

## Learning Pipeline

Runs daily at 4:45 PM ET (20:45 UTC):

1. **Bias Detector** — Multi-dimensional analysis: per-indicator accuracy, side bias, edge calibration, time/volatility patterns
2. **TA Strategy Evolver** — Sends full analysis + recent trades to Claude API as a quant strategist. Returns weight adjustments, parameter recommendations, reasoning, and risk warnings. Falls back to local math if API is unavailable.
3. **Weight Optimizer** — Backtests recommendations against historical edge data, auto-adopts if Sharpe improves >= 3%, hot-swaps all parameters at runtime **and persists them to settings.yaml** so they survive restarts

Outcomes enriched with full trade context (BTC price, strike, time remaining, model probability, edge). Claude's analysis and key findings posted to `#polybot-daily`. Negative Sharpe warnings posted to `#polybot-control`.

## Discord Commands

| Command | Description |
|---------|-------------|
| `!commands` | Show all commands |
| `!status` | Mode, bankroll, positions, P&L |
| `!positions` | Open positions with targets |
| `!history [n]` | Last n closed trades |
| `!performance` | Sharpe ratio, win rate, total P&L |
| `!pause` / `!resume` | Pause/resume trading |
| `!agents` | Learning agent schedule |
| `!lessons` | Top learnings from memory |
| `!clear [trades|control|all]` | Purge messages from channels |
| `!session` | Re-send session banner |

## Secrets Required

| Key | When needed |
|-----|-------------|
| `ANTHROPIC_API_KEY` | Always (daily learning analysis) |
| `DISCORD_BOT_TOKEN` | Always (monitoring) |

Binance.US and Polymarket CLOB APIs are free and need no key. Live trading (future work) will require a Polymarket private key for EIP-712 order signing.

## Tests

```bash
python -m pytest polybot/tests/ -v   # 233 tests
```
