# PolyBot

Automated 5-minute BTC Up/Down trader for Polymarket. Computes the mathematical probability that BTC finishes above/below the opening strike price using a 10-layer signal model (Student-t CDF + 9 adjustment layers in logit space), compares that to the market's price, and trades when mispricing exceeds the noise floor and Kelly fraction justifies a position. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the fee-aware exit threshold. Up to 2 concurrent positions from different windows with half-Kelly sizing.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up secrets in .env
cp polybot/config/.env.example polybot/config/.env
# Edit .env with your keys (minimum: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN)
# For live trading: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

# Paper trading (simulated, persistent bankroll across sessions)
python -m polybot.main --mode paper

# Live trading (real USDC on Polymarket via EIP-712 signed CLOB orders)
python -m polybot.main --mode live

# Defaults to mode in settings.yaml
python -m polybot.main
```

## How It Works

```
6 WebSocket feeds + 2 REST polls (real-time data)
        |
  200-candle Binance buffer + track strike at 5-min window open
        |
  10-LAYER PROBABILITY MODEL (logit-space combination):
    L1  -- Student-t CDF (df=5, fat tails): z = distance / (vol * sqrt(time) * iv_ratio)
    L2  -- Regime detection: autocorrelation of last N 1-min returns (+/-3%)
    L3  -- CLOB order flow: book imbalance + trade flow (+/-4%)
    L3b -- Spot market flow: CVD + taker ratio from Binance aggTrades (+/-4%)
    L3c -- Wall pressure: L2 depth near strike from Binance 1000-level book (+/-5%)
    L3d -- Perpetual price lead: Bybit perp/spot divergence (+/-3%)
    L3e -- Liquidation pressure: Bybit OI drop + price direction (+/-3%)
    L4  -- Indicator momentum: RSI/MACD/Stochastic/OBV/VWAP (+/-4%)
    L5  -- Previous window momentum carry (+/-2%)
    +   -- Platt scaling calibration (fitted daily)
        |
  SPRT evidence accumulation during 60s observe phase
  HMM regime classifier adjusts Kelly/edge per market state
  Signal consensus multiplier (>80% agree -> 1.3x Kelly)
  Alpha decay tracker for adaptive entry timing
  GEX from Deribit options (stabilizing vs amplifying)
        |
  Edge = model_prob - market_price (from CLOB /price endpoint)
        |
  9 entry gates: confidence >= 65%, edge >= 4%, Kelly >= 1.5%,
    spread <= 10%, depth >= $50, price sanity, timing, edge cap, layer agreement
        |
  Kelly sizing (0.15 fraction), capped to 50% of book depth
  Convex slippage model, net-edge gate
        |
  While holding: continuously re-evaluate with same model
    holding_edge > fee-aware threshold? HOLD : SCALP EXIT
    Trailing profit exit for cheap entries that peak then drop
        |
  Resolution: Gamma/Chainlink oracle ($1 win / $0 loss)
  --> Log outcome --> Daily learning pipeline
