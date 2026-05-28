# PolyBot

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

**This file is the single source of truth.** Update it with every behavioral change.

**Sections 1–9 below are PERMANENTLY FROZEN — final, locked, and closed to all further changes.** They describe the trading logic itself — model, gates, sizing, ordering, exits, flips, resolution, loss handling. The code and this spec have been reconciled and verified, and §1–9 is now immutable: no structural change and no hand-edit to these sections or to the code implementing them — ever. The ONLY thing that still moves is the set of numeric knobs declared in §12, which the nightly pipeline (§11) tunes *inside* these fixed structures; the pipeline can never restructure §1–9. This freeze is final.

## Quick Start

```bash
pip install -r requirements.txt

cp polybot/config/.env.example polybot/config/.env
# Required: ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN
# Live mode also needs: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

python -m polybot.main --mode paper       # paper trading
python -m polybot.main --mode live        # real USDC (needs allowance)
python -m polybot.main --run-pipeline     # one nightly cycle, no trading
python -m pytest polybot/tests/           # full suite
.\run_polybot.ps1                         # daily cycle: trade → pipeline → commit → restart
```

### Secrets

| Key | When |
|---|---|
| `ANTHROPIC_API_KEY` | Always (daily learning) |
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_PRIVATE_KEY` | Live mode (EIP-712 signing) |
| `POLYMARKET_FUNDER` | Live mode (USDC funding address) |

Binance.com, Polymarket, Coinbase: free.

---

# Part A — Frozen Trading Logic (§1–9)

The sections in Part A describe the trading mechanism. They are intentionally locked: the structural decisions (Student-t over Gaussian, polarity-split L4, regime-damped L5, closed L6 library, the redundancy-discounted L3+L3b+L3e flow combine, isotonic-only calibration, the entry-gate set, the sizing pipeline, the exit-branch order, the flip-premium formula, Chainlink-only resolution, the loss-handling stack) have been deliberately chosen and will not be optimized further. The nightly pipeline tunes the **numeric values** within these structures (§12) — it cannot change the structure itself.

## 1. What you're betting on

Every 5 minutes, Polymarket runs a market: "Will BTC close higher or lower at the end of this 5-minute window than at the start of it?" Two sides — **Up** and **Down** — each trading as its own ERC-1155 token between $0 and $1. The winning side pays $1/share; the loser pays $0. Chainlink's BTC/USD oracle is the official resolution source; Polymarket's Gamma API mirrors it for the slug feed.

The bot picks which side to bet on, how much, when to scale into it, and when (if ever) to sell early.

It runs in two modes from the same engine:

- **`paper`** — full realism shim: real CLOB books, FOK semantics, convex slippage, configurable network-fail / latency jitter, $1 minimum, tick-size snapping. Bankroll lives in a paper SQLite DB.
- **`live`** — `py-clob-client-v2` FOK orders against the real CLOB. Same engine, same gates, same telemetry; wraps the trader in `LiveTrader` and verifies USDC balance + allowance before the first order.

The schedule is wired through `run_polybot.ps1`: starts at 12:01 AM ET, stops accepting entries at 11:30 PM ET, kicks off the learning pipeline at 11:45 PM ET, commits, and restarts.

## 2. How the bot forms an opinion

For each window, the bot computes **P(Up)** by stacking evidence in logit space, then squashes through sigmoid + isotonic calibration. Every layer except L1 contributes via `weight × logit_scale × signal` (with `logit_scale` the global amplifier, default 4.0). The final logit is clamped to ±`final_logit_clamp` (default 4.0) → prob ∈ [0.018, 0.982].

### L1 — Student-t CDF (core)

"How far is BTC from the strike vs. how much it typically moves in the time remaining?"

```
ac         = clamp(regime, ±0.5)                 # regime = lag-1 autocorr (shared with L2)
vol_scaled = (max(atr, atr_floor) / atr_sigma_ratio) × sqrt(minutes_remaining) × sqrt((1 + ac) / (1 − ac))
z          = (btc_price − strike) / vol_scaled
t_scale    = sqrt(df / (df − 2))
prob_up    = StudentT_CDF(df, z × t_scale)
```

- **`student_t_df`** — default 5, clamped ≥3 (df ≤ 2 has undefined variance and the t_scale fallback discontinuity would inject a 1.0 → 1.73 jump). Gaussian undersizes BTC's fat tails (kurtosis 6–8).
- **`atr_sigma_ratio`** — default 1.3, pipeline-tunable 1.2–2.5. The single highest-leverage knob in the model.
- **Autocorrelation-scaled vol** — BTC 1-min returns aren't i.i.d., so plain `sqrt(minutes_remaining)` misstates the terminal spread. `vol_scaled` is multiplied by the AR(1) terminal-SD ratio `sqrt((1 + ac) / (1 − ac))`, where `ac` is the same lag-1 autocorrelation L2 consumes (clamped ±0.5 so a noisy estimate can't dominate the core probability). Positive autocorr (trend) widens the spread → P(Up) pulled toward 0.5; negative (mean-reversion) tightens it → pushed away. `regime` is computed once and shared by L1, L2, L4, and L5.
- **ATR floor**, dynamic: `max(min_atr, 0.30 × rolling_20)`. When `rolling_20 / long_term_200 < atr_regime_shift_threshold` (default 0.60), widens to `max(base_floor, long_term_mean × threshold × 0.30)` so the model doesn't get overconfident when vol collapses. `rolling_20` / `long_term_200` are rolling buffers of the last 20 / 200 ATR **samples**, where one sample is appended per `compute_probability` call (i.e. per decision tick — entry *and* hold evals) — **not** per 1-min candle. Their effective horizon therefore tracks decision cadence, not wall-clock minutes.
- **L1 clip** at `1e-6` → logit ±13.8, well past the final ±4 clamp. The clamp is the precision floor, not the clip.
- `btc_price` comes from `_fastest_btc_price`: **Coinbase WS only (<2s)** — the lowest-latency feed and the venue Chainlink resolves against. There is no Binance fallback: sourcing a price from a venue that can diverge across the strike on a transient print would flip P(side) on a tick the resolver never sees. Coinbase stale (≥2s) → the decision is skipped, not zeroed (Binance spot is still read, but only to log the cross-venue gap — see §9).

### L2 — Regime

```
last_return = (live_btc_price − closes[-2]) / closes[-2]
regime      = lag1_autocorr(closes, regime_lookback)
direction   = sign(last_return)
logit += regime × direction × regime_weight × logit_scale
```

Single `lag1_autocorr` helper in `polybot/core/returns.py` — `SignalEngine.compute_regime_factor` and `RegimeDetector` both delegate to it (so they can never disagree on the same closes). `last_return` deliberately mixes the live Coinbase tick price against the most recent fully-closed Binance candle to eliminate the minute-boundary mismatch that would otherwise exist between what L1 sees and what L2's numerator computes.

### L3 — CLOB flow

```
book_imbalance = (top-5 bid_up + top-5 ask_down − top-5 bid_down − top-5 ask_up) / total
trade_flow     = recency_weighted_net_flow (120s window, 30s half-life decay)
flow_score     = 0.6 × book_imbalance + 0.4 × trade_flow
logit += flow_score × flow_weight × logit_scale     # combined with L3b + L3e (redundancy-discounted)
```

Top-5 levels each side by best price. Trade flow is recency-weighted exponential decay with a 30s half-life inside the 120s window.

### L3b — Spot CVD (Coinbase)

Coinbase is the largest US-volume BTC venue and is the venue Chainlink resolves against. Per-trade CVD + taker ratio from the WS L2 + match feed:

```
cvd_60s    = signed Coinbase BTC volume over 60s
vol_factor = clamp(atr / atr_long_term_mean, 0.5, 3.0)   # current volatility regime
cvd_comp   = tanh(cvd_60s / (30 × vol_factor)) × 0.8     # saturation scale tracks regime
taker_comp = (taker_60s − 0.5) × 0.4            # only when ≥20 trades in window
spot_flow  = clamp(cvd_comp + taker_comp, ±1)
logit += spot_flow × spot_flow_weight × logit_scale     # combined with L3 + L3e (redundancy-discounted)
```

`compute_spot_flow_signal` lives in `polybot/core/aux_layers.py` so live and replay can never drift. A fixed scale would saturate `tanh` in high-volume regimes (losing resolution exactly when flow is most informative); `vol_factor` scales the saturation point to the current volatility regime using vol the model already measures.

**CVD-acceleration gate** (sizing-time guard, not L3b magnitude): `coinbase_feed.get_cvd_acceleration(recent_s=15, baseline_s=45)` requires ≥10 recent trades. Skips the entry when `|spot_flow| ≥ 0.20` **and** `spot_flow × cvd_accel < 0` — buying signal has already peaked.

**Flow-family combine (L3 + L3b + L3e).** Book flow, spot CVD, and liquidations are three venues watching the *same* BTC move, so summing them additively double-counts the shared signal whenever they agree — the exact high-conviction case that drives the largest sizing. Instead they're combined per direction: on each side the strongest contribution enters at full weight and same-direction corroborators are discounted by `_FLOW_REDUNDANCY` (0.5); opposing signals offset naturally (no discount — disagreement is information). The combined result — all three legs including liquidations — is clamped to **±0.50 logits**, so no flow leg or correlated cluster can dominate L1.

### L3e — Direct futures liquidations (Binance)

Per-event `btcusdt@forceOrder` from Binance futures. Each liquidation message is one order with side, qty, price.

```
long_usd  = sum of price × qty for order.side == SELL (closing longs → price-down event)
short_usd = sum of price × qty for order.side == BUY  (closing shorts → price-up event)
vol_factor = clamp(atr / atr_long_term_mean, 0.5, 3.0)
liq        = tanh((short_usd − long_usd) / (50_000 × (btc_price / 65_000) × vol_factor))   # scale tracks price + vol
liq × liquidation_weight × logit_scale     # enters the L3+L3b+L3e flow combine (see L3b), not added separately
```

Sign convention: **short liquidation → price-up (+)**, **long liquidation → price-down (−)**. Helper `compute_liquidation_signal` in `aux_layers.py` is shared with replay. A fixed USD threshold would silently recalibrate as BTC's price level drifts; the cascade scale tracks `btc_price` (relative to a $65k reference) and the current volatility regime.

### L4 — Indicator committee (polarity-split, regime-conditional)

Five indicators (RSI, MACD, Stochastic, OBV, VWAP) computed from the 1-min candle buffer, raw `score` field consumed directly (no adaptive normalizer). Split into two groups:

- **Mean-revert group:** RSI, Stochastic, VWAP
- **Trend-confirm group:** MACD, OBV

Both groups dot-product with their L4 weights (`weights` dict), then mix smoothly by regime via `t = tanh(regime / regime_momentum_threshold)`:

```
contrarian_mult   = (1 − t) × 0.5
trend_confirm_mult = 0.5 + 0.5 × max(0, t)
score = mean_revert × contrarian_mult
      + |mean_revert| × direction × max(0, t)        # in trend regime, polarity FLIPS to sign(last_return)
      + trend_confirm × trend_confirm_mult
