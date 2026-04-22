# CLAUDE.md

## Project Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. Computes P(Up) via an 8-layer probability model, compares to market price, trades when edge > noise floor and Kelly justifies size. Holds to $1 resolution when confident, scalps early when holding_edge drops below fee-aware threshold.

## Key Architecture

**Signal layers (all logit-space except L1):**
- L1: Student-t CDF (df=5). `z = (btc - strike) / ((ATR/atr_sigma_ratio) * sqrt(minutes) * iv_ratio)`
- L2: Regime detection (1-lag autocorrelation of 1-min returns)
- L3: CLOB order flow (60% book imbalance + 40% trade flow)
- L3b: Spot CVD from Binance aggTrades (taker-gated, min 5 trades)
- L3c: Wall pressure — DISABLED (wall_weight=0.00, gamed by HFT)
- L3e: Liquidation pressure (Bybit OI drop + price direction)
- L4: Indicator momentum (RSI/MACD/Stochastic/OBV/VWAP). Base `momentum_weight=-0.02` (fade). **Regime-conditional at runtime**: trending (L2 autocorr > +0.15) flips sign and amplifies 1.5× to ride momentum; mean-reverting (autocorr < -0.15) amplifies fade 1.5×; inside the ±0.15 band dampens 0.5×. Effective weight clamped to [-0.10, +0.10].
- L5: Previous window momentum carry (prev_margin / ATR)
- Platt scaling calibration applied after all layers

**Entry gates (all must pass):** prob >= 58%, edge >= 4% (+ 1.5% for flips), Kelly >= 0.015, spread <= 10%, depth >= $50, price_sum in [0.98, 1.02], edge <= 20%, adverse_rate_30s <= `adverse_selection_threshold` (post-fill reversal rate over rolling 2h window; informed-flow detector — fills older than `lookback_s=7200s` are dropped from the sample to prevent stale-regime state from blocking trading for hours after a bad morning), last 30s: prob >= 90%

**Sizing chain:** `min(bankroll × kelly, max_single_pct, max_single_usd) × breaker × uncertainty_discount(floor=0.40) × time_mult × concurrent_multiplier`. Absolute caps apply to the RAW Kelly size FIRST so the soft multiplicative discounts (uncertainty / breaker / phase / correlation) actually reduce below the cap instead of being no-ops when the cap binds. `concurrent_multiplier` is **correlation-aware + size-weighted**: same-side (ρ≈0.75) at full size gets 0.35×, opposite-side (ρ≈-0.25) gets 0.90×, with piecewise buckets in between. Correlation contribution is scaled by `existing_position_size / max_single_usd` so a tiny residual position no longer hits a new entry as hard as a full-size concurrent.

**Exit:** `evaluate_hold()` — same model for entry and exit. Scalp when `holding_edge <= fee_aware_threshold`. Trailing exit: entry < $0.50, peaked > $0.65, drops 15%+ from peak.

**Flip trading:** After scalp, re-enter opposite side same window. Max 1 flip. Requires +1.5% extra edge.

**Circuit breaker:** Tiered floor — locks at `tier × 0.85` each time bankroll crosses $100/$150/$200/$300... Kelly scales 1.0→0.40 between tier and floor. Never resets down. `floor_pct` and `max_single_position_usd` are manual-only, not pipeline-tunable.

**Execution:** FOK-only market orders. 3 retries with exponential backoff. Live mode: `signature_type=2` (GNOSIS_SAFE), `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` (.env). Bybit REST (`api.bybit.com/v5/market/tickers`) is geo-blocked for US IPs — on first 401/403/451 the poll loop stops permanently; WS is the primary OI/funding source regardless.

**Auto-restart:** `run_polybot.ps1` — starts 12:15 AM ET, pipeline at 12:05 AM, commits outcomes/DB/config to git, restarts.

## Project Structure

