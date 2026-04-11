# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes the mathematical probability that BTC finishes above/below the opening strike price, compares that to the market's price, and trades when mispricing exceeds the noise floor and Kelly fraction justifies a position. Holds to $1 resolution when confident, exits early (scalps) when holding_edge drops below the exit threshold.

## Key Architecture Decisions

- **Multi-layer probability model (10 signal layers).** CDF drives decisions — layers nudge in logit space. Platt scaling calibration after all layers.
  - L1: Student-t CDF (df=5, fat tails). z = distance / (ATR/sigma_ratio * sqrt(time) * iv_ratio)
  - L2: Regime detection (1-lag autocorrelation of last N 1-min returns)
  - L3: CLOB order flow (book imbalance + trade flow)
  - L3b: Spot market flow (CVD + taker ratio from Binance aggTrades)
  - L3c: Wall pressure (L2 depth near strike from Binance 1000-level book)
  - L3d: Perpetual price lead (Bybit perp/spot divergence, 0.5-2s lead)
  - L3e: Liquidation pressure (Bybit OI drop + price direction)
  - L4: Indicator momentum (RSI, MACD, Stochastic, OBV, VWAP)
  - L5: Previous window momentum carry (resolution margin / ATR)
- **SPRT entry gate.** Sequential Probability Ratio Test accumulates evidence during 60s observe phase. Strong signals enter in 5-7 ticks, weak signals correctly skipped.
- **HMM regime detection.** Multi-state classifier (trending/reverting/volatile/quiet) adjusts Kelly and edge thresholds per market condition.
- **Signal consensus multiplier.** >80% signals agree -> Kelly 1.3x. <40% agree -> Kelly 0.6x.
- **Adaptive alpha decay.** Tracks edge decay rate. Fast decay triggers early SPRT entry before observe phase ends.
- **Gamma exposure (GEX).** Net options gamma from Deribit: stabilizing (dampen momentum) or amplifying (boost momentum).
- **Active position management.** Hold to $1 resolution when the model is confident. Exit early (scalp) when holding_edge drops below a fee-aware threshold that accounts for exit costs and time urgency. **Trailing profit exit**: for cheap entries (<$0.50), tracks peak market price — exits if market peaked above $0.65 then drops 15%+ from peak (prevents riding winners to zero). Same probability model for entry AND exit. Not fixed take-profit/stop-loss — the math decides.
- **Up to 2 concurrent positions from different windows.** Half-Kelly per position when concurrent (total exposure = same as single full-Kelly). Next window's strike must be established (candle closed at boundary) before entry. Same-window duplicates still blocked.
- **Dynamic entry timing.** First 60s of each 5-min window: OBSERVE ONLY (collect L2/CVD/flow signals). 60-180s: normal entry. 180-240s: reduced Kelly (0.7x). Last 60s: only at >90% confidence, half Kelly.
- **Conviction multiplier on Kelly.** Kelly scales 1.3x when model confidence >90%, 1.15x at 85-90%, 0.7x below 72%. Concentrates capital on highest-conviction trades.
- **Bankroll acceleration.** Kelly fraction ratchets from 0.15 base to 0.18 (100+ trades, >55% WR), 0.22 (250+, >56%), 0.25 (500+, >57%). Drops back if win rate falls.
- **Maker orders with FOK fallback.** LiveTrader posts limit orders (0% fee) first, waits up to 60s, falls back to FOK market order (1.8% fee) if not filled. Expected fee savings: ~60%.
- **Bybit perpetual price lead + funding rate.** Bybit BTC perp leads Binance.US spot by 0.5-2s. Used as directional signal and staleness detector for latency arbitrage. Funding rate provides contrarian crowding indicator.
- **Deribit options IV.** Forward-looking volatility from BTC options market. Dynamically adjusts ATR-to-sigma ratio in Layer 1 when market expects more/less vol than ATR shows.
- **One trade per 5-min contract.** After any exit, that contract is blacklisted.
- **Auto-restart cycle.** `run_polybot.ps1` wrapper script manages the daily lifecycle: starts bot at midnight with `--auto-restart`, bot trades until 11:55 PM pipeline, exits cleanly after pipeline, wrapper commits config/outcomes/DB to git, pushes to remote, waits for midnight, restarts. All memory syncs across machines via git.
- **Cross-machine sync.** Outcomes (`memory/outcomes/`), counterfactuals (`memory/counterfactuals/`), and `polybot/db/polybot.db` are tracked in git (not gitignored). Machine A pushes at 11:55 PM, Machine B pulls at midnight — full state synchronization across machines.
- **Kelly fraction = 0.15.** Conservative for binary outcomes where losses are total.
- **Dual entry gate + safety gates.** Primary: Kelly fraction >= min_kelly (0.015, price-aware — naturally accounts for odds at different price levels). Secondary: edge >= entry_threshold (0.04, noise floor). Both must pass. Additional safety: edge cap (>30% = skip, model miscalibration), layer disagreement penalty (momentum opposing trade direction with |score|>0.5 halves effective edge).
- **Signal layer weights (logit space).** Layer 1 (Student-t base) provides the core probability — the CDF drives all decisions. Layers 2-4 apply adjustments in logit (log-odds) space with config weight x 4.0 as max logit shift. Layer 2 (regime) weight 0.03. Layer 3 (order flow) weight 0.04. Layer 4 (momentum/indicators) weight 0.04. Logit-space application ensures adjustments near probability extremes (0.1 or 0.9) are naturally dampened. The CDF must show a direction before layers can push past the 65% confidence gate.
- **Real-time WebSocket + Gamma API for prices.** Primary: CLOB WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) provides real-time book snapshots, price deltas, best bid/ask, last trades, and market resolution events. Trading loop is event-driven — reacts instantly to book changes instead of polling. HTTP fallback: `clob.polymarket.com/book?token_id=TOKEN` if WS disconnected. Gamma API (`gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}`) for contract discovery and `token_id_up`/`token_id_down`. Supplementary: `GET /spread` for liquidity check, `GET /midpoints` for quick price ref, `GET /last-trades-prices` for fill validation. Gamma `outcomePrices` are stale fallback only.
- **NegRisk execution pricing via GET /price.** The raw token book (`GET /book`) only shows direct token orders — in negRisk binary markets the CLOB cross-matches across complementary tokens, so the raw book shows asks at $0.99 on both sides while the real executable price is ~$0.50. **Always use `GET /price?token_id=X&side=BUY` for entry pricing and `side=SELL` for exit pricing.** This is what Polymarket's website shows. Never use raw book best ask/bid for edge calculation.
- **Outcomes are "Up"/"Down".** Contract fields: `price_up`, `price_down`.
- **Binance.US, not Binance.com.** HTTP 451 for US IPs on .com.
- **Strike = BTC price at 5-min window boundary.** Derived from candle buffer, not "first time bot sees the contract."
- **`--mode paper` CLI flag.** Paper mode uses persistent SQLite bankroll with real CLOB order book prices for realistic fill simulation. Fee rates fetched live from `GET /fee-rate?token_id=X` (crypto = 1.8%). Entry fees collected in shares (fewer shares received), exit fees in USDC — matching Polymarket's actual collection method. Prices snapped to market tick size. Min order size enforced from CLOB book. FOK fill semantics (100% fill or reject). Orders capped to 50% of available book depth. **Convex slippage model**: fills are penalized by `fill_pct * impact_factor * (1 + fill_pct)` where `fill_pct = order_size / book_depth`. Cost accelerates as the order walks through deeper price levels — at 50% depth the cost is 50% higher than a linear model, at 100% it is 2x. **Net-edge gate**: after Kelly sizing, estimated slippage is subtracted from edge; the trade is rejected if `net_edge < min_edge`. This prevents trades where execution cost eats the edge. **Price sum gate**: `price_up + price_down` must be in [0.98, 1.02] or the entry is skipped (stale/broken prices). Resolutions ($1/$0) have no slippage. **Maker/FOK fee blend**: paper mode simulates a 65/35 maker/FOK fee split — each trade randomly gets 0% fee (65% chance, simulating maker fill) or full taker fee (35% chance, simulating FOK fallback). This models the expected fee savings from LiveTrader's maker-first strategy. Live mode on polymarket.com uses the py-clob-client SDK for EIP-712 signed CLOB orders. Requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env.

