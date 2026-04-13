# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes the mathematical probability that BTC finishes above/below the opening strike price, compares that to the market's price, and trades when mispricing exceeds the noise floor and Kelly fraction justifies a position. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the exit threshold.

## Key Architecture Decisions

- **Multi-layer probability model (10 signal layers).** CDF drives decisions — layers nudge in logit space. Platt scaling calibration after all layers.
  - L1: Student-t CDF (df=5, fat tails). z = distance / (ATR/sigma_ratio * sqrt(time) * iv_ratio)
  - L2: Regime detection (1-lag autocorrelation of last N 1-min returns)
  - L3: CLOB order flow (book imbalance + trade flow)
  - L3b: Spot market flow (CVD-dominant from Binance aggTrades, taker gated by min trade count)
  - L3c: Wall pressure (L2 depth near strike from Binance 1000-level book)
  - L3e: Liquidation pressure (Bybit OI drop + price direction, weight now configurable)
  - L4: Indicator momentum (RSI, MACD, Stochastic, OBV, VWAP)
  - L5: Previous window momentum carry (resolution margin / ATR)
- **SPRT entry gate.** Sequential Probability Ratio Test accumulates evidence during 60s observe phase. Strong signals enter in 5-7 ticks, weak signals correctly skipped. **Actively gates entries** — trades are blocked if SPRT reaches SKIP status.
- **Rule-based regime detection.** Multi-state classifier (trending_up/trending_down/reverting/volatile/quiet/neutral) adjusts Kelly and edge thresholds per market condition. Trending direction derived from price returns (majority up vs down count), not CVD. Lookback=50 for stable autocorrelation (SE=0.14).
- **Signal consensus multiplier.** >80% signals agree -> Kelly 1.3x. <40% agree -> Kelly 0.6x. All thresholds and multipliers pipeline-tunable.
- **Adaptive alpha decay.** Tracks edge decay rate. Fast decay triggers early SPRT entry before observe phase ends. When `should_enter_now()` fires AND SPRT has reached ENTER, the bot breaks out of the observe phase early.
- **Gamma exposure (GEX).** Net options gamma from Deribit. Stabilizing gamma (positive GEX) -> 0.7x position size. Amplifying gamma (negative GEX) -> 1.3x position size.
- **Active position management.** Hold to $1 resolution when confident. Scalp exit when holding_edge < fee-aware threshold. **Trailing profit exit**: cheap entries (<$0.50) that peaked >$0.65 then drop 15%+ from peak. Same probability model for entry AND exit.
- **Up to 2 concurrent positions from different windows.** Half-Kelly when concurrent. Next window's strike must be established before entry. Same-window duplicates blocked.
- **Dynamic entry timing.** 0-60s: OBSERVE ONLY. 60-180s: normal. 180-240s: Kelly 0.7x. Last 60s: >90% confidence only, half Kelly.
- **Conviction multiplier on Kelly.** 1.3x at >90% confidence, 1.15x at 85-90%, 0.7x below 72%. All thresholds pipeline-tunable.
- **Bankroll acceleration.** Kelly ratchets 0.15 -> 0.18/0.22/0.25 at 200/400/750 trades. Uses Wilson score 95% CI lower bound (not point estimate) — prevents ratcheting on luck. `compute_kelly_tier()` called per trade, queries DB for track record. Drops back if WR falls.
- **Maker orders with FOK fallback.** Limit order first (0% fee), FOK fallback after 60s (1.8% fee). ~60% fee savings.
- **Coinbase Exchange feed.** Faster BTC/USD price source (leads Binance.US by 0.5-2s). Used as primary BTC price when fresh (<5s), Binance.US as fallback. No auth required.
- **Chainlink oracle feed.** Reads BTC/USD price from the same oracle Polymarket uses for resolution. Preferred for strike computation when available.
- **CLOB flow velocity.** Tracks midprice rate of change on Polymarket CLOB (cents/sec over 5s window). Detects informed flow (e.g., contract 60c->90c in 3s). Logged in trade_context for pipeline analysis.
- **Deribit options IV.** Forward-looking vol adjusts ATR-to-sigma ratio in L1. IV ratio clamped to [iv_ratio_min, iv_ratio_max] (configurable, default [0.5, 3.0]).
- **One trade per 5-min contract.** After any exit, that contract is blacklisted.
- **Auto-restart cycle.** `run_polybot.ps1` manages daily lifecycle: start at 12:15 AM ET, trade until 11:59 PM, pipeline at 12:05 AM, exit, commit config/outcomes/DB to git, push, restart at 12:15 AM.
- **Git-backed persistence.** Outcomes, counterfactuals, and DB tracked in git. `run_polybot.ps1` commits and pushes after the 12:05 AM pipeline, preserving state across restarts.
- **Kelly fraction = 0.15.** Conservative for binary outcomes where losses are total. Bankroll acceleration ratchets up with proven track record.
- **Dual entry gate + safety gates.** Kelly >= 0.015 (primary) AND edge >= 0.04 (noise floor). Safety: edge >20% = skip (miscalibration cap), momentum disagreement halves edge.
- **Signal layer weights (logit space).** L1 Student-t CDF drives decisions. L2-L5 adjust in logit space (weight x `logit_scale` max shift, default 4.0). Logit-space dampens adjustments near extremes. CDF must show direction before layers can push past 65% gate. `logit_scale` is pipeline-tunable.
- **Real-time WebSocket + Gamma API for prices.** CLOB WS provides real-time books, BBA, last trades, resolution events. Event-driven loop, HTTP fallback if WS disconnected. Gamma API for contract discovery. `outcomePrices` are stale — never use for edge.
- **Outcomes are "Up"/"Down".** Contract fields: `price_up`, `price_down`.
- **Binance.US, not Binance.com.** HTTP 451 for US IPs on .com.
- **Strike = BTC price at 5-min window boundary.** Derived from Chainlink oracle (preferred) or Binance candle buffer (fallback), not "first time bot sees the contract."
- **`--mode paper` CLI flag.** Persistent SQLite bankroll, real CLOB prices, live fee rates. Entry fees in shares, exit fees in USDC (matches Polymarket). Tick-snapped prices, FOK fill semantics, 50% max book depth. **Convex slippage**: `fill_pct * impact * (1 + fill_pct)`. **Net-edge gate**: rejects if slippage eats the edge. **Price sum gate**: skip if `price_up + price_down` outside [0.98, 1.02]. **Maker/FOK fee blend**: 65/35 random split (0% or full taker fee). Live mode uses py-clob-client SDK (EIP-712 signed orders). Requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env.

