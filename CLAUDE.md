# CLAUDE.md

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, trades when edge clears noise floor and Kelly justifies size, then lets the edge math decide every tick: hold to $1 or scalp.

## Probability Model

All layers compose in logit space except L1, then sigmoid + Platt.

- **L1 — Student-t CDF** (df=5). `vol = max(atr, atr_floor)/atr_sigma_ratio · √min · iv_ratio`, `z = (btc-strike)/vol · √(df/(df-2))`. ATR floor adapts: rolling-20 < 60% of long-term-200 → floor widens.
- **L2** — 1-lag autocorr × sign(last 1-min return). `regime_autocorr` + `regime_direction` stored per trade.
- **L3** — CLOB book imbalance × 0.6 + trade flow × 0.4.
- **L3b** — Binance CVD + taker ratio (taker requires trade_count ≥ 5).
- **L3e** — Bybit OI drop × price direction.
- **L4** — RSI/MACD/Stoch/OBV/VWAP. Base `momentum_weight=-0.02` (fade). Trending (autocorr > +0.15) flips sign + amplifies 1.5×; reverting (< -0.15) keeps fade + amplifies 1.5×; neutral dampens 0.5×. Clamped ±0.10.
- **L5** — `tanh(prev_resolution_margin/atr) · prev_margin_weight · logit_scale`.
- **Platt** — sole overconfidence correction, re-fit each pipeline cycle. Identity `a=-1.0, b=0.0` when no calibrator.

L3+L3b combined contribution capped at ±0.35 logits.

## Entry Gates

`prob ≥ 0.58`, `edge ≥ 0.04` (+0.015 per flip), `Kelly ≥ 0.015`, `spread ≤ 10%`, `depth ≥ $50`, `price_sum ∈ [0.98, 1.02]`, `edge ≤ 0.20`, `adverse_rate_30s ≤ 0.85`. Pre-submit edge re-check on fresh ask. CVD deceleration: skip if `|spot_flow| ≥ 0.20` AND `spot_flow × cvd_accel < 0`. SPRT: blocks SKIP, low-confidence after 2+ obs, or favored-side mismatch >30%. ATR gate: lower-bound only (`atr < 5th pctile`).

## Sizing & Exit

**Sizing:** `bankroll · kelly · breaker · time_mult · concurrent_mult`, clipped to `bankroll · max_bankroll_deployed` and `book_depth · max_book_fill_pct`.

**Concurrent:** correlation-aware + size-weighted. Same-side full size → 0.35×; opposite full size → 0.90×.

**Exit (`evaluate_hold`):** `holding_edge = model_prob − market_price`. `effective_threshold = max(exit_edge_threshold − fee_cost, exit_boundary_threshold)`. Scalp when `−0.10 < holding_edge ≤ effective_threshold`. **Deep-loss hold:** if `holding_edge < −0.10`, hold to resolution (binary residual is +EV vs scalping at ~38% accuracy in that zone). **Loss-cut:** if `market_price < entry × 0.65` AND `seconds_remaining < 120`, exit regardless.

**Flip trading:** after a scalp, can re-enter the same window unboundedly (one position at a time). Each re-entry pays `flip_edge_premium` (+1.5% or actual spread cost, whichever is higher) on top of `min_edge`.

**Circuit breaker:** tier-locked floor at $100/$150/$200/$300/...; locks at 85% of tier crossed. Kelly scales 1.0→0.40 between tier and floor (concave sqrt). Never resets down.

## Live Execution & Safety

- FOK-only via py-clob-client; 3 retries w/ backoff; HTTP/2 keepalive 30s.
- `verify_auth`: `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER`, USDC balance + allowance ≥ `max_single × max_concurrent × 10`.
- `open_position_and_debit_bankroll`: atomic SQLite tx (insert + bankroll).
- Auth errors → `AuthError` → clean exit; `run_polybot.ps1` won't auto-restart.
- Startup: alerts if shares deviate >5% from `shares_held` per token.
- Feed staleness skip: Coinbase >30s, Chainlink >60s.
- Chainlink orphan fallback: Gamma silent 30+ min past expiry.
- Per-mode DB (`polybot_paper.db` / `polybot_live.db`); `memory/` shared.

Bybit REST geo-blocked for US — first 401/403/451 stops poll loop; WS is the OI source.

## Project Structure