## Project Structure

```
polybot/
  main.py                    # Entry point, trading loop (9 extracted helper functions)
  config/settings.yaml       # ALL tunable parameters
  core/
    binance_feed.py          # WebSocket + candle buffer
    clob_ws.py               # Real-time CLOB WebSocket feed (order books, trades, resolution)
    market_scanner.py        # Gamma API discovery + CLOB HTTP helpers (spread, midpoints, volume)
    signal_engine.py         # Probability model: P(Up) from BTC vs strike + time + vol
    order_flow.py          # Book imbalance + trade flow signal from CLOB data
    binance_depth.py         # L2 order book: wall detection, spot imbalance, book depth
    binance_trades.py        # Aggregate trade stream: CVD, taker ratio, large trades, volume surge
    bybit_feed.py            # BTC perpetual price lead + funding rate signal
    deribit_iv.py            # BTC options implied volatility (forward-looking vol)
    bankroll_strategy.py     # Tiered Kelly acceleration based on track record
    sprt.py                  # Sequential Probability Ratio Test for evidence-based entry
    regime.py                # Multi-state regime detector (trending/reverting/volatile/quiet)
    liquidation.py           # OI-based liquidation pressure from Bybit
    gamma_exposure.py        # Net gamma exposure from Deribit options chain
    alpha_decay.py           # Edge decay rate tracker for adaptive entry timing
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all 7, manages weight versions
  execution/
    base.py                  # BaseTrader ABC, TradeResult, FillResult, fee functions
    paper_trader.py          # PaperTrader(BaseTrader) — instant simulated fills
    live_trader.py           # LiveTrader(BaseTrader) — FOK market orders via py-clob-client SDK
    circuit_breaker.py       # Drawdown-based Kelly scaling (tracks drawdown from initial principal, scales Kelly 1.0→0.25 as drawdown deepens)
  agents/
    scheduler.py             # Daily learning pipeline
    outcome_reviewer.py      # Logs resolved trades
    counterfactual_tracker.py # Tracks counterfactuals for both scalps (what if held?) and holds (what if scalped?)
    bias_detector.py         # Per-indicator accuracy + counterfactual analysis
    ta_evolver.py            # Recommends weight adjustments
    weight_optimizer.py      # Versions and auto-adopts weights
  brain/
    claude_client.py         # analyze_strategy() for daily pipeline
  memory/
    outcomes/                # One JSON per trade
    counterfactuals/         # One JSON per scalp or hold — bidirectional "what if?"
    weights/                 # Versioned weight configs
    biases.json              # Indicator correction factors
  discord_bot/
    bot.py                   # Commands: status, positions, history, performance, clear, session, pause/resume
    commands.py              # Formatting helpers
    alerts.py                # Trade alerts, session banners, channel purging
  db/models.py               # SQLite: positions, trade_history, bankroll
  math_engine/
    returns.py               # Log returns, gain_pct (arithmetic returns for binary), Sharpe ratio
```