score = clamp(score, ±1)
logit += score × effective_momentum_weight × logit_scale
```

`effective_momentum_weight` = unsigned magnitude scaled `0.5×` → `1.5×` by `|tanh(regime / regime_momentum_threshold)|` — no cliff at the threshold. The pipeline's `momentum_weight` bound (0–0.10) caps the magnitude; **sign is dead at the L4 level** — it's reborn per-group inside `compute_momentum` from the regime + realized direction.

In **revert regime** (negative `t`): mean-revert keeps its contrarian sign at full power, trend-confirm is dampened.
In **trend regime** (positive `t`): the mean-revert group's sign is **replaced** by `sign(last_1min_return)` so polarity tracks the trend direction (continuation expectation, not a direction-agnostic flip); trend-confirm runs at full power.

`regime_momentum_threshold` default 0.15, pipeline-tunable 0.08–0.25.

### L5 — Previous-window margin carry

```
logit += tanh(prev_resolution_margin / max(atr, 1)) × prev_margin_weight × logit_scale
       × (1 − min(l5_regime_damp_cap, |regime|))
```

The dampener is the orthogonality patch: when `|regime|` is high, L2 already encodes the same drift, so L5 contributes only its orthogonal-info portion. `l5_regime_damp_cap` default 0.7, pipeline-tunable 0.4–0.9.

`prev_resolution_margin` is persisted with a `saved_at` timestamp in `memory/prev_resolution_margin.json`; if older than 30 min on load the carry is zeroed (the previous window is no longer adjacent).

### L6 — Derived feature library (closed)

A frozen library of **4 bounded transforms** of state already tracked by `compute_probability` (see `polybot/core/derived_features.py`). Every weight defaults to **0.0** — the layer is dead until the pipeline raises one off zero with evidence.

| Feature | Formula | Notes |
|---|---|---|
| `log_atr_ratio` | `clip(log(ATR_short / ATR_long), ±1.5)` | Vol regime expansion (+) or collapse (−) |
| `autocorr_signed_mag` | `regime × tanh(last_return × 100)` | Direction-aware momentum strength |
| `flow_disagreement` | `tanh(flow + spot_flow)` | Direction-aware flow consensus |
| `liq_signed_sqrt` | `sign(liq) × min(√\|liq\|, 1)` | Softer saturation than L3e's tanh |

```
l6_total = Σ derived_weights[name] × logit_scale × feature(ctx)
l6_total = clamp(l6_total, ±L6_LOGIT_CAP)        # ±0.25 logits
logit += l6_total
```

The cap is enforced at the call site and **`claude_client` validator drops any L6 weight-change set whose `Σ|w| × logit_scale` would push past `L6_LOGIT_CAP`**, so the budget is unbreakable from either side. The library is closed: adding a feature requires a code change in `derived_features.py` plus a `ParamSpec` row.

### Calibration (isotonic) — sole overconfidence correction

`IsotonicCalibrator` in `polybot/core/calibrator.py`. Identity by default. Fit on the last 7 days of trades (`needs ≥150 samples` default, pool comes from the calibration train split; ≥75 in train, otherwise skipped entirely).

Adoption is a **single OOB bootstrap-CI gate**: the lower-80% bound of weighted log-loss improvement vs identity, computed across **300 OOB resamples** with per-bootstrap weight renormalization, must be strictly positive. RNG seeded from `time.time_ns()` each fit so the CI tracks real sampling variance instead of locking onto a fixed seed.

Pre-CI **range check**: the fit's `y_min ≤ 0.50` and `y_max ≥ 0.55` (otherwise the curve doesn't span the decision region and is rejected without bootstrapping).

`last_fit_diagnostics` (`oob_ci_lower_nats`, `oob_ci_median_nats`, `n_samples`, `bootstrap_n_completed`, `y_min`, `y_max`, `decision`) is populated on every `fit()` call that reaches the bootstrap CI stage — both the accept path (`decision="adopted"`) and the CI-reject path (`decision="rejected_ci"`) stamp the full dict to `pipeline_info["cal_info"]["fit_diagnostics"]`. Structural early-rejects (sample count, zero-weight, sklearn fit exception, pre-CI range check) return `False` without stamping; those failure modes are observable in the log line emitted at the reject site.

`lowest_learned_prob` (the lowest `y_thresholds_[0]` the calibrator can output) is consumed by `evaluate_hold` as the "dead side" floor — see §6.

## 3. Entry gates

Edge = `calibrated_model_prob − market_price`. **All** of the following must pass; any single failure → skip the tick.

| Gate | Threshold | Source |
|---|---|---|
| Chosen-side `prob` | ≥ `min_model_probability` (default 0.56) | `SignalEngine.evaluate` |
| `edge` | ≥ `min_edge` (default 0.04, scaled by flip premium — see §7) | `SignalEngine.evaluate` |
| `Kelly` (fee-aware) | ≥ `min_kelly` (default 0.01); `b_eff = b × (1 − fee_rate)` | `SignalEngine._kelly` |
| Spread on either side | `spread/2 + DEFAULT_FEE_RATE ≤ max_spread` (default 0.10) | `_fetch_market_prices` |
| Book depth | both-sides-thin gate first (≥ `min_book_depth_usd = $50` on at least one side); chosen-side depth must also clear the same floor | `_evaluate_signal_and_enter` |
| Price sum | `price_up + price_down ∈ [0.98, 1.02]` (cross-book no-arb sanity) | `_fetch_market_prices` |
| Book freshness | both sides' WS BBO must be ≤ `_WS_STALE_S = 10s` old | `clob_ws.both_books_fresh` |
| `edge ≤ max_edge` | default 0.20 — wider edge = stale phantom price | `_evaluate_signal_and_enter` |
| ATR gate | ATR ≥ 5th-percentile (lower-bound only; no upper guard) | `IndicatorEngine.atr` |
| SPRT | not `SKIP`; not opposing the chosen side when conf > 60% with ≥6 obs | `SPRTAccumulator` |
| Adverse-selection hard skip | `adverse_rate_at_30s ≥ adverse_selection_threshold` (default 0.80) → reject | `AdverseSelectionMonitor` |
| Edge-decay | mean 15s post-fill drift (30-min lookback) ≥ `edge_decay_threshold` (default −0.05). Inactive until ≥15 resolved fills exist in the lookback | `AdverseSelectionMonitor.get_recent_decay_mean` |
| Layer disagreement | reject when `compute_momentum` opposes the chosen side (>0.5 magnitude) and `edge × 0.5 < min_edge` | inline |
| CVD deceleration | skip if `\|spot_flow\| ≥ 0.20` AND `spot_flow × cvd_accel < 0` | inline |
| Regime | skip when `RegimeDetector` classifies `quiet` | `RegimeDetector.classify` |
| Net-edge after slippage | `edge − price × est_slip ≥ min_edge` | `slippage_pct` |
| Pre-submit re-check | walk current ask ladder for FOK VWAP — recomputed net edge must clear `[min_edge, max_edge]`. Fresh-BBA fallback (when the book is unavailable) checks net edge against `min_edge` and **gross** edge against `max_edge` — `max_edge` is a stale-phantom-price guard, slippage is execution cost, so the BBA-only path is most informative against pre-slippage edge | `compute_buy_vwap` |
| Min order size | size ≥ $1 (Polymarket CLOB floor; paper mirrors live) | inline |
| Feed staleness | Coinbase ≤ 30s, Chainlink ≤ 60s, Binance aggTrade ≤ 30s, Binance kline ≤ 45s | inline |

**Adverse selection is sizing-side, not entry-side.** Above the soft penalty floor (`adverse_penalty_floor` default 0.45):

```
kelly_mult = max(adverse_penalty_min,
                 1 − adverse_penalty_slope × max(0, adverse_rate_at_30s − adverse_penalty_floor))
           = max(0.30, 1 − 1.5 × max(0, adverse_rate − 0.45))
