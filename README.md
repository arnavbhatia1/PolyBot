# PolyBot

Automated 5-minute BTC Up/Down trader for Polymarket. Computes P(BTC closes above/below the strike) via a 7-layer logit-space model, compares to market price, trades when mispricing clears the noise floor and Kelly justifies size. Holds to $1 resolution when confident; scalps early when `holding_edge` decays into the profitable zone. Up to `max_concurrent_positions` (2) live across windows, with correlation-aware sizing.

## Quick Start

```bash
pip install -r requirements.txt

cp polybot/config/.env.example polybot/config/.env
# Required: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN
# Live mode also needs: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

python -m polybot.main                 # mode from settings.yaml
python -m polybot.main --run-pipeline  # one pipeline run, no trading
.\run_polybot.ps1                      # daily cycle: trade → pipeline → commit → restart
```

## How It Works

```
9 WebSocket feeds + REST polls
        |
  BTC price: Coinbase (primary) > Kraken (secondary) > Binance.US (fallback)
  Strike:    Chainlink BTC/USD (preferred) → Binance candle boundary
        |
  PROBABILITY MODEL (logit space, then sigmoid + Platt):
    L1  Student-t CDF (df=5, fat tails)
    L2  Regime: 1-lag autocorr × sign(last 1-min return)
    L3  CLOB flow: book imbalance + trade flow
    L3b Spot CVD: Binance aggTrades CVD + taker ratio
    L3e Liquidation: Bybit OI drop × price direction
    L4  Indicator momentum: RSI/MACD/Stoch/OBV/VWAP (regime-conditional)
    L5  Previous-window margin carry
    +   Platt scaling (sole overconfidence correction; re-fit each pipeline cycle)
        |
  Edge = calibrated_model_prob - market_price (CLOB /price endpoint)
        |
  Entry gates: prob ≥ 0.56, edge ≥ 0.04, Kelly ≥ 0.01, spread ≤ 10%, depth ≥ $50,
    price_sum ∈ [0.98, 1.02], edge ≤ 0.20, adverse_rate_30s ≤ 0.85, ATR ≥ 5th-pctile,
    SPRT not SKIP (and not opposing), pre-submit edge re-check, CVD-deceleration skip
        |
  Sizing: bankroll × Kelly × breaker × time_mult × correlation-aware concurrent_mult,
    capped to bankroll × max_bankroll_deployed and book_depth × max_book_fill_pct
        |
  While holding: re-evaluate every tick with the same model
    Scalp when -0.10 < holding_edge ≤ effective_threshold (profitable zone)
    Hold when holding_edge < -0.10 (deep-loss zone, binary residual is +EV)
    Loss-cut: market < entry × 0.65 AND seconds_remaining < 120 → exit
    Flip: after a scalp, re-enter same window (one position at a time, +1.5% premium per re-entry)
        |
  Resolution: Gamma/Chainlink ($1 win / $0 loss)
        |
  Outcome → daily learning pipeline
```

## Architecture

| Module | Purpose |
|---|---|
| `core/signal_engine.py` | Probability model + `evaluate_hold` |
| `core/calibrator.py` | Platt scaling (`a`, `b`) |
| `core/order_flow.py` | Book imbalance + trade flow signal |
| `core/regime.py` | Multi-state regime classifier |
| `core/liquidation.py` | OI-based liquidation pressure |
| `core/exit_boundary.py` | Time/price-aware exit threshold curve |
| `core/sprt.py` | Sequential probability ratio test (entry gate) |
| `core/adverse_selection.py` | Post-fill reversal monitor |
| `core/returns.py` | `gain_pct` / `log_return` |
| `feeds/coinbase_feed.py` | Primary BTC price (WS) |
| `feeds/kraken_feed.py` | Secondary BTC price (WS, Chainlink-aligned) |
| `feeds/binance_feed.py` | 1-min candles, ATR, fallback BTC price |
| `feeds/binance_depth.py` | L2 book depth |
| `feeds/binance_trades.py` | aggTrades → CVD, taker ratio |
| `feeds/bybit_feed.py` | OI + funding (WS only — REST is US geo-blocked) |
| `feeds/deribit_iv.py` | ATM implied vol |
| `feeds/chainlink_feed.py` | BTC/USD oracle (strike + resolution) |
| `feeds/clob_ws.py` | Polymarket CLOB stream |
| `feeds/market_scanner.py` | Gamma contract discovery + CLOB helpers |
| `indicators/{rsi,macd,stochastic,ema,obv,vwap,atr}.py` + `engine.py` | L4 indicators |
| `execution/base.py` | `BaseTrader` ABC, fee math, atomic open_trade |
| `execution/{paper_trader,live_trader}.py` | Mode-specific traders |
| `execution/circuit_breaker.py` | Tiered floor Kelly scaling |
| `execution/correlation.py` | Concurrent-position correlation buckets |
| `agents/` | Pipeline: scheduler, outcome_reviewer, counterfactual_tracker, ghost_tracker, bias_detector, ta_evolver, weight_optimizer, pipeline_tracker, pipeline_analytics, claude_client, local_recommender |
| `discord_bot/` | Commands, alerts, daily banners |
| `db/models.py` | SQLite (positions, trade_history, bankroll, peak_bankroll) |

