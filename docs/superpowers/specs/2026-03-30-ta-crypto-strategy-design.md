# PolyBot TA Crypto Strategy Design Spec

**Date:** 2026-03-30
**Status:** Approved
**Scope:** Replace general market Claude brain with TA-driven 5-min BTC crypto micro-trader

## Overview

Evolve PolyBot from a Claude-powered general prediction market bot into a focused, lightning-fast crypto micro-trader for Polymarket's 5-min BTC Up/Down markets. Technical indicators replace Claude as the trading brain. Claude remains only in the learning pipeline for pattern analysis. The 7-indicator system (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) feeds a gates + weighted scoring engine that makes sub-second decisions. The self-learning pipeline tunes indicator weights, gate thresholds, and entry parameters over time.

**Core philosophy:** Indicators generate the signal, gates prevent bad trades, weighted scoring quantifies conviction, Kelly sizes the bet, and the learning pipeline tunes every parameter while you sleep. Claude never touches the trading decision — it only analyzes patterns in the learning loop.

## Constraints

- Target market: 5-min BTC Up/Down on Polymarket only
- Price data: Binance WebSocket (free, no API key for public data)
- Decision cycle: 1 second
- Data ingestion: Event-driven (WebSocket, sub-second)
- Entry window: First 2 minutes of each 5-min contract
- Starting capital: <$100, paper trading first
- Risk tolerance: Conservative (Quarter Kelly)

---

## Layer 1: Binance Price Feed & Candle Store

**Data source:** Binance WebSocket streams:
- `wss://stream.binance.com:9443/ws/btcusdt@trade` — real-time ticks
- `wss://stream.binance.com:9443/ws/btcusdt@kline_1m` — 1-minute candles

**Candle store:** Rolling in-memory buffer of the last 200 1-minute candles. Updated in real-time as Binance pushes data.

**Each candle stores:**
- Open, High, Low, Close price
- Volume
- Tick count
- Timestamp

**Two data paths:**
```
INGESTION (event-driven, continuous):
  Binance WebSocket → parse tick/kline → update candle buffer
  No polling. Sub-second updates.

CONSUMPTION (1-second decision loop):
  Decision loop reads latest candle buffer snapshot
  Computes indicators → evaluates gates → scores signal
```

**Startup:** On boot, fetch last 200 1-min candles via Binance REST API (`GET /api/v3/klines`) to backfill the buffer. Then WebSocket takes over for live updates. No API key needed.

**Resilience:** If WebSocket disconnects, auto-reconnect with exponential backoff. If gap detected, re-fetch missing candles via REST.

---

## Layer 2: The 7 Indicators

Each indicator is a pure function: takes candle array in, outputs a signal value. Computed fresh every 1-second decision cycle from the candle buffer. All parameters configurable in `settings.yaml` and tunable by learning agents.

### 1. RSI (Relative Strength Index) — Context
- Period: 14 candles
- Output: 0-100
- Signal: <30 = oversold (favor YES/Up), >70 = overbought (favor NO/Down)

### 2. MACD (Moving Average Convergence Divergence) — Momentum
- Fast EMA: 12, Slow EMA: 26, Signal line: 9
- Output: MACD line, signal line, histogram
- Signal: MACD crosses above signal = bullish, below = bearish

### 3. Stochastic Oscillator — Timing
- %K period: 14, %D smoothing: 3
- Output: %K (0-100), %D (0-100)
- Signal: %K crosses %D below 20 = buy trigger, above 80 = sell trigger

### 4. EMA (Exponential Moving Average) — Structure
- Fast: 9-period, Slow: 21-period
- Output: two price levels
- Signal: fast above slow = uptrend, fast below slow = downtrend. Price chopping around both = no-trade zone.

### 5. OBV (On-Balance Volume) — Confirmation
- Cumulative volume tracking
- Output: OBV line and its slope (rising/falling over last 5 candles)
- Signal: OBV slope aligns with price direction = confirmed move, divergence = weak move

### 6. VWAP (Volume Weighted Average Price) — Value
- Resets each session (each 5-min contract period)
- Output: VWAP price level
- Signal: price far above VWAP = overextended, price far below = undervalued. Deviation > 1 std dev = mean reversion likely.

### 7. ATR (Average True Range) — Volatility Filter
- Period: 14 candles
- Output: volatility value
- Signal: ATR below 25th percentile of recent history = too quiet, skip. ATR above 90th percentile = too chaotic, skip. Only trade in the middle band.

---

## Layer 3: Signal Engine (Gates + Weighted Scoring)

### Phase 1: Hard Gates (must ALL pass or skip)

