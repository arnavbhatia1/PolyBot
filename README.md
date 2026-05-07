# PolyBot

Automated 5-minute BTC Up/Down trader for Polymarket. Computes the mathematical probability that BTC finishes above/below the opening strike price using an 8-layer signal model (Student-t CDF + 7 active adjustment layers in logit space), compares that to the market's price, and trades when mispricing exceeds the noise floor and Kelly fraction justifies a position. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the fee-aware exit threshold. Up to 2 concurrent positions from different windows with 0.50x discount sizing.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up secrets in .env
cp polybot/config/.env.example polybot/config/.env
# Edit .env with your keys (minimum: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN)
# For live trading: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

# Defaults to mode in settings.yaml
python -m polybot.main

# Run learning pipeline manually (no trading, analyzes all outcomes and exits)
python -m polybot.main --run-pipeline

# Auto-restart mode (daily cycle: trade -> pipeline -> commit -> push -> restart)
.\run_polybot.ps1
```

## How It Works

```
9 WebSocket feeds + 2 REST polls (real-time data)
        |
  BTC price: Coinbase (primary) > Kraken (secondary, Chainlink source) > Binance (fallback)
  + Binance candle buffer + Chainlink strike
        |
  8-LAYER PROBABILITY MODEL (logit-space combination):
    L1  -- Student-t CDF (df=5, fat tails): z = distance / (vol * sqrt(time) * iv_ratio)
    L2  -- Regime detection: autocorrelation of last N 1-min returns (+/-3%)
    L3  -- CLOB order flow: book imbalance + trade flow (+/-4%)
    L3b -- Spot market flow: CVD-dominant from Binance aggTrades, taker gated by min 5 trades (+/-4%)
    L3c -- Wall pressure: DISABLED (wall_weight=0.00, gamed by HFT)
    L3e -- Liquidation pressure: Bybit OI drop + price direction (+/-3%)
    L4  -- Indicator momentum: RSI/MACD/Stochastic/OBV/VWAP (negative weight -0.02, fades indicators)
    L5  -- Previous window momentum carry (+/-2%)
    +   -- Platt scaling calibration (fitted daily)
        |
  Continuous time multiplier (no hard phases, confidence-conditional decay)
  Flip trading: re-enter same window on opposite side with flat +1.5% edge premium (max 1 flip)
  Uncertainty-adjusted Kelly (f* = f_kelly x (1 - sigma^2/edge^2), floor 0.50)
  Drawdown velocity trigger (rolling 25-trade PnL < -15% -> force base Kelly)
  Logged only: regime, consensus, GEX, vol ratio, oracle divergence, crowd bias, adverse selection, edge realization ratio
        |
  Edge = calibrated_model_prob - market_price (from CLOB /price endpoint)
        |
  10 entry gates: confidence >= 58%, edge >= 4% (+ 1.5% flat premium for flip), Kelly >= 1.5%,
    spread <= 10%, depth >= $50, price sanity, edge cap, layer agreement,
    last 30s: prob >= 90%, flip: opposite side only (max 1 flip per window)
        |
  Kelly sizing x breaker x uncertainty_discount(floor=0.50) x time_mult
  Concurrent: x0.50 if holding. Regime/consensus/GEX/vol/oracle logged only.
  Capped to 50% of book depth
  Convex slippage model, net-edge gate
        |
  While holding: continuously re-evaluate with same model
    Binary option exit curve: ITM holds to $1 resolution, OTM cuts losses
    Trailing profit exit for cheap entries that peak then drop
        |
  Resolution: Gamma/Chainlink oracle ($1 win / $0 loss)
  --> Log outcome --> Daily learning pipeline
