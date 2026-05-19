# CLAUDE.md

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

## Probability Model

All layers compose in logit space (except L1's CDF), then sigmoid + isotonic calibration.

- **L1 — Student-t CDF** (df=5, clamped ≥3). `vol = max(atr, atr_floor)/atr_sigma_ratio · √min`, `z = (btc-strike)/vol · √(df/(df-2))`. ATR floor: `max(min_atr, 0.3 × rolling_20)`, widens when rolling_20 < 60% of long-term_200. Prob clipped to [1e-6, 1-1e-6] before logit (final ±4 clamp is the precision floor).
- **L2** — 1-lag autocorr × sign(last 1-min return). Single `lag1_autocorr` helper in `returns.py` — signal_engine and `RegimeDetector` both delegate to it.
- **L3** — CLOB book imbalance (top-5 levels each side, by best price) × 0.6 + trade flow × 0.4. Trade flow recency-weighted, 30s half-life inside the 120s window.
- **L3b** — Binance CVD + taker ratio (taker requires trade_count ≥ 5). CVD acceleration gate requires ≥ 3 trades in the recent 15s window (returns 0 otherwise).
- **L3e** — Bybit OI drop × price direction, normalized to %/minute using `oi_updated - oi_updated_prev`. tanh saturation `× 8` per minute (5%/min → 0.38, 10%/min → 0.66, 15%/min → 0.83 — softer than the old `× 20` on raw drop).
- **L4** — RSI/MACD/Stoch/OBV/VWAP. Polarity-split: mean-revert group (RSI/Stoch/VWAP) vs trend-confirm group (MACD/OBV). Trending (autocorr > +0.15) flips ONLY mean-revert sign; reverting (< −0.15) keeps mean-revert + dampens trend-confirm 0.5×; neutral dampens both 0.5×. Magnitude scaler from `effective_momentum_weight` is unsigned, clamped ±0.10.
- **L5** — `tanh(prev_margin/atr) · prev_margin_weight · logit_scale · (1 − min(l5_regime_damp_cap, |regime|))`. Dampened by regime strength to orthogonalize with L2 early in the window. `l5_regime_damp_cap` default 0.7, pipeline-tunable.
- **L6 — derived feature library.** Closed library of bounded transforms of already-tracked state (see `polybot/core/derived_features.py`). Every weight defaults to 0.0; the layer is dead until the pipeline raises one off zero. Combined L6 contribution hard-capped at ±0.25 logits regardless of individual weights.
- **Calibration (isotonic)** — sole overconfidence correction. Class name is legacy (`PlattCalibrator`); internals are isotonic regression. Fit on last 7d of trades (not the global walk-forward split; needs ≥125 trades in window or skips entirely; train split ≥75 to fit). Identity by default. Adoption requires the fit to beat identity on weighted log-loss by ≥ `1e-4` on the same pool — replaces the brittle Platt slope-near-zero heuristic that previously let near-degenerate sigmoid collapses through.

L3+L3b combined capped at ±`flow_combined_cap` (default 0.35) logits. Final logit clamped ±`final_logit_clamp` (default 4.0) → prob ∈ [0.018, 0.982]. Both caps pipeline-tunable.

## Entry Gates

`prob ≥ 0.56`, `edge ≥ 0.04` (+1.5% per flip), `Kelly ≥ 0.01` (fee-aware: `b_eff = b × (1 − fee_rate)`), `spread ≤ 10%`, `depth ≥ $50`, `price_sum ∈ [0.98, 1.02]`, `edge ≤ max_edge`, `adverse_rate_30s ≤ adverse_selection_threshold` (30-min rolling window over the last 20 fills, neutral 0.5 below 5 resolved samples). Pre-submit edge re-check uses fresh ask AND slippage (matches the entry-gate net_edge math). CVD deceleration: skip if `|spot_flow| ≥ 0.20` AND `spot_flow × cvd_accel < 0`. SPRT: blocks SKIP, low-confidence after 2+ obs, or favored-side mismatch > 30%. ATR gate: lower-bound only (`atr < 5th pctile`).

## Sizing & Exit

**Sizing:** `bankroll · kelly · breaker · time_mult · consensus_mult · concurrent_mult`, clipped to `bankroll · max_bankroll_deployed` and `book_depth · max_book_fill_pct`. Min $1 (Polymarket CLOB floor).

**Concurrent (correlation-aware + size-weighted):** same-side full-size ≈ 0.35×; opposite ≈ 0.90×. Same-market → flip logic instead.

**Exit (`evaluate_hold`):** `holding_edge = model_prob − market_price`.
- `effective_threshold` blends `deep_loss_floor = exit_edge_threshold × (1 + 0.5 × itm_depth)` with the `ExitBoundary.compute_exit_threshold` curve. ATM trusts the boundary; deep ITM weights toward the more patient floor.
- Scalp when `deep_loss_hold_threshold < holding_edge ≤ effective_threshold` (deep_loss_hold_threshold pipeline-tunable, default −0.10).
- **Deep-loss hold:** `holding_edge < −0.10` AND `market < entry` → hold (binary residual beats locking the loss).
- **Loss-cut:** `market < entry × loss_cut_fraction` AND `seconds_remaining < loss_cut_time_s` AND BTC on wrong side of strike by ≥ 0.5×ATR (whipsaw guard).

**Flip trading:** after a scalp, re-enter the same window unboundedly (one position at a time). Each re-entry pays `flip_edge_premium` (or actual spread cost, whichever is higher) above `min_edge`.

**Circuit breaker:** tier-locked floor at $100/$150/$200/$300/…/$10,000; locks at 85% of tier crossed. Kelly scales 1.0 → `min_multiplier` between tier and floor (concave sqrt). Never resets down.

## Live Execution & Safety

- FOK-only via py-clob-client-v2; 3 retries w/ jittered exp backoff; HTTP/2 keepalive 30s.
- `verify_auth`: `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER`, USDC balance + allowance ≥ `max_single × max_concurrent × 10`. Allowance re-checked every 10 fills mid-session.
- `open_position_and_debit_bankroll` / `close_position(... bankroll_delta=... | new_bankroll=...)`: atomic SQLite tx (single close path; pass `bankroll_delta` for relative credit, `new_bankroll` to set absolute on resolution).
- Auth errors → `AuthError` → clean exit; `run_polybot.ps1` won't auto-restart on auth.
- Feed staleness skip: Coinbase >30s, Chainlink >30s.
- Chainlink orphan fallback: Gamma silent 30+ min past expiry.
- CLOB WS heartbeat: PING every 10s, force reconnect if no PONG within 25s.
- `fill.fill_size` is always USDC notional (BUY: requested; SELL: shares × fill_price).
- Per-mode DB (`polybot_paper.db` / `polybot_live.db`); `memory/` shared.
- WS is the only OI source.

## Project Structure

```
polybot/
  main.py                Trading loop, entry/exit/sizing
  config/                {settings.yaml, loader.py, param_registry.py}
  core/                  signal_engine, calibrator, order_flow, returns,
                         regime, liquidation, exit_boundary, sprt, adverse_selection
  feeds/                 coinbase, kraken, binance(+depth+trades), bybit, chainlink, clob_ws, market_scanner
  indicators/            rsi/macd/stoch/obv/vwap/ema/atr + engine
  execution/             base, paper_trader, live_trader, circuit_breaker, correlation
  agents/                scheduler, outcome_reviewer, counterfactual_tracker, ghost_tracker,
                         bias_detector, ta_evolver, weight_optimizer, pipeline_tracker,
                         pipeline_analytics, claude_client, local_recommender
  memory/                calibration, outcomes, ghost_outcomes, counterfactuals, pipeline_*
  discord_bot/           !status !history !pause !resume !clear !session !commands
  db/models.py           SQLite (positions, trade_history, bankroll, peak_bankroll)
```

## Parameter Ownership

`param_registry.py` is the single source of truth for ALL defaults. Code-side fallbacks read `default_for(name)`; settings.yaml drives runtime.

**Pipeline-tunable** (Claude proposes, walk-forward adopts):
- **Layer weights / L1:** `atr_sigma_ratio` 1.2–2.5 (L1, highest leverage), `logit_scale` 2.0–5.0, `student_t_df` 3–8, `momentum_weight` 0.0–0.10 (magnitude only — sign is dead, polarity is regime-conditional per group inside `compute_momentum`), `regime_weight` 0.02–0.10, `flow_weight` 0.02–0.12, `spot_flow_weight` 0.01–0.15, `liquidation_weight` 0.01–0.10, `prev_margin_weight` 0.01–0.05, `min_atr` 8.0–25.0, `kelly_fraction` 0.05–0.18, `weights` (sum=1.0, ≥0.05 each), `min_model_probability` 0.52–0.70, `min_edge` 0.02–0.10, `min_kelly` 0.005–0.04, `exit_edge_threshold` −0.10..−0.03.
- **Structural constants (Investment 2 — promoted 2026-05-19):** `regime_momentum_threshold` 0.08–0.25, `flow_combined_cap` 0.20–0.60, `final_logit_clamp` 3.0–5.0, `deep_loss_hold_threshold` −0.20..−0.05, `l5_regime_damp_cap` 0.4–0.9, `atr_regime_shift_threshold` 0.40–0.80.
- **L6 derived feature weights (Investment 3 — added 2026-05-19, default 0.0):** `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_vol_regime_shift_weight`, `derived_flow_disagreement_weight`, `derived_distance_atr_ratio_weight`, `derived_time_remaining_logit_weight`, `derived_liq_signed_sqrt_weight`, `derived_prev_margin_sq_weight`. Each 0.0–0.05; combined L6 contribution hard-capped at ±0.25 logits. Library is closed — see `polybot/core/derived_features.py`.

**Manual-only** (claude_client validator reroutes `changes` → `manual_observations`):
`exit_edge_threshold`, `loss_cut_*`, `max_edge`, `adverse_selection_threshold`, `normal_fraction`, `late_max_penalty`, `flip_*`, `trading_*`, `max_concurrent_positions`, `max_bankroll_deployed`, `circuit_breaker.*`, `indicators.*`, `sprt.*`.

## Running

```bash
python -m polybot.main --mode paper       # Paper
python -m polybot.main --mode live        # Real USDC
python -m polybot.main --run-pipeline     # Pipeline once, no trading
python -m pytest polybot/tests/
```

`run_polybot.ps1` starts at 12:01 AM ET, stops trading at 11:15 PM ET, runs the pipeline at 11:30 PM ET, commits, restarts.

## Learning Pipeline

Daily 23:30 ET. Dataset bounded to the **last 60 days** before splitting (older trades came from probability machines that no longer exist). Walk-forward 60% train / 40% across folds [60:70][70:80][80:90][90:100] applied inside that window.

**Calibration** (isotonic) has its own 7-day window (needs ≥125 trades in pool; ≥75 in train split; skips entirely if below either threshold) — calibration must reflect the *current* model, not last month's.

**Adoption gate:** `candidate_sharpe > 0`, `n ≥ 100`, `z = Δ_sharpe / JK_SE ≥ 0.3` (Newey-West multi-lag autocorr-adjusted). Fold-consistency: worst-fold floor `min(fold_sharpes) ≥ -0.10` (magnitude-aware — a single tiny dip is fine, a deep collapse rejects). Regime-stratified veto activates per regime bucket once that bucket has ≥20 trades: dominant regime must improve AND no other regime may degrade >0.10 Sharpe.

**Interaction back-out:** if combined Δ_sharpe < 0.7 × sum(individual deltas), iteratively remove the weakest-z change until either the bound clears or ≤1 change remains.

**Crisis mode:** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`). The loss-ratio leg catches winning-small/losing-big pathologies the WR-only trigger misses. ≥3 consecutive cycles → halve `kelly_fraction` (floor 0.04), restore on first non-crisis. `kelly_reduced` flag persisted BEFORE the cut applies so a crash can't compound the halving.

**Atomic commit:** Calibrator save is deferred until after weight-optimizer persists config; mid-pipeline crash leaves on-disk calibrator + weights coherent.

**Stages:** PipelineTracker (review + auto-revert) → BiasDetector → Calibrator (isotonic) → KS shift → SPRT aggregate → TAEvolver (Claude or LocalRecommender) → WeightOptimizer → deferred calibrator save.

## Invariants

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded priors.
- **`model_probability_raw`** stores pre-calibration P(side) so re-fits don't compound.
- **Recency weighting:** `0.97^days_ago` on backtest + isotonic fit (~23-day half-life), applied inside the window cutoff.
- **`gain_pct = pnl/size`** (arithmetic). Never `log_return` for Sharpe.
- **UTC everywhere**; ET only for date-bucketing and trading-window logic.
- **Daily rollup** at 12:05 AM bundles per-trade JSON into `rollup_YYYY-MM-DD.json`.
- **L6 derived feature library is closed.** New entries require a code change in `polybot/core/derived_features.py` plus a ParamSpec; never generated at runtime.

## What NOT to Change

- Student-t, not normal CDF (BTC kurtosis 6–8).
- `momentum_weight` magnitude ≤ 0.10.
- Never `log_return` for Sharpe.
- Pricing from `GET /price?side=BUY|SELL`, not raw CLOB book.
- Fee from `GET /fee-rate`, not hardcoded.
- Resolution from Gamma/Chainlink, not Binance.
- Don't bypass circuit breaker.
- Don't delete `polybot/db/polybot_*.db`.
- Regime direction = sign of last 1-min return, not `sign(prob−0.5)`.
- Layer adjustments in logit space, not probability space.
- Binance.us, polymarket.com.

Update this file with every behavioral change.
