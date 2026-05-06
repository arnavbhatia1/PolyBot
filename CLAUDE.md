# CLAUDE.md

## Overview

PolyBot is a 5-minute BTC Up/Down trader for Polymarket. It computes P(Up) via a 7-layer probability model, compares to market price, trades when edge clears the noise floor and Kelly justifies size, then lets the edge math decide every tick whether to hold to $1 resolution or scalp early.

## Probability Model

All layers compose in logit space except L1, then sigmoid + Platt at the end.

- **L1 — Student-t CDF (df=5).** `vol = (max(atr, atr_floor) / atr_sigma_ratio) * sqrt(minutes) * iv_ratio`, `z = (btc - strike)/vol * sqrt(df/(df-2))`, `P(Up) = t.cdf(z, df)`. Fat tails capture BTC kurtosis. ATR floor adapts: when rolling-20 ATR collapses below 60% of the long-term 200-sample mean, the floor widens proportionally so vol estimate stays close to baseline.
- **L2 — Regime.** 1-lag autocorrelation of recent 1-min returns × sign of last return. Both `regime_autocorr` and `regime_direction` are stored per trade so the backtest replays L2 exactly.
- **L3 — CLOB flow.** Book imbalance × 0.6 + trade flow × 0.4 from CLOB WS.
- **L3b — Spot CVD.** Binance aggTrades CVD + taker ratio (taker gated to trade_count ≥ 5; below that, CVD-only).
- **L3e — Liquidation pressure.** Bybit OI drop × price direction.
- **L4 — Indicator momentum.** Weighted RSI/MACD/Stoch/OBV/VWAP. Base `momentum_weight=-0.02` (fade). Regime-conditional at runtime: trending (autocorr > +0.15) flips sign and amplifies 1.5×; reverting (< −0.15) keeps fade and amplifies 1.5×; inside the band dampens 0.5×. Effective weight clamped to ±0.10.
- **L5 — Prev-window margin.** `tanh(prev_resolution_margin / atr) * (prev_margin_weight × logit_scale)`.
- **Sigmoid → Platt.** Final probability is `calibrator.calibrate(sigmoid(logit_p))`. Identity when no calibrator loaded.

L3 + L3b combined contribution capped at ±0.35 logits to prevent triple-counting flow evidence.

### Adaptive compression (two orthogonal learning loops)

Rolling 100-trade buffer `(predicted_prob, market_price, won)` in `signal_engine`. After every resolution, two multipliers refit:

- **Confidence buckets** by `max(p, 1-p)`: moderate [0.58, 0.70), high [0.70, 0.85), extreme [0.85, 1.0]. Drift = `|mean_predicted − realized_WR|` per bucket.
- **Disagreement buckets** by `|model − market|`: agree [0, 0.10), medium [0.10, 0.25), strong [0.25+]. Same drift formula. Trains on cases where the market disagreed and was right.

Drift → multiplier mapping: 3pp → 1.0 (no compression), 25pp → 0.5 (max), linear in between, floored at 0.5. Min sample size per bucket: 15. Runtime applies `min(conf_mult, dis_mult)` so either signal can trigger compression independently. Static `probability_compression` multiplies on top. Persisted to `memory/adaptive_calibration.json` (backward-compatible with old single-multiplier format).

## Entry Gates (all must pass)

`prob ≥ min_model_probability (0.58)`, `edge ≥ min_edge (0.04)` (+0.015 for flips), `Kelly ≥ min_kelly (0.015)`, spread ≤ 10%, depth ≥ $50, `price_sum ∈ [0.98, 1.02]`, `edge ≤ max_edge (0.20)`, `adverse_rate_30s ≤ adverse_selection_threshold (0.85)`, last 30s: `prob ≥ final_min_probability (0.90)`. **Pre-submit edge re-check** uses the fresh ask: rejects if fresh edge falls below `min_edge` OR exceeds `max_edge` (book moved between signal and submit). **CVD deceleration gate:** if `|spot_flow_signal| ≥ 0.20` AND `spot_flow_signal × cvd_accel < 0` (spike already peaked and reversing), skip — these entries resolve at $0 because the flow momentum driving the signal has already mean-reverted.

## Sizing & Exit

