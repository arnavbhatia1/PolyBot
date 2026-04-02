# PolyBot

Automated 5-minute BTC Up/Down trader for Polymarket. Computes the mathematical probability that BTC finishes above/below the opening strike price using Brownian motion, compares that to the market's price, and trades when mispricing exceeds 10%. Hold to resolution — no scalping.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up secrets in .env
cp polybot/config/.env.example polybot/config/.env
# Edit .env with your keys (minimum: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN)

# Paper trading (simulated, fresh $1,000 bankroll each run)
python -m polybot.main --mode paper

# Live trading (real USDC on Polymarket — requires all secrets)
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
  Momentum nudge: P(Up) += indicator_score * 0.08  (max ±8%)
        |
  Edge = model probability - market price
        |
  Edge >= 10%? --> Kelly size (0.15 fraction) --> Place trade
        |
  While holding: continuously re-evaluate with same model
  holding_edge = model_prob - market_price for our side
  If holding_edge > -5%: HOLD (ride to $1 resolution)
  If holding_edge ≤ -5%: EXIT (take profit or cut loss)
  On resolution or exit --> Log outcome --> Learn
```

## Architecture

| Module | Purpose |
|--------|---------|
| `core/binance_feed.py` | WebSocket price stream + rolling candle buffer |
| `core/signal_engine.py` | Probability model: BTC vs strike + time + vol + momentum nudge |
| `core/market_scanner.py` | Finds active 5-min BTC contracts via Gamma API deterministic slugs |
| `indicators/` | 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) |
| `indicators/engine.py` | Combines all 7, manages weight versions |
| `execution/paper_trader.py` | Simulated trading with bankroll management |
| `execution/live_trader.py` | Real trading via Polymarket CLOB (py-clob-client) |
| `agents/` | Self-learning pipeline (bias detector, TA evolver, weight optimizer) |
| `discord_bot/` | Commands, trade alerts, session management |
| `db/models.py` | SQLite for positions, trade history, bankroll |

## Configuration

All parameters in `polybot/config/settings.yaml`:

- **Minimum edge** — 10% mispricing between model and market required to trade
- **Momentum weight** — 0.08, indicators nudge base probability by max ±8% (below min edge so indicators alone can't trigger trades)
- **Kelly fraction** — 0.15 (conservative for binary outcomes where losses are total)
- **Single position** — one trade at a time, full Kelly on the best edge
- **One trade per contract** — no re-entry after exit on same 5-min window
- **Active position management** — hold to $1 when model is confident, exit early when holding edge drops below -5%
- **Exit edge threshold** — -0.05 (same probability model for entry AND exit decisions)
- **Extreme price filter** — won't enter when market is < 0.15 or > 0.85
- **Entry window** — full 5-min contract, last 5 seconds blocked
- **Indicator weights** — RSI 0.20, MACD 0.25, Stochastic 0.20, OBV 0.15, VWAP 0.20

## Learning Pipeline

Runs daily at 2 AM UTC:

1. **Bias Detector** — Multi-dimensional analysis: per-indicator accuracy, side bias, edge calibration, time/volatility patterns
2. **TA Strategy Evolver** — Sends full analysis + recent trades to Claude API as a quant strategist. Returns weight adjustments, parameter recommendations, reasoning, and risk warnings. Falls back to local math if API is unavailable.
3. **Weight Optimizer** — Backtests recommendations against historical edge data, auto-adopts if Sharpe improves >= 3%, hot-swaps all parameters (weights, momentum_weight, min_edge, kelly_fraction) at runtime

Outcomes enriched with full trade context (BTC price, strike, time remaining, model probability, edge). Claude's analysis and key findings posted to `#polybot-trades`. Negative Sharpe warnings posted to `#polybot-control`.

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
| `!clear [trades\|control\|all]` | Purge messages from channels |
| `!session` | Re-send session banner |

### Session Management

Each bot startup sends a **session banner** to both Discord channels with a unique session ID, timestamp, and bankroll. This makes it easy to distinguish different runs when running locally.

Use `!clear` before or after a run to wipe channel history clean.

## Secrets Required

| Key | When needed |
|-----|-------------|
| `ANTHROPIC_API_KEY` | Always (daily learning analysis) |
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_API_KEY` | Live trading only |
| `POLYMARKET_SECRET` | Live trading only |
| `POLYMARKET_PASSPHRASE` | Live trading only |
| `PRIVATE_KEY` | Live trading only |

Binance API is free and needs no key.

## Deployment

- **Paper:** `python -m polybot.main --mode paper` (simulated, fresh $1K bankroll each run)
- **Live:** `python -m polybot.main --mode live` (real USDC via Polymarket CLOB)
- **VPS:** `docker build -t polybot . && docker run -d --restart=always polybot`

## Tests

```bash
python -m pytest polybot/tests/ -v   # 191 tests
```
