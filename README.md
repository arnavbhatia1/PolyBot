# PolyBot

Automated 5-minute BTC Up/Down trader for Polymarket. Computes the mathematical probability that BTC finishes above/below the opening strike price using Brownian motion, compares that to the market's price, and trades when mispricing exceeds 10%. Holds to $1 resolution when confident, exits early (scalps) when the model detects conditions have flipped.

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
  4-LAYER PROBABILITY MODEL:
    Layer 1 — Student-t CDF (fat tails, df=4):
      z = (BTC - strike) / (ATR * sqrt(time))
      P(Up) = t.cdf(z, df=4)              <-- CDF drives all decisions
    Layer 2 — Regime: autocorrelation of last 20 1-min returns (max +/-3%)
    Layer 3 — Order flow: book imbalance + trade flow (max +/-4%)
    Layer 4 — Momentum: RSI/MACD/Stochastic/OBV/VWAP (max +/-4%)
        |
  Edge = model probability - market price (from CLOB /price endpoint)
        |
  Model confidence >= 65%? Edge >= 10%?
    --> Kelly size (0.15 fraction), capped to 50% of book depth
    --> Entry fill = /price + slippage (size/depth * 3%)
    --> Place trade
        |
  While holding: continuously re-evaluate with same 4-layer model
  holding_edge = model_prob - market_price for our side
  Fee-aware threshold with time urgency bonus near expiry
  If holding_edge > threshold: HOLD (ride to $1 resolution)
  If holding_edge <= threshold: EXIT (scalp)
        |
  On resolution: Gamma/Chainlink oracle data ($1 win / $0 loss, no Binance fallback)
  On early exit: sell at CLOB /price (negRisk cross-matched)
  --> Log outcome (gain_pct, PnL, fees) --> Learn
```

## Architecture

| Module | Purpose |
|--------|---------|
| `core/binance_feed.py` | Binance.US WebSocket price stream + rolling candle buffer |
| `core/clob_ws.py` | Real-time Polymarket CLOB WebSocket (order books, trades, resolution) |
| `core/market_scanner.py` | Gamma API contract discovery + CLOB HTTP helpers (spread, midpoints, volume, fees, tick size) |
| `core/signal_engine.py` | 4-layer probability model: Student-t CDF + regime + order flow + momentum |
| `core/order_flow.py` | Book imbalance + trade flow signal from CLOB data |
| `indicators/` | 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) |
| `indicators/engine.py` | Combines all 7, manages weight versions |
| `execution/paper_trader.py` | Realistic simulated trading — real CLOB prices, dynamic fees, FOK fills |
| `execution/live_trader.py` | Stub — polymarket.com live trading (EIP-712 signed CLOB orders) is future work |
| `agents/` | Self-learning pipeline (bias detector, TA evolver, weight optimizer, counterfactual tracker for scalps AND holds) |
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
- **Order size cap** — capped to 50% of available ask depth (no fantasy fills)
- **Proportional slippage** — fills penalized by `(order_size / depth) * 3%` — larger orders get worse prices
- **Dynamic fee rates** — fetched live from `GET /fee-rate` per token (crypto = 1.8%)
- **Correct fee collection** — entry fees in shares (fewer shares received), exit fees in USDC
- **FOK fill semantics** — order must fill 100% or reject (scaled to available depth)
- **Tick size enforcement** — prices snapped to market tick via `GET /tick-size`
- **Min order size** — from CLOB book response (typically 5 shares)
- **Spread filter** — skip entry if bid-ask spread > 10%
- **Event-driven loop** — reacts instantly to WebSocket book changes (~1-2ms per cycle)

## Configuration

All parameters in `polybot/config/settings.yaml` (validated by `validate_config()` on startup):

- **Minimum edge** — 10% mispricing between model and market required to trade
- **Min model probability** — 0.65, skip coin-flip trades (model must be confident)
- **Layer weights** — CDF drives decisions. Regime +/-3%, order flow +/-4%, momentum +/-4%. Total max layer swing: +/-11%
- **Kelly fraction** — 0.15 (conservative for binary outcomes where losses are total)
- **Single position** — one trade at a time, full Kelly on the best edge
- **One trade per contract** — no re-entry after exit on same 5-min window
- **Active position management** — hold to $1 when model is confident, exit early when holding edge drops below fee-aware threshold
- **Exit edge threshold** — -0.10, adjusted for exit fee cost and time urgency (same 4-layer model for entry AND exit)
- **Circuit breaker** — drawdown-based Kelly scaling: 1.0 at peak, linearly scales to 0.25 at 15% drawdown from peak bankroll (`max_drawdown_pct: 0.15`, `min_multiplier: 0.25`). Streaks tracked for Discord alerts only.
- **Regime lookback** — 20 (number of 1-min returns for autocorrelation, configurable via `signal.regime_lookback`)
- **Max spread** — 0.10 (skip illiquid markets)
- **Indicator weights** — RSI 0.20, MACD 0.25, Stochastic 0.20, OBV 0.15, VWAP 0.20
- All signal/entry params tunable by the learning pipeline (Claude recommends, optimizer backtests)

## Learning Pipeline

Runs daily at 4:45 PM ET (20:45 UTC). Scheduler enforces minimum 50 trades in code -- TAEvolver and WeightOptimizer are skipped if fewer than 50 trades exist. BiasDetector still runs regardless.

1. **Bias Detector** — Multi-dimensional analysis: per-indicator accuracy, side bias, edge calibration, time/volatility patterns, counterfactual analysis for both scalps and holds
2. **TA Strategy Evolver** — Sends full analysis + recent trades to Claude API as a quant strategist. Returns weight adjustments, parameter recommendations, reasoning, and risk warnings. Falls back to local math if API is unavailable.
3. **Weight Optimizer** — Backtests recommendations against historical edge data, auto-adopts if Sharpe improves >= 3%, hot-swaps all parameters at runtime **and persists them to settings.yaml** so they survive restarts

Outcomes enriched with full trade context (BTC price, strike, time remaining, model probability, edge, flow score) plus `gain_pct` (arithmetic return), `pnl`, and `fees`. Sharpe calculated from `gain_pct`, not `log_return` (log returns are broken for binary outcomes). Claude's analysis and key findings posted to `#polybot-daily`.

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
python -m pytest polybot/tests/ -v   # 422 tests
```