## Project Structure

```
polybot/
  main.py                    # Entry point, trading loop, _build_signal_engine() shared constructor
  config/settings.yaml       # ALL tunable parameters (30+)
  core/
    binance_feed.py          # WebSocket + candle buffer (ATR, indicators, strike)
    coinbase_feed.py         # Coinbase Exchange BTC-USD ticker (faster price source)
    clob_ws.py               # Real-time CLOB WebSocket feed (order books, trades, resolution, price velocity)
    market_scanner.py        # Gamma API discovery + CLOB HTTP helpers (spread, midpoints, volume)
    signal_engine.py         # Probability model: P(Up) from BTC vs strike + time + vol
    calibrator.py            # Platt scaling probability calibration (fitted daily)
    order_flow.py            # Book imbalance + trade flow signal from CLOB data
    binance_depth.py         # L2 order book: wall detection, spot imbalance, book depth
    binance_trades.py        # Aggregate trade stream: CVD, taker ratio, large trades, volume surge
    bybit_feed.py            # BTC perpetual price lead + funding rate signal
    deribit_iv.py            # BTC options implied volatility (forward-looking vol) + GEX computation
    bankroll_strategy.py     # Tiered Kelly acceleration based on track record
    sprt.py                  # Sequential Probability Ratio Test for evidence-based entry gate
    regime.py                # Multi-state regime detector (trending/reverting/volatile/quiet)
    liquidation.py           # OI-based liquidation pressure from Bybit
    gamma_exposure.py        # Net gamma exposure from Deribit options chain
    alpha_decay.py           # Edge decay rate tracker — triggers early SPRT entry
    chainlink_feed.py        # Chainlink BTC/USD oracle (resolution price source)
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all 7, manages weight versions, IndicatorNormalizer
  execution/
    base.py                  # BaseTrader ABC, TradeResult, FillResult, fee functions
    paper_trader.py          # PaperTrader(BaseTrader) — instant simulated fills
    live_trader.py           # LiveTrader(BaseTrader) — FOK market orders via py-clob-client SDK
    circuit_breaker.py       # Drawdown-based Kelly scaling (from initial principal, not peak)
  agents/
    scheduler.py             # Daily learning pipeline orchestrator
    outcome_reviewer.py      # Logs resolved trades to memory/outcomes/
    counterfactual_tracker.py # Tracks what-if for both scalps and holds
    bias_detector.py         # Per-indicator accuracy + counterfactual analysis
    ta_evolver.py            # Claude API recommendations for weight adjustments
    weight_optimizer.py      # Backtests and auto-adopts if Sharpe improves >= 3%
  brain/
    claude_client.py         # analyze_strategy() for daily pipeline
  memory/
    outcomes/                # One JSON per trade
    counterfactuals/         # One JSON per scalp or hold
    calibration/             # platt_params.json (A, B parameters)
    weights/                 # Versioned weight configs
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py                   # Commands: status, positions, history, performance, clear, session, pause/resume
    commands.py              # Formatting helpers
    alerts.py                # Trade alerts, session banners, channel purging
  db/models.py               # SQLite: positions, trade_history, bankroll, get_trade_stats()
  math_engine/
    returns.py               # Log returns, gain_pct (arithmetic returns for binary)
```