**Sizing chain:** `min(bankroll × kelly, max_single_pct, max_single_usd) × breaker_mult × uncertainty_discount × time_mult × concurrent_mult`. Caps apply to the raw Kelly first so soft discounts actually reduce below the cap.

**Concurrent multiplier** is correlation-aware + size-weighted: same-side (ρ≈0.75) at full size → 0.35×; opposite-side (ρ≈-0.25) → 0.90×. Correlation contribution scales by `existing_size / max_single_usd`.

**Exit (`evaluate_hold`):** same model as entry. Every tick: `holding_edge = model_prob − market_price`. `effective_threshold = max(exit_edge_threshold − fee_cost, exit_boundary_threshold)`. Scalp when `holding_edge ≤ effective_threshold`. No pattern-based triggers, no confidence overrides — the math decides. `exit_boundary` is a time/price-aware curve: deep ITM (>70¢) gets a resolution premium near expiry; deep OTM (<30¢) patience decays faster; ATM follows sqrt(time) optionality.

**Flip trading:** after a scalp, re-enter opposite side same window. Max 1 flip. Requires +1.5% extra edge.

**Circuit breaker:** tier-locked floor at $100/$150/$200/$300/... — locks at `tier × floor_pct (0.85)` when bankroll crosses each tier. Never resets down. Kelly scales 1.0→0.40 between tier and floor (concave sqrt). `floor_pct` and `min_multiplier` are manual-only.

## Live Execution & Safety

- FOK-only market orders via py-clob-client. 3 retries with exponential backoff. HTTP/2 keepalive pings every 30s.
- **Live preflight:** `verify_auth` checks `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER`, USDC balance, and USDC allowance to the CTF Exchange. Min allowance = `max_single_usd × max_concurrent × 10`.
- **Atomic DB writes:** `open_position_and_debit_bankroll` runs INSERT + bankroll UPDATE in a single SQLite transaction with rollback on error. A crash mid-write can never leave a position record without the bankroll debit.
- **AuthError fail-loud:** any 401/403/signature/nonce error in the FOK loop raises `AuthError`, the trading loop catches it, sends a Discord alert, and exits. `run_polybot.ps1` won't auto-restart on hard exit.
- **Startup reconciliation (live mode):** for every DB-open position, fetches `get_balance_allowance` for the token_id and alerts if shares deviate >5% from `shares_held`.
- **Feed staleness gate:** before each signal evaluation, skips if Coinbase or Kraken hasn't ticked in 30s, or Chainlink in 60s.
- **Chainlink orphan fallback:** if Gamma is silent for 30+ minutes past expiry, resolve via `chainlink_feed.get_strike(window_ts)` vs `chainlink_feed.price`.
- **Per-mode DB:** `polybot/db/polybot_paper.db` for paper, `polybot/db/polybot_live.db` for live — auto-suffixed by mode. Memory state (`memory/calibration/`, `memory/weights/`, etc.) is intentionally shared so paper learnings transfer to live.

Bybit REST is geo-blocked for US IPs — on first 401/403/451 the poll loop stops permanently; WS is the OI/funding source regardless.

## Project Structure

```
polybot/
  main.py                    # Trading loop, entry/exit/sizing
  config/settings.yaml       # All tunable parameters
  core/
    signal_engine.py         # Probability model + compute_probability + evaluate_hold
    calibrator.py            # Platt scaling
    order_flow.py            # CLOB book imbalance + trade flow
    returns.py               # gain_pct (arithmetic)
    bankroll_strategy.py     # Uncertainty-adjusted Kelly + drawdown velocity
    regime.py                # Multi-state regime classifier
    liquidation.py           # OI-based liquidation pressure (L3e)
    exit_boundary.py         # Binary-option exit threshold
    sprt.py                  # Sequential probability ratio test
    adverse_selection.py     # Rolling post-fill reversal monitor
  feeds/
    coinbase_feed.py kraken_feed.py binance_feed.py        # BTC price (WS)
    binance_depth.py binance_trades.py                      # Depth + aggTrades
    bybit_feed.py deribit_iv.py chainlink_feed.py           # OI / IV / Chainlink
    clob_ws.py market_scanner.py                            # Polymarket
  indicators/                # rsi/macd/stoch/obv/vwap/ema/atr + engine
  execution/
    base.py                  # BaseTrader ABC, fee math, atomic open_trade
    paper_trader.py live_trader.py                          # Mode-specific traders
    circuit_breaker.py       # Tiered floor Kelly scaling
    correlation.py           # Concurrent-position correlation buckets
  agents/
    scheduler.py             # Pipeline orchestrator (walk-forward, cooldown, auto-revert)
    outcome_reviewer.py      # memory/outcomes/ JSON per trade
    counterfactual_tracker.py ghost_tracker.py
    bias_detector.py         # Per-indicator/regime/edge/time/phase/flip analysis
    ta_evolver.py            # Calls Claude or LocalRecommender
    weight_optimizer.py      # JK-SE-scaled adoption gate
    pipeline_tracker.py      # Adoption history, decay, directional table
    pipeline_analytics.py    # Time-weighting, KS shift, SPRT aggregation
    claude_client.py         # Anthropic API + prompt builder + validator
    local_recommender.py     # Rule-based fallback when Claude is down
  memory/                    # Calibration, weights, outcomes, ghosts, pipeline state
  discord_bot/               # !status !history !pause !resume !commands
  db/models.py               # SQLite: positions, trade_history, bankroll, peak_bankroll
```

