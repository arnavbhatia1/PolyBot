# CLAUDE.md

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

## Probability Model

All layers compose in logit space (except L1's CDF), then sigmoid + isotonic calibration.

- **L1 — Student-t CDF** (df=5, clamped ≥3). `vol = max(atr, atr_floor)/atr_sigma_ratio · √min`, `z = (btc-strike)/vol · √(df/(df-2))`. ATR floor: `max(min_atr, 0.3 × rolling_20)`, widens when rolling_20 < 60% of long-term_200. Prob clipped to [1e-6, 1-1e-6] before logit (final ±4 clamp is the precision floor). `btc_price` comes from `_fastest_btc_price` (Coinbase WS <2s → Binance aggTrade <3s → Binance kline receipt <5s); all-stale → skip the decision.
- **L2** — 1-lag autocorr × sign(last 1-min return). Single `lag1_autocorr` helper in `returns.py` — signal_engine and `RegimeDetector` both delegate to it.
- **L3** — CLOB book imbalance (top-5 levels each side, by best price) × 0.6 + trade flow × 0.4. Trade flow recency-weighted, 30s half-life inside the 120s window.
- **L3b** — Coinbase per-trade CVD + taker ratio via `polybot/core/aux_layers.compute_spot_flow_signal`. Coinbase is the largest US-volume BTC venue and the venue Chainlink resolves against. CVD scaled by `tanh(cvd / 30 BTC) × 0.8`; taker ratio gated on `≥ 20` trades in the 60s window. CVD-acceleration gate (`get_cvd_acceleration`) requires `≥ 10` recent trades for the 15s/45s comparison.
- **L3e** — Direct per-event futures liquidations from Binance (`btcusdt@forceOrder`). Net `(short_liq − long_liq) USD/min`, tanh-saturated at `50,000` USD/min via `compute_liquidation_signal`. Sign: short liquidation → price-up (+); long liquidation → price-down (−).
- **L4** — RSI/MACD/Stoch/OBV/VWAP, raw `score` field consumed directly (no adaptive normalizer). Polarity-split: mean-revert group (RSI/Stoch/VWAP) vs trend-confirm group (MACD/OBV). Smooth `tanh(autocorr / regime_momentum_threshold)` curve gates each group — no cliff at the threshold. In revert regime, mean-revert keeps its contrarian sign at full power and trend-confirm is dampened. In trend regime, mean-revert's sign is replaced by `sign(last_1min_return)` so polarity tracks the trend direction (continuation expectation, not a direction-agnostic flip) and trend-confirm runs at full power. Magnitude scaler from `effective_momentum_weight` is unsigned, smoothed via tanh between DAMPEN (0.5×) and AMPLIFY (1.5×) — the pipeline `momentum_weight` range bound is the only ceiling.
- **L5** — `tanh(prev_margin/atr) · prev_margin_weight · logit_scale · (1 − min(l5_regime_damp_cap, |regime|))`. Dampened by regime strength to orthogonalize with L2 early in the window. `l5_regime_damp_cap` default 0.7, pipeline-tunable. `prev_resolution_margin` is persisted with a `saved_at` timestamp and zeroed on load if older than 30 min.
- **L6 — derived feature library.** Closed library of 4 bounded, direction-aware transforms of already-tracked state (see `polybot/core/derived_features.py`): `log_atr_ratio` (clipped ±1.5), `autocorr_signed_mag` (regime × signed last_return), `flow_disagreement` (tanh of flow + spot_flow, direction-aware), `liq_signed_sqrt`. Every weight defaults to 0.0; the layer is dead until the pipeline raises one off zero. Combined L6 contribution hard-capped at ±0.25 logits regardless of individual weights — and `claude_client` validator drops any L6 weight change set whose `sum(|w|) · logit_scale` would push past that cap.
- **Calibration (isotonic) — `IsotonicCalibrator`** — sole overconfidence correction. Fit on last 7d of trades (not the global walk-forward split; needs ≥125 trades in window or skips entirely; train split ≥75 to fit). Identity by default. Adoption is a **single OOB bootstrap-CI gate** — the lower-80% bound of weighted log-loss improvement vs identity, computed across 300 OOB resamples with per-bootstrap weight renormalization, must be strictly positive. RNG seeded from `time.time_ns()` each fit so the CI tracks real sampling variance. `last_fit_diagnostics` is populated on every `fit()` that reaches the bootstrap CI stage (both `adopted` and `rejected_ci`) — `oob_ci_lower_nats`, `oob_ci_median_nats`, `n_samples`, `bootstrap_n_completed`, `y_min`, `y_max`, `decision` — and stamped to `pipeline_info["cal_info"]["fit_diagnostics"]`. Structural early-rejects (sample-count, zero-weight, sklearn exception, pre-CI range check) return `False` without stamping; the reject site logs the reason.

L3 + L3b add in logit space with a **joint ±0.50-logit clamp** so a weight asymmetry can't let one leg dominate L1 during a CLOB↔CVD disagreement. Final logit clamped ±`final_logit_clamp` (default 4.0) → prob ∈ [0.018, 0.982]. Pipeline-tunable.

## Entry Gates

`prob ≥ 0.56`, `edge ≥ 0.04` (flip premium scales — `0.015 + 0.005 · max(0, flip_count − 2)`), `Kelly ≥ 0.01` (fee-aware: `b_eff = b × (1 − fee_rate)`), `spread ≤ 10%`, `depth ≥ $50`, `price_sum ∈ [0.98, 1.02]`, `edge ≤ max_edge`, `mean_decay_15s ≥ edge_decay_threshold` (signed mean 15s post-fill drift over a 30-min lookback; default −0.05, inactive until ≥15 resolved fills in the lookback). Adverse selection is **sizing-side, not entry-side**: `kelly_mult ×= max(0.3, 1 − 1.5 · max(0, adverse_rate_at_30s − 0.45))`; emergency hard-skip only at `adverse_rate ≥ 0.80`. The 30-min lookback is Bayesian-shrunk to a neutral prior so the penalty stays calibrated in low-volume hours. Pre-submit edge re-check uses fresh ask AND slippage (matches the entry-gate net_edge math). CVD deceleration: skip if `|spot_flow| ≥ 0.20` AND `spot_flow × cvd_accel < 0`. SPRT: blocks SKIP (sequential evidence rejects the side), or favored-side mismatch (SPRT's accumulated favored side differs from current proposal when confidence > 60% with ≥6 obs). ATR gate: lower-bound only (`atr < 5th pctile`).

## Sizing & Exit

**Sizing:** `bankroll · kelly · breaker · time_mult · consensus_mult · concurrent_mult`, clipped to `bankroll · max_bankroll_deployed` and `book_depth · max_book_fill_pct`. Min $1 (Polymarket CLOB floor).

**Concurrent (correlation-aware):** worst ρ across open positions buckets to 0.35× (ρ>0.6) / 0.55× / 0.70× / 0.90× (ρ≤−0.2). Same-market → flip logic instead. ρ is a **fixed prior** (`+0.75` same-side, `−0.25` opposite-side), not an empirical estimate — see `correlation.py:16-17`. Promoting to a windowed empirical estimator with sample-size shrinkage is a future-work item, not a current behavior.

**Exit (`evaluate_hold`):** `holding_edge = model_prob − market_price`.
- `effective_threshold` blends `deep_loss_floor = exit_edge_threshold × (1 + 0.5 × itm_depth)` with the `ExitBoundary.compute_exit_threshold` curve. ATM trusts the boundary; deep ITM weights toward the more patient floor.
- Scalp when `deep_loss_hold_threshold < holding_edge ≤ effective_threshold`. `exit_edge_threshold` default −0.10, pipeline-tunable. `deep_loss_hold_threshold` default −0.10, pipeline-tunable.
- **Deep-loss hold:** `holding_edge < −0.10` AND `market < entry` → hold (binary residual beats locking the loss).
- **Loss-cut:** `market < entry × loss_cut_fraction` AND `seconds_remaining < loss_cut_time_s` AND BTC on wrong side of strike by ≥ 0.5×ATR (whipsaw guard). Engine stamps `last_loss_cut_event` ∈ {`""`, `"fired"`, `"whipsaw_blocked"`} per call; the trading loop counts those into `gate_stats` to audit the cushion's selectivity.
- **Pipeline learns `exit_edge_threshold`:** when the candidate change is `exit_edge_threshold`, the backtest replays scalped outcomes through the counterfactual tracker — trades whose recorded `holding_edge_at_scalp` is above the candidate threshold are repriced using the matched hold-to-resolution PnL.

**Flip trading:** after a scalp, re-enter the same window unboundedly (one position at a time). Per-flip premium = `flip_edge_premium + 0.005 · max(0, flip_count − 2)` — flips 1–2 pay the base, deeper flips pay more (or actual spread cost, whichever is higher) above `min_edge`.

**Circuit breaker:** tier-locked floor at $100/$150/$200/$300/…/$10,000; locks at 85% of tier crossed. Kelly scales 1.0 → `min_multiplier` between tier and floor (concave sqrt). Never resets down.

## Live Execution & Safety

- FOK-only via py-clob-client-v2; 3 retries w/ jittered exp backoff; HTTP/2 keepalive ping every 10s.
- `verify_auth`: `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER`, USDC balance + allowance ≥ `max_single × max_concurrent × 10`. Allowance re-checked every 10 fills mid-session.
- `open_position_and_debit_bankroll` / `close_position(... bankroll_delta=... | new_bankroll=...)`: atomic SQLite tx (single close path; pass `bankroll_delta` for relative credit, `new_bankroll` to set absolute on resolution).
- Auth errors → `AuthError` → clean exit; `run_polybot.ps1` won't restart same-day, but its outer `while ($true)` loop restarts the bot at the next 12:01 AM ET — fix auth before then.
- Feed staleness skip: Coinbase >30s (L3b CVD source + fast price), Chainlink >60s (resolution oracle), Binance aggTrade >30s (CVD-accel + 2nd-fastest price). L3b reads Coinbase, L3e reads the direct Binance forceOrder per-event liquidation stream.
- Chainlink orphan fallback: Gamma silent 30+ min past expiry.
- CLOB WS heartbeat: PING every 10s, force reconnect if no PONG within 25s.
- `fill.fill_size` is always USDC notional (BUY: requested; SELL: shares × fill_price).
- Per-mode DB (`polybot_paper.db` / `polybot_live.db`); `memory/` shared.
- `polybot/feeds/_socket.enable_nodelay(ws, name)` verifies `TCP_NODELAY` via `getsockopt` on every WS connect; `polybot/feeds/_staleness.StalenessTracker` persists per-feed P50/P95/P99 inter-arrival gaps to `polybot/memory/feed_staleness.json` every 60s.

## Project Structure

```
polybot/
  main.py                Trading loop, entry/exit/sizing
  config/                {settings.yaml, loader.py, param_registry.py}
  core/                  signal_engine, calibrator, order_flow, returns,
                         regime, liquidation, exit_boundary, sprt, adverse_selection, derived_features
  feeds/                 coinbase_feed, binance_feed(+depth+trades+forceorder), chainlink_feed, clob_ws, market_scanner
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
- **Structural constants:** `regime_momentum_threshold` 0.08–0.25, `final_logit_clamp` 3.0–5.0, `deep_loss_hold_threshold` −0.20..−0.05, `l5_regime_damp_cap` 0.4–0.9, `atr_regime_shift_threshold` 0.40–0.80.
- **L6 derived feature weights (default 0.0):** `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_flow_disagreement_weight`, `derived_liq_signed_sqrt_weight`. Each 0.0–0.05; combined L6 contribution hard-capped at ±0.25 logits. Library is closed — see `polybot/core/derived_features.py`. The previous 8-feature library was pruned to 4 in Pillar 2 (redundant + direction-loss bugs).

**Manual-only** (claude_client validator reroutes `changes` → `manual_observations`):
`loss_cut_*`, `max_edge`, `adverse_selection_threshold`, `edge_decay_threshold`, `trading_*`, `max_concurrent_positions`, `max_bankroll_deployed`, `circuit_breaker.*`, `indicators.*`, `sprt.*`.

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

**Adoption gate:** `n ≥ 100`, `z = Δ_sharpe / JK_SE ≥ 0.3` (Newey-West autocorr-adjusted, data-adaptive lag per Newey & West 1994). **Soft abs floor:** `candidate_sharpe < min(0, current_sharpe) − 0.05` is blocked — the loop can adopt a less-negative candidate during a regime shift, but not an outright collapse. Fold-consistency: worst-fold floor `min(fold_sharpes) ≥ -0.10`. Regime-stratified veto activates per regime bucket once that bucket has ≥8 trades (lowered from 20 to populate non-`neutral` buckets on typical BTC folds). Two acceptance branches: (a) candidate improves in ≥2 of 3 populated regime buckets with no regression >0.10 Sharpe; OR (b) dominant regime improves AND no other regime degrades >0.10 Sharpe. **Holdout confirmation:** the last 7 days are excluded from all folds and the evolver context; after a candidate clears the gates above, candidate-vs-baseline backtests run on the holdout pool (when ≥30 trades) and adoption is blocked unless `candidate_sharpe ≥ baseline + max(0.02, ADOPTION_Z_FLOOR × holdout_jk_se)` — margin scales with holdout sample size. `pipeline_info["holdout_active"]` is stamped explicitly each cycle.

**Interaction back-out:** if combined Δ_sharpe < `backout_coef` × sum(individual deltas), iteratively remove the weakest-z change until either the bound clears or ≤1 change remains. `backout_coef` ramps 0.7 (at n=2 adoptions) → 0.9 cap, +0.05 per additional change — larger adoption sets need stronger interaction to back out.

**Crisis mode:** triggers on EITHER (a) baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`) OR (b) trailing-3-day Sharpe < 0 over ≥20 recent trades. The trailing-3d leg catches sustained multi-day collapses the recent-50 smoothing misses. ≥3 consecutive cycles → halve `kelly_fraction` (floor 0.04 — **intentionally below the 0.05–0.18 pipeline-tunable range** so crisis can size more defensively than any state the optimizer can adopt; do not "fix" the discrepancy), restore on first non-crisis. `kelly_reduced` flag persisted BEFORE the cut applies so a crash can't compound the halving.

**Atomic commit:** Calibrator save is deferred until after weight-optimizer persists config; mid-pipeline crash leaves on-disk calibrator + weights coherent.

**Stages:** PipelineTracker (review + auto-revert) → BiasDetector → Calibrator (isotonic) → KS shift → SPRT aggregate → TAEvolver (Claude or LocalRecommender) → WeightOptimizer → deferred calibrator save.

**Adaptive exploration:** `recommender_base._rule_exploratory` ramps `EXPLORE_STEPS[param]` upward when the directional table shows past probes returned |bt_delta| under the noise floor. The noise floor is **empirical per cycle** — `empirical_noise_floor(baseline_jk_se) = max(0.003, 0.3 × baseline_jk_se)` — so it tracks real sampling variance instead of a static constant. Each "dead" direction adds +50% to the step multiplier (cap 3.0×). Adoptions reset the loop because the directional table shows non-trivial deltas.

**Structural probes:** `recommender_base.STRUCTURAL_PROBES` is a small forced-exploration table that fires once per `(param, value)` until evidence appears in the directional table. Drives the `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` sweep (counterfactual data backs the less-strict exit) and the L6 turn-on probes for `log_atr_ratio`, `autocorr_signed_mag`, `liq_signed_sqrt` (raise to 0.005 from the default 0.0 so the layer can be evaluated for adoption). Both `LocalRecommender` and `ClaudeRecommender` call `_rule_structural_probes()` before the rotational `_rule_exploratory`.

**Calibrator diagnostics:** `IsotonicCalibrator.last_fit_diagnostics` is populated on every `fit()` call regardless of accept/reject — `n_samples`, `in_sample_improvement_nats`, `oob_ci_lower_nats`, `oob_ci_median_nats`, `bootstrap_n_completed`, `y_min`, `y_max`, `decision`. Stamped to `pipeline_info["cal_info"]["fit_diagnostics"]` so every gate decision is observable.

**L6 directional table:** the optimizer captures `old_value` from `signal_engine.derived_weights[fname]` for `derived_*_weight` params (not from `getattr(signal_engine, param)` which would return `None`), so L6 probes populate the directional table on every cycle.

## Telemetry

- **`gate_stats_YYYYMMDD.json`** (per ET day): in-process accumulator persists on every position resolution to a date-keyed file. `gate_stats.json` mirrors the current day. Mid-day restarts preserve the day's counts — the in-process dict reloads from disk on first record. Includes `loss_cut_fired` / `loss_cut_whipsaw_blocked` to audit the 0.5×ATR cushion's selectivity.
- **`adverse_kelly_mult`** is stamped per-trade in `indicator_snapshot.trade_context`: the actual Kelly multiplier applied at sizing (1.0 = no penalty, `adverse_penalty_min` floor). Enables per-bucket retrospective Sharpe analysis.

## Invariants

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded priors.
- **`model_probability_raw`** stores pre-calibration P(side) so re-fits don't compound.
- **Recency weighting:** `0.94^days_ago` on backtest + isotonic fit (~11-day half-life), applied inside the window cutoff. Microstructure-trade edge decays faster than weeks.
- **`gain_pct = pnl/size`** (arithmetic). Never `log_return` for Sharpe.
- **UTC everywhere**; ET only for date-bucketing and trading-window logic.
- **Daily rollup** at 12:05 AM bundles per-trade JSON into `rollup_YYYY-MM-DD.json`.
- **L6 derived feature library is closed.** New entries require a code change in `polybot/core/derived_features.py` plus a ParamSpec; never generated at runtime.
- **`edge_decay.deltas` stamped at open, persisted at close:** side-signed post-fill mid drift at 5/10/15/30/60s (positive = market moved in our favor). Captured by `AdverseSelectionMonitor` keyed by `position_id` and merged into the outcome JSON at close. The 15s mean over a 30-min lookback drives the live `edge_decay_threshold` entry gate. Null windows = trade closed before that checkpoint resolved.

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