```

So at `adverse_rate = 0.85` the multiplier collapses to 0.30, but the trade can still fire — only the hard 0.80 threshold blocks entry outright. The 30-min lookback is **Bayesian-shrunk to a neutral prior** (n=10, rate=0.5) so the gate stays calibrated in low-volume hours and the rate doesn't snap to 1.0 after a single bad fill.

Every rejection of a **pipeline-tunable or signal-derived gate** feeds a **ghost** rejection into `GhostTracker` with the full L1-L5 inputs + aux microstructure — these resolve at the same window's close and feed the nightly pipeline's backtest pool. Raising a gate filters the same ghosts out of baseline + candidate equally; lowering one includes them. Non-tunable structural gates (`regime` quiet skip, chosen-side `thin_book_depth` against the operator-owned `min_book_depth_usd`, `min_size` $1 CLOB floor) reject without ghosting — the pipeline can't adopt a change that would re-include them, so the ghost would be dead weight in the backtest pool.

## 4. Sizing

Hard caps first, soft multipliers second. Then a final $1 floor and a real round-trip net-edge sanity check.

```
raw_kelly_size = bankroll × signal.kelly_size
size           = raw_kelly_size × circuit_breaker.kelly_multiplier × time_mult
size          ×= consensus_mult × adverse_kelly_mult            # both ≤ 1.3 / ≤ 1.0
size          ×= concurrent_multiplier(side, market, opens)     # correlation-aware
size           = min(size, bankroll × max_bankroll_deployed)
size           = min(size, side_depth × max_book_fill_pct)
if size < 1.0: skip                                              # CLOB floor
```

### Soft multipliers

- **Circuit breaker** — tier-locked floor at $100/$150/$200/$300/$400/$600/$800/$1000/$1500/$2000/$3000/$4000/$6000/$8000/$10000. Floor = locked_tier × `floor_pct` (default 0.85). Kelly multiplier:
  - `1.0×` at or above the locked tier
  - `min_multiplier` (default 0.40) at or below the floor
  - **Concave (sqrt) interpolation** between — shallow drawdowns penalize lightly (a $100/$85 tier midpoint gives ~0.82× vs 0.70× linear), deep drawdowns penalize aggressively.
  - Tier never resets down; it ratchets up when bankroll crosses a new tier.

- **Time multiplier** — `compute_time_multiplier`. In the first `normal_fraction` of the window (default 60% → 0–180s), full Kelly. After that, penalty scales by `(1 − conviction)` up to `late_max_penalty` (default 0.30). High-conviction late entries (`prob` far from 0.5) are barely penalized; ATM late entries take the full hit.

- **Consensus multiplier** — `compute_signal_consensus` counts how many of `flow`, `spot_flow`, `cvd_accel_norm` agree with the chosen side (after dropping signals below `consensus_dead_zone = 0.05`):
  | Agree % | Mult |
  |---|---|
  | ≥ 80% | 1.30× |
  | ≥ 60% | 1.00× |
  | ≥ 40% | 0.80× |
  | else | 0.60× |

- **Concurrent multiplier (correlation-aware)** — `polybot/execution/correlation.py`. Polymarket's adjacent 5-min BTC windows share regime + microstructure; same-side concurrent bets are highly correlated (ρ ≈ 0.7–0.9) and opposite-side bets are naturally hedged.
  - ρ is a **fixed prior**: `+0.75` same-side, `−0.25` opposite-side (`_CORR_SAME_SIDE`, `_CORR_OPPOSITE_SIDE`). Same-market triggers flip logic, not this multiplier.
  - Worst ρ across open positions buckets:
    | Worst ρ | Mult |
    |---|---|
    | > 0.6 | 0.35 |
    | > 0.3 | 0.55 |
    | > −0.2 | 0.70 |
    | ≤ −0.2 | 0.90 |
  - Promoting to a windowed empirical estimator with sample-size shrinkage is a future-work item; not current behavior.

### Hard caps

- `bankroll × max_bankroll_deployed` (default 0.80)
- `side_depth × max_book_fill_pct` (default 0.50) — under the thin-CLOB upstream gate that requires at least one side ≥ $50 depth; if the chosen side is the empty leg of a one-sided book, that's an explicit skip.

## 5. Placing the order

FOK ("Fill-or-Kill") via `py-clob-client-v2`. 3 retries with jittered exponential backoff. HTTP/2 keepalive ping every 5s (against a 60s `keepalive_expiry` pool, so the connection never lapses between pings).

Live mode boot:
1. `verify_auth` checks `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` are set and the Safe is reachable.
2. USDC balance fetched. Allowance fetched as `min(allowances[spender] for all spenders) / 1e6`.
3. Required allowance: `max_single × max_concurrent_positions × 10` (10× safety multiplier), where `max_single = bankroll × kelly_fraction` (a max Kelly-sized single bet — **not** the `bankroll × max_bankroll_deployed` hard cap). If allowance < required → `AuthError`, clean exit, **no retry that day** (`run_polybot.ps1`'s outer `while ($true)` loop will restart the bot the next midnight ET; fix the allowance before then).
4. **Mid-session allowance recheck** — every `_ALLOWANCE_RECHECK_EVERY = 10` submits, re-fetch allowance and warn (or fail) if it's been revoked or run down.

Paper mode boot: skips auth entirely, uses the same `BaseTrader` open/close path but `PaperTrader` simulates the book-walk fill + latency + occasional FOK rejection.

Per-trade DB write is atomic in a single SQLite transaction: `open_position_and_debit_bankroll` and `close_position(... bankroll_delta=... | new_bankroll=...)`. Pass `bankroll_delta` for a relative credit (scalp), `new_bankroll` to set absolute on resolution.

**`fill.fill_size` is always USDC notional** — for BUY it's the requested $ size; for SELL it's `shares × fill_price`. Logs always show actual fill price (book-walk for paper, FOK fill for live), never the signal-moment price.

## 6. Holding vs scalping (`evaluate_hold`)

Every tick while we have a position, re-run the full model and decide HOLD vs EXIT. `holding_edge = model_prob − market_price_for_side`.

The exit threshold blends two curves:

```
itm_ref            = market_mid_for_side or market_price_for_side
itm_depth          = max(0, (itm_ref − 0.5) / 0.5)
deep_loss_floor    = exit_edge_threshold × (1 + 0.5 × itm_depth)
optimal_threshold  = ExitBoundary.compute_exit_threshold(seconds_remaining, entry_price,
                                                         fee_rate, market_price_for_side)
