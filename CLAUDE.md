# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down scalper for Polymarket. It uses 7 technical indicators for trading decisions and actively scalps within each 5-min window. Claude is only used in the daily learning pipeline to analyze trade patterns.

## Key Architecture Decisions

- **Indicators are the brain, not Claude.** The 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) produce the trading signal. Claude only runs once daily in the TA Strategy Evolver.
- **Gates before scoring.** ATR and EMA are hard gates (pass/fail). Only if both pass do the remaining 5 indicators produce a weighted score.
- **Active scalping.** Bot monitors open positions every second. Sells on take-profit (10%) or stop-loss (8%). Does NOT just wait for resolution.
- **5-min markets use Gamma API with deterministic slugs.** The CLOB `/markets` endpoint does NOT list these. Use `gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}` where `window_ts = int(time.time() // 300) * 300`.
- **Outcomes are "Up"/"Down", not "Yes"/"No".** Contract fields: `price_up`, `price_down`, `token_id_up`, `token_id_down`.
- **Binance.US, not Binance.com.** Binance.com returns HTTP 451 for US IPs.
- **1-second decision loop.** Every second: check open positions for scalp exit, then check for new entry signals.
- **Entry window is the full 5 minutes.** Last 5 seconds blocked to avoid unfillable orders.
- **Outcomes recorded after every exit.** Both take-profit and stop-loss exits log to `memory/outcomes/` for the learning pipeline.

## Project Structure

```
polybot/
  main.py                    # Entry point, 1-second trading loop with scalp exits
  config/settings.yaml       # ALL tunable parameters
  core/
    binance_feed.py          # WebSocket + candle buffer (data ingestion)
    market_scanner.py        # Gamma API slug-based contract discovery
    signal_engine.py         # Gates + weighted scoring
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all 7, manages weight versions
  execution/
    base.py                  # TradeResult dataclass
    paper_trader.py          # Simulated trades (live_trader.py is Phase 2)
  agents/
    scheduler.py             # Orchestrates daily learning pipeline
    outcome_reviewer.py      # Logs resolved trades with indicator snapshots
    bias_detector.py         # Finds indicator-level biases
    ta_evolver.py            # Recommends weight/threshold adjustments
    weight_optimizer.py      # Versions and adopts weight configs
  brain/
    claude_client.py         # ONLY used by ta_evolver, NOT in trading loop
    prompt_builder.py        # Builds prompts for ta_evolver analysis
  memory/
    outcomes/                # One JSON per trade (learning data)
    weights/                 # Versioned weight configs (weights_v001.json, etc.)
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py, commands.py, alerts.py
  db/models.py               # SQLite: positions, trade_history, bankroll
  math_engine/
    decision_table.py        # Kelly fraction position sizing
    returns.py               # Log returns, Sharpe ratio
```

## DB Schema Field Names

Fields use TA-specific names (not the old Claude-era names):
- `signal_score` — indicator weighted score (0-1)
- `signal_strength` — confidence level
- `weight_version` — which weight config was used (e.g. "weights_v001")
- `indicator_snapshot` — JSON blob of all 7 indicator values at entry

## Config

Everything tunable lives in `polybot/config/settings.yaml`:
- `indicators:` — periods, thresholds for each of the 7 indicators
- `signal:` — entry threshold (0.40 for paper trading), indicator weights, active weight version
- `market:` — entry window (300s / full window), min time remaining (5s)
- `scalping:` — take_profit_pct (0.10), stop_loss_pct (0.08)
- `binance:` — symbol, WebSocket/REST URLs (binance.us), buffer size
- `math:` — EV threshold, Kelly fraction, exit target, stop loss
- `execution:` — max slippage, bankroll limits, position limits

## Running

```bash
python -m polybot.main          # From the PolyBot/PolyBot directory
python -m pytest polybot/tests/ # Run all tests (148 tests)
```

## Logging

- Base log level is ERROR (suppresses httpx, discord, websockets noise)
- `polybot` logger is INFO — only shows startup, trades, and errors
- Log file resets on each startup

## Common Issues

- **No trades happening:** Check EMA trend (chop = no trades), ATR gate, entry threshold (lower in settings.yaml). BTC needs to be moving directionally.
- **Binance 451 error:** Using binance.com instead of binance.us.
- **No market found:** 5-min BTC markets use deterministic slugs via Gamma API, not CLOB.
- **Discord token error:** Use the Bot Token (Bot tab), not the Client Secret. No quotes in .env.
- **Config not taking effect:** main.py must pass indicator params from settings.yaml to IndicatorEngine(params=...).
- **Learning pipeline empty:** Outcomes are recorded after scalp exits. If no trades happen, no learning data accumulates.

## Learning Pipeline

Daily at 2 AM UTC, three agents run in sequence:
1. BiasDetector reads `memory/outcomes/`, writes `memory/biases.json`
2. TAEvolver reads outcomes + biases, recommends weight changes, writes `memory/strategy_log.md`
3. WeightOptimizer backtests recommended weights against historical trades. If Sharpe improves >= 3%, **auto-adopts** new weights — hot-swaps indicator_engine and signal_engine at runtime. No manual intervention needed.

**Discord alerts from pipeline:**
- Weights adopted: posts new Sharpe, win rate, and weight values to `#polybot-trades`
- No change: posts current vs candidate Sharpe
- Negative Sharpe (<-0.5): posts WARNING to `#polybot-control` suggesting `!pause`
- Pipeline error: posts error to `#polybot-control`

## What NOT to Change

- Don't put Claude in the trading loop. Indicators are faster and cheaper.
- Don't switch back to Binance.com — US IPs are blocked.
- Don't use CLOB `/markets` for 5-min crypto markets — only Gamma API slugs work.
- Don't use "Yes"/"No" for crypto markets — they use "Up"/"Down".
- Don't use old field names (claude_probability, etc.) — use signal_score, signal_strength, weight_version.
- Outcome records use: signal_score, profitable (bool), weight_version, indicator_snapshot. NOT predicted_probability/prompt_version.
- Positions that aren't scalped get auto-closed when contract expires.
- Bias detector analyzes per-indicator accuracy from indicator_snapshot, not category-level probabilities.

## Always Update

When making changes, update BOTH this file and README.md to reflect the current state.
