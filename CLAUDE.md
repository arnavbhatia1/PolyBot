# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down micro-trader for Polymarket. It uses 7 technical indicators for trading decisions and actively scalps within the 5-min window. Claude is only used in the daily learning pipeline to analyze trade patterns.

## Key Architecture Decisions

- **Indicators are the brain, not Claude.** The 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) produce the trading signal. Claude only runs once daily in the TA Strategy Evolver to analyze what patterns distinguish winners from losers.
- **Gates before scoring.** ATR and EMA are hard gates (pass/fail). Only if both pass do the remaining 5 indicators produce a weighted score.
- **5-min markets use Gamma API with deterministic slugs.** The CLOB `/markets` endpoint does NOT list these. Use `gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}` where `window_ts = int(time.time() // 300) * 300`.
- **Outcomes are "Up"/"Down", not "Yes"/"No".** The contract fields are `price_up`, `price_down`, `token_id_up`, `token_id_down`.
- **Binance.US, not Binance.com.** Binance.com returns HTTP 451 for US IPs. All endpoints use `api.binance.us` and `stream.binance.us`.
- **1-second decision loop.** Every second: check open positions for scalp exit, then check for new entry signals.
- **Active scalping, not hold-to-resolution.** The bot monitors open positions every second and sells when take-profit (10%) or stop-loss (8%) is hit. It does NOT just wait for the 5-min market to resolve.
- **Entry window is 4 minutes.** Bot can enter during the first 240 seconds of each 5-min contract. Last 30 seconds are blocked.

## Project Structure

```
polybot/
  main.py                    # Entry point, async trading loop with scalp exits
  config/settings.yaml       # ALL tunable parameters
  core/
    binance_feed.py          # WebSocket + candle buffer (data ingestion)
    market_scanner.py        # Gamma API slug-based contract discovery
    signal_engine.py         # Gates + weighted scoring
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all 7, manages weight versions
  execution/
    paper_trader.py          # Simulated trades (live_trader.py is Phase 2)
  agents/
    scheduler.py             # Orchestrates daily learning pipeline
    outcome_reviewer.py      # Logs resolved trades with indicator snapshots
    bias_detector.py         # Finds indicator-level biases
    ta_evolver.py            # Recommends weight/threshold adjustments
    weight_optimizer.py      # Versions and adopts weight configs
  brain/
    claude_client.py         # ONLY used by ta_evolver, NOT in trading loop
  memory/
    outcomes/                # One JSON per trade (learning data)
    weights/                 # Versioned weight configs (weights_v001.json, etc.)
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py, commands.py, alerts.py
  db/models.py               # SQLite: positions, trade_history, bankroll
```

## Config

Everything tunable lives in `polybot/config/settings.yaml`. Key sections:
- `indicators:` -- periods, thresholds for each of the 7 indicators
- `signal:` -- entry threshold (0.40 for paper trading), indicator weights, active weight version
- `market:` -- entry window (240s), min time remaining (30s)
- `scalping:` -- take_profit_pct (0.10), stop_loss_pct (0.08)
- `binance:` -- symbol, WebSocket/REST URLs (binance.us), buffer size

## Running

```bash
python -m polybot.main          # From the PolyBot/PolyBot directory
python -m pytest polybot/tests/ # Run all tests
```

## Logging

- Base log level is ERROR (suppresses httpx, discord, websockets noise)
- `polybot` logger is INFO -- only shows startup, trades, and errors
- Log file resets on each startup (no growing log)
- PyNaCl/davey Discord warnings are suppressed

## Common Issues

- **No trades happening:** Check EMA trend (chop = no trades), ATR gate (too quiet/volatile = no trades), entry threshold (lower it in settings.yaml for paper trading). The bot is designed to be selective.
- **Binance 451 error:** Using binance.com instead of binance.us. Check `binance.rest_url` and `binance.ws_url` in settings.yaml.
- **No market found:** 5-min BTC markets use deterministic slugs via Gamma API, not the CLOB markets listing. Check `market_scanner.py`.
- **Discord token error:** Use the Bot Token (Bot tab), not the Client Secret. No quotes in .env.
- **Config not taking effect:** `main.py` must pass indicator params from settings.yaml to `IndicatorEngine(params=...)`. Check that config values are being read, not hardcoded defaults.

## Learning Pipeline

Daily at 2 AM UTC, three agents run in sequence:
1. BiasDetector reads `memory/outcomes/`, writes `memory/biases.json`
2. TAEvolver reads outcomes + biases, recommends weight changes, writes `memory/strategy_log.md`
3. WeightOptimizer backtests recommended weights, saves new version to `memory/weights/` if Sharpe improves >= 3%

Weight versions: `weights_v001.json` (initial), `weights_v002.json` (first evolution), etc.

## Testing

178 tests across all modules. Tests use `tmp_path` fixtures and in-memory SQLite -- no external dependencies needed to run tests.

## What NOT to Change

- Don't put Claude in the trading loop. Indicators are faster and cheaper.
- Don't switch back to Binance.com -- US IPs are blocked.
- Don't use CLOB `/markets` for 5-min crypto markets -- they only exist via Gamma API slugs.
- Don't use "Yes"/"No" for crypto markets -- they use "Up"/"Down".

## Always Update

When making changes, update BOTH this file and README.md to reflect the current state. Documentation must stay in sync with the code.