## Config

`polybot/config/settings.yaml` (validated by `validate_config()` on startup):
- `circuit_breaker.max_drawdown_pct:` — 0.15 (from initial principal, not peak), `min_multiplier:` — 0.25, `losses_to_reduce:` — 3, `wins_to_restore:` — 2
- `math.kelly_fraction:` — 0.15
- `signal.entry_threshold:` — 0.04 (noise floor, range 0.01-0.10)
- `signal.min_kelly:` — 0.015 (primary gate, range 0.005-0.05)
- `signal.exit_edge_threshold:` — -0.10
- `signal.min_model_probability:` — 0.65
- `signal.momentum_weight:` — 0.04 (L4), `regime_weight:` — 0.03 (L2), `flow_weight:` — 0.04 (L3)
- `signal.spot_flow_weight:` — 0.04 (L3b), `wall_weight:` — 0.05 (L3c), `perp_lead_weight:` — 0.03 (L3d)
- `signal.prev_margin_weight:` — 0.02 (L5)
- `signal.atr_sigma_ratio:` — 1.4 (range 1.2-2.5), `student_t_df:` — 5, `regime_lookback:` — 10
- `signal.weights:` — per-indicator weights for momentum
- `execution.max_concurrent_positions:` — 2, `max_bankroll_deployed:` — 0.80, `max_book_fill_pct:` — 0.50
- `execution.slippage_impact_pct:` — 0.03, `use_maker_orders:` — true, `maker_timeout_s:` — 60.0
- `market.entry_window_seconds:` — 300, `min_time_remaining_seconds:` — 20, `max_spread:` — 0.10
- `market.clob_ws_url:` — `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `schedule.trading_start_hour_et:` — 0 (midnight ET), `trading_end_hour_et:` — 23, `trading_end_minute:` — 45
- `agents.daily_pipeline_hour:` — 23, `daily_pipeline_minute:` — 55 (11:55 PM ET)
- `binance_depth.poll_interval_s:` — 5.0, `binance_trades` / `bybit` / `deribit` WS URLs in config
- `entry_timing.observe_seconds:` — 60, `late_kelly_multiplier:` — 0.7, `final_min_probability:` — 0.90
- `bankroll_acceleration.enabled:` — true (0.15 -> 0.18/0.22/0.25 as track record grows)
- `sprt.alpha:` — 0.05, `sprt.beta:` — 0.10
- `regime.lookback:` — 20, `vol_high_percentile:` — 75, `vol_low_percentile:` — 25, `autocorr_threshold:` — 0.25

## Running

```bash
python -m polybot.main --mode paper   # Paper trading (persistent bankroll across sessions)
python -m polybot.main --mode live    # Live trading (real USDC on Polymarket)
python -m polybot.main                # Defaults to mode in settings.yaml
python -m pytest polybot/tests/       # 545 tests
```

## How the Probability Model Works

```
Strike = BTC price at 5-min window open (from Binance candle at the CONTRACT's
window boundary, derived from slug — not current time). Polymarket resolves using
Chainlink BTC/USD oracle (eventMetadata.priceToBeat/finalPrice from Gamma API),
which can differ from Binance by $20-200. Entry uses Binance (Chainlink not
available during active windows). Resolution always uses Gamma/Chainlink data.
Distance = current BTC price - strike
Vol = ATR (average true range from 1-min candles, period=7) / atr_sigma_ratio (1.7)
Time = minutes remaining in the window