## Parameter Ownership

### 🟢 Pipeline-tunable (Claude proposes, walk-forward adopts)

These flow through `_kelly_bankroll_returns` so the backtest can replay them.

| Param | Range | What it does |
|---|---|---|
| `atr_sigma_ratio` | 1.2–2.5 | L1 aggressiveness. Lower = sharper probabilities. Highest leverage. |
| `logit_scale` | 2.0–6.0 | Master amplifier on L2–L5 weights. |
| `probability_compression` | 0.5–1.0 | Static shrink toward 0.5 (compounds with adaptive multipliers). |
| `student_t_df` | 3–8 | Tail fatness. |
| `momentum_weight` | -0.10 to +0.10 | L4. Negative = fade. |
| `regime_weight` | 0.02–0.10 | L2. |
| `flow_weight` | 0.02–0.12 | L3. |
| `spot_flow_weight` | 0.01–0.15 | L3b. |
| `liquidation_weight` | 0.01–0.10 | L3e. |
| `prev_margin_weight` | 0.01–0.05 | L5. |
| `min_atr` | 4.0–25.0 | Static ATR floor (runtime: `max(min_atr, 0.3 × rolling_20)`). |
| `kelly_fraction` | 0.05–0.25 | Sizing. Rarely moved. |
| `weights` | sum=1.0, each ≥ 0.05 | L4 indicator mix. |
| `min_model_probability` | 0.52–0.70 | Entry gate. Tunable via ghost backtest. |
| `min_edge` | 0.02–0.10 | Entry gate. Tunable via ghost backtest. |
| `min_kelly` | 0.005–0.04 | Entry gate (primary). Tunable via ghost backtest. |

### 🔴 Manual-only (operator edits settings.yaml)

Either the backtest can't simulate the change (exit/timing/schedule) or it's operator-owned risk policy. Validator in `claude_client.py` reroutes any attempt in `changes` to `manual_observations`.

| Param | Default | Why manual |
|---|---|---|
| `exit_edge_threshold` | -0.05 | Backtest replays fixed gain_pct |
| `adverse_selection_threshold` | 0.85 | Entry-time only |
| `normal_fraction` | 0.60 | Time-of-day Kelly envelope |
| `late_max_penalty` | 0.60 | Late-window Kelly cut |
| `final_min_probability` | 0.90 | Last-30s hard gate |
| `max_edge` | 0.20 | Stale-price cap |
| `flip_enabled` / `flip_edge_premium` | true / 0.015 | Same-window re-entry |
| `trading_start/end_*` | — | Schedule |
| `max_concurrent_positions` | 2 | Risk cap |
| `max_bankroll_deployed` | 0.80 | Total exposure cap |
| `circuit_breaker.floor_pct/min_multiplier` | 0.85 / 0.40 | Risk caps |
| `indicators.{rsi,macd,stochastic,ema,obv,vwap,atr}.*` | settings.yaml | Backtest replays stored norm_score (live-period only); raw candles aren't snapshotted. Claude proposes via `manual_observations` when an indicator's per_indicator accuracy is consistently poor at N≥50. |
| `sprt.{alpha,beta,observation_interval_s}` | 0.05 / 0.10 / 10.0 | SPRT decides intra-window entry timing; backtest replays gain_pct from a fixed entry. Manual-only via `manual_observations` when execution-quality evidence (n≥50) suggests SPRT is firing too eagerly / too cautiously. |