```

## Architecture

| Module | Purpose |
|--------|---------|
| `core/signal_engine.py` | 8-layer probability model: Student-t CDF + 7 active logit-space layers |
| `core/coinbase_feed.py` | Coinbase Exchange BTC-USD ticker (primary price, 0.5-2s faster) |
| `core/kraken_feed.py` | Kraken XBT/USD WebSocket ticker (secondary price, Chainlink oracle source) |
| `core/binance_feed.py` | Binance.US WebSocket candle buffer (ATR, indicators, fallback price) |
| `core/clob_ws.py` | Real-time Polymarket CLOB WebSocket (order books, trades, resolution, price velocity) |
| `core/chainlink_feed.py` | Chainlink BTC/USD oracle (resolution price source, preferred for strike) |
| `core/market_scanner.py` | Gamma API contract discovery + CLOB HTTP helpers |
| `core/order_flow.py` | Book imbalance + trade flow signal from CLOB data |
| `core/binance_depth.py` | L2 order book: wall detection, spot imbalance, book depth |
| `core/binance_trades.py` | Aggregate trade stream: CVD, taker ratio, large trades, volume surge |
| `core/bybit_feed.py` | BTC perpetual price lead + funding rate signal |
| `core/deribit_iv.py` | BTC options implied volatility (forward-looking vol) |
| `core/sprt.py` | Sequential Probability Ratio Test — telemetry only (does not gate entries) |
| `core/regime.py` | Multi-state regime detector (trending/reverting/volatile/quiet) |
| `core/liquidation.py` | OI-based liquidation pressure from Bybit |
| `core/gamma_exposure.py` | Net gamma exposure from Deribit options chain |
| `core/alpha_decay.py` | Edge decay rate tracker — logged in trade_context |
| `core/bankroll_strategy.py` | Uncertainty-adjusted Kelly + drawdown velocity trigger |
| `core/calibrator.py` | Platt scaling calibration for probability model |
| `core/exit_boundary.py` | Binary option exit curve (ITM holds to $1, OTM cuts losses) |
| `core/adverse_selection.py` | Post-fill price tracking — detects if being picked off |
| `core/edge_halflife.py` | Strategy-level edge decay detection (7d vs 30d rolling) |
| `core/garch_vol.py` | Realized vol ratio — logged in trade_context only (not applied to sizing) |
| `core/crowd_bias.py` | Favorite-longshot bias, recency fade, round number anchoring |
| `indicators/` | 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) |
| `indicators/engine.py` | Combines all 7, manages weight versions |
| `execution/base.py` | BaseTrader ABC, TradeResult, FillResult, shared fee math + gates |
| `execution/paper_trader.py` | Realistic simulated trading -- real CLOB prices, dynamic fees, FOK fills |
| `execution/live_trader.py` | Real Polymarket CLOB trading with FOK-only market orders |
| `execution/circuit_breaker.py` | Tiered floor Kelly scaling (locks in floor at each milestone tier, 1.0x at tier → 0.40x at floor) |
| `agents/` | Autonomous learning pipeline (walk-forward validation, statistical adoption, pipeline self-tracking) |
| `discord_bot/` | Commands, trade alerts, session management |
| `db/models.py` | SQLite for positions, trade history, bankroll |

## Data Sources

| Source | Feed | What |
|--------|------|------|
| **Coinbase** | `ticker` WS (BTC-USD) | **Primary BTC price** (0.5-2s faster than Binance.US) |
| **Kraken** | `ticker` WS (XBT/USD) | **Secondary BTC price** (Chainlink oracle data source, fallback when Coinbase stale) |
| **Binance.US** | `btcusdt@kline_1m` WS | 1-min candles -- ATR, indicators, fallback BTC price |
| **Binance.US** | `btcusdt@depth20@100ms` WS | Top 20 book levels -- spot imbalance |
| **Binance.US** | `btcusdt@aggTrade` WS | Every trade with taker side -- CVD, taker ratio |
| **Binance.US** | `GET /depth?limit=1000` REST | Full order book -- DISABLED (wall_weight=0.00, gamed by HFT) |
| **Polymarket CLOB** | WS market stream | Order books, BBA, last trades, resolution events |
| **Polymarket CLOB** | `GET /price`, `/book`, `/spread` | Execution prices, order books, liquidity |
| **Polymarket Gamma** | `GET /events?slug=...` | Contract discovery, resolution, token IDs |
| **Bybit** | `tickers.BTCUSDT` WS | BTC perpetual price lead + funding rate |
| **Deribit** | `GET /get_book_summary_by_currency` | ATM implied volatility + net gamma exposure (polled every 60s) |
| **Chainlink** | Ethereum RPC `latestRoundData()` | BTC/USD oracle — resolution price source |
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
- **Min model probability** -- 58% confidence gate
- **Layer weights** -- L2 regime 3%, L3 flow 4%, L3b spot flow 4%, L3c wall 0% (disabled), L3e liquidation 3%, L4 momentum -2% (negative = fade), L5 carry 2%
- **Max concurrent positions** -- 2 (0.50x discount when concurrent)
- **Circuit breaker** -- Tiered floor protection: bankroll milestones ($100/$150/$200/$300/...) lock in a floor at 85% of that tier. Kelly scales 1.0→0.40 between tier and floor. Floor never resets down. Hard per-trade cap: `max_single_position_usd` $18 (not pipeline-tunable)
- **SPRT** -- alpha 0.05, beta 0.10, observation_interval 10s -- telemetry only (logged in trade_context, does not gate entries)
- **Entry timing** -- continuous time multiplier: `normal_fraction` 0.60, `late_max_penalty` 0.60, `final_min_probability` 0.90 (last 30s gate). Flip trading: `flip_enabled` true, `flip_edge_premium` 0.015 (flat +1.5% extra edge for flip, max 1 flip per window)
- **Execution** -- FOK only (`use_maker_orders` false). Maker orders disabled (60s timeout wastes window time)
- **Logged only (not applied to sizing)** -- regime, consensus, GEX, vol ratio, oracle divergence, crowd bias, adverse selection, edge realization ratio

## Learning Pipeline

Fully autonomous — no human in the loop. Runs daily at 12:05 AM ET. Walk-forward validation with statistical adoption (z >= 1.65). 3-day cooldown between parameter changes. Pipeline tracks its own adoption outcomes (7d/30d actual vs predicted Sharpe).

1. **PipelineTracker** -- Reviews past adoptions, fills in actual 7d/30d Sharpe, feeds track record to Claude
2. **BiasDetector** -- Per-indicator/regime/time-weighted accuracy, edge realization quartiles, counterfactual analysis, distribution shift detection (KS-test)
3. **PlattCalibrator** -- Fits Platt scaling (A, B) on training set. Validates on holdout -- adopts only if log-loss improves
4. **TAEvolver** -- Sends distilled analysis card to Claude (regime stats, edge quartiles, SPRT evidence, pipeline track record). Robust JSON extractor. Principled local fallback: rule-based, max 2 params, max 15% change
5. **WeightOptimizer** -- Walk-forward backtest across 4 expanding-window folds. Adoption requires Jobson-Korkie z >= 1.65, all folds positive, n >= 100. SPRT evidence modulates threshold. Hot-swaps all params and persists to settings.yaml

Tunes 30+ parameters: indicator weights, all layer weights, student_t_df, min_edge, kelly_fraction, min_kelly, atr_sigma_ratio, min_model_probability, exit_edge_threshold, normal_fraction, late_max_penalty, flip_edge_premium, trading hours, logit_scale, probability_compression, liquidation_weight, consensus thresholds/multipliers, exit patience/urgency params, iv_ratio bounds.

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

Binance.US, Polymarket CLOB/Gamma, Bybit, Deribit, Coinbase, and Kraken APIs are all free and need no key.

## Git-Backed Persistence

All memory syncs via git -- outcomes, counterfactuals, DB, and config are tracked (not gitignored). The `run_polybot.ps1` wrapper commits and pushes at 12:05 AM after the daily pipeline, preserving state across restarts.

## Tests

```bash
python -m pytest polybot/tests/ -q   # 623 tests
```