LAYER 1 — Fat-tailed base (Student-t CDF, df=5):
  vol = (ATR / atr_sigma_ratio) x sqrt(minutes) x iv_ratio    [iv_ratio from Deribit options IV]
  z = distance / vol
  z_scaled = z x sqrt(df / (df-2))               [variance normalization]
  P(Up) = t.cdf(z_scaled, df=5)                  [df=5: excess kurtosis=6]
  
  Why Student-t: BTC 1-min returns have kurtosis ~6-8 (normal=3).
  Normal CDF underestimates reversal probability. When BTC is $50 above
  strike with 2 min left, normal says 85% Up, Student-t says ~78%.
  The difference is edge on the underdog side.

LAYER 2 — Regime detection (in logit space):
  autocorr = 1-lag autocorrelation of last N 1-min returns
  direction = sign(most_recent_return)    [NOT sign(prob - 0.5)]
  logit_p += autocorr x direction x (regime_weight x 4.0)

LAYER 3 — Order flow (in logit space):
  book_imbalance = (bid_depth - ask_depth) / total_depth (across both Up/Down books)
  trade_flow = net_buy_volume / total_volume (from WebSocket last_trade_price events)
  flow_signal = 0.6 * book_imbalance + 0.4 * trade_flow
  logit_p += flow_signal x (flow_weight x 4.0)
  
  Why: Order flow LEADS price. Informed traders accumulate before CLOB reprices.