effective_threshold = (1 − itm_depth) × max(deep_loss_floor, optimal_threshold)
                    + itm_depth × min(deep_loss_floor, optimal_threshold)
```

ATM trusts the `ExitBoundary` curve; deep ITM weights toward the more patient floor.

**`ExitBoundary.compute_exit_threshold`** in `polybot/core/exit_boundary.py`. Binary-payoff math (unlike European options, the payoff kinks at $0/$1):
- **Deep ITM (`p ≥ 0.70`):** base time value × `(1 − itm_depth × 0.5)` + resolution premium (`itm_depth × 0.05 × (1 − minutes/5)`) — wants to hold for the $1 resolution.
- **Deep OTM (`p ≤ 0.30`):** base time value × `(1 − otm_depth × 0.7)` + urgency premium that ramps in the last 2 minutes — exhausted time value, cut losses.
- **ATM (in between):** `0.07 × √minutes × 0.4 + fee_cost`.

The OTM urgency can push the threshold **positive**, forcing exit even when the model is optimistic — final clamp is `[−0.30, urgency_premium > 0 ? 0.30 : −0.01]`.

### Exit branches (in order)

1. **Loss-cut** — `entry_price > 0 AND market_price < entry × loss_cut_fraction (0.65) AND seconds_remaining < loss_cut_time_s (90s) AND BTC on the wrong side of strike by ≥ 0.5×ATR`.
   - The 0.5×ATR cushion is the whipsaw guard: when BTC is sitting on the strike and the contract flickers between $0.05 and $0.70 on thin prints, we don't lock in the bottom of the flicker.
   - Engine stamps `last_loss_cut_event` ∈ {`""`, `"fired"`, `"whipsaw_blocked"`} per call; the trading loop counts those into `gate_stats` to audit the cushion's selectivity.

2. **Deep-loss hold** — `holding_edge < deep_loss_hold_threshold (−0.10) AND market < entry AND model_prob > calibrator.lowest_learned_prob`.
   - The binary residual ($1 if we win) beats locking in the loss at a depressed price.
   - **Override:** when `model_prob ≤ calibrator.lowest_learned_prob`, the calibrator is saying "the training data showed this side won essentially never at this raw prob" — selling at market beats holding for ~$0 expected. Identity calibrator returns `0.0` for the floor, disabling the override; refits update it dynamically.

3. **Scalp** — `holding_edge ≤ effective_threshold` (and not in the deep-loss hold zone), **unless BTC is within 0.5×ATR of the strike on the wrong side** — the same whipsaw cushion as loss-cut (branch 1), so a borderline strike-side flip can't scalp out a position whose binary residual is still ~coin-flip.

4. **Hold** — otherwise.

No confidence override. Math says exit, it exits.

`exit_edge_threshold` is the only operator-touchable exit knob and the **only exit knob the pipeline can tune** (range −0.10..−0.03). When the pipeline proposes a change to it, the backtest replays the **counterfactual tracker**'s recorded scalp outcomes through the new threshold: trades whose recorded `holding_edge_at_scalp` is above the candidate threshold are re-priced using the matched hold-to-resolution PnL.

## 7. Flip trading

After a scalp, the bot can re-enter the same window — including the opposite side — **unboundedly** (one position at a time). Each re-entry must clear the standard entry gates plus a flip premium:

```
flip_premium = flip_edge_premium + 0.005 × max(0, flip_count − 2)
spread_cost  = spread + 2 × fee_rate × p × (1 − p)            # real round-trip
flip_hurdle  = min_edge + max(flip_premium, spread_cost)
```

Flips 1–2 pay only the base `flip_edge_premium` (default 0.015); flip 3 pays +0.5pp; flip 4 pays +1.0pp; etc. Or the actual round-trip spread+fee cost, whichever is higher. This guarantees flips can't churn on micro-edge that won't survive the round trip.

## 8. Resolution

- **Early scalp** — sold before expiry into the book. The bot keeps the difference; counterfactual tracker logs what hold-to-resolution would have paid.
- **Resolution** — window closes; Chainlink decides; winning side pays $1/share, losing side $0. PnL credited atomically to bankroll.
- **Never resolves from Binance** — can diverge from Chainlink by $20–$200 at the close (the period right before Chainlink's snapshot is the worst time to trust spot).
- **Chainlink orphan fallback** — if Gamma is silent 30+ minutes past expiry, the bot reads Chainlink directly via `chainlink_feed` and resolves the position locally. Restart safety comes from the position staying `open`/`pending_resolution` in the DB until resolved (re-evaluated on the next boot) — not from a file. `memory/orphan_positions.json` is written by a *separate* startup check (`LiveTrader.detect_orphan_positions`) that flags on-chain positions the DB doesn't know about.

## 9. Built-in loss handling

- **Circuit breaker** — tiered floor at $100/$150/$200/$300/.../$10000, locked at 85% of crossed tier. Kelly scales 1.0 → 0.40 concavely between tier and floor; never resets down. Streak counters (3 losses / 2 wins) exist for Discord alerts only — they do not drive sizing.
- **Adverse-selection monitor** — Bayesian-shrunk fade-rate gate; sizing penalty + emergency hard-skip. State persisted to `memory/adverse_state.json` on every fill so restarts inherit the rolling window.
- **Edge-decay monitor** — same `AdverseSelectionMonitor` but watches signed 15s post-fill drift; gate activates after ≥15 resolved fills in the 30-min lookback.
- **Regime skip** — `quiet` (ATR below `vol_low_percentile`) skips entry entirely; `volatile` is allowed but tracked.
- **Feed staleness skip** — Coinbase >30s, Chainlink >60s, Binance aggTrade >30s, Binance kline >45s → skip the cycle. The L1 BTC price is Coinbase-only: a Coinbase gap ≥2s → skip the decision (not zero), with no Binance price fallback (skip = hold an open position, no entry when flat).
- **CLOB WS heartbeat** — PING every 10s, force-reconnect if no PONG within 25s.
- **Cross-venue gap** — Coinbase vs Binance BTC spot delta logged on every decision for monitoring; the L1 price is Coinbase-only, so a Binance divergence can't move P(side).

---

# Part B — Mutable Operational Layer (§10–19)

Everything below is the surrounding scaffolding — telemetry, the nightly pipeline, the param registry, project layout, data sources, run commands, invariants, Discord, persistence. The pipeline tunes numeric values declared in §12 against the realized-fill backtest; structural changes to telemetry or pipeline mechanics are operator decisions.

## 10. Live execution telemetry

### Per-decision `trade_context` (stamped into outcome + ghost)

- Entry-time facts: `btc_price`, `strike_price`, `seconds_remaining`, `market_price_up`, `market_price_down`, `closes_tail` (last 2 closes, so the L6 backtest can reconstruct `last_return`).
- Probabilities: `model_probability` (post-calibrator), `model_probability_raw` (pre-calibrator — stored separately so re-fits don't compound).
- Composite signals: `flow_score`, `spot_flow_signal`, `liquidation_pressure`, `regime_autocorr`, `regime_direction`, `prev_resolution_margin`.
- Microstructure aux: `coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`, `binance_liq_long_usd_min`, `binance_liq_short_usd_min`. **Every signal field (`coinbase_cvd_60s`, `coinbase_taker_60s`, `binance_liq_long_usd_min`, `binance_liq_short_usd_min`) is `None` when the source feed is missing/stale**, never `0.0` — so the pipeline can distinguish "feed cold" from "real zero." `coinbase_taker_n` is an observation **count**, not a signal: it is `0` (not `None`) when the feed is cold, while its paired `coinbase_taker_60s` is `None`, so the sole consumer (which requires `n ≥ 20`) contributes nothing either way.
- SPRT state: `sprt_confidence`, `sprt_status`.
- Sizing audit: `adverse_rate_at_30s`, **`adverse_kelly_mult`** (the actual Kelly multiplier applied at sizing — enables per-bucket retrospective Sharpe analysis), `entry_phase`, `flip_count`, `is_flip`.

**Ghost rejections share the same schema**, including `entry_phase`, `flip_count`, and `is_flip` stamped at gate-fire time. The pipeline's by-phase and flip-segmented bias cards therefore see the full ghost population — a candidate that lowers a phase-sensitive or flip-sensitive gate gets evaluated on the correct slice of evidence.

### `edge_decay.deltas` (merged at close, persisted to outcome JSON)

Side-signed post-fill mid drift at **5/10/15/30/60s**. Captured by `AdverseSelectionMonitor` keyed by `position_id` and merged into the outcome JSON at close. The 15s mean over a 30-min lookback drives the live `edge_decay_threshold` entry gate. Null windows = trade closed before that checkpoint resolved.

### `gate_stats_YYYYMMDD.json` (per ET day, in-process accumulator)

Persists on every position resolution to a date-keyed file. `gate_stats.json` mirrors the current day. Mid-day restarts preserve the day's counts — the in-process dict reloads from disk on first record. Rollover at midnight ET. Includes `loss_cut_fired` / `loss_cut_whipsaw_blocked` to audit the 0.5×ATR cushion's selectivity.

### Feed staleness telemetry

`polybot/feeds/_staleness.StalenessTracker` persists per-feed P50/P95/P99 inter-arrival gaps to `polybot/memory/feed_staleness.json` every 60s. `polybot/feeds/_socket.enable_nodelay` verifies `TCP_NODELAY` via `getsockopt` on every WS connect.

`BiasDetector` reads `feed_staleness.json` into the nightly analysis card as `feed_health` (per-feed `{n, p50, p95, p99, max}` + a `degraded_p95_ge_10s` list). A feed that creeps from P50≈1s to P95≈25s is then a surfaced fact in the analysis dict the evolver sees, not an invisible distribution shift the optimizer attributes to layer signals like `spot_flow` or `coinbase_cvd_60s`.

## 11. Nightly learning pipeline

Runs at 23:45 ET (via `run_polybot.ps1`). Five steps; calibrator save is deferred to the end so on-disk state stays coherent across crashes.

### Dataset boundaries

- Active dataset bounded to the **last 60 days** before any splits (older trades came from probability machines that no longer exist). Falls back to the full history only if the 60-day window has fewer than 500 trades.
- Walk-forward folds inside that window: train 60% / test split across `[60:70][70:80][80:90][90:100]` (each test fold genuinely OOS).
- **7-day holdout** — the last 7 days are excluded from all folds AND from the evolver's context. The calibrator's fit pool sits in a **separate** 7-day window immediately before the holdout (days `[HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS]` back), so the optimizer's holdout-confirmation gate evaluates candidates on trades the calibrator has never seen.
- **Realized fills only** — `gain_pct = pnl / size` from closed-trade outcomes, where `pnl` already nets actual fee and actual fill price. No mid-price replay; candidate strategies inherit the same slippage cost any live trade paid.
- **Recency weighting** — `0.94^days_ago` (~11-day half-life) applied inside the window cutoff. Microstructure-trade edge decays in days, not weeks.
- **Backtest L1 ATR-floor fidelity (approximate).** Live advances the rolling-20 / long-term-200 ATR buffers **per decision tick** (entry + every hold eval); the backtest holds one stored snapshot per trade, so `_kelly_bankroll_returns` advances a local ATR buffer **once per stored trade** (entry-only) when recomputing the dynamic L1 floor. `min_atr` and `atr_regime_shift_threshold` stay backtest-evaluable, and the approximation largely cancels in the baseline-vs-candidate delta (both use the same buffer; the dynamic floor rarely binds vs the static `min_atr`) — but absolute floor fidelity drifts during vol-regime transitions and on regime-bucketed subsets. (L6 features and the L3b/L3e `regime_vol_factor` instead read the faithfully-stamped `atr_rolling_20` / `atr_long_term_mean` from `trade_context`.)

### Calibration window

`IsotonicCalibrator.fit` operates on its own 7-day pool sitting immediately before the holdout (days `[HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS]` back), disjoint from the holdout window so the optimizer's holdout-confirmation gate is genuinely OOS for the calibrator. The pool must hold **≥125 trades**; it splits 60/40 into `cal_train` / `cal_val`. `fit` is called with **`min_samples=75`** (overriding the calibrator class default of 150), so `cal_train` must be **≥75** to fit, and the Kelly-Sharpe sizing gate ((iii) in stage 3) needs **≥50** `cal_val` trades. See §2 for the per-fit bootstrap-CI gate and stage 3 for the production-adoption gates layered on top.

### Stages (in order)

1. **PipelineTracker** — review of prior adoptions (7d/14d/30d realized Sharpe per adopted version); auto-revert anything that materially underperformed since adoption. Revert criterion is **symmetric with the adoption gate**: `actual_sharpe < baseline − ADOPTION_Z_FLOOR × JK_SE`, using the **identical `_jk_se` function and `ADOPTION_Z_FLOOR`** — evaluated on the post-adoption *realized* Sharpe and its trade count (the live analogue of the candidate Sharpe adoption tested). Adopt and revert thus share the same z-floor and n-aware SE. Prevents the adopt → noise-dip → revert → re-propose oscillation that a fixed absolute floor would invite.
2. **BiasDetector** — per-indicator/side/edge-bucket/regime/time-of-window/phase/flip stats + edge-realization quartiles + execution quality. Runs on `opt_outcomes` only (excludes holdout) so the analysis dict fed to the evolver has no leakage from the last 7 days.
3. **Calibrator (isotonic)** — fit attempted every cycle. A new fit is **adopted into production only when it clears all three gates**: (i) the per-fit **bootstrap-CI** lower bound > 0 (§2); (ii) it beats the *current* calibrator's recency-weighted log-loss on the full cal pool by ≥ `LOG_LOSS_FLOOR` (0.005 nats); and (iii) it does not reduce Kelly-Sharpe vs the *current* calibrator on `cal_val`. If the current calibrator has itself drifted worse than identity it is reverted to identity (or replaced directly when the new fit beats identity on both log-loss and sizing). The §2 CI gate is necessary but **not sufficient** — gates (ii)/(iii) run on top of it. Adoption applies the calibrator in-memory immediately (so this cycle's weight backtests use it); only the on-disk save is deferred to step 6.
4. **TAEvolver** — either `ClaudeRecommender` (calls Anthropic with the full analysis + directional table + structural-probe targets) or `LocalRecommender` (rule-based fallback) returns `{changes, manual_observations}`. The `claude_client` validator drops/reroutes manual-only params from `changes` → `manual_observations`. **Combined L6 weight changes are dropped if `Σ|w| × logit_scale` would breach the ±0.25 cap.**
5. **WeightOptimizer** — per-param walk-forward backtest; gate decisions live here.
6. **Deferred calibrator save** — happens only after `WeightOptimizer.save_config` commits. A crash before this line leaves new weights paired with the previous-session calibrator on disk: slightly mismatched but each is a valid, coherent artifact. Saving the calibrator first risked a brand-new calibrator paired with stale weights, which is the worse half.

### Adoption gate (WeightOptimizer)

For each candidate change tested on the 4-fold walk-forward:

```
n_candidate_trades ≥ MIN_CANDIDATE_TRADES (100)
z = Δ_sharpe / JK_SE ≥ ADOPTION_Z_FLOOR (0.3)        # lag-1 autocorr-adjusted
```

`JK_SE = sqrt((1 + 0.5 × sharpe²) / n) × sqrt(max(1, 1 + 2·ρ₁))`. An earlier
data-adaptive Newey-West correction over L≈4-5 lags was collapsed to lag-1
only after the production gain_pct autocorrelogram showed lags 2–5 sitting
inside the ±2/√n noise band at every meaningful sample size — summing them
added estimator variance without removing bias.

- **Soft abs floor.** `candidate_sharpe < min(0, baseline) − 0.05` is blocked — the loop can adopt a less-negative candidate during a regime shift (the recovery path), but not an outright collapse.
- **Fold-consistency floor** — `min(fold_sharpes) ≥ −0.10`. Magnitude-aware: a single tiny dip is fine, a deep collapse rejects.
- **Regime-stratified veto** — activates per regime bucket once that bucket has **≥8 trades** in the validation fold (lowered from 20 to populate non-`neutral` buckets in typical BTC samples). Two acceptance branches share a "no regime degrades >0.10 Sharpe" floor:
  - **(a)** Candidate improves in ≥2 of 3 populated regime buckets.
  - **(b)** Dominant regime improves AND no other regime degrades >0.10 Sharpe.
- **Holdout confirmation** — after a candidate clears all the above, run baseline vs candidate on the held-out 7-day pool (≥30 trades required). Margin scales with holdout sample size:
  ```
  HOLDOUT_ADOPTION_MARGIN = max(0.02, ADOPTION_Z_FLOOR × holdout_jk_se)
  ```
  Candidate must clear `baseline_h + HOLDOUT_ADOPTION_MARGIN`. `pipeline_info["holdout_active"]` is stamped explicitly each cycle.

### Combined-holdout interaction check

When `≥2` changes adopt: run **one combined backtest on the holdout pool** (same data the per-change holdout confirmation used; requires `≥ HOLDOUT_MIN_TRADES`). Each individual change reaching this stage has already cleared per-change z-test, fold-consistency, soft-abs floor, regime-stratified veto, and per-change holdout confirmation — but two changes that each pass alone can still interfere when combined (shared logit budget, joint clamps).

```
margin = max(0.02, ADOPTION_Z_FLOOR × holdout_jk_se)
if combined_holdout_sharpe < baseline_holdout_sharpe + margin:
    back out the WHOLE batch