```
polybot/
  main.py                    Trading loop, entry/exit/sizing
  config/{settings.yaml, loader.py, param_registry.py}
  core/                      signal_engine, calibrator, order_flow, returns,
                             regime, liquidation, exit_boundary, sprt, adverse_selection
  feeds/                     coinbase, kraken, binance(+depth+trades), bybit, chainlink, clob_ws, market_scanner
  indicators/                rsi/macd/stoch/obv/vwap/ema/atr + engine
  execution/                 base, paper_trader, live_trader, circuit_breaker, correlation
  agents/                    scheduler, outcome_reviewer, counterfactual_tracker, ghost_tracker,
                             bias_detector, ta_evolver, weight_optimizer, pipeline_tracker,
                             pipeline_analytics, claude_client, local_recommender
  memory/                    calibration, outcomes, ghost_outcomes, counterfactuals, pipeline_*
  discord_bot/               !status !history !pause !resume !clear !session !commands
  db/models.py               SQLite (positions, trade_history, bankroll, peak_bankroll)
```

## Parameter Ownership

**Pipeline-tunable** (Claude proposes, walk-forward adopts; replay via `_kelly_bankroll_returns`):
`atr_sigma_ratio` 1.2–2.5 (L1, highest leverage), `logit_scale` 2.0–6.0, `student_t_df` 3–8, `momentum_weight` -0.10..+0.10 (L4, negative=fade), `regime_weight` 0.02–0.10 (L2), `flow_weight` 0.02–0.12 (L3), `spot_flow_weight` 0.01–0.15 (L3b), `liquidation_weight` 0.01–0.10 (L3e), `prev_margin_weight` 0.01–0.05 (L5), `min_atr` 4.0–25.0, `kelly_fraction` 0.05–0.25, `weights` (sum=1.0, ≥0.05 each), `min_model_probability` 0.52–0.70, `min_edge` 0.02–0.10, `min_kelly` 0.005–0.04.

**Manual-only** (validator in `claude_client.py` reroutes `changes` → `manual_observations`):
`exit_edge_threshold`, `loss_cut_*`, `max_edge`, `adverse_selection_threshold`, `normal_fraction`, `late_max_penalty`, `flip_enabled`, `flip_edge_premium`, `trading_start/end_*`, `max_concurrent_positions`, `max_bankroll_deployed`, `circuit_breaker.{floor_pct,min_multiplier}`, `indicators.*`, `sprt.*`.

## Running

```bash
python -m polybot.main --mode paper      # Paper
python -m polybot.main --mode live       # Real USDC
python -m polybot.main --run-pipeline    # Pipeline once, no trading
python -m pytest polybot/tests/
```

`run_polybot.ps1` starts at 12:15 AM ET, runs pipeline at 11:15 PM ET, commits, restarts.

## Learning Pipeline

Daily 23:15 ET. Walk-forward 60% train / 40% across folds [60:70][70:80][80:90][90:100].

**Adoption gate:** `candidate_sharpe > 0`, `n ≥ 100`, `z = Δ_sharpe / JK_SE ≥ 0.5` (autocorr-adjusted). Regime-stratified: dominant regime improves AND no regime degrades >0.10 (regimes need ≥35 trades to veto). 2-day per-param cooldown. Combined backtest after ≥2 adoptions backs out lowest-z change if combined Δ < 0.7 × sum.

**Crisis mode:** recent-50 WR < 48% AND baseline Sharpe < 0.10. ≥3 consecutive cycles → halve `kelly_fraction` (floor 0.04), restore on first non-crisis.

**Stages:** PipelineTracker (review + auto-revert) → BiasDetector → PlattCalibrator (Kelly-Sharpe holdout, raw-vs-Platt meta-warning) → KS shift → SPRT aggregate → TAEvolver (Claude or LocalRecommender) → WeightOptimizer.

## Invariants

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded priors.
- **`model_probability_raw`** stores the pre-Platt P(side) so re-fits don't compound.
- **Recency weighting:** `0.97^days_ago` on backtest and Platt MLE (~23-day half-life).
- **`gain_pct = pnl/size`** (arithmetic). Never `log_return` for Sharpe.
- **UTC everywhere**; ET only for date-bucketing.
- **Daily rollup** at 12:05 AM bundles per-trade JSON into `rollup_YYYY-MM-DD.json`.

## What NOT to Change

- Student-t, not normal CDF (BTC kurtosis 6–8).
- `momentum_weight` magnitude ≤ 0.10.
- Never `log_return` for Sharpe.
- Pricing from `GET /price?side=BUY|SELL`, not raw CLOB book.
- Fee from `GET /fee-rate`, not hardcoded.
- Resolution from Gamma/Chainlink, not Binance.
- Don't bypass circuit breaker.
- Don't delete `polybot/db/polybot_*.db`.
- Regime direction = sign of last 1-min return, not `sign(prob-0.5)`.
- Layer adjustments in logit space, not probability space.
- Binance.us, polymarket.com.

Update this file with every behavioral change.