LAYER 4 — Indicator momentum (in logit space):
  Z-score normalized scores per indicator (IndicatorNormalizer)
  Weighted RSI/MACD/Stochastic/OBV/VWAP x (momentum_weight x 4.0)

LAYER 3b — Spot market flow (in logit space):
  CVD = sum of (+qty if buyer taker, -qty if seller taker) over 120s
  taker_ratio = buy_taker_volume / total_volume over 60s
  spot_flow = tanh(CVD * 2) * 0.6 + (taker_ratio - 0.5) * 2 * 0.4
  logit_p += spot_flow * (spot_flow_weight x 4.0)
  
  Why: Binance spot trades are a leading indicator. Aggressive buyers
  (taker side) signal directional commitment before candles close.

LAYER 3c — Wall pressure near strike (in logit space):
  wall_pressure = (ask_vol_near_strike - bid_vol_near_strike) / total
  logit_p -= wall_pressure * (wall_weight x 4.0)
  
  Why: Large sell walls between current price and strike block upward
  movement. No other signal captures "there's a $3M wall blocking the path."
  Uses full 1000-level Binance order book polled every 5s.

LAYER 3d — Perpetual price lead (in logit space):
  perp_lead = tanh((bybit_perp_price - binance_spot) / spot * 350)
  logit_p += perp_lead * (perp_lead_weight x 4.0)
  
  Why: Bybit BTC perpetual leads Binance spot by 0.5-2s because
  leveraged traders react first. Also provides staleness detection
  for latency arbitrage when Polymarket hasn't repriced.

LAYER 3e — Liquidation pressure (in logit space):
  If OI drops + price drops → long liquidations → bearish pressure
  If OI drops + price rises → short liquidations → bullish pressure
  logit_p += liquidation_pressure * (0.03 x 4.0)
  
  Why: Forced liquidations cascade. $50M of longs just got margin-called
  means more sell pressure is coming as cascading liquidations trigger.

LAYER 5 — Previous window momentum carry (in logit space):
  normalized_margin = (prev_btc_at_expiry - prev_strike) / ATR
  logit_p += tanh(normalized_margin) * (prev_margin_weight x 4.0)
  
  Why: BTC momentum persists across adjacent 5-min windows.
  Strong resolution in window N predicts direction in window N+1.

CALIBRATION — Platt scaling:
  calibrated = 1 / (1 + exp(A x logit(raw_prob) + B))
  A, B fitted daily by learning pipeline (identity until 100+ outcomes)

NEGRISK EXECUTION PRICING:
  price_up = GET /price?token_id=UP&side=BUY   (cross-matched, not raw book)
  price_down = GET /price?token_id=DOWN&side=BUY
  sell_price = GET /price?token_id=TOKEN&side=SELL (for scalp exits)

ENTRY:
  ATR gate: skip if volatility too quiet or too volatile
  Edge = P(Up) - effective_price_up    [or P(Down) - effective_price_down]
  If model_prob < 65%: SKIP (coin-flip filter)
  If edge < 0.03: SKIP (noise floor)
  If Kelly < 0.015: SKIP (primary gate — position must justify execution cost)
  Size = Kelly(probability, market_price) x kelly_fraction, capped to 50% of book depth
  Net-edge gate: net_edge = edge - (price * convex_slippage); if net_edge < min_edge: SKIP
  Entry fill: /price quote + convex slippage: fill_pct * impact * (1 + fill_pct)

WHILE HOLDING (active position management):
  holding_edge = model_prob_for_our_side - current_market_price_for_our_side
  Fee-aware threshold: base_threshold - exit_fee_cost + time_urgency_bonus
  Time urgency: near expiry (<2 min), threshold relaxes by up to +5%
  If holding_edge ≤ effective_threshold: EXIT (scalp)
  If holding_edge > effective_threshold: HOLD

RESOLUTION (same as before — contract expired):
  Winning side pays $1.00/share, losing side pays $0.00/share.