```

## Architecture

| Module | Purpose |
|--------|---------|
| `core/signal_engine.py` | 10-layer probability model: Student-t CDF + 9 logit-space layers |
| `core/binance_feed.py` | Binance.US WebSocket price stream + rolling candle buffer |
| `core/clob_ws.py` | Real-time Polymarket CLOB WebSocket (order books, trades, resolution) |
| `core/market_scanner.py` | Gamma API contract discovery + CLOB HTTP helpers |
| `core/order_flow.py` | Book imbalance + trade flow signal from CLOB data |
| `core/binance_depth.py` | L2 order book: wall detection, spot imbalance, book depth |
| `core/binance_trades.py` | Aggregate trade stream: CVD, taker ratio, large trades, volume surge |
| `core/bybit_feed.py` | BTC perpetual price lead + funding rate signal |
| `core/deribit_iv.py` | BTC options implied volatility (forward-looking vol) |
| `core/sprt.py` | Sequential Probability Ratio Test for evidence-based entry |
| `core/regime.py` | Multi-state regime detector (trending/reverting/volatile/quiet) |
| `core/liquidation.py` | OI-based liquidation pressure from Bybit |
| `core/gamma_exposure.py` | Net gamma exposure from Deribit options chain |
| `core/alpha_decay.py` | Edge decay rate tracker for adaptive entry timing |
| `core/bankroll_strategy.py` | Tiered Kelly acceleration based on track record |
| `core/calibrator.py` | Platt scaling calibration for probability model |
| `indicators/` | 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) |
| `indicators/engine.py` | Combines all 7, manages weight versions |
| `execution/base.py` | BaseTrader ABC, TradeResult, FillResult, shared fee math + gates |
| `execution/paper_trader.py` | Realistic simulated trading -- real CLOB prices, dynamic fees, FOK fills |
| `execution/live_trader.py` | Real Polymarket CLOB trading with maker orders + FOK fallback |
| `execution/circuit_breaker.py` | Drawdown-based Kelly scaling (1.0 at peak, 0.25 at 15% drawdown) |
| `agents/` | Self-learning pipeline (bias detector, TA evolver, weight optimizer, counterfactual tracker) |
| `discord_bot/` | Commands, trade alerts, session management |
| `db/models.py` | SQLite for positions, trade history, bankroll |

## Data Sources

| Source | Feed | What |
|--------|------|------|
| **Binance.US** | `btcusdt@kline_1m` WS | 1-min candles -- BTC price, ATR, indicators |
| **Binance.US** | `btcusdt@depth20@100ms` WS | Top 20 book levels -- spot imbalance |
| **Binance.US** | `btcusdt@aggTrade` WS | Every trade with taker side -- CVD, taker ratio |
| **Binance.US** | `GET /depth?limit=1000` REST | Full order book -- wall detection near strike |
| **Polymarket CLOB** | WS market stream | Order books, BBA, last trades, resolution events |
| **Polymarket CLOB** | `GET /price`, `/book`, `/spread` | Execution prices, order books, liquidity |
| **Polymarket Gamma** | `GET /events?slug=...` | Contract discovery, resolution, token IDs |
| **Bybit** | `tickers.BTCUSDT` WS | BTC perpetual price lead + funding rate |
| **Deribit** | `GET /get_book_summary_by_currency` | ATM implied volatility (polled every 60s) |
| **Anthropic Claude** | `claude-sonnet-4-6` via SDK | Daily learning pipeline recommendations |

## Paper Trading Realism

Paper mode simulates live execution as closely as possible:

- **Real CLOB prices** -- order books from WebSocket (not stale Gamma prices)
- **Order size cap** -- capped to 50% of available ask depth (no fantasy fills)
- **Convex slippage** -- fills penalized by `(size/depth) * 3% * (1 + size/depth)` -- larger orders get worse prices
- **Dynamic fee rates** -- fetched live from `GET /fee-rate` per token (crypto = 1.8%)
- **Correct fee collection** -- entry fees in shares (fewer shares received), exit fees in USDC
- **FOK fill semantics** -- order must fill 100% or reject (scaled to available depth)
- **Tick size enforcement** -- prices snapped to market tick via `GET /tick-size`
- **Min order size** -- from CLOB book response (typically 5 shares)
- **Spread filter** -- skip entry if bid-ask spread > 10%
- **Event-driven loop** -- reacts instantly to WebSocket book changes (~1-2ms per cycle)

## Configuration

All parameters in `polybot/config/settings.yaml` (validated by `validate_config()` on startup).

Key parameters (all tunable by learning pipeline):
- **Kelly fraction** -- 0.15 (conservative for binary outcomes)
- **Entry threshold** -- 4% minimum edge (noise floor)
- **Min model probability** -- 65% confidence gate
- **Layer weights** -- L2 regime 3%, L3 flow 4%, L3b spot flow 4%, L3c wall 5%, L3d perp 3%, L4 momentum 4%, L5 carry 2%
- **Max concurrent positions** -- 2 (half-Kelly when concurrent)
- **Circuit breaker** -- Kelly scales 1.0 to 0.25 as drawdown reaches 15%
- **SPRT** -- alpha 0.05, beta 0.10 for evidence accumulation
- **Regime** -- 4-state HMM classifier adjusts Kelly and thresholds

## Learning Pipeline

Runs daily at 11:55 PM ET. Minimum 50 trades required (enforced in code). 60/40 hold-out split prevents overfitting.

1. **BiasDetector** -- Per-indicator accuracy, side bias, edge calibration, time/vol patterns, counterfactual analysis (scalps AND holds)
2. **PlattCalibrator** -- Fits Platt scaling (A, B) on training set. Validates on holdout -- adopts only if log-loss improves
3. **TAEvolver** -- Sends analysis + trades to Claude API. Returns weight adjustments, all layer weights, kelly_fraction, atr_sigma_ratio, reasoning, risk warnings
4. **WeightOptimizer** -- Backtests on validation set (last 40%). Auto-adopts if Sharpe improves >= 3%. Hot-swaps all params and persists to settings.yaml

Tunes 15+ parameters: indicator weights, all layer weights, student_t_df, min_edge, kelly_fraction, min_kelly, atr_sigma_ratio, min_model_probability, exit_edge_threshold, min_time_remaining, trading hours.

## Discord Commands

| Command | Description |
|---------|-------------|
| `!commands` | Show all commands |
| `!status` | Mode, bankroll, positions, P&L |
| `!positions` | Open positions with targets |
| `!history [n]` | Last n closed trades |
| `!performance` | Sharpe ratio, win rate, total P&L |
| `!pause` / `!resume` | Pause/resume trading (position management continues while paused) |
| `!agents` | Learning agent schedule |
| `!lessons` | Top learnings from memory |
| `!clear [trades|control|all]` | Purge messages from channels |
| `!session` | Re-send session banner |

## Secrets Required

| Key | When needed |
|-----|-------------|
| `ANTHROPIC_API_KEY` | Always (daily learning analysis) |
| `DISCORD_BOT_TOKEN` | Always (monitoring and alerts) |
| `POLYMARKET_PRIVATE_KEY` | Live mode (EIP-712 order signing) |
| `POLYMARKET_FUNDER` | Live mode (USDC funding address) |

Binance.US, Polymarket CLOB/Gamma, Bybit, and Deribit APIs are all free and need no key.

## Tests

```bash
python -m pytest polybot/tests/ -q   # 545 tests
```