```

No iteration. The per-change adoption gates have already done the directional filtering; the question here is purely "does the joint set survive on fresh data at the same z-floor used for per-change adoption?" If not, drop everything and let the next cycle re-propose individually with the directional table now reflecting this evidence.

### Crisis mode

Triggers on **either**:
- **(a)** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`), or
- **(b)** trailing-3-day Sharpe < 0 over ≥20 recent trades.

The trailing-3d leg catches sustained multi-day collapses the recent-50 smoothing masks — a multi-day bleed where the freshest fills are still mixed in the rolling 50.

- **≥3 consecutive crisis cycles** → halve `kelly_fraction` with floor **0.04** (**intentionally below the 0.05–0.18 pipeline-tunable range**, so crisis can size more defensively than any state the optimizer can adopt; do not "fix" the discrepancy).
- **Restore on first non-crisis cycle** — the original Kelly is persisted in `crisis_state.json` before the cut, so a crash mid-pipeline can't compound the halving on restart.
- **Optimizer defers `kelly_fraction` while the halving is active.** `_run_weight_optimizer` reads `crisis_state.json` at entry; if `kelly_reduced=True`, any candidate change targeting `kelly_fraction` is marked `decision="deferred_crisis"` and skipped for the cycle. The claim is preserved — it re-enters the directional table on the first non-crisis cycle — but it cannot override the safety floor while crisis is engaged.

