# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down micro-trader for Polymarket. It uses technical indicators (not an LLM) for trading decisions. Claude is only used in the daily learning pipeline to analyze trade patterns.

## Key Architecture Decisions

- **Indicators are the brain, not Claude.** The 7 indicators (RSI, MACD, Stochastic, EMA, OBV, VWAP, ATR) produce the trading signal. Claude only runs once daily in the TA Strategy Evolver to analyze what patterns distinguish winners from losers.
- **Gates before scoring.** ATR and EMA are hard gates (pass/fail). Only if both pass do the remaining 5 indicators produce a weighted score.
- **5-min markets use Gamma API with deterministic slugs.** The CLOB `/markets` endpoint does NOT list these. Use `gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}` where `window_ts = int(time.time() // 300) * 300`.
- **Outcomes are "Up"/"Down", not "Yes"/"No".** The contract fields are `price_up`, `price_down`, `token_id_up`, `token_id_down`.
- **Binance.US, not Binance.com.** Binance.com returns HTTP 451 for US IPs. All endpoints use `api.binance.us` and `stream.binance.us`.
- **1-second decision loop.** Every second: check for active contract, compute indicators from candle buffer, evaluate gates + score, trade if strong.
- **No manual exits.** 5-min markets resolve automatically. The bot enters and waits.

## Project Structure

```
polybot/
  main.py                    # Entry point, async trading loop
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
- `signal:` -- entry threshold, indicator weights, active weight version
- `market:` -- entry window (120s), min time remaining (30s)
- `binance:` -- symbol, WebSocket/REST URLs, buffer size

## Running

```bash
python -m polybot.main          # From the PolyBot/PolyBot directory
python -m pytest polybot/tests/ # Run all tests
```

## Common Issues

- **No trades happening:** Check EMA trend (chop = no trades), ATR gate (too quiet/volatile = no trades), entry threshold (lower it in settings.yaml for paper trading). Run diagnostic: check indicator values and gate results.
- **Binance 451 error:** Using binance.com instead of binance.us. Check `binance.rest_url` and `binance.ws_url` in settings.yaml.
- **No market found:** 5-min BTC markets use deterministic slugs via Gamma API, not the CLOB markets listing. Check `market_scanner.py`.
- **Discord token error:** Use the Bot Token (Bot tab), not the Client Secret. No quotes in .env.

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
- Don't add manual exit logic -- 5-min markets self-resolve.