```

## External APIs

All APIs are free, no auth required (except Claude and Discord).

### Binance.US (not .com — HTTP 451 for US IPs)

| Endpoint | Usage |
|----------|-------|
| `wss://stream.binance.us:9443/ws/btcusdt@kline_1m` | Real-time 1-min candles. Drives probability model, ATR, indicators. |
| `GET https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=200` | REST backfill on startup. |
| `wss://stream.binance.us:9443/ws/btcusdt@depth20@100ms` | Top 20 order book levels, 100ms. Spot imbalance + depth. |
| `GET https://api.binance.us/api/v3/depth?symbol=BTCUSDT&limit=1000` | Full 1000-level book for wall detection near strike. Polled every 5s. |
| `wss://stream.binance.us:9443/ws/btcusdt@aggTrade` | Every trade with taker side. Drives CVD + taker ratio (Layer 3b). |

### Polymarket CLOB API (`https://clob.polymarket.com`)

| Endpoint | Usage |
|----------|-------|
| `WSS .../ws/market` | **Primary price source.** Real-time books, BBA, last trades, resolution events. |
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

`GET /get_book_summary_by_currency?currency=BTC&kind=option` — ATM implied volatility, polled every 60s. Drives iv_ratio in Layer 1.

### Claude API + Discord

- **Claude**: `claude-sonnet-4-6` via SDK. TAEvolver sends analysis + trades, gets structured JSON recommendations. Falls back to local math.
- **Discord**: Trade alerts, session banners, commands (`!status`, `!positions`, `!history`, `!performance`, `!pause`/`!resume`), daily reports to `#polybot-daily`. `!pause` blocks entries only — position management continues.

## Canonical Paper Trader Dataflow — DO NOT DEVIATE

Live trader preserves the same dataflow shape — same gates, ordering, invariants. Only `open_trade()`, `close_trade()`, `resolve_position()` differ (mock fill vs real CLOB order).

### Phase 1: Market Discovery

```
Binance WS (1-min candles)          Gamma API (contract discovery)
        |                                    |
        v                                    v
200-candle rolling buffer         btc-updown-5m-{window_ts}
  BTC price, strike, ATR,          token_id_up, token_id_down,
  indicators                       seconds_remaining, conditionId
                                             |
                                    CLOB WS subscribe(token_up, token_down)
                                    -> book snapshots, BBA, last trades, resolution
```

### Phase 2: Signal Generation

All 10 signal layers feed `signal_engine.evaluate()` (see "How the Probability Model Works" for formulas). Layers 2-5 applied in logit space. Output: `final_prob`, `edge`, `side`, `kelly_size`.

9 entry gates (all must pass): confidence >= 0.65, edge >= 0.04, Kelly >= 0.015, spread <= 0.10, depth >= $50, price_sum in [0.98, 1.02], time >= 20s, edge <= 0.30, layer agreement.

### Phase 3: Sizing