### Adaptive exploration + structural probes (`recommender_base`)

- **`EXPLORE_STEPS`** maps each tunable to a base step size (e.g., `atr_sigma_ratio = 0.15`, `final_logit_clamp = 0.50`).
- **`_rule_exploratory`** ramps step size upward when the directional table shows past probes returned `|bt_delta|` under the noise floor. Noise floor is **empirical per cycle**: `max(0.003, 0.3 × baseline_jk_se)`. Each dead direction adds +50% to the step multiplier (cap 3.0×). Adoptions reset the loop because the directional table sees a non-trivial delta.
- **`STRUCTURAL_PROBES`** is a small forced-exploration table that fires once per `(param, value)` until evidence appears in the directional table. Currently:
  - `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` — counterfactual data backs a less-strict exit.
  - L6 turn-on at `0.005` for `log_atr_ratio`, `autocorr_signed_mag`, `flow_disagreement`, `liq_signed_sqrt` (all four L6 weights raised from the default 0.0 so every feature in the closed library gets at least one evaluation cycle).

Both `LocalRecommender` and `ClaudeRecommender` call `_rule_structural_probes()` before the rotational `_rule_exploratory`.

### L6 directional bookkeeping

The optimizer captures `old_value` from `signal_engine.derived_weights[fname]` for `derived_*_weight` params (not from `getattr(signal_engine, param)` which would return `None` since L6 weights live in a dict). L6 probes populate the directional table on every cycle.