```
polybot/
  main.py                    # Entry point, trading loop
  config/settings.yaml       # ALL tunable parameters
  core/
    signal_engine.py         # 8-layer probability model + evaluate_hold
    calibrator.py            # Platt scaling (fitted daily)
    order_flow.py            # CLOB book imbalance + trade flow
    returns.py               # gain_pct (arithmetic, not log — log(0)=-inf)
    bankroll_strategy.py     # Uncertainty-adjusted Kelly + drawdown velocity
    regime.py                # Multi-state regime classifier
    liquidation.py           # OI-based liquidation pressure (L3e)
    exit_boundary.py         # Optimal binary exit curve (MDP-based)
    sprt.py, alpha_decay.py, adverse_selection.py
    garch_vol.py, crowd_bias.py, gamma_exposure.py
  feeds/
    binance_feed.py          # 1-min candles, ATR, indicators, strike
    coinbase_feed.py         # Primary BTC price (fastest)
    kraken_feed.py           # Secondary BTC price (Chainlink oracle source)
    clob_ws.py               # Real-time CLOB WS (books, trades, resolution)
    market_scanner.py        # Gamma API discovery + CLOB HTTP helpers
    binance_depth.py         # Spot L2 book (imbalance, depth)
    binance_trades.py        # aggTrades: CVD, taker ratio (L3b)
    bybit_feed.py            # Perp price lead + OI (WS only, REST geo-blocked)
    deribit_iv.py            # IV + GEX from options chain
    chainlink_feed.py        # Chainlink BTC/USD oracle (resolution source)
  indicators/
    ema.py, rsi.py, macd.py, stochastic.py, obv.py, vwap.py, atr.py
    engine.py                # Combines all, manages weight versions
  execution/
    base.py                  # BaseTrader ABC, fee math, shared gates
    paper_trader.py          # Instant simulated fills
    live_trader.py           # FOK market orders via py-clob-client
    circuit_breaker.py       # Tiered floor Kelly scaling
  agents/
    scheduler.py             # Pipeline orchestrator (walk-forward, per-param cooldown)
    outcome_reviewer.py      # Writes memory/outcomes/ JSON per trade
    counterfactual_tracker.py
    bias_detector.py         # Per-indicator/regime/time accuracy analysis
    ta_evolver.py            # Claude recommendations + local fallback
    weight_optimizer.py      # Walk-forward backtest, z-test adoption
    pipeline_tracker.py      # Adoption track record + run log (directional table, prediction accuracy, decay)
    pipeline_analytics.py    # Time-weighting, KS shift, SPRT aggregation
    claude_client.py         # analyze_strategy() — distilled card to Claude
  memory/
    outcomes/                # One JSON per trade (timestamped UTC)
    counterfactuals/         # One JSON per scalp or hold what-if
    ghost_outcomes/          # Downstream-gate rejections tracked to resolution
    calibration/platt_params.json
    weights/                 # Versioned weight configs
    biases.json              # BiasDetector output
    pipeline_history.json    # Adoption track record (1d/3d/7d/14d/30d reviews, decay status)
    pipeline_run_log.json    # Every change tested per cycle (direction, backtest Δ, Claude prediction)
    gate_stats.json          # Entry gate skip counts (reset each restart)
    fill_stats.json          # FOK fill rate (total/buy/sell attempts vs fills)
    adverse_state.json       # Per-fill post-fill price evolution (30s/60s) for adverse selection
  discord_bot/
    bot.py                   # !status !history !pause !resume !clear !session
    alerts.py                # Trade alerts, banners, daily report, purge
  db/models.py               # SQLite: positions, trade_history, bankroll
```

## Config (`config/settings.yaml`)

Parameters fall into three ownership classes. Which class a param lives in is
determined by whether the walk-forward backtest (`_kelly_bankroll_returns`) can
simulate its change and whether the value is user-owned risk policy.

### Pipeline-Tunable (Claude + backtest adoption)
These flow through `_kelly_bankroll_returns`, so the nightly pipeline can test them
in backtest and adopt when the candidate Sharpe clears the dynamic floor.
- `math.kelly_fraction` 0.15 (range 0.05-0.25)
- `signal.momentum_weight` **-0.02** (NEGATIVE = fade indicators. Range -0.10 to +0.10)
- `signal.regime_weight` 0.03 (range 0.02-0.10)
- `signal.flow_weight` 0.04 (range 0.02-0.12)
- `signal.spot_flow_weight` 0.04 (range 0.01-0.10)
- `signal.liquidation_weight` 0.03 (range 0.01-0.06)
- `signal.prev_margin_weight` 0.02 (range 0.01-0.05)
- `signal.wall_weight` 0.00 (disabled — gamed by HFT)
- `signal.atr_sigma_ratio` 1.4 (range 1.2-2.5)
- `signal.student_t_df` 5 (range 3-8, int)
- `signal.logit_scale` 4.0 (range 2.0-6.0)
- `signal.probability_compression` 1.0 (range 0.5-1.0)
- `signal.min_atr` 8.0 (range 5.0-15.0; runtime floor = `max(min_atr, 0.3 × rolling_mean_atr_20)`)
- `signal.weights` rsi/macd/stochastic/obv/vwap (sum to 1.0, each >= 0.05)

