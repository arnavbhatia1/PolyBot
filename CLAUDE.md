# CLAUDE.md

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

## Probability Model

All layers compose in logit space (except L1's CDF), then sigmoid + isotonic calibration.

- **L1 — Student-t CDF** (df=5, clamped ≥3). `vol = max(atr, atr_floor)/atr_sigma_ratio · √min`, `z = (btc-strike)/vol · √(df/(df-2))`. ATR floor: `max(min_atr, 0.3 × rolling_20)`, widens when rolling_20 < 60% of long-term_200. Prob clipped to [1e-6, 1-1e-6] before logit (final ±4 clamp is the precision floor).
- **L2** — 1-lag autocorr × sign(last 1-min return). Single `lag1_autocorr` helper in `returns.py` — signal_engine and `RegimeDetector` both delegate to it.
- **L3** — CLOB book imbalance (top-5 levels each side, by best price) × 0.6 + trade flow × 0.4. Trade flow recency-weighted, 30s half-life inside the 120s window.
- **L3b** — Binance CVD + taker ratio (taker requires trade_count ≥ 5). CVD acceleration gate requires ≥ 3 trades in the recent 15s window (returns 0 otherwise).
- **L3e** — Bybit OI drop × price direction, normalized to %/minute using `oi_updated - oi_updated_prev`. tanh saturation `× 8` per minute (5%/min → 0.38, 10%/min → 0.66, 15%/min → 0.83 — softer than the old `× 20` on raw drop).
- **L4** — RSI/MACD/Stoch/OBV/VWAP. Polarity-split: mean-revert group (RSI/Stoch/VWAP) vs trend-confirm group (MACD/OBV). Smooth `tanh(autocorr / regime_momentum_threshold)` curve gates each group — no cliff at the threshold. In revert regime, mean-revert keeps its contrarian sign at full power and trend-confirm is dampened. In trend regime, mean-revert's sign is replaced by `sign(last_1min_return)` so polarity tracks the trend direction (continuation expectation, not a direction-agnostic flip) and trend-confirm runs at full power. Magnitude scaler from `effective_momentum_weight` is unsigned, also smoothed via tanh between DAMPEN (0.5×) and AMPLIFY (1.5×), clamped ±0.10.
- **L5** — `tanh(prev_margin/atr) · prev_margin_weight · logit_scale · (1 − min(l5_regime_damp_cap, |regime|))`. Dampened by regime strength to orthogonalize with L2 early in the window. `l5_regime_damp_cap` default 0.7, pipeline-tunable.
- **L6 — derived feature library.** Closed library of bounded transforms of already-tracked state (see `polybot/core/derived_features.py`). Every weight defaults to 0.0; the layer is dead until the pipeline raises one off zero. Combined L6 contribution hard-capped at ±0.25 logits regardless of individual weights — and `claude_client` validator drops any L6 weight change set whose `sum(|w|) · logit_scale` would push past that cap, so the optimizer can't search above adoptable space.
- **Calibration (isotonic) — `IsotonicCalibrator`** — sole overconfidence correction. Fit on last 7d of trades (not the global walk-forward split; needs ≥125 trades in window or skips entirely; train split ≥75 to fit). Identity by default. Adoption requires the bootstrap-CI lower bound (300 resamples, lower 80%) of weighted log-loss improvement vs identity to exceed 0 — accounts for the step-function variance of isotonic on thin pools.

L3+L3b combined capped at ±`flow_combined_cap` (default 0.35) logits. Final logit clamped ±`final_logit_clamp` (default 4.0) → prob ∈ [0.018, 0.982]. Both caps pipeline-tunable.

## Entry Gates

`prob ≥ 0.56`, `edge ≥ 0.04` (+1.5% per flip), `Kelly ≥ 0.01` (fee-aware: `b_eff = b × (1 − fee_rate)`), `spread ≤ 10%`, `depth ≥ $50`, `price_sum ∈ [0.98, 1.02]`, `edge ≤ max_edge`, `adverse_rate_at_30s ≤ adverse_selection_threshold` (30s post-fill checkpoint over a 30-min lookback, Bayesian-shrunk to a neutral prior so the gate stays active in low-volume hours), `mean_decay_15s ≥ edge_decay_threshold` (signed mean 15s post-fill drift over a 30-min lookback; default −0.05, inactive until ≥15 resolved fills in the lookback). Pre-submit edge re-check uses fresh ask AND slippage (matches the entry-gate net_edge math). CVD deceleration: skip if `|spot_flow| ≥ 0.20` AND `spot_flow × cvd_accel < 0`. SPRT: blocks SKIP (sequential evidence rejects the side), or favored-side mismatch (SPRT's accumulated favored side differs from current proposal when confidence > 60% with ≥6 obs). ATR gate: lower-bound only (`atr < 5th pctile`).

## Sizing & Exit

**Sizing:** `bankroll · kelly · breaker · time_mult · consensus_mult · concurrent_mult`, clipped to `bankroll · max_bankroll_deployed` and `book_depth · max_book_fill_pct`. Min $1 (Polymarket CLOB floor).

**Concurrent (correlation-aware):** worst ρ across open positions buckets to 0.35× (ρ>0.6) / 0.55× / 0.70× / 0.90× (ρ≤−0.2). Same-market → flip logic instead. ρ is a **fixed prior** (`+0.75` same-side, `−0.25` opposite-side), not an empirical estimate — see `correlation.py:16-17`. Promoting to a windowed empirical estimator with sample-size shrinkage is a future-work item, not a current behavior.

**Exit (`evaluate_hold`):** `holding_edge = model_prob − market_price`.
- `effective_threshold` blends `deep_loss_floor = exit_edge_threshold × (1 + 0.5 × itm_depth)` with the `ExitBoundary.compute_exit_threshold` curve. ATM trusts the boundary; deep ITM weights toward the more patient floor.
- Scalp when `deep_loss_hold_threshold < holding_edge ≤ effective_threshold` (deep_loss_hold_threshold pipeline-tunable, default −0.10).
- **Deep-loss hold:** `holding_edge < −0.10` AND `market < entry` → hold (binary residual beats locking the loss).
- **Loss-cut:** `market < entry × loss_cut_fraction` AND `seconds_remaining < loss_cut_time_s` AND BTC on wrong side of strike by ≥ 0.5×ATR (whipsaw guard).

**Flip trading:** after a scalp, re-enter the same window unboundedly (one position at a time). Each re-entry pays `flip_edge_premium` (or actual spread cost, whichever is higher) above `min_edge`.

**Circuit breaker:** tier-locked floor at $100/$150/$200/$300/…/$10,000; locks at 85% of tier crossed. Kelly scales 1.0 → `min_multiplier` between tier and floor (concave sqrt). Never resets down.

## Live Execution & Safety

- FOK-only via py-clob-client-v2; 3 retries w/ jittered exp backoff; HTTP/2 keepalive ping every 10s.
- `verify_auth`: `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER`, USDC balance + allowance ≥ `max_single × max_concurrent × 10`. Allowance re-checked every 10 fills mid-session.
- `open_position_and_debit_bankroll` / `close_position(... bankroll_delta=... | new_bankroll=...)`: atomic SQLite tx (single close path; pass `bankroll_delta` for relative credit, `new_bankroll` to set absolute on resolution).
- Auth errors → `AuthError` → clean exit; `run_polybot.ps1` won't restart same-day, but its outer `while ($true)` loop restarts the bot at the next 12:01 AM ET — fix auth before then.
- Feed staleness skip: Coinbase >30s, Chainlink >60s, Binance aggTrade >30s (L3b CVD/taker), Bybit OI >60s (L3e liquidation).
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
                         regime, liquidation, exit_boundary, sprt, adverse_selection, derived_features
  feeds/                 coinbase_feed, binance_feed(+depth+trades), bybit_feed, chainlink_feed, clob_ws, market_scanner
  indicators/            rsi/macd/stochastic/obv/vwap/ema/atr + engine
  execution/             base, paper_trader, live_trader, circuit_breaker, correlation
  agents/                scheduler, outcome_reviewer, counterfactual_tracker, ghost_tracker,
                         bias_detector, ta_evolver, weight_optimizer, pipeline_tracker,
                         pipeline_analytics, claude_client, claude_recommender, recommender_base, local_recommender
  memory/                calibration, outcomes, ghost_outcomes, counterfactuals, pipeline_*
  discord_bot/           !status !history !pause !resume !clear !session !commands
  db/models.py           SQLite (positions, trade_history, bankroll, peak_bankroll)
```

## Parameter Ownership

`param_registry.py` is the single source of truth for ALL defaults. Code-side fallbacks read `default_for(name)`; settings.yaml drives runtime.

**Pipeline-tunable** (Claude proposes, walk-forward adopts):
- **Layer weights / L1:** `atr_sigma_ratio` 1.2–2.5 (L1, highest leverage), `logit_scale` 2.0–5.0, `student_t_df` 3–8, `momentum_weight` 0.0–0.10 (magnitude only — sign is dead, polarity is regime-conditional per group inside `compute_momentum`), `regime_weight` 0.02–0.10, `flow_weight` 0.02–0.12, `spot_flow_weight` 0.01–0.15, `liquidation_weight` 0.01–0.10, `prev_margin_weight` 0.01–0.05, `min_atr` 8.0–25.0, `kelly_fraction` 0.05–0.18, `weights` (sum=1.0, ≥0.05 each), `min_model_probability` 0.52–0.70, `min_edge` 0.02–0.10, `min_kelly` 0.005–0.04, `exit_edge_threshold` −0.10..−0.03, `normal_fraction` 0.40–0.80, `late_max_penalty` 0.10–0.60, `flip_edge_premium` 0.005–0.05.
- **Structural constants (Investment 2 — promoted 2026-05-19):** `regime_momentum_threshold` 0.08–0.25, `flow_combined_cap` 0.20–0.60, `final_logit_clamp` 3.0–5.0, `deep_loss_hold_threshold` −0.20..−0.05, `l5_regime_damp_cap` 0.4–0.9, `atr_regime_shift_threshold` 0.40–0.80.
- **L6 derived feature weights (Investment 3 — added 2026-05-19, default 0.0):** `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_vol_regime_shift_weight`, `derived_flow_disagreement_weight`, `derived_distance_atr_ratio_weight`, `derived_time_remaining_logit_weight`, `derived_liq_signed_sqrt_weight`, `derived_prev_margin_sq_weight`. Each 0.0–0.05; combined L6 contribution hard-capped at ±0.25 logits. Library is closed — see `polybot/core/derived_features.py`.

**Manual-only** (claude_client validator reroutes `changes` → `manual_observations`):
`loss_cut_*`, `max_edge`, `adverse_selection_threshold`, `edge_decay_threshold`, `flip_enabled`, `trading_*`, `max_concurrent_positions`, `max_bankroll_deployed`, `circuit_breaker.*`, `indicators.*`, `sprt.*`.

## Running

```bash
python -m polybot.main --mode paper       # Paper
python -m polybot.main --mode live        # Real USDC
python -m polybot.main --run-pipeline     # Pipeline once, no trading
python -m pytest polybot/tests/
```

`run_polybot.ps1` starts at 12:01 AM ET, stops trading at 11:30 PM ET, runs the pipeline at 11:45 PM ET, commits, restarts.

## Learning Pipeline

Daily 23:45 ET. Dataset bounded to the **last 60 days** before splitting (older trades came from probability machines that no longer exist). Walk-forward 60% train / 40% across folds [60:70][70:80][80:90][90:100] applied inside that window. **Backtest Sharpe uses realized fills** — `gain_pct = pnl/size` from closed-trade outcomes, where `pnl` already nets actual fee and actual fill price (see `pipeline_analytics.py:77`, `scheduler.py:780`). No mid-price replay, no assumed-fill correction needed; candidate strategies inherit the same slippage cost any live trade would pay.

**Calibration** (isotonic) has its own 7-day window (needs ≥125 trades in pool; ≥75 in train split; skips entirely if below either threshold) — calibration must reflect the *current* model, not last month's.

**Adoption gate:** `candidate_sharpe > 0`, `n ≥ 100`, `z = Δ_sharpe / JK_SE ≥ 0.3` (Newey-West autocorr-adjusted with data-adaptive lag `L = floor(4·(n/100)^(2/9))`, per Newey & West 1994). Fold-consistency: worst-fold floor `min(fold_sharpes) ≥ -0.10` (magnitude-aware — a single tiny dip is fine, a deep collapse rejects). Regime-stratified veto activates per regime bucket once that bucket has ≥20 trades. Two acceptance branches: (a) candidate improves in ≥2 of 3 populated regime buckets with no regression >0.10 Sharpe; OR (b) dominant regime improves AND no other regime degrades >0.10 Sharpe. **Holdout confirmation:** the last 7 days are excluded from all folds and the evolver context (matches the calibrator's 7-day fit window — the candidate's last fold is never scored against trades the live calibrator already saw); after a candidate clears the gates above, candidate-vs-baseline backtests run on the holdout pool (when ≥30 trades) and adoption is blocked unless candidate Sharpe ≥ baseline + 0.02 (`HOLDOUT_ADOPTION_MARGIN`, slack against single-sample noise).

**Interaction back-out:** if combined Δ_sharpe < `backout_coef` × sum(individual deltas), iteratively remove the weakest-z change until either the bound clears or ≤1 change remains. `backout_coef` ramps 0.7 (at n=2 adoptions) → 0.9 cap, +0.05 per additional change — larger adoption sets need stronger interaction to back out.

**Crisis mode:** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`). The loss-ratio leg catches winning-small/losing-big pathologies the WR-only trigger misses. ≥3 consecutive cycles → halve `kelly_fraction` (floor 0.04 — **intentionally below the 0.05–0.18 pipeline-tunable range** so crisis can size more defensively than any state the optimizer can adopt; do not "fix" the discrepancy), restore on first non-crisis. `kelly_reduced` flag persisted BEFORE the cut applies so a crash can't compound the halving.

**Atomic commit:** Calibrator save is deferred until after weight-optimizer persists config; mid-pipeline crash leaves on-disk calibrator + weights coherent.

**Stages:** PipelineTracker (review + auto-revert) → BiasDetector → Calibrator (isotonic) → KS shift → SPRT aggregate → TAEvolver (Claude or LocalRecommender) → WeightOptimizer → deferred calibrator save.

## Invariants

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded priors.
- **`model_probability_raw`** stores pre-calibration P(side) so re-fits don't compound.
- **Recency weighting:** `0.94^days_ago` on backtest + isotonic fit (~11-day half-life), applied inside the window cutoff. Microstructure-trade edge decays faster than weeks.
- **`gain_pct = pnl/size`** (arithmetic). Never `log_return` for Sharpe.
- **UTC everywhere**; ET only for date-bucketing and trading-window logic.
- **Daily rollup** at 12:05 AM bundles per-trade JSON into `rollup_YYYY-MM-DD.json`.
- **L6 derived feature library is closed.** New entries require a code change in `polybot/core/derived_features.py` plus a ParamSpec; never generated at runtime.
- **Per-trade telemetry stamped at open, persisted at close:**
  - `trade_context.calibrator_hash` — 12-char digest (or `"identity"`) of the calibration curve live at fill time. Logged for audit only; no current consumer stratifies by it.
  - `edge_decay.deltas` — side-signed post-fill mid drift at 5/10/15/30/60s (positive = market moved in our favor). Captured by `AdverseSelectionMonitor` keyed by `position_id` and merged into the outcome JSON at close. The 15s mean over a 30-min lookback drives the live `edge_decay_threshold` entry gate. Null windows = trade closed before that checkpoint resolved.

## What NOT to Change

- Student-t, not normal CDF (BTC kurtosis 6–8).
- `momentum_weight` magnitude ≤ 0.10.
- Never `log_return` for Sharpe.
- Pricing from `GET /price?side=BUY|SELL`, not raw CLOB book.
- Fee uses Polymarket's binary-payoff formula `rate × shares × p × (1-p)` (zero at $0/$1 extremes, max at $0.50). The `rate` is a constant `0.018` for crypto markets — lives in `base.DEFAULT_FEE_RATE` and is what `market_scanner.fetch_fee_rate` returns. Price-dependent variation is in the formula, not the rate, so no per-token `GET /fee-rate` call is needed. If Polymarket ever changes the rate or makes it token-variable, restore the live API call and cache it.
- Resolution from Gamma/Chainlink, not Binance.
- Don't bypass circuit breaker.
- Don't delete `polybot/db/polybot_*.db`.
- Regime direction = sign of last 1-min return, not `sign(prob−0.5)`.
- Layer adjustments in logit space, not probability space.
- Binance.com, polymarket.com.

Update this file with every behavioral change.