**Rerouting feedback:** the next cycle's prompt explicitly lists rerouted params under "Last Cycle Rerouting Notice" so Claude stops wasting `changes` slots on manual-only proposals.

## Running

```bash
python -m polybot.main --mode paper      # Paper trading
python -m polybot.main --mode live       # Real USDC
python -m polybot.main --run-pipeline    # Pipeline once, no trading
python -m pytest polybot/tests/          # Test suite
```

`run_polybot.ps1` starts trading at 12:15 AM ET, runs pipeline at 11:15 PM ET, commits outcomes/DB/config to git, restarts.

## External Data

| Source | Purpose |
|---|---|
| Coinbase WS | Primary BTC price (fastest, leads 0.5–2s) |
| Kraken WS | Secondary BTC price (Chainlink oracle source) |
| Binance.US WS | 1-min candles, ATR, CVD, depth (NOT .com — 451) |
| Polymarket CLOB `/price?side=BUY|SELL` | Execution price (negRisk cross-matched) |
| Polymarket Gamma `/events?slug=...` | Contract discovery, resolution |
| Bybit WS | OI + perp price (REST 403 for US — WS only) |
| Deribit | IV (`iv_ratio`) |
| Chainlink RTDS | Strike + resolution + orphan fallback |

**Never use:** raw CLOB book for pricing (use `/price`), Gamma `outcomePrices` for edge (stale), Binance for resolution.