```
raw_size = bankroll x kelly_size x breaker.kelly_multiplier
CAP CHAIN: size < $0.10 -> REJECT | size > 80% bankroll -> cap | size > 50% depth -> cap
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
- **Wrong strike:** Strike for the probability model is derived from Binance candle buffer at the contract's window boundary (parsed from slug). Polymarket resolves using Chainlink oracle, which can differ from Binance by $20-200. Resolution always waits for Gamma API eventMetadata or closed+outcomePrices — never guesses from Binance. If buffer is empty on startup, first few windows may have no strike.
- **All trades losing:** Check if model is systematically miscalibrated. Lower kelly_fraction or raise min_edge.
- **Startup config error:** `validate_config()` in `loader.py` validates all parameter bounds on startup and raises `ValueError` listing all violations. Fix settings.yaml values to be within documented ranges.
- **Orphaned position not resolving:** Positions now wait indefinitely for Gamma/Chainlink resolution data (no Binance fallback). A Discord alert fires after 1 hour. This is by design — Binance and Chainlink can disagree by $20-200, so guessing from Binance is unsafe.

## Learning Pipeline

Daily at 11:55 PM ET (configurable via `agents.daily_pipeline_hour` and `daily_pipeline_minute`):

**Hold-out split:** Outcomes are sorted chronologically and split 60/40. The first 60% (older trades) are used for analysis and recommendations (steps 1-2). The last 40% (newer trades) are used for backtest validation (step 3). This prevents in-sample overfitting — Claude's recommendations are tested against data it hasn't seen.

**Minimum data requirement:** The scheduler (`scheduler.py`) programmatically skips TAEvolver and WeightOptimizer if fewer than 50 trades exist. BiasDetector still runs regardless. This is enforced in code (not just a Claude prompt instruction). Win rate variance at N=25 is ±13 percentage points — noise, not signal. The WeightOptimizer also requires at least 10 outcomes to run.

1. **BiasDetector** — Analyzes the **training set** (first 60% of outcomes):
   - Per-indicator accuracy (bullish/bearish breakdown, sample sizes)
   - Side analysis (Up vs Down win rate)
   - Edge calibration (do larger edges actually win more?)
   - Time patterns (win rate by seconds remaining at entry)
   - Volatility patterns (win rate by ATR regime)
   - Overall statistics (Sharpe, win rate, avg edge)
   - Counterfactual analysis (both scalps AND holds): was the exit/hold decision optimal?

1.5. **PlattCalibrator** — Fits Platt scaling parameters (A, B) on training set model probabilities vs actual outcomes. Validates on holdout — only adopts if log-loss improves. Persists to `memory/calibration/platt_params.json`. Applied after all 4 layers in `compute_probability()`.

2. **TAEvolver** — Sends training-set analysis + trades + current config to Claude API:
   - System prompt defines Chief Quantitative Strategist role with full 4-layer model description
   - Trade data includes flow_score, exit_reason (scalp vs resolution) for each trade
   - Claude returns structured JSON: weight adjustments, all 4 layer weights (momentum, regime, flow, student_t_df), min_edge, kelly_fraction, min_kelly, atr_sigma_ratio, reasoning, findings, risk warnings
   - Response validated server-side (weights sum to 1.0, all constraints enforced, new params clamped)
   - Falls back to local math if Claude API fails (resilient)

3. **WeightOptimizer** — Backtests recommendations against the **validation set** (last 40%):
   - Recomputes hypothetical edge with new weights/parameters
   - Auto-adopts if Sharpe improves >= 3%
   - Hot-swaps ALL params at runtime: indicator weights, momentum_weight, regime_weight, flow_weight, student_t_df, min_edge, kelly_fraction, min_kelly, atr_sigma_ratio, min_model_probability, exit_edge_threshold, min_time_remaining, trading hours
   - **Persists all tuned parameters to settings.yaml** — values survive restarts
   - Discord alerts include Claude's key findings and reasoning

Outcome data enriched with `trade_context` in indicator_snapshot: btc_price, strike_price, seconds_remaining, market prices, model_probability, edge, momentum_score, ATR, size, flow_score, flow_book_imbalance, flow_trade_count. Each outcome also stores `gain_pct` (arithmetic return: pnl/size), `pnl`, and `fees`.

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

## Baseline — LOCKED 2026-04-09

Engine math optimized: logit-space layer combination, ATR-to-sigma scaling, Student-t variance normalization, regime direction fix (sign of recent return), Kelly-based entry gate, indicator z-score normalization, Platt calibration. Baseline re-locked — only the daily learning pipeline tunes parameters.

The core trading logic is FROZEN. Do not make structural changes to:
- `signal_engine.py` (10-layer probability model)
- `order_flow.py` (book imbalance + trade flow)
- Entry/exit/pricing logic in `main.py` (now 9 extracted helper functions — logic unchanged, just organized)
- `base.py` (BaseTrader ABC, fee math, shared gates/DB ops)
- `paper_trader.py` / `live_trader.py` (extend BaseTrader — only 3 abstract methods each)

Only the daily learning pipeline (11:55 PM ET) tunes parameters slowly. Any proposed "improvement" to frozen code requires explicit user approval. New features go in NEW files/modules.

## Always Update

Update this file and README.md with every behavioral change.