## Config

`polybot/config/settings.yaml` (validated by `validate_config()` on startup):
- `circuit_breaker.max_drawdown_pct:` — 0.30, `min_multiplier:` — 0.40, `losses_to_reduce:` — 3, `wins_to_restore:` — 2
- `math.kelly_fraction:` — 0.15
- `signal.entry_threshold:` — 0.04 (noise floor, range 0.01-0.10), `max_edge:` — 0.20 (safety net behind Platt, range 0.10-0.30)
- `signal.min_kelly:` — 0.015 (primary gate, range 0.005-0.05)
- `signal.exit_edge_threshold:` — -0.05
- `signal.min_model_probability:` — 0.65
- `signal.momentum_weight:` — 0.02 (L4, reduced — r=-0.04, pipeline can zero out), `regime_weight:` — 0.03 (L2), `flow_weight:` — 0.04 (L3)
- `signal.spot_flow_weight:` — 0.04 (L3b), `wall_weight:` — 0.05 (L3c)
- `signal.prev_margin_weight:` — 0.02 (L5), `liquidation_weight:` — 0.03 (L3e)
- `signal.atr_sigma_ratio:` — 1.4 (range 1.2-2.5), `student_t_df:` — 5, `min_atr:` — 8.0 (range 1.0-30.0)
- `signal.logit_scale:` — 4.0 (prob-to-logit multiplier), `probability_compression:` — 1.0 (CDF shrink, 1.0=off), `consensus_dead_zone:` — 0.05
- `signal.conviction:` — high_prob/mult (0.90/1.3x), mid_prob/mult (0.85/1.15x), low_prob/mult (0.72/0.7x) — all pipeline-tunable
- `signal.consensus:` — agreement thresholds and Kelly multipliers — all pipeline-tunable
- `signal.exit:` — patience_seconds, urgency_seconds, hold_min_prob, panic_edge, low_price_hold — all pipeline-tunable
- `signal.weights:` — per-indicator weights for momentum
- `deribit.iv_ratio_min:` — 0.5, `iv_ratio_max:` — 3.0
- `coinbase.ws_url:` — `wss://ws-feed.exchange.coinbase.com`, `product_id:` — `BTC-USD`
- `execution.max_concurrent_positions:` — 2, `max_bankroll_deployed:` — 0.80, `max_single_position_pct:` — 0.12, `max_book_fill_pct:` — 0.50
- `execution.slippage_impact_pct:` — 0.03, `use_maker_orders:` — true, `maker_timeout_s:` — 60.0
- `market.entry_window_seconds:` — 300, `min_time_remaining_seconds:` — 20, `max_spread:` — 0.10
- `market.clob_ws_url:` — `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `schedule.trading_start_hour_et:` — 0 (12:15 AM ET), `trading_start_minute:` — 15, `trading_end_hour_et:` — 23, `trading_end_minute:` — 59
- `agents.daily_pipeline_hour:` — 0, `daily_pipeline_minute:` — 5 (12:05 AM ET)
- `binance_depth.poll_interval_s:` — 5.0, `binance_trades` / `bybit` / `deribit` WS URLs in config
- `entry_timing.observe_seconds:` — 60, `late_kelly_multiplier:` — 0.7, `final_min_probability:` — 0.90
- `bankroll_acceleration.enabled:` — true (0.15 -> 0.18/0.22/0.25 as track record grows)
- `sprt.alpha:` — 0.05, `sprt.beta:` — 0.10
- `regime.lookback:` — 50 (increased from 20 for stable autocorrelation), `vol_high_percentile:` — 75, `vol_low_percentile:` — 25, `autocorr_threshold:` — 0.25

## Running

```bash
python -m polybot.main --mode paper   # Paper trading (persistent bankroll across sessions)
python -m polybot.main --mode live    # Live trading (real USDC on Polymarket)
python -m polybot.main                # Defaults to mode in settings.yaml
python -m polybot.main --run-pipeline # Run daily learning pipeline once and exit (no trading)
python -m pytest polybot/tests/       # 550 tests
```

## How the Probability Model Works

```
Strike = BTC price at 5-min window open. Chainlink oracle preferred (matches
Polymarket's priceToBeat), Binance candle buffer as fallback. Polymarket resolves
using Chainlink BTC/USD oracle, which can differ from Binance by $20-200.
Resolution always uses Gamma API eventMetadata or closed+outcomePrices.
Distance = current BTC price - strike
Vol = ATR (average true range from 1-min candles, period=7) / atr_sigma_ratio (1.4)
Time = minutes remaining in the window
BTC price = Coinbase Exchange (primary, 0.5-2s faster) or Binance.US (fallback)

LAYER 1 — Fat-tailed base (Student-t CDF, df=5):
  ATR_effective = max(ATR, min_atr)                   [floor prevents extreme z in quiet markets]
  vol = (ATR_effective / atr_sigma_ratio) x sqrt(minutes) x iv_ratio
  z = distance / vol
  z_scaled = z x sqrt(df / (df-2))               [variance normalization]
  P(Up) = t.cdf(z_scaled, df=5)                  [df=5: excess kurtosis=6]
  IF probability_compression < 1.0:               [shrink CDF toward 0.5, pipeline-tunable]
    P(Up) = 0.5 + (P(Up) - 0.5) x compression
  Why: BTC kurtosis ~6-8 — normal CDF underestimates reversal probability.

LAYER 2 — Regime detection (in logit space):
  autocorr = 1-lag autocorrelation of last N 1-min returns
  direction = sign(most_recent_return)    [NOT sign(prob - 0.5)]
  logit_p += autocorr x direction x (regime_weight x logit_scale)

LAYER 3 — Order flow (in logit space):
  flow_signal = 0.6 * book_imbalance + 0.4 * trade_flow
  logit_p += flow_signal x (flow_weight x logit_scale)

LAYER 4 — Indicator momentum (in logit space):
  Z-score normalized scores per indicator (IndicatorNormalizer)
  Weighted RSI/MACD/Stochastic/OBV/VWAP x (momentum_weight x logit_scale)

LAYER 3b — Spot market flow (in logit space, CVD-dominant):
  cvd_z = IndicatorNormalizer.normalize("cvd", CVD)  [z-scored from running EMA]
  cvd_component = tanh(cvd_z) * 0.8
  taker_component = (taker - 0.5) * 2 * 0.2   [only if trade_count >= 5, else 0]
  spot_flow = cvd_component + taker_component
  logit_p += spot_flow * (spot_flow_weight x logit_scale)

LAYER 3c — Wall pressure near strike (in logit space):
  wall_pressure = (ask_vol_near_strike - bid_vol_near_strike) / total
  logit_p -= wall_pressure * (wall_weight x logit_scale)

LAYER 3e — Liquidation pressure (in logit space):
  OI drop + price drop → bearish; OI drop + price rise → bullish
  logit_p += liquidation_pressure * (liquidation_weight x logit_scale)

LAYER 5 — Previous window momentum carry (in logit space):
  normalized_margin = (prev_btc_at_expiry - prev_strike) / ATR
  logit_p += tanh(normalized_margin) * (prev_margin_weight x logit_scale)

CALIBRATION — Platt scaling (ACTIVE):
  calibrated = 1 / (1 + exp(A x logit(raw_prob) + B))
  A, B persisted in memory/calibration/platt_params.json
  Re-fitted daily by pipeline (60/40 train/holdout validation)

NEGRISK EXECUTION PRICING:
  price_up = GET /price?token_id=UP&side=BUY   (cross-matched, not raw book)
  price_down = GET /price?token_id=DOWN&side=BUY
  sell_price = GET /price?token_id=TOKEN&side=SELL (for scalp exits)

ENTRY (10 gates — all must pass):
  SPRT != SKIP, prob >= 65%, edge >= 0.04, Kelly >= 0.015, spread <= 10%,
  depth >= $50, price_sum in [0.98,1.02], time >= 20s, edge <= 0.20, layer agreement
  Size = Kelly x kelly_fraction x breaker x phase x regime x consensus x GEX_bias
  Capped to 50% of book depth, 12% of bankroll, 80% total deployed
  Net-edge gate: rejects if slippage eats the edge

WHILE HOLDING (active position management):
  holding_edge = model_prob_for_our_side - current_market_price_for_our_side
  Fee-aware threshold: base_threshold - exit_fee_cost
  Patience: >patience_seconds remaining, threshold tightens by up to patience_max_penalty
  Time urgency: <urgency_seconds remaining, threshold relaxes by up to urgency_max_bonus
  If holding_edge ≤ effective_threshold: EXIT (scalp)
  If holding_edge > effective_threshold: HOLD

RESOLUTION (contract expired):
  Winning side pays $1.00/share, losing side pays $0.00/share.
```

## External APIs

All APIs are free, no auth required (except Claude and Discord).

### Coinbase Exchange (`wss://ws-feed.exchange.coinbase.com`)

| Endpoint | Usage |
|----------|-------|
| `WSS ticker` channel for BTC-USD | **Primary BTC price** (0.5-2s faster than Binance.US). Falls back to Binance when stale >5s. |

### Binance.US (not .com — HTTP 451 for US IPs)

| Endpoint | Usage |
|----------|-------|
| `wss://stream.binance.us:9443/ws/btcusdt@kline_1m` | Real-time 1-min candles. Drives ATR, indicators, candle buffer. Fallback BTC price. |
| `GET https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=200` | REST backfill on startup. |
| `wss://stream.binance.us:9443/ws/btcusdt@depth20@100ms` | Top 20 order book levels, 100ms. Spot imbalance + depth. |
| `GET https://api.binance.us/api/v3/depth?symbol=BTCUSDT&limit=1000` | Full 1000-level book for wall detection near strike. Polled every 5s. |
| `wss://stream.binance.us:9443/ws/btcusdt@aggTrade` | Every trade with taker side. Drives CVD + taker ratio (Layer 3b). |

### Polymarket CLOB API (`https://clob.polymarket.com`)

| Endpoint | Usage |
|----------|-------|
| `WSS .../ws/market` | **Primary price source.** Real-time books, BBA, last trades, resolution events. Price velocity tracked. |
| `GET /book?token_id=TOKEN` | Full order book. HTTP fallback when WS disconnected. |
| `GET /price?token_id=TOKEN&side=BUY\|SELL` | **Primary execution price.** NegRisk cross-matched (not raw book). |
| `GET /fee-rate?token_id=TOKEN` | Taker fee rate (crypto = 1.8%). Fee = rate x shares x p x (1-p). |
| `GET /tick-size?token_id=TOKEN` | Min price increment. Prices snapped via `snap_to_tick()`. |
| `GET /spread?token_id=TOKEN` | Bid-ask spread. Entry filter (skip if > 10%). |
| `GET /midpoints?token_ids=T1,T2` | Midpoint prices for hold evaluation. |
| `GET /last-trades-prices?token_ids=T1,T2` | Last trade prices. Fill validation. |

### Polymarket Gamma API (`https://gamma-api.polymarket.com`)

| Endpoint | Usage |
|----------|-------|
| `GET /events?slug=btc-updown-5m-{window_ts}` | Contract discovery, resolution detection, token IDs, seconds_remaining. |

**Note:** `outcomePrices` from Gamma are stale — never use for edge calculation.

### Polymarket Data API (`https://data-api.polymarket.com`)

`GET /live-volume?id=EVENT_ID` — Dead market filter.

### Bybit (`wss://stream.bybit.com/v5/public/linear`)

Real-time BTC/USDT perpetual ticker (lastPrice, fundingRate). REST backup: `GET /v5/market/tickers` every 300s.

### Deribit (`https://www.deribit.com/api/v2/public`)

`GET /get_book_summary_by_currency?currency=BTC&kind=option` — ATM implied volatility, polled every 60s. Drives iv_ratio in Layer 1. Also computes net gamma exposure (GEX) from full options chain.

### Claude API + Discord

- **Claude**: `claude-sonnet-4-6` via SDK. TAEvolver sends analysis + trades, gets structured JSON recommendations. Falls back to local math.
- **Discord**: Trade alerts, session banners, commands (`!status`, `!positions`, `!history`, `!performance`, `!pause`/`!resume`), daily reports to `#polybot-daily`. `!pause` blocks entries only — position management continues.

## Canonical Paper Trader Dataflow — DO NOT DEVIATE

Live trader preserves the same dataflow shape — same gates, ordering, invariants. Only `open_trade()`, `close_trade()`, `resolve_position()` differ (mock fill vs real CLOB order).

### Phase 1: Market Discovery

```
Coinbase WS (BTC-USD ticker)        Binance WS (1-min candles)          Gamma API (contract discovery)
  fastest BTC price (primary)          candle buffer, ATR, indicators       btc-updown-5m-{window_ts}
        |                                    |                              token IDs, seconds_remaining
        +-----> btc_price <---------+        |                                    |
                                             v                            CLOB WS subscribe(token_up, token_down)
                                    200-candle rolling buffer              -> book snapshots, BBA, last trades, resolution
                                    strike from Chainlink (preferred)         + price velocity tracking
                                    or Binance candle boundary (fallback)
```

### Phase 2: Signal Generation

All 10 signal layers feed `signal_engine.evaluate()` (see "How the Probability Model Works" for formulas). Layers 2-5 applied in logit space. Output: `final_prob`, `edge`, `side`, `kelly_size`.

10 entry gates (all must pass): SPRT != SKIP, confidence >= 0.65, edge >= 0.04, Kelly >= 0.015, spread <= 0.10, depth >= $50, price_sum in [0.98, 1.02], time >= 20s, edge <= 0.20, layer agreement.

### Phase 3: Sizing

```
bankroll_acceleration: compute_kelly_tier(trade_count, win_rate) -> dynamic kelly_fraction
raw_size = bankroll x kelly_size x breaker.kelly_multiplier
PHASE MULT: normal=1.0, late=0.7, final=0.5
REGIME MULT: trending=1.2, reverting=0.8, volatile=0.7, quiet=SKIP
CONSENSUS MULT: >80% agree=1.3, 60-80%=1.0, 40-60%=0.8, <40%=0.6
GEX MULT: stabilizing=0.7, neutral=1.0, amplifying=1.3
CAP CHAIN: size < $0.10 -> REJECT | size > 80% bankroll -> cap | size > 12% bankroll -> cap | size > 50% depth -> cap
NET EDGE: net_edge = gross_edge - (price x convex_slippage); if < min_edge -> REJECT
```

### Phase 4: Execution

`BaseTrader.open_trade()` (shared): GET /price -> apply slippage -> snap_to_tick -> GET /fee-rate -> compute entry fee (in shares) -> 3 rejection gates (duplicate, max positions, max deployed) -> DB insert + bankroll debit -> TradeResult.

### Phase 5: Position Management

Event-driven loop (~1-2ms per tick from CLOB WS):
- **Expired + closed**: Resolve via Gamma/Chainlink ($1.00 or $0.00). Orphaned positions wait indefinitely (Discord alert after 1hr).
- **Still active**: `evaluate_hold()` recomputes holding_edge. If below fee-aware threshold -> scalp exit. Trailing profit exit: cheap entries (<$0.50) that peaked above $0.65 then dropped 15%+ from peak.
- **Counterfactuals**: Scalps tracked until window expires (what if held?). Holds track worst moment (what if scalped?). Both written to `memory/counterfactuals/`.

### Phase 6: Outcome -> Learning

Outcomes saved to `memory/outcomes/`. Daily pipeline (see "Learning Pipeline" section) analyzes training set (60%), recommends params, backtests on validation set (40%), auto-adopts if Sharpe improves >= 3%.

### Paper vs Live

| Aspect | Paper | Live |
|--------|-------|------|
| `_execute_buy/sell()` | Instant simulated fill | FOK market order + retry |
| `_resolve_bankroll()` | Compute shares x price - fee | Fetch real USDC balance |
| Bankroll init | From SQLite | Fetch from Polymarket API |
| Slippage | Convex model simulation | Actual VWAP fill (convex for pre-trade gate) |
| **Everything else** | **Shared via BaseTrader ABC** | **Shared via BaseTrader ABC** |

**Invariants (both modes):** Entry fee in shares, exit fee in USDC, rejection gates before exchange interaction, TradeResult/FillResult contract boundaries.

## Common Issues

- **No trades:** BTC is near the strike (no edge) or market is efficiently priced. This is correct behavior — no edge means no trade.
- **Binance 451:** Using .com instead of .us.
- **Wrong strike:** Strike for the probability model is derived from Chainlink oracle (preferred) or Binance candle buffer at the contract's window boundary (parsed from slug). Polymarket resolves using Chainlink oracle, which can differ from Binance by $20-200. Resolution always waits for Gamma API eventMetadata or closed+outcomePrices — never guesses from Binance. If buffer is empty on startup, first few windows may have no strike.
- **All trades losing:** Check if model is systematically miscalibrated. Lower kelly_fraction or raise min_edge.
- **Startup config error:** `validate_config()` in `loader.py` validates all parameter bounds on startup and raises `ValueError` listing all violations. Fix settings.yaml values to be within documented ranges.
- **Orphaned position not resolving:** Positions now wait indefinitely for Gamma/Chainlink resolution data (no Binance fallback). A Discord alert fires after 1 hour. This is by design — Binance and Chainlink can disagree by $20-200, so guessing from Binance is unsafe.

## Learning Pipeline

Daily at 12:05 AM ET (configurable via `agents.daily_pipeline_hour` and `daily_pipeline_minute`):

**Hold-out split:** 60/40 chronological — first 60% for analysis, last 40% for backtest validation. Prevents in-sample overfitting.

**Minimum data:** TAEvolver and WeightOptimizer skip if <200 trades (enforced in code). BiasDetector always runs. Platt calibrator requires >=200 outcomes for re-fitting.

1. **BiasDetector** — Analyzes the **training set** (first 60% of outcomes):
   - Per-indicator accuracy (bullish/bearish breakdown, sample sizes)
   - Side analysis (Up vs Down win rate)
   - Edge calibration (do larger edges actually win more?)
   - Time patterns (win rate by seconds remaining at entry)
   - Volatility patterns (win rate by ATR regime)
   - Overall statistics (Sharpe, win rate, avg edge)
   - Counterfactual analysis (both scalps AND holds): was the exit/hold decision optimal?

1.5. **PlattCalibrator** — Fits Platt scaling parameters (A, B) on training set model probabilities vs actual outcomes. Validates on holdout — only adopts if log-loss improves. Persists to `memory/calibration/platt_params.json`. Applied after all layers in `compute_probability()`.

2. **TAEvolver** — Sends training-set analysis + trades + config to Claude API. Returns structured JSON with weight adjustments and parameter recommendations. Server-side validated (weights sum to 1.0, constraints enforced). Falls back to local math if API fails.

3. **WeightOptimizer** — Backtests recommendations against the **validation set** (last 40%). Auto-adopts if Sharpe improves >= 3%. Hot-swaps ALL signal weights and entry/exit params at runtime, persists to settings.yaml. Discord alerts with findings.

Outcomes enriched with `trade_context` (btc_price, strike, seconds_remaining, market prices, model_probability, edge, ATR, flow scores, clob_velocity, coinbase_btc) plus `gain_pct`, `pnl`, and `fees`.

**Performance metrics use `gain_pct` (arithmetic returns), NOT `log_return`.** Log returns are mathematically broken for binary outcomes where exit_price=0 produces log(0)=-infinity. The `gain_pct` metric is bounded [-1, +inf) and gives an honest, positive Sharpe for profitable strategies. The `log_return` field is still stored for backward compatibility but is never used for Sharpe calculation.

## What NOT to Change

- Don't add fixed take-profit/stop-loss percentages — use the probability model for exit decisions (evaluate_hold).
- Don't increase momentum_weight above 0.10 — indicators alone should not trigger trades.
- Don't use normal CDF / logistic approximation — use Student-t CDF (fat tails). The normal distribution underestimates reversal probability for BTC.
- Don't remove complementary pricing — it's essential for seeing real underdog prices in negRisk binary markets.
- Don't increase flow_weight above 0.10 — order flow should nudge, not dominate. CDF drives decisions.
- Don't use `log_return` for Sharpe calculation — use `gain_pct` (arithmetic returns). Log returns are broken for binary outcomes (log(0) = -infinity).
- Don't use raw CLOB book asks/bids for entry/exit pricing — use `GET /price?token_id=X&side=BUY|SELL` for negRisk cross-matched execution prices. Raw book shows $0.99 on both sides; `/price` shows the real ~$0.50 price.
- Don't use Gamma API `outcomePrices` for edge calculation — they're stale/initial prices, not live order book.
- Don't hardcode fee rates — fetch from `GET /fee-rate?token_id=X`. Crypto is 0.072, not 0.05.
- Don't use polymarket.us for crypto — US platform has sports only. All crypto trading is on polymarket.com.
- Don't use Binance.com — use Binance.us.
- Don't allow more than 2 concurrent positions — max 2 from different windows, half-Kelly when concurrent. Same-window duplicates still blocked.
- Don't bypass the circuit breaker — it scales Kelly proportionally to drawdown from initial principal (not peak), protecting against compounding losses while still trading. Growing from $60 to $80 and dipping to $75 does NOT trigger drawdown — only falling below $60 does.
- Don't auto-delete the DB — bankroll persists across sessions in both modes. Never delete `polybot/db/polybot.db` between runs.
- Don't use limit orders in LiveTrader — FOK market orders for 5-min contract speed.
- Don't resolve positions by comparing Binance BTC price vs Binance strike — always wait for Gamma API `eventMetadata` or `closed` + `outcomePrices`. Binance and Chainlink (Polymarket's oracle) can disagree by $20-200, causing false WIN/LOSS.
- Don't compute entry strike from `int(now_ts // 300) * 300` — derive from the contract slug. The bot can find the next window's contract early, and current-time flooring gives the wrong window boundary.
- Don't apply layer adjustments in raw probability space — use logit (log-odds) space. Additive probability adjustments violate Bayesian evidence combination near the extremes.
- Don't derive regime direction from sign(prob - 0.5) — use sign(most recent return). The autocorrelation measures persistence, but the DIRECTION comes from recent returns.
- Don't bypass the SPRT gate — it's mathematically optimal for sequential binary decisions.

## Baseline — LOCKED

The core trading logic is FROZEN. Do not make structural changes to:
- `signal_engine.py` (10-layer probability model + evaluate_hold)
- `order_flow.py` (book imbalance + trade flow)
- Entry/exit/pricing logic in `main.py` (extracted helper functions)
- `base.py` (BaseTrader ABC, fee math, shared gates/DB ops)
- `paper_trader.py` / `live_trader.py` (extend BaseTrader — only 3 abstract methods each)

Only the daily learning pipeline (12:05 AM ET) tunes parameters slowly. Any proposed "improvement" to frozen code requires explicit user approval. New features go in NEW files/modules.

## Always Update

Update this file and README.md with every behavioral change.