### Read-Only for Claude (entry gates — corrupt the backtest comparison)
The backtest replays stored outcomes. Raising any gate filters historical trades
out of BOTH baseline and candidate runs, so the comparison is no longer apples-to-apples.
Change these manually in `settings.yaml` only.
- `signal.min_model_probability` 0.58
- `signal.entry_threshold` (min_edge) 0.04
- `signal.min_kelly` 0.015 (primary gate)

### Manual-Only (unbacktestable or user-owned risk policy)
Either the backtest cannot simulate the change (exit/timing/schedule) or these are
operator-owned risk caps. Claude is instructed not to propose them; the pipeline
silently drops any attempt.
- `signal.exit_edge_threshold` -0.05 — backtest can't re-simulate scalp vs hold on stored gain_pct
- `signal.max_edge` 0.20 — stale-price filter (entry-time only)
- `entry_timing.normal_fraction` 0.60 — time-of-day Kelly envelope (backtest ignores)
- `entry_timing.late_max_penalty` 0.60 — late-window Kelly penalty (backtest ignores)
- `entry_timing.final_min_probability` 0.90 — last-30s hard gate (entry-time only)
- `entry_timing.adverse_selection_threshold` 0.55 — informed-flow filter (entry-time only)
- `entry_timing.flip_enabled` true, `flip_edge_premium` 0.015
- `schedule.trading_start_hour_et` 0, `trading_end_hour_et` 23, `trading_end_minute` 59
- `execution.max_concurrent_positions` 2, `max_bankroll_deployed` 0.80
- `execution.max_single_position_pct` 0.12, `max_single_position_usd` 18.00 (risk cap)
- `circuit_breaker.floor_pct` 0.85, `min_multiplier` 0.40 (risk cap)

**When Claude proposes a manual-only param** (e.g., from counterfactual scalp analysis
pointing at `exit_edge_threshold`): the validator in `claude_client.py` drops it before
it ever reaches the backtest. The pipeline will translate the finding into a proposable
param (e.g., scalp-too-early → raise `logit_scale` or lower `atr_sigma_ratio`).

## Parameter Ownership Quick Reference

Who can change what, and why. **Pipeline = nightly Claude + walk-forward adoption. Operator = you, editing settings.yaml.**

### 🟢 Pipeline-Tunable (Claude proposes, walk-forward adopts)

These flow through `_kelly_bankroll_returns` so the backtest can simulate changes.

| Param | Range | What it does |
|---|---|---|
| `atr_sigma_ratio` | 1.2–2.5 | L1 aggressiveness. Lower = tighter probabilities, more edge found. HIGHEST leverage. |
| `logit_scale` | 2.0–6.0 | Amplifies L2–L5 signals. Higher = flow/regime/momentum matter more. |
| `probability_compression` | 0.5–1.0 | Shrinks final prob toward 0.5. 1.0 = off; lower = fix overconfidence at extremes. |
| `student_t_df` | 3–8 | Tail fatness. Lower = fatter tails, more reversal edge. |
| `momentum_weight` | -0.10 to +0.10 | L4. NEGATIVE = fade indicators. |
| `regime_weight` | 0.02–0.10 | L2 autocorrelation adjustment. |
| `flow_weight` | 0.02–0.12 | L3 CLOB order flow (book imbalance + trade flow). |
| `spot_flow_weight` | 0.01–0.10 | L3b Binance CVD + taker ratio. |
| `liquidation_weight` | 0.01–0.06 | L3e Bybit OI liquidation pressure. |
| `prev_margin_weight` | 0.01–0.05 | L5 previous-window momentum carry. |
| `min_atr` | 5.0–15.0 | Floor on ATR (runtime: `max(min_atr, 0.3 × rolling_20)`). |
| `kelly_fraction` | 0.05–0.25 | Sizing aggressiveness. Claude rarely moves this. |
| `weights` | sum=1.0, each ≥ 0.05 | L4 indicator mix (rsi/macd/stochastic/obv/vwap). |