## Discord Commands

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all]` `!session` `!commands`

Daily P&L from `get_day_stats(today_et)` — ET-correct, no row limit. Daily banner at 12:01 AM ET (open) + 11:00 PM ET (close).

## Learning Pipeline

Runs daily at 23:15 ET. Walk-forward: 60% train, 40% across 4 folds [60:70], [70:80], [80:90], [90:100].

**Adoption gates:** candidate Sharpe > 0, n ≥ 100, `Δ ≥ max(min_improvement, 0.25 × JK_SE)`, regime-stratified Sharpe (dominant regime improves AND no regime degrades >0.10, only regimes with ≥35 trades get veto power), per-parameter 2-day cooldown.

**Crisis mode:** when recent 50-trade WR < 48% AND baseline Sharpe < 0.10, lowers floor (abs=0.005, SE coeff=0.15) so the pipeline keeps adapting. **Sustained crisis (≥3 cycles)** halves `kelly_fraction` (floor 0.04), restored on first non-crisis cycle.

**Pipeline stages:**

1. `PipelineTracker.review_past_adoptions` — fills 1d/3d/7d/14d/30d Sharpe; computes decay status (PERSISTED/PARTIAL/DECAYED/REVERSED). **Auto-revert:** 1d trailing baseline > 0.10 (n≥20) OR 7d trailing > 0.05 (n≥100) → param reverted to pre-adoption value.
2. `BiasDetector.detect` — per-indicator, side, edge, time, volatility, regime, **entry_phase**, **flip_analysis**, edge realization quartiles, time-weighted, overall.
3. `PlattCalibrator` — recency-weighted MLE on train, Kelly-Sharpe holdout gate (`Δ ≥ 0.001` + improvement). Meta-check: a `calibrator=None` baseline runs each cycle; if `raw_kelly_sharpe ≥ 0.95 × platt_kelly_sharpe`, surfaces `platt_meta_warning` to Claude.
4. KS distribution shift (recent 50 vs historical) — diagnostic.
5. SPRT aggregate — diagnostic only.
6. `TAEvolver.evolve` — sends analysis card + 100 stratified trades (50 recent + 50 spaced) to Claude on full dataset. Returns `{changes: [...], manual_observations: [...]}`. Local fallback: `LocalRecommender` walks the same analysis dict with rule-based heuristics (2× noise floor, IMPROVING-trend skip, decisive moves sized to clear `adoption_dynamic_floor`, family diversity, cumulative-failures avoidance, direction sourced from the empirical directional table).
7. `WeightOptimizer` — per-parameter walk-forward backtests in isolation. If ≥2 changes adopt, a combined backtest runs; if `combined Δ < 0.7 × sum(individual Δ)`, the lowest-z change is backed out (z is structured `change_info["z_score"]`). `_kelly_bankroll_returns` replays the full logit composition using stored `regime_autocorr` + `regime_direction`. Calibrator is the just-adopted Platt for the cycle. Sample = real outcomes + resolved ghosts (entry gates apples-to-apples).

**Key invariants:**

- **Direction sourcing:** read exclusively from the empirical directional table (`pipeline_run_log.json`). No hardcoded "test HIGHER X" priors. Directions marked `DECAYS` or with consistently negative BT delta are blocked.
- **Noise reference accuracy:** Sharpe noise computed from actual `baseline_kelly_sharpe` and `baseline_n_trades`, matching the JK SE the gate uses.
- **Adaptive calibration buckets** (confidence + disagreement) surfaced in the prompt so Claude / LocalRecommender can see drift per region without proposing static `probability_compression` changes that would damp calibrated buckets.
- **Counterfactual scalp data** flows into both `manual_observations` (`exit_edge_threshold` suggestion) AND backtestable proposals (`atr_sigma_ratio` / `probability_compression` via `_rule_scalp_overconfidence` mapping).
- **Entry-phase + flip analysis** are diagnostic-only; they map to manual-only timing/flip levers, not `changes`.
- **Recency weighting:** `0.97^days_ago` on each trade's return in backtest and Platt MLE — half-life ~23 days.
- **Pipeline run log** records every change tested (adopted + rejected) with direction, backtest Δ, and Claude's `predicted_delta_sharpe_7d`. Powers the directional table and prediction-accuracy track record.
- `gain_pct = pnl / size` (arithmetic). Never use `log_return` for Sharpe.
- All timestamps stored UTC, converted to ET only for date-bucketing.
- Daily rollup at 12:05 AM consolidates per-trade JSON files into `rollup_YYYY-MM-DD.json`.

## Common Issues

- **No trades:** BTC near strike = no edge. Correct.
- **Binance 451:** Using `.com` — must be `.us`.
- **Bybit REST 403:** US IP geo-block; WS still works.
- **Wrong strike:** Derive from Chainlink oracle / Binance candle boundary at slug `window_ts`, not `int(now // 300) * 300`.
- **Orphaned position:** Waits for Gamma resolution; Discord alert at 1hr; Chainlink fallback at 30min.
- **Pipeline not adopting:** candidate must clear `Δ ≥ max(0.010, 0.25 × JK_SE)` AND regime gate. "No change" usually means proposed moves were too small relative to backtest noise — Claude needs decisive moves.
- **Live auth failure:** AuthError raises and the bot exits cleanly; check `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, and USDC allowance to the CTF Exchange.

## Frozen Baseline — DO NOT CHANGE

The execution and architecture are complete. Don't modify:

- `signal_engine.py` (probability model + evaluate_hold)
- Entry/exit/pricing/sizing logic in `main.py`
- `execution/base.py` (BaseTrader ABC, fee math, atomic open_trade)
- `paper_trader.py` / `live_trader.py`
- `circuit_breaker.py`, `correlation.py`, `bankroll_strategy.py`

**Pipeline optimizations only.** New features go in new files. Only the nightly pipeline tunes params.

## What NOT to Change

- Don't use normal CDF — Student-t required (BTC kurtosis 6–8).
- Don't make `momentum_weight` magnitude > 0.10 — indicators are weak signal.
- Don't use `log_return` for Sharpe — `log(0) = -inf` for losses.
- Don't use raw CLOB book for pricing — use `GET /price?side=BUY|SELL`.
- Don't hardcode the fee rate — fetch from `GET /fee-rate`.
- Don't resolve from Binance price — always wait for Gamma/Chainlink.
- Don't bypass the circuit breaker — `floor_pct` and risk caps are manual-only.
- Don't delete `polybot/db/polybot_*.db` — bankroll persists across sessions.
- Don't derive regime direction from `sign(prob - 0.5)` — use sign of last 1-min return (stored as `regime_direction`).
- Don't apply layer adjustments in probability space — use logit space.
- Don't use `.com` Binance or `polymarket.us`.

## Always Update

Update this file with every behavioral change.