```
Gate 1: ATR Volatility Filter
  → ATR in tradable range? NO → SKIP

Gate 2: EMA Trend Structure
  → Clear trend (fast/slow separated)? NO → SKIP
  → Price chopping between EMAs? YES → SKIP

Gate 3: Active Market Check
  → Is there a live 5-min BTC contract on Polymarket? NO → SKIP
  → Within first 2 minutes of contract? NO → SKIP
  → Already have a position on this contract? YES → SKIP
```

If all gates pass, proceed to scoring.

### Phase 2: Weighted Signal Score

Each indicator outputs a value from -1.0 (strong bearish) to +1.0 (strong bullish):

| Indicator | What it scores | Starting weight |
|-----------|---------------|-----------------|
| RSI | Oversold/overbought extremes | 0.20 |
| MACD | Momentum direction + crossover strength | 0.25 |
| Stochastic | Entry timing precision | 0.20 |
| OBV | Volume confirmation | 0.15 |
| VWAP | Price vs fair value | 0.20 |

```
final_score = sum(indicator_score * weight) for all 5 scoring indicators
```

### Entry Rules
- `final_score >= 0.60` → BUY YES (betting Up)
- `final_score <= -0.60` → BUY NO (betting Down)
- Between -0.60 and 0.60 → no trade (signal not strong enough)

The 0.60 threshold and all 5 weights are tunable by the learning agents. They start at these defaults and evolve based on outcome data.

### Exit
These are 5-minute markets — they resolve on their own. No manual exit needed. The bot enters a position and waits for resolution. This eliminates exit timing entirely.

### Position Sizing
Same Quarter Kelly from the existing math engine, applied to signal confidence. Higher absolute score = more confident = larger position within Kelly limits.

---

## Layer 4: Market Scanner (5-min BTC Contracts)

**Scan loop (every 1 second, integrated with decision loop):**
1. Query Polymarket CLOB API for active BTC 5-min markets
2. Cache the current contract — don't re-query every second, only when the current one expires
3. When a new contract opens, signal the decision loop that a fresh trading opportunity exists

**Contract data captured:**
- Condition ID, token IDs (YES/NO)
- Current YES/NO prices
- Time to resolution (countdown)
- Volume and liquidity

**Timing awareness:**
- New contract opens → first 2 minutes is the entry window
- After 2 minutes → hold existing positions only, no new entries
- Between contracts → idle, keep indicators warm

---

## Layer 5: Learning Pipeline (TA-Focused)

Reuses the existing memory infrastructure with TA-specific intelligence.

### Outcome Reviewer (every hour)
Logs every resolved trade with:
- All 7 indicator values at entry time
- Signal score and weights used
- Gate states (which passed/failed)
- Entry timing (seconds into contract)
- Outcome (win/loss) and P&L
- Contract details

### Bias Detector (daily, pipeline step 1)
Detects indicator-level biases:
- Examples: "RSI signals below 25 have 80% accuracy but RSI between 25-35 only 45%"
- "OBV confirmation adds no value — trades with/without have same win rate"
- Stores correction factors per indicator in `memory/biases.json`

### TA Strategy Evolver (daily, pipeline step 2)
Analyzes outcome data to tune:
- Indicator weights (increase winning indicators, decrease losers)
- Gate thresholds (ATR band width, EMA chop detection sensitivity)
- Entry threshold (raise/lower the 0.60 signal score requirement)
- Entry window timing (early entries vs late entries profitability)

Uses Claude to analyze patterns: sends last 100 trades with full indicator snapshots, asks "what patterns distinguish winners from losers?"

Recommendations flagged in Discord for approval during paper trading.

### Weight Optimizer (daily, pipeline step 3)
- Backtests weight adjustments against historical outcomes
- If new weight configuration improves Sharpe ratio by >= 3%, recommends adoption
- Tracks weight versions: `weights_v001.json`, `weights_v002.json`, etc.

### Pipeline Chain
```
Bias Detector → TA Strategy Evolver → Weight Optimizer
```

### Memory Structure
```
memory/
  outcomes/              # One JSON per resolved trade (now with indicator snapshots)
  biases.json            # Correction factors per indicator
  strategy_log.md        # Evolution history
  lessons.json           # Accumulated learnings
  weights/
    weights_v001.json    # Starting weights
    weights_v002.json    # Evolved weights
  weight_scores.json     # Sharpe ratio per weight version
```

---

## Layer 6: Integration with Existing Infrastructure

