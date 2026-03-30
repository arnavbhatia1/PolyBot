# PolyBot

Automated micro-trader for Polymarket's 5-minute BTC Up/Down markets. Uses 7 technical indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) with a gates + weighted scoring engine that makes 1-second decisions. Actively scalps within each window. Self-learning pipeline tunes all parameters daily.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up secrets in .env
cp polybot/config/.env.example polybot/config/.env
# Edit .env with your keys (minimum: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN)

# Run
python -m polybot.main
```

## How It Works

```
Binance WebSocket (live BTC price)
        |
  200-candle buffer (1-min candles)
        |
  7 indicators computed every 1 second
        |
  [OPEN POSITIONS?] --> Check take-profit (10%) / stop-loss (8%) --> Sell if hit
        |
  Hard gates: ATR volatility + EMA trend + entry window (full 5 min)
        |
  Weighted score from RSI, MACD, Stochastic, OBV, VWAP
        |
  Score > threshold? --> Place trade on Polymarket 5-min BTC contract
        |
  Active scalping within window --> Log outcome --> Learning agents tune weights
```

## Architecture

| Module | Purpose |
|--------|---------|
| `core/binance_feed.py` | WebSocket price stream + rolling candle buffer |
| `indicators/` | 7 pure-function indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) |
| `indicators/engine.py` | Combines all 7, manages weight versions |
| `core/signal_engine.py` | Hard gates + weighted scoring --> trade signals |
| `core/market_scanner.py` | Finds active 5-min BTC contracts via Gamma API deterministic slugs |
| `execution/paper_trader.py` | Simulated trading with bankroll management |
| `agents/` | Self-learning pipeline (bias detector, TA evolver, weight optimizer) |
| `discord_bot/` | Commands and trade alerts |
| `db/models.py` | SQLite for positions, trade history, bankroll |

## Configuration

All parameters in `polybot/config/settings.yaml`:

- **Indicator periods** (RSI 14, MACD 12/26/9, etc.)
- **Gate thresholds** (ATR percentiles, EMA chop detection)
- **Entry threshold** (minimum signal score to trade, 0.40 for paper trading)
- **Indicator weights** (how much each indicator contributes)
- **Scalping** (take-profit 10%, stop-loss 8% within the window)
- **Entry window** (full 5-min contract, last 5s blocked)
- **Kelly fraction** (position sizing conservatism)

## Learning Pipeline

Runs daily at 2 AM UTC:

1. **Bias Detector** -- finds indicator-level accuracy patterns
2. **TA Strategy Evolver** -- recommends weight/threshold adjustments using Claude
3. **Weight Optimizer** -- backtests and **auto-adopts** improved weights if Sharpe improves >= 3%. Hot-swaps at runtime. Posts results to Discord.

Outcomes logged after every scalp exit. Negative Sharpe warnings posted to `#polybot-control`.

## Discord Commands

| Command | Description |
|---------|-------------|
| `!commands` | Show all commands |
| `!status` | Mode, bankroll, positions, P&L |
| `!positions` | Open positions with targets |
| `!history [n]` | Last n closed trades |
| `!pause` / `!resume` | Pause/resume trading |
| `!agents` | Learning agent schedule |
| `!lessons` | Top learnings from memory |

## Secrets Required

| Key | When needed |
|-----|-------------|
| `ANTHROPIC_API_KEY` | Always (daily learning analysis) |
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_API_KEY` | Live trading only |
| `POLYMARKET_SECRET` | Live trading only |
| `ALCHEMY_RPC_URL` | Live trading only |
| `PRIVATE_KEY` | Live trading only |

Binance API is free and needs no key.

## Deployment

- **Phase 1 (current):** `python -m polybot.main` on local machine, paper trading
- **Phase 2:** `docker build -t polybot . && docker run -d --restart=always polybot` on a $5/month VPS

## Tests

```bash
python -m pytest polybot/tests/ -v   # 148 tests
```