Adoption gates: candidate Sharpe > 0, n ≥ 100, Δ ≥ max(0.020, 0.25 × JK_SE), ≥ 2/4 folds improve, dominant regime improves without any regime degrading > 0.10, param not in 2-day cooldown.

### 🟡 Read-Only for Claude (entry gates — corrupt the backtest comparison)

The backtest replays stored outcomes. Changing these alters which trades qualify in BOTH baseline and candidate → comparison no longer apples-to-apples. Claude sees them for context; validator silently drops any change attempt.

| Param | Value | Why off-limits |
|---|---|---|
| `min_model_probability` | 0.58 | Filters historical trades out of backtest when raised |
| `min_edge` (entry_threshold) | 0.04 | Same issue |
| `min_kelly` | 0.015 | Same issue — primary entry gate |

Could migrate to pipeline-tunable if the backtest was re-derived from raw signal/price data rather than stored outcomes.

### 🔴 Manual-Only (operator — edit settings.yaml directly)

Either unbacktestable (gain_pct is post-hoc, backtest can't simulate different exits) or user-owned risk policy.

**Unbacktestable — exit / timing / filters:**

| Param | Default | Why manual |
|---|---|---|
| `exit_edge_threshold` | -0.05 | Scalp-vs-hold decision; backtest replays fixed gain_pct |
| `adverse_selection_threshold` | 0.55 | Informed-flow filter; entry-time only |
| `normal_fraction` | 0.60 | Time-of-day Kelly envelope; backtest ignores time-of-day |
| `late_max_penalty` | 0.60 | Late-window Kelly penalty; backtest ignores |
| `final_min_probability` | 0.90 | Last-30s hard gate; entry-time only |
| `max_edge` | 0.20 | Stale-price safety cap; entry-time only |
| `trading_start_hour_et` / `trading_end_hour_et` / `trading_end_minute` | 0 / 23 / 59 | Schedule; backtest ignores |
| `flip_enabled` / `flip_edge_premium` | true / 0.015 | Same-window re-entry; not in backtest |

**Risk caps — operator's call, not the model's:**

| Param | Default | Purpose |
|---|---|---|
| `max_single_position_usd` | 18.00 | Hard dollar ceiling per trade |
| `max_single_position_pct` | 0.12 | Bankroll concentration cap |
| `max_concurrent_positions` | 2 | Hedged Up+Down allowed |
| `max_bankroll_deployed` | 0.80 | Total exposure cap |
| `circuit_breaker.floor_pct` | 0.85 | Protect 85% of each locked tier |
| `circuit_breaker.min_multiplier` | 0.40 | Kelly scaling at the floor |

### Mental model

- **Pipeline** optimizes the *probability model* (L1–L5 weights + calibration).
- **Operator** owns *exit behavior, schedule, and risk policy*.
- The read-only middle class is a backtest-design limitation, not a conceptual one.

## Running

```bash
python -m polybot.main --mode paper    # Paper (persistent bankroll)
python -m polybot.main --mode live     # Live (real USDC)
python -m polybot.main --run-pipeline  # Run pipeline once, no trading
python -m pytest polybot/tests/        # Test suite
```

**Live preflight:** `verify_auth(min_allowance_usd)` checks `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER`, **USDC balance, and USDC allowance** to the CTF Exchange. Main.py passes `max_single_position_usd × max_concurrent_positions × 10` as the allowance floor — a revoked or exhausted allowance is caught here before the bot starts "placing" orders that would silently fail at the exchange level. Syncs DB bankroll from real Polymarket balance on startup. Circuit breaker first tier is $100 — start with $100+.

## Probability Model (condensed)

```
L1: vol = (ATR_eff / atr_sigma_ratio) * sqrt(minutes) * iv_ratio
    z_scaled = (btc - strike) / vol * sqrt(df / (df-2))
    P(Up) = t.cdf(z_scaled, df=5)

L2-L5: logit_p += signal * (weight * logit_scale)   [all in log-odds space]

Calibration: calibrated = 1 / (1 + exp(A * logit(raw) + B))
  A,B in memory/calibration/platt_params.json, re-fitted daily

Resolution: always from Gamma API eventMetadata or closed+outcomePrices.
  Never guess from Binance — Chainlink and Binance can disagree by $20-200.
```

## External Data Sources

| Source | Purpose |
|--------|---------|
| Coinbase `wss://ws-feed.exchange.coinbase.com` | Primary BTC price (fastest, leads 0.5-2s) |
| Kraken `wss://ws.kraken.com` | Secondary BTC price (Chainlink oracle source) |
| Binance.US `wss://stream.binance.us:9443` | 1-min candles, ATR, CVD. NOT .com (451) |
| Polymarket CLOB `GET /price?side=BUY\|SELL` | **Execution price** (negRisk cross-matched) |
| Polymarket Gamma `GET /events?slug=...` | Contract discovery, resolution |
| Bybit `wss://stream.bybit.com/v5/public/linear` | OI + perp price (WS only; REST 403 for US) |
| Deribit `GET /get_book_summary_by_currency` | IV (iv_ratio), GEX |
| Chainlink feed | Strike computation + resolution verification |

**Never use:** raw CLOB book for pricing (use `/price`), Gamma `outcomePrices` for edge (stale), Binance for resolution.

## Discord Commands

`!status` — bankroll, today P&L, all-time Sharpe/WR, open positions, current window  
`!history [n]` — last n closed trades (default 10)  
`!pause` / `!resume` — pause/resume entries (position management continues)  
`!clear [trades|control|all]` — purge channel messages  
`!session` — re-send session banner  
`!commands` — list commands  

24h P&L uses `get_day_stats(today_et)` — ET-timezone correct, no row limit.

## Learning Pipeline

Runs daily at 12:05 AM ET. `run_polybot.ps1` commits results to git and restarts.

**Walk-forward split:** 60% train, 40% validation across 4 folds [60:70], [70:80], [80:90], [90:100].

**Adoption gates:** candidate Sharpe > 0, n >= 100 candidate trades, `delta >= max(min_improvement, 0.25 × JK_SE)` (noise-scaled floor — absolute floor fixed at 0.020; the 0.25×SE term dominates at realistic N), improvement in ≥2/4 walk-forward folds (loosened from 3/4 — distribution shifts make older folds a different regime), regime-stratified Sharpe check (dominant regime must improve AND no regime degrades >0.10 Sharpe). **Per-parameter 2-day cooldown** after adoption (a param can't re-adopt within 2 days; other params are free to adopt). The old fixed `z >= 1.0` Jobson-Korkie gate was removed because at realistic N=150-250 with Sharpe~0.2, JK_SE is ~0.08 so z≥1 required Δ≥0.08+ — effectively rejecting every candidate. Fold consistency is now the primary noise guard. SPRT remains a diagnostic only and no longer modulates the adoption floor.

**Pipeline stages:**
1. `PipelineTracker` — fills 1d/3d/7d/14d/30d actual Sharpe for past adoptions; computes decay status (PERSISTED/PARTIAL/DECAYED/REVERSED) and 14d retention ratio; prediction accuracy (directional hit rate, MAE vs Claude's predicted_delta_sharpe_7d); empirical directional table from pipeline_run_log.json; all fed back to Claude
2. `BiasDetector` — trains on 60% set. Edge buckets: 4-8%, 8-12%, 12-20%, 20%+. Per-indicator accuracy, side/time/regime/volatility patterns, edge realization quartiles
3. `PlattCalibrator` — fits A,B on train, validates on holdout, adopts if **Kelly-sized-Sharpe** on validation improves AND z >= 1.0 (≥50 validation trades must pass production gates under both old and new calibrator, else rejected). Log-loss retained in telemetry, not adoption. Gated this way so a flatter/smoother calibrator that would silently shrink edges below the Kelly gate (and kill realized Sharpe) can't be adopted just because it improves log-loss. **Meta-check each cycle:** a third backtest runs with `calibrator=None` (raw model). If `raw_kelly_sharpe >= 0.95 × current_platt_kelly_sharpe`, a WARNING logs and `platt_meta_warning` is surfaced to Claude — calibration isn't earning its keep and may be simplified away.
4. Distribution shift (KS-test recent 50 vs historical)
5. SPRT aggregate (diagnostic only — reports edge-state of recent 50 trades; no longer modulates the adoption floor)
6. `TAEvolver` — sends analysis card + 100 stratified trades (50 recent + 50 spaced across the day) to Claude. Returns structured JSON with a `changes` list (0–5 entries, empty is valid). Each change requires `param`, `value`, `reason`, `predicted_delta_sharpe_7d`, `confidence_interval`. Local fallback when Claude unavailable (max 2 params, 15% change cap). Analysis card includes: calibration curve (reliability diagram), gate skip stats, realized edge vs predicted edge, ghost trade gate analysis (by_gate with sim_pnl), time-to-resolution distribution, cross-window correlation, execution quality with slippage breakdown by spread/time, counterfactual exit analysis with holding_edge accuracy buckets, statistical noise reference, prediction accuracy track record, empirical directional table, adoption decay analysis, **parameter change history** (last 5 adoptions with actual vs predicted Sharpe and decay status).
7. `WeightOptimizer` — **per-parameter** walk-forward backtests (one backtest per proposed change). Each change tested in isolation against baseline on the same 4 folds, then through regime-stratified gate (dominant improves + no regime degrades >0.10). Params in per-param cooldown are skipped before backtest. Changes that pass all gates are adopted independently. If ≥2 changes are adopted, a **combined backtest** runs: if combined Δ < 0.7 × sum(individual Δ), the lowest-z-score change is backed out (interaction detected). `_kelly_bankroll_returns` replays the full logit composition (L1 CDF, L2 regime×direction, L3 flow with 0.35-logit cap, L3b spot-flow, L3c wall, L3e liq, L5 prev-margin, L4 indicator momentum, Platt). Backtest freezes on `self.signal_engine.calibrator`. Entry gates (`min_model_probability`, `min_edge`, `min_kelly`) are held constant in ALL backtests — never varied by candidate — so the trade population stays identical between baseline and candidate runs.

**Key pipeline invariants:**
- `momentum_weight` can be negative (-0.10 to +0.10). Negative = fade indicators (current: -0.02). Claude knows this.
- Claude response format: `{"changes": [{"param": ..., "value": ..., "reason": ..., "predicted_delta_sharpe_7d": ..., "confidence_interval": [lo, hi]}, ...], "key_findings": [...], ...}`. 0–5 changes (empty is valid). `min_model_probability`, `min_edge`, `min_kelly` are READ-ONLY (not in changes list).
- `_validate_strategy_response` uses current_config as defaults — params Claude omits are NOT silently overwritten with hardcoded stale values
- Strategy log reads back last 15,000 chars (up from 6,000) for more context
- Outcomes sorted by `exit_timestamp` (actual trade close time) for correct walk-forward ordering; old outcomes fall back to write-time `timestamp`
- Daily rollup: at 12:05 AM pipeline, previous days' individual outcome files are consolidated into one file per day (`rollup_YYYY-MM-DD.json`) to keep git manageable. `load_all_outcomes()` handles both formats transparently.
- `gain_pct = pnl / size` (arithmetic). Never use `log_return` for Sharpe (log(0) = -inf for losses)
- Recency weighting: `0.995^days_ago` applied to each trade's return in backtest — recent trades count ~2x more than 3-week-old trades
- Ghost trades: downstream-gate rejections tracked to resolution in `memory/ghost_outcomes/`. `analyze_ghosts` returns `{total_ghosts, pct_profitable, by_gate}` — each gate shows `count`, `pct_profitable`, `simulated_pnl` (dollar impact if removed), and `interpretation`. `adverse_rate_30s` is a PROTECTIVE gate — low win_rate means it's correctly filtering informed flow, not over-filtering. NOT used for Platt calibration.
- **Pipeline run log** (`memory/pipeline_run_log.json`): every change tested (adopted + rejected) is logged with direction (`up`/`down`), backtest delta Sharpe, and Claude's `predicted_delta_sharpe_7d`. Powers the empirical directional table and prediction accuracy tracking.
- **Adoption decay tracking**: 1d/3d/7d/14d/30d reviews computed per adoption. `decay_status`: PERSISTED (>80% retention), PARTIAL (50-80%), DECAYED (<50%), REVERSED (negative). If >50% of adoptions DECAYED, pipeline warns Claude to reduce proposal volume.
- **Pairwise interaction check**: if ≥2 changes adopted in one cycle, a combined backtest runs. If combined Δ < 0.7 × sum(individual Δ), the lowest-z-score change is backed out. Coupled groups (config-driven via `pipeline_groups` in settings.yaml; defaults below) expand to within-group pairs:
  - `volatility_core`: [atr_sigma_ratio, student_t_df, logit_scale]
  - `flow_stack`: [flow_weight, spot_flow_weight, liquidation_weight]
  - `momentum_regime`: [momentum_weight, regime_weight]
  - `sizing`: [kelly_fraction, probability_compression]
- **Regime-stratified adoption gate**: after passing main gates, the dominant regime must improve AND no regime may degrade >0.10 Sharpe. (The earlier "≥2/3 regimes improve" alternative path was dropped — it was weaker evidence and correlated with fold consistency, double-counting noise.)
- **Per-parameter cooldown**: a param adopted within the last 2 days is skipped in subsequent pipeline runs (via `pipeline_tracker.params_in_cooldown()`). Replaces the global 2-day pipeline cooldown so independent params can adopt back-to-back without blocking each other.
- **Statistical noise reference**: injected into Claude context based on N — win rate noise ±1/sqrt(N), Sharpe noise ±sqrt(1.125/N). Findings must exceed 2× noise.
- Claude changes now require `predicted_delta_sharpe_7d` and `confidence_interval` per change. After 7 days, actual delta compared to predicted — hit rate and MAE fed back as "Your Prediction Track Record".
- **Flexible change count**: 0-5 changes per cycle (was "exactly 5"). Empty list is valid and appropriate when current config is performing well.
- Realized edge (`signal_prob - fill_price`) and fill slippage stored per outcome — pipeline reports slippage breakdown (by spread bucket and time-in-window) to Claude; actionable for `max_edge`, `logit_scale`, `kelly_fraction` — NOT `min_edge` (read-only)
- All timestamps stored UTC, converted to ET only for date-bucketing (daily report, get_day_stats)

## Common Issues

- **No trades:** BTC near strike = no edge. Correct behavior.
- **Binance 451:** Using .com — must use .us
- **Bybit REST 403:** Geo-blocked for US IPs. WS still works. REST poll loop exits permanently on first 401/403/451.
- **Wrong strike:** Derived from Chainlink oracle or Binance candle boundary from slug, not `int(now // 300) * 300`
- **Startup config error:** `validate_config()` raises ValueError listing all violations
- **Orphaned position:** Waits indefinitely for Gamma resolution. Discord alert after 1hr. By design.
- **Pipeline not adopting:** candidate must clear `delta >= max(abs_floor, 0.25 × JK_SE)` AND improve in 3/4 folds AND pass regime-stratified check. "No change" = current config is defensible or proposed changes were too small relative to backtest noise. Check `per_change` log in pipeline_info — if delta is positive but below floor, Claude needs to propose LARGER moves. If a change is on a manual-only param (e.g. `exit_edge_threshold`), the validator dropped it before backtest.

## Frozen Baseline — DO NOT CHANGE

The bot execution logic and architecture are complete. Do not modify:

- `signal_engine.py` — 8-layer model + evaluate_hold
- `order_flow.py` — book imbalance + trade flow
- Entry/exit/pricing/sizing logic in `main.py`
- `base.py` — BaseTrader ABC, fee math, shared gates
- `paper_trader.py` / `live_trader.py`
- `circuit_breaker.py`, `correlation.py`, `bankroll_strategy.py`

**Pipeline optimizations only going forward.** The learning pipeline (`scheduler.py`, `bias_detector.py`, `outcome_reviewer.py`, `ghost_tracker.py`) may be improved. New features go in new files. Only the 12:05 AM pipeline tunes params.

## What NOT to Change

- Don't use normal CDF — Student-t required (BTC kurtosis 6-8)
- Don't make `momentum_weight` magnitude > 0.10 — indicators are weak signal
- Don't increase `flow_weight` > 0.12 — CDF drives decisions, flow nudges
- Don't use `log_return` for Sharpe — use `gain_pct`
- Don't use raw CLOB book for pricing — use `GET /price?side=BUY|SELL`
- Don't hardcode fee rate — fetch from `GET /fee-rate`
- Don't resolve from Binance price — always wait for Gamma/Chainlink
- Don't bypass circuit breaker — `floor_pct` and `max_single_position_usd` are manual-only
- Don't delete `polybot/db/polybot.db` — bankroll persists across sessions
- Don't derive regime direction from `sign(prob - 0.5)` — use `sign(most_recent_return)`
- Don't apply layer adjustments in probability space — use logit space
- Don't use Binance.com or polymarket.us for crypto

## Always Update

Update this file with every behavioral change.