### What stays as-is
- `config/` — add new TA sections to settings.yaml
- `db/models.py` — same schema, add indicator_snapshot column to positions
- `execution/paper_trader.py` — same safety checks and bankroll management
- `math_engine/decision_table.py` — Kelly sizing still applies
- `math_engine/returns.py` — log returns and Sharpe ratio unchanged
- `discord_bot/` — same commands and alerts, add TA-specific info
- `memory/` — same directory structure, outcomes include indicator data

### What gets replaced
- `core/scanner.py` → rewritten as `core/market_scanner.py` for 5-min BTC contract discovery
- `core/filters.py` → removed (gates in signal engine replace filters)
- `core/websocket_monitor.py` → replaced by `core/binance_feed.py`
- `agents/strategy_evolver.py` → replaced by `agents/ta_evolver.py`
- `agents/prompt_optimizer.py` → replaced by `agents/weight_optimizer.py`

### What stays but changes role
- `brain/claude_client.py` → only used by TA Strategy Evolver for analysis, not trading
- `brain/prompt_builder.py` → only used by evolver's Claude calls
- `brain/prompts/` → analysis prompts for evolver, not trading prompts

### What's new
- `core/binance_feed.py` — WebSocket price stream + rolling candle buffer
- `indicators/rsi.py` — RSI calculation
- `indicators/macd.py` — MACD calculation
- `indicators/stochastic.py` — Stochastic oscillator
- `indicators/ema.py` — EMA calculation
- `indicators/obv.py` — On-Balance Volume
- `indicators/vwap.py` — Volume Weighted Average Price
- `indicators/atr.py` — Average True Range
- `indicators/engine.py` — Combines all 7, manages weights
- `core/signal_engine.py` — Gates + weighted scoring
- `agents/ta_evolver.py` — TA-specific strategy evolution
- `agents/weight_optimizer.py` — Weight versioning and backtesting

### Updated main.py flow
```
Startup:
  Backfill 200 candles from Binance REST
  Connect Binance WebSocket
  Connect Polymarket for contract discovery
  Start Discord bot
  Start learning agent scheduler

1-second decision loop:
  1. Binance feed updates candle buffer (continuous)
  2. Check: is there an active 5-min BTC contract in entry window?
  3. Check: do we already have a position?
  4. Compute all 7 indicators from candle buffer
  5. Run gates (ATR, EMA, active market)
  6. If gates pass → compute weighted score
  7. If score exceeds threshold → size with Kelly → place trade
  8. Wait for resolution → log outcome

All within single asyncio process.
```

---

## Updated Project Structure

```
polybot/
  __init__.py
  main.py
  config/
    __init__.py
    loader.py
    settings.yaml
    .env.example
  db/
    __init__.py
    models.py
  math_engine/
    __init__.py
    decision_table.py
    returns.py
  core/
    __init__.py
    binance_feed.py       # NEW: WebSocket price stream + candle buffer
    market_scanner.py     # REPLACED: 5-min BTC contract discovery
    signal_engine.py      # NEW: gates + weighted scoring
  indicators/
    __init__.py
    rsi.py                # NEW
    macd.py               # NEW
    stochastic.py         # NEW
    ema.py                # NEW
    obv.py                # NEW
    vwap.py               # NEW
    atr.py                # NEW
    engine.py             # NEW: combines all 7, manages weights
  brain/
    __init__.py
    claude_client.py      # KEPT: used by TA evolver only
    prompt_builder.py     # KEPT: used by TA evolver only
    prompts/
      v001.txt
  execution/
    __init__.py
    base.py
    paper_trader.py
    live_trader.py
  agents/
    __init__.py
    scheduler.py
    outcome_reviewer.py   # MODIFIED: logs indicator snapshots
    bias_detector.py      # MODIFIED: indicator-level bias detection
    ta_evolver.py         # NEW: replaces strategy_evolver
    weight_optimizer.py   # NEW: replaces prompt_optimizer
  memory/
    outcomes/
    biases.json
    strategy_log.md
    lessons.json
    weights/
      weights_v001.json
    weight_scores.json
  discord_bot/
    __init__.py
    bot.py
    commands.py
    alerts.py
  tests/
    ...
  Dockerfile
  requirements.txt
  README.md
```

### New/Updated Dependencies
```
websockets>=13.0        # Binance WebSocket connection
numpy>=2.0.0            # Indicator calculations (already present)
```

---

## Deployment Path

1. **Phase 1 (now):** Paper trading on local machine. Indicators compute on real BTC data, trades simulated against real Polymarket 5-min contracts. Learning pipeline accumulates data and tunes weights.
2. **Phase 2 (when ready):** Docker → $5/month VPS. Switch to live mode. Start with <$100.
3. **Phase 3 (proven):** Scale capital based on Sharpe ratio. Enable auto-apply on weight optimizer.
