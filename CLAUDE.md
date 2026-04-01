# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes the mathematical probability that BTC finishes above/below the opening strike price, compares that to the market's price, and trades when mispricing exceeds 10%. Hold to resolution — no scalping.

## Key Architecture Decisions

- **Probability model, not indicators.** The bot computes P(Up) using Brownian motion: z = (BTC - strike) / (ATR * sqrt(time)), then P = logistic(1.7z). Indicators provide a small momentum nudge (±8%). The edge is: model probability - market price.
- **Hold to resolution.** Binary markets resolve to 0 or 1. Scalping throws away edge. Kelly sizing already accounts for total loss risk.
- **Single position at a time.** Full Kelly on the best edge, no capital dilution.
- **One trade per 5-min contract.** After any exit, that contract is blacklisted.
- **Kelly fraction = 0.15.** Conservative for binary outcomes where losses are total.
- **Minimum edge = 10%.** Only trade when model disagrees with market by 10%+.
- **Momentum weight = 0.08.** Indicators nudge probability by max ±8%. This ensures indicators alone (without BTC movement from strike) cannot trigger a trade.
- **5-min markets use Gamma API with deterministic slugs.** `gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}` where `window_ts = int(time.time() // 300) * 300`.
- **Outcomes are "Up"/"Down".** Contract fields: `price_up`, `price_down`.
- **Binance.US, not Binance.com.** HTTP 451 for US IPs on .com.
- **Strike = BTC price at 5-min window boundary.** Derived from candle buffer, not "first time bot sees the contract."

## Project Structure

```
polybot/
  main.py                    # Entry point, trading loop (hold to resolution)
  config/settings.yaml       # ALL tunable parameters
  core/
    binance_feed.py          # WebSocket + candle buffer
    market_scanner.py        # Gamma API slug-based contract discovery
    signal_engine.py         # Probability model: P(Up) from BTC vs strike + time + vol
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all 7, manages weight versions
  execution/
    base.py                  # TradeResult dataclass
    paper_trader.py          # Simulated trades
  agents/
    scheduler.py             # Daily learning pipeline
    outcome_reviewer.py      # Logs resolved trades
    bias_detector.py         # Per-indicator accuracy
    ta_evolver.py            # Recommends weight adjustments
    weight_optimizer.py      # Versions and auto-adopts weights
  brain/
    claude_client.py         # Used by ta_evolver only
  memory/
    outcomes/                # One JSON per trade
    weights/                 # Versioned weight configs
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py, commands.py, alerts.py
  db/models.py               # SQLite: positions, trade_history, bankroll
  math_engine/
    decision_table.py        # Legacy — not used in main trading loop
    returns.py               # Log returns, Sharpe ratio
```

## Config

`polybot/config/settings.yaml`:
- `math.kelly_fraction:` — 0.15 (fraction of full Kelly)
- `signal.entry_threshold:` — 0.10 (minimum 10% edge to trade)
- `signal.momentum_weight:` — 0.08 (max ±8% indicator adjustment to probability)
- `signal.weights:` — per-indicator weights for momentum calculation
- `execution.max_concurrent_positions:` — 1 (single position, full focus)
- `execution.max_bankroll_deployed:` — 0.80
- `market.entry_window_seconds:` — 300 (full 5-min window)
- `market.min_time_remaining_seconds:` — 5

## Running

```bash
rm polybot/db/polybot.db              # Fresh bankroll
python -m polybot.main                # Run the bot
python -m pytest polybot/tests/       # 150 tests
```

## How the Probability Model Works

```
Strike = BTC price at 5-min window open (from candle buffer)
Distance = current BTC price - strike
Vol = ATR (average true range from 1-min candles)
Time = minutes remaining in the window

z = distance / (vol * sqrt(time))
P(Up) = 1 / (1 + exp(-1.7 * z))

Momentum nudge: P(Up) += indicator_score * 0.08

Edge = P(Up) - market_price_up    [or P(Down) - market_price_down]
If edge >= 10%: TRADE, size = Kelly(probability, market_price) * 0.15
If edge < 10%: SKIP
```

## Common Issues

- **No trades:** BTC is near the strike (no edge) or market is efficiently priced. This is correct behavior — no edge means no trade.
- **Binance 451:** Using .com instead of .us.
- **Wrong strike:** Strike is derived from candle buffer at window boundary. If buffer is empty on startup, first few windows may have wrong strike.
- **All trades losing:** Check if model is systematically miscalibrated. Lower kelly_fraction or raise min_edge.

## Learning Pipeline

Daily at 2 AM UTC:
1. BiasDetector: per-indicator accuracy from outcomes
2. TAEvolver: recommends weight adjustments
3. WeightOptimizer: backtests, auto-adopts if Sharpe improves >= 3%

Hot-swaps weights at runtime. Discord alerts for all pipeline events.

## What NOT to Change

- Don't add scalping/stop-losses — binary markets resolve to 0 or 1. Hold to resolution.
- Don't increase momentum_weight above 0.10 — indicators alone should not trigger trades.
- Don't use CLOB `/markets` for 5-min markets — Gamma API slugs only.
- Don't use Binance.com — use Binance.us.
- Don't allow multiple concurrent positions — one at a time, full Kelly.

## Always Update

Update this file and README.md with every behavioral change.