## Data Sources

| Source | Feed | What |
|---|---|---|
| Coinbase | `ticker` WS (BTC-USD) | Primary BTC price |
| Kraken | `ticker` WS (XBT/USD) | Secondary / Chainlink-aligned |
| Binance.US | `kline_1m` / `depth20@100ms` / `aggTrade` WS | Candles, ATR, depth, CVD |
| Polymarket CLOB | WS + `GET /price`, `/book`, `/spread`, `/fee-rate` | Books, fills, fees |
| Polymarket Gamma | `GET /events?slug=...` | Discovery + resolution |
| Bybit | `tickers.BTCUSDT` WS | OI + funding |
| Deribit | `GET /get_book_summary_by_currency` | ATM IV |
| Chainlink | `latestRoundData()` via Eth RPC | Strike + resolution oracle |
| Anthropic | `claude-sonnet-4-6` SDK | Daily learning pipeline |

## Paper Realism

- Real CLOB prices from WebSocket (not stale Gamma).
- Order size capped to 50% of available depth.
- Convex slippage: `(size/depth) × 3% × (1 + size/depth)`.
- Live `GET /fee-rate` per token (entry fee in shares, exit in USDC).
- FOK semantics (100% fill or reject).
- Tick-size enforcement, min order size from book, 10% spread cap.
- Latency mean / jitter / network-fail rate configurable.

## Configuration

All parameters in `polybot/config/settings.yaml`. Key knobs:

- `kelly_fraction` 0.08, `min_edge` 0.04, `min_model_probability` 0.56, `min_kelly` 0.01
- L2 0.03 / L3 0.04 / L3b 0.10 / L3e 0.03 / L4 0.04 (magnitude only — sign is regime-conditional) / L5 0.02
- `max_concurrent_positions` 2 (correlation-aware sizing on top)
- Circuit breaker: tiered floor at $100/$150/$200/... locks at 85% of crossed tier; Kelly 1.0→0.40 between tier and floor
- SPRT: alpha 0.05, beta 0.10, observation_interval 10s, min_confidence 0.20 (gates entries)
- Entry timing: `normal_fraction` 0.60, `late_max_penalty` 0.35
- Flip trading: `flip_enabled` true, `flip_edge_premium` 0.015 (no cap on flips per window; one position at a time)

## Learning Pipeline

Daily 23:15 ET. Walk-forward 60% train / 40% across folds [60:70][70:80][80:90][90:100].

1. **PipelineTracker** — fills 1d/3d/7d/14d/30d Sharpe; auto-revert if 1d trailing < -0.10 (n≥20) or 7d < -0.05 (n≥100).
2. **BiasDetector** — per-indicator/side/edge/time/regime/phase/flip stats + edge-realization quartiles + KS shift.
3. **PlattCalibrator** — recency-weighted MLE on train; adopts only if Kelly-Sharpe on holdout improves. Meta-warning when raw-model Sharpe ≥ 0.95 × Platt Sharpe.
4. **TAEvolver** — Claude (or `LocalRecommender` fallback) reads the analysis card, returns `{changes, manual_observations}`.
5. **WeightOptimizer** — per-param walk-forward backtest. Adoption gate: `candidate_sharpe > 0`, `n ≥ 100`, `z = Δ_sharpe / JK_SE ≥ 0.5`. Regime-stratified veto. 2-day per-param cooldown. Combined backtest after ≥2 adoptions backs out lowest-z change if combined Δ < 0.7 × sum.

Crisis mode (baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`)): after 3 consecutive cycles, halve `kelly_fraction` (floor 0.04). Restored on first non-crisis cycle.

Tunes ~15 backtestable params plus the L4 weight vector. Manual-only params (exit/timing/risk/schedule) get `manual_observations` for operator review.

## Discord

`!status` `!history [n]` `!positions` `!performance` `!pause` `!resume` `!session` `!agents` `!lessons` `!clear [trades|control|all]` `!commands`

## Secrets

| Key | When |
|---|---|
| `ANTHROPIC_API_KEY` | Always (daily learning) |
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_PRIVATE_KEY` | Live mode (EIP-712 signing) |
| `POLYMARKET_FUNDER` | Live mode (USDC funding address) |

Binance.US, Polymarket, Bybit, Deribit, Coinbase: free.

## Persistence

`memory/` (outcomes, counterfactuals, ghosts, pipeline_*, calibration), the per-mode SQLite DB, and `settings.yaml` are all git-tracked. `run_polybot.ps1` commits + pushes at 12:05 AM after the pipeline.