## 12. What the pipeline can vs cannot touch

### Pipeline-tunable (`PIPELINE_PARAMS` in `polybot/config/param_registry.py`)

| Group | Params | Range |
|---|---|---|
| **L1 / volatility** | `atr_sigma_ratio` | 1.2–2.5 (highest leverage) |
| | `student_t_df` | 3–8 |
| | `min_atr` | 8.0–25.0 |
| **Logit amplifier** | `logit_scale` | 2.0–5.0 |
| **L2-L5 weights** | `regime_weight` (0.01–0.15), `flow_weight` (0.02–0.12), `spot_flow_weight` (0.01–0.15), `liquidation_weight` (0.01–0.10), `prev_margin_weight` (0.01–0.05) | per-param |
| | `momentum_weight` | 0.0–0.10 (magnitude only — sign is dead at L4 level) |
| **Indicator committee (L4)** | `weights` (RSI/MACD/Stochastic/OBV/VWAP dict) | each ≥ 0.05, renormalized to sum 1.0; adopted via the L4 backtest (validator renormalizes). Not a scalar `ParamSpec` — handled as a dict by the optimizer and `claude_client`. |
| **Sizing** | `kelly_fraction` | 0.05–0.18 |
| **Entry gates** | `min_edge`, `min_kelly`, `min_model_probability` | tight bands |
| **Exit** | `exit_edge_threshold` | −0.10..−0.03 |
| **Structural constants** | `regime_momentum_threshold` (0.08–0.25), `final_logit_clamp` (3.0–5.0), `l5_regime_damp_cap` (0.4–0.9), `atr_regime_shift_threshold` (0.40–0.80) | |
| **L6 derived weights** | `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_flow_disagreement_weight`, `derived_liq_signed_sqrt_weight` | 0.0–0.05 each; combined L6 hard-capped at ±0.25 logits |

### Manual-only (`MANUAL_ONLY_PARAMS`, validator reroutes `changes` → `manual_observations`)

- **Exit / hold magnitudes outside the curve:** `loss_cut_fraction`, `loss_cut_time_s`, `deep_loss_hold_threshold` — the backtest replays a single stored fill and can't re-simulate the hold/exit branches these control; only `exit_edge_threshold` has a counterfactual backtest path (§6).
- **Entry-timing envelope + flip hurdle:** `normal_fraction`, `late_max_penalty`, `flip_edge_premium` — the backtest applies raw Kelly sizing + entry gates only; it models neither the time-of-window multiplier nor the flip hurdle, so a change yields zero backtest delta (never adoptable).
- **Entry-time filters operator owns:** `max_edge`, `adverse_selection_threshold`, `edge_decay_threshold`.
- **Risk caps:** `max_concurrent_positions`, `max_bankroll_deployed`.
- **Circuit breaker:** `circuit_breaker.floor_pct`, `circuit_breaker.min_multiplier`.
- **Indicator periods:** `indicators.{rsi,macd,stochastic,ema,obv,atr}.*` — backtest replays stored scores at the *active* period; alternate periods would need raw candles per snapshot.
- **SPRT:** `sprt.{alpha,beta,observation_interval_s,min_confidence}` — controls intra-window timing; backtest replays a single stored fill instant, so alternate timings are uncomparable.
- **Schedule:** `trading_{start,end}_{hour_et,minute}`.

`is_manual_only(name)` is the single source of truth. If a param appears in both lists (operator error), tunable wins.

## 13. What it deliberately won't do

- Use Gaussian (BTC has fat tails — `student_t_df` clamped ≥3, default 5).
- Resolve from Binance (Chainlink only; Gamma mirrors).
- Size big — single-position caps via `max_bankroll_deployed` and `max_book_fill_pct`. Compounds via frequency, not size.
- Let the pipeline touch exit magnitudes outside `exit_edge_threshold`, position caps, circuit breaker, indicator periods, SPRT timing, or trading hours.
- Chase momentum unconditionally — in trend regimes L4's mean-revert group is **sign-replaced** with the realized 1-min return; in revert regimes it keeps its contrarian sign.
- Use pattern-based exit rules (exit comes from edge + time-value math; no "RSI > 80, sell" rules).
- Override a scalp signal because it "feels confident" — math says exit, it exits.
- Hold a dead side just because it's a binary residual. When the calibrator's lowest-learned knot says ~0% but the market still prices at 30%, selling at market beats $0 expected.
- Run cumulative-product Sharpe (`log_return`) — `gain_pct = pnl / size` arithmetic, single source of truth across live + backtest + isotonic fit.
- Use raw CLOB book prices for entry-edge math. Entries use `GET /price?side=BUY` (the executable price); books are walked only for FOK VWAP slippage estimation.
- Skip the fee impact. Polymarket's binary-payoff formula `rate × shares × p × (1 − p)` is zero at $0/$1 and maximal at $0.50; `rate = 0.018` is a constant for crypto markets in `base.DEFAULT_FEE_RATE`. If Polymarket ever makes the rate per-token, restore the `GET /fee-rate` call.
- Bypass the circuit breaker. Don't delete `polybot/db/polybot_*.db`. Don't take regime direction from `sign(prob−0.5)` — it's `sign(last 1-min return)`. Don't do layer adjustments in probability space — always logit space.

## 14. Project layout

```
polybot/
  main.py                      Trading loop, entry/exit/sizing orchestration
  config/                      settings.yaml, loader.py, param_registry.py (single source of truth)
  core/                        signal_engine, calibrator, order_flow, returns, regime,
                               exit_boundary, sprt, adverse_selection, derived_features,
                               aux_layers (compute_spot_flow_signal, compute_liquidation_signal)
  feeds/                       coinbase_feed (primary BTC + CVD),
                               binance_feed (1m candles, ATR), binance_depth, binance_trades,
                               binance_forceorder (L3e), chainlink_feed (strike + resolution),
                               clob_ws, market_scanner, _socket, _staleness
  indicators/                  rsi, macd, stochastic, obv, vwap, ema, atr + engine
  execution/                   base (BaseTrader, fee math), paper_trader, live_trader,
                               circuit_breaker (tiered floor), correlation
  agents/                      scheduler (orchestrator), outcome_reviewer,
                               counterfactual_tracker, ghost_tracker, bias_detector,
                               ta_evolver, weight_optimizer, pipeline_tracker, pipeline_analytics,
                               claude_client (validator), claude_recommender,
                               recommender_base (EXPLORE_STEPS, STRUCTURAL_PROBES),
                               local_recommender
  memory/                      calibration/, outcomes/, ghost_outcomes/, counterfactuals/,
                               pipeline_run_log.json, adverse_state.json, crisis_state.json,
                               feed_staleness.json, fill_stats.json, gate_stats_*.json,
                               orphan_positions.json, prev_resolution_margin.json
  discord_bot/                 !status !history !positions !performance !pause !resume
                               !session !agents !lessons !clear !commands
  db/models.py                 SQLite (positions, trade_history, bankroll, peak_bankroll).
                               Per-mode: polybot_paper.db / polybot_live.db. memory/ shared.
```

## 15. Data sources

| Source | Feed | What |
|---|---|---|
| Coinbase | `ticker` WS (BTC-USD) | Primary BTC price + per-trade CVD |
| Binance.com | `kline_1m` / `depth20@100ms` / `aggTrade` / `forceOrder` WS | Candles, ATR, depth, CVD-fallback, liquidations |
| Polymarket CLOB | WS + `GET /price`, `/book`, `/spread`, `/fee-rate` | Books, fills, fees |
| Polymarket Gamma | `GET /events?slug=...` | Discovery + resolution |
| Chainlink | `latestRoundData()` via Eth RPC | Strike + resolution oracle |
| Anthropic | `claude-sonnet-4-6` SDK | Daily learning pipeline |

## 16. Running

```bash
python -m polybot.main --mode paper       # paper trading
python -m polybot.main --mode live        # real USDC (needs allowance)
python -m polybot.main --run-pipeline     # one nightly cycle, no trading
python -m pytest polybot/tests/           # full suite
```

`run_polybot.ps1` is the daily loop: starts at 12:01 AM ET, stops trading at 11:30 PM ET, runs the pipeline at 11:45 PM ET, commits + pushes as soon as the pipeline exits (~11:55 PM ET), then sleeps until the next 12:01 AM ET restart. The outer `while ($true)` survives auth errors but won't retry the same day — fix auth before midnight.

## 17. Invariants (what doesn't drift)

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded priors per param.
- **`model_probability_raw`** stores pre-calibration P(side) so re-fits don't compound.
- **Recency weighting:** `0.94^days_ago` (~11-day half-life), applied inside the 60-day window cutoff. Single source `RECENCY_DECAY_PER_DAY` in `pipeline_analytics.py`.
- **`gain_pct = pnl/size`** (arithmetic). Never `log_return` for Sharpe.
- **UTC everywhere** for storage; ET only for date-bucketing (gate_stats, daily rollup) and trading-window logic.
- **Daily rollup** runs inside the 11:45 PM ET pipeline (`rollup_old_outcomes` / `rollup_old_ghosts` / `rollup_old_counterfactuals`) and bundles per-trade JSON into `rollup_YYYY-MM-DD.json`.
- **L6 library is closed.** New entries require code in `derived_features.py` plus a `ParamSpec`; never generated at runtime.
- **`edge_decay.deltas` stamped at open, persisted at close:** side-signed post-fill mid drift at 5/10/15/30/60s. Captured by `AdverseSelectionMonitor` keyed by `position_id`. Null windows = trade closed before that checkpoint resolved.
- **`adverse_kelly_mult`** stamped per-trade in `trade_context`: the actual Kelly multiplier applied at sizing (1.0 = no penalty, `adverse_penalty_min = 0.30` floor). Enables per-bucket retrospective Sharpe analysis.
- **Aux signal fields are `None` when stale**, never `0.0` — Pillar 2 layers distinguish "feed cold" from "real zero." (`coinbase_taker_n` is the lone exception: a count, `0` when cold; see §10.)
- **Shared model math** — the L1 vol-autocorrelation scale (`autocorr_vol_scale`), the L3+L3b+L3e flow combine (`combine_flow_family`), and the L3b/L3e regime-relative normalization (`regime_vol_factor` + `compute_spot_flow_signal`/`compute_liquidation_signal`) all live in `aux_layers.py` and are called by `signal_engine` (live) and `scheduler` (backtest replay) alike, so the optimizer can never tune against a model production doesn't run.
- **Atomic open/close** — single SQLite transaction; `bankroll_delta` for relative, `new_bankroll` for absolute.
- **Per-mode DB** (`polybot_paper.db` / `polybot_live.db`); `memory/` shared so the pipeline sees the union.

## 18. Discord

`!status` `!history [n]` `!positions` `!performance` `!pause` `!resume` `!session` `!agents` `!lessons` `!clear [trades|control|all]` `!commands`

## 19. Persistence

`memory/` (outcomes, counterfactuals, ghosts, pipeline_*, calibration), the per-mode SQLite DB, and `settings.yaml` are all git-tracked. `run_polybot.ps1` commits + pushes immediately after the pipeline exits (~11:55 PM ET), then sleeps until 12:01 AM ET for the next session.
