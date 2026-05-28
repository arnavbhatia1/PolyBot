# PolyBot

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

**This file is the single source of truth.** Update it with every behavioral change.

**Sections 1–9 below are FROZEN.** They describe the trading logic itself — model, gates, sizing, ordering, exits, flips, resolution, loss handling. This logic has been hand-tuned to its current shape and is not subject to further optimization. The nightly pipeline (§11) only tunes the numeric knobs declared in §12 — it does not restructure these sections. Code changes touching §1–9 require an explicit operator decision, not a pipeline adoption.

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

The sections in Part A describe the trading mechanism. They are intentionally locked: the structural decisions (Student-t over Gaussian, polarity-split L4, regime-damped L5, closed L6 library, joint L3+L3b clamp, isotonic-only calibration, the entry-gate set, the sizing pipeline, the exit-branch order, the flip-premium formula, Chainlink-only resolution, the loss-handling stack) have been deliberately chosen and will not be optimized further. The nightly pipeline tunes the **numeric values** within these structures (§12) — it cannot change the structure itself.

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
vol_scaled = (max(atr, atr_floor) / atr_sigma_ratio) × sqrt(minutes_remaining)
z          = (btc_price − strike) / vol_scaled
t_scale    = sqrt(df / (df − 2))
prob_up    = StudentT_CDF(df, z × t_scale)
```

- **`student_t_df`** — default 5, clamped ≥3 (df ≤ 2 has undefined variance and the t_scale fallback discontinuity would inject a 1.0 → 1.73 jump). Gaussian undersizes BTC's fat tails (kurtosis 6–8).
- **`atr_sigma_ratio`** — default 1.3, pipeline-tunable 1.2–2.5. The single highest-leverage knob in the model.
- **ATR floor**, dynamic: `max(min_atr, 0.30 × rolling_20)`. When `rolling_20 / long_term_200 < atr_regime_shift_threshold` (default 0.60), widens to `max(base_floor, long_term_mean × threshold × 0.30)` so the model doesn't get overconfident when vol collapses.
- **L1 clip** at `1e-6` → logit ±13.8, well past the final ±4 clamp. The clamp is the precision floor, not the clip.
- `btc_price` comes from `_fastest_btc_price`: **Coinbase WS (<2s) → Binance aggTrade (<3s) → Binance kline receipt (<5s)**. All three stale → the decision is skipped, not zeroed.

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
logit += flow_score × flow_weight × logit_scale     # combined with L3b
```

Top-5 levels each side by best price. Trade flow is recency-weighted exponential decay with a 30s half-life inside the 120s window.

### L3b — Spot CVD (Coinbase)

Coinbase is the largest US-volume BTC venue and is the venue Chainlink resolves against. Per-trade CVD + taker ratio from the WS L2 + match feed:

```
cvd_60s    = signed Coinbase BTC volume over 60s
cvd_comp   = tanh(cvd_60s / 30) × 0.8           # cascade scale = 30 BTC
taker_comp = (taker_60s − 0.5) × 0.4            # only when ≥20 trades in window
spot_flow  = clamp(cvd_comp + taker_comp, ±1)
logit += spot_flow × spot_flow_weight × logit_scale     # combined with L3
```

`compute_spot_flow_signal` lives in `polybot/core/aux_layers.py` so live and replay can never drift.

**CVD-acceleration gate** (sizing-time guard, not L3b magnitude): `coinbase_feed.get_cvd_acceleration(recent_s=15, baseline_s=45)` requires ≥10 recent trades. Skips the entry when `|spot_flow| ≥ 0.20` **and** `spot_flow × cvd_accel < 0` — buying signal has already peaked.

**Joint L3 + L3b clamp.** The sum `flow_signal × flow_w + spot_flow_signal × spot_flow_w`, both already amplified by `logit_scale`, is clamped to **±0.50 logits**. A weight asymmetry between book flow and CVD can't let one leg dominate L1 during a CLOB↔CVD disagreement.

### L3e — Direct futures liquidations (Binance)

Per-event `btcusdt@forceOrder` from Binance futures. Each liquidation message is one order with side, qty, price.

```
long_usd  = sum of price × qty for order.side == SELL (closing longs → price-down event)
short_usd = sum of price × qty for order.side == BUY  (closing shorts → price-up event)
liq       = tanh((short_usd − long_usd) / 50_000)   # USD/minute, cascade scale
logit += liq × liquidation_weight × logit_scale
```

Sign convention: **short liquidation → price-up (+)**, **long liquidation → price-down (−)**. Helper `compute_liquidation_signal` in `aux_layers.py` is shared with replay.

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

FOK ("Fill-or-Kill") via `py-clob-client-v2`. 3 retries with jittered exponential backoff. HTTP/2 keepalive ping every 10s.

Live mode boot:
1. `verify_auth` checks `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` are set and the Safe is reachable.
2. USDC balance fetched. Allowance fetched as `min(allowances[spender] for all spenders) / 1e6`.
3. Required allowance: `max_single × max_concurrent_positions × 10` (10× safety multiplier). If allowance < required → `AuthError`, clean exit, **no retry that day** (`run_polybot.ps1`'s outer `while ($true)` loop will restart the bot the next midnight ET; fix the allowance before then).
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

3. **Scalp** — `holding_edge ≤ effective_threshold` (and not in the deep-loss hold zone).

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
- **Chainlink orphan fallback** — if Gamma is silent 30+ minutes past expiry, the bot reads Chainlink directly via `chainlink_feed` and resolves the position locally. Orphan state persists in `memory/orphan_positions.json` for restart safety.

## 9. Built-in loss handling

- **Circuit breaker** — tiered floor at $100/$150/$200/$300/.../$10000, locked at 85% of crossed tier. Kelly scales 1.0 → 0.40 concavely between tier and floor; never resets down. Streak counters (3 losses / 2 wins) exist for Discord alerts only — they do not drive sizing.
- **Adverse-selection monitor** — Bayesian-shrunk fade-rate gate; sizing penalty + emergency hard-skip. State persisted to `memory/adverse_state.json` on every fill so restarts inherit the rolling window.
- **Edge-decay monitor** — same `AdverseSelectionMonitor` but watches signed 15s post-fill drift; gate activates after ≥15 resolved fills in the 30-min lookback.
- **Regime skip** — `quiet` (ATR below `vol_low_percentile`) skips entry entirely; `volatile` is allowed but tracked.
- **Feed staleness skip** — Coinbase >30s, Chainlink >60s, Binance aggTrade >30s, Binance kline >45s → skip the cycle. All-stale BTC price → skip the decision (not zero).
- **CLOB WS heartbeat** — PING every 10s, force-reconnect if no PONG within 25s.
- **Cross-venue gap** — Coinbase vs Binance BTC spot delta logged on every decision; if a hostile drift opens up, the size cap on the chosen side absorbs the leg-mismatch risk.

---

# Part B — Mutable Operational Layer (§10–19)

Everything below is the surrounding scaffolding — telemetry, the nightly pipeline, the param registry, project layout, data sources, run commands, invariants, Discord, persistence. The pipeline tunes numeric values declared in §12 against the realized-fill backtest; structural changes to telemetry or pipeline mechanics are operator decisions.

## 10. Live execution telemetry

### Per-decision `trade_context` (stamped into outcome + ghost)

- Entry-time facts: `btc_price`, `strike_price`, `seconds_remaining`, `market_price_up`, `market_price_down`, `closes_tail` (last 2 closes, so the L6 backtest can reconstruct `last_return`).
- Probabilities: `model_probability` (post-calibrator), `model_probability_raw` (pre-calibrator — stored separately so re-fits don't compound).
- Composite signals: `flow_score`, `spot_flow_signal`, `liquidation_pressure`, `regime_autocorr`, `regime_direction`, `prev_resolution_margin`.
- Microstructure aux: `binance_book_imbalance_5`, `cross_venue_gap`, `coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`, `fast_realized_vol_60s`, `binance_liq_long_usd_min`, `binance_liq_short_usd_min`. **Every field is `None` when the source feed is missing/stale**, never `0.0` — so the pipeline can distinguish "feed cold" from "real zero."
- SPRT state: `sprt_confidence`, `sprt_status`.
- Sizing audit: `adverse_rate_at_30s`, **`adverse_kelly_mult`** (the actual Kelly multiplier applied at sizing — enables per-bucket retrospective Sharpe analysis), `entry_phase`, `flip_count`, `is_flip`.

### `edge_decay.deltas` (merged at close, persisted to outcome JSON)

Side-signed post-fill mid drift at **5/10/15/30/60s**. Captured by `AdverseSelectionMonitor` keyed by `position_id` and merged into the outcome JSON at close. The 15s mean over a 30-min lookback drives the live `edge_decay_threshold` entry gate. Null windows = trade closed before that checkpoint resolved.

### `gate_stats_YYYYMMDD.json` (per ET day, in-process accumulator)

Persists on every position resolution to a date-keyed file. `gate_stats.json` mirrors the current day. Mid-day restarts preserve the day's counts — the in-process dict reloads from disk on first record. Rollover at midnight ET. Includes `loss_cut_fired` / `loss_cut_whipsaw_blocked` to audit the 0.5×ATR cushion's selectivity.

### Feed staleness telemetry

`polybot/feeds/_staleness.StalenessTracker` persists per-feed P50/P95/P99 inter-arrival gaps to `polybot/memory/feed_staleness.json` every 60s. `polybot/feeds/_socket.enable_nodelay` verifies `TCP_NODELAY` via `getsockopt` on every WS connect.

## 11. Nightly learning pipeline

Runs at 23:45 ET (via `run_polybot.ps1`). Five steps; calibrator save is deferred to the end so on-disk state stays coherent across crashes.

### Dataset boundaries

- Active dataset bounded to the **last 60 days** before any splits (older trades came from probability machines that no longer exist). Falls back to the full history only if the 60-day window has fewer than 500 trades.
- Walk-forward folds inside that window: train 60% / test split across `[60:70][70:80][80:90][90:100]` (each test fold genuinely OOS).
- **7-day holdout** — the last 7 days are excluded from all folds AND from the evolver's context. The holdout window also contains the calibrator's fit pool, so the optimizer's walk-forward folds never overlap with the calibrator's training data.
- **Realized fills only** — `gain_pct = pnl / size` from closed-trade outcomes, where `pnl` already nets actual fee and actual fill price. No mid-price replay; candidate strategies inherit the same slippage cost any live trade paid.
- **Recency weighting** — `0.94^days_ago` (~11-day half-life) applied inside the window cutoff. Microstructure-trade edge decays in days, not weeks.

### Calibration window

`IsotonicCalibrator.fit` operates on its own 7-day pool (default `min_samples=150`; train split must be ≥75 to fit). Calibration must reflect the *current* model, not last month's. See §2 for the bootstrap-CI gate.

### Stages (in order)

1. **PipelineTracker** — review of prior adoptions (7d/14d/30d realized Sharpe per adopted version); auto-revert anything that materially underperformed since adoption.
2. **BiasDetector** — per-indicator/side/edge-bucket/regime/time-of-window/phase/flip stats + edge-realization quartiles + execution quality. Runs on `opt_outcomes` only (excludes holdout) so the analysis dict fed to the evolver has no leakage from the last 7 days.
3. **Calibrator (isotonic)** — see §2. Fit attempted every cycle; adopted only if bootstrap CI clears 0.
4. **KS shift detection** — two-sample Kolmogorov-Smirnov between train (first 60%) and test (last 40%) on key features. Informational only — no veto, but Claude/local sees it in the analysis card.
5. **SPRT aggregate** — diagnostic only ("is the live model still discriminating?"); not an adoption gate.
6. **TAEvolver** — either `ClaudeRecommender` (calls Anthropic with the full analysis + directional table + structural-probe targets) or `LocalRecommender` (rule-based fallback) returns `{changes, manual_observations}`. The `claude_client` validator drops/reroutes manual-only params from `changes` → `manual_observations`. **Combined L6 weight changes are dropped if `Σ|w| × logit_scale` would breach the ±0.25 cap.**
7. **WeightOptimizer** — per-param walk-forward backtest; gate decisions live here.
8. **Deferred calibrator save** — happens only after `WeightOptimizer.save_config` commits. A crash before this line leaves new weights paired with the previous-session calibrator on disk: slightly mismatched but each is a valid, coherent artifact. Saving the calibrator first risked a brand-new calibrator paired with stale weights, which is the worse half.

### Adoption gate (WeightOptimizer)

For each candidate change tested on the 4-fold walk-forward:

```
n_candidate_trades ≥ MIN_CANDIDATE_TRADES (100)
z = Δ_sharpe / JK_SE ≥ ADOPTION_Z_FLOOR (0.3)        # Newey-West autocorr-adjusted, data-adaptive lag
```

`JK_SE = sqrt((1 + 0.5 × sharpe²) / n) × NW_factor`, with `NW_factor = sqrt(1 + 2 × Σ wₖ·ρₖ)` and Bartlett weights `wₖ = 1 − k/(L+1)`. Lag `L = max(1, floor(4 × (n/100)^(2/9)))` per Newey & West (1994) — scales naturally with sample size.

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

### Interaction back-out

When `≥2` changes adopt: run **one combined backtest** on the validation fold. If `combined_Δ < backout_coef × Σ individual_Δ`, iteratively remove the **weakest-z** adopted change and re-test, until the bound clears or ≤1 change remains.

`backout_coef` ramps with the size of the adopted set (more changes → more chances for interaction):

```
backout_coef = min(0.9, 0.7 + 0.05 × max(0, len(adopted_changes) − 2))
```

So pairs use 0.7; 3-change sets use 0.75; 5-change sets cap at 0.9.

### Crisis mode

Triggers on **either**:
- **(a)** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`), or
- **(b)** trailing-3-day Sharpe < 0 over ≥20 recent trades.

The trailing-3d leg catches sustained multi-day collapses the recent-50 smoothing masks — a multi-day bleed where the freshest fills are still mixed in the rolling 50.

- **≥3 consecutive crisis cycles** → halve `kelly_fraction` with floor **0.04** (**intentionally below the 0.05–0.18 pipeline-tunable range**, so crisis can size more defensively than any state the optimizer can adopt; do not "fix" the discrepancy).
- **Restore on first non-crisis cycle** — the original Kelly is persisted in `crisis_state.json` before the cut, so a crash mid-pipeline can't compound the halving on restart.

### Adaptive exploration + structural probes (`recommender_base`)

- **`EXPLORE_STEPS`** maps each tunable to a base step size (e.g., `atr_sigma_ratio = 0.15`, `final_logit_clamp = 0.50`).
- **`_rule_exploratory`** ramps step size upward when the directional table shows past probes returned `|bt_delta|` under the noise floor. Noise floor is **empirical per cycle**: `max(0.003, 0.3 × baseline_jk_se)`. Each dead direction adds +50% to the step multiplier (cap 3.0×). Adoptions reset the loop because the directional table sees a non-trivial delta.
- **`STRUCTURAL_PROBES`** is a small forced-exploration table that fires once per `(param, value)` until evidence appears in the directional table. Currently:
  - `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` — counterfactual data backs a less-strict exit.
  - L6 turn-on at `0.005` for `log_atr_ratio`, `autocorr_signed_mag`, `liq_signed_sqrt` (raised from the default 0.0 so the layer can be evaluated for adoption).

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
| **L2-L5 weights** | `regime_weight`, `flow_weight`, `spot_flow_weight`, `liquidation_weight`, `prev_margin_weight` | 0.01–0.15 (each) |
| | `momentum_weight` | 0.0–0.10 (magnitude only — sign is dead at L4 level) |
| **Sizing** | `kelly_fraction` | 0.05–0.18 |
| **Entry gates** | `min_edge`, `min_kelly`, `min_model_probability` | tight bands |
| **Entry timing** | `normal_fraction` (0.40–0.80), `late_max_penalty` (0.10–0.60), `flip_edge_premium` (0.005–0.05) | |
| **Exit** | `exit_edge_threshold` | −0.10..−0.03 |
| **Structural constants** | `regime_momentum_threshold` (0.08–0.25), `final_logit_clamp` (3.0–5.0), `deep_loss_hold_threshold` (−0.20..−0.05), `l5_regime_damp_cap` (0.4–0.9), `atr_regime_shift_threshold` (0.40–0.80) | |
| **L6 derived weights** | `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_flow_disagreement_weight`, `derived_liq_signed_sqrt_weight` | 0.0–0.05 each; combined L6 hard-capped at ±0.25 logits |

### Manual-only (`MANUAL_ONLY_PARAMS`, validator reroutes `changes` → `manual_observations`)

- **Exit / hold magnitudes outside the curve:** `loss_cut_fraction`, `loss_cut_time_s`.
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
- **Aux fields are `None` when stale**, never `0.0` — Pillar 2 layers distinguish "feed cold" from "real zero."
- **Atomic open/close** — single SQLite transaction; `bankroll_delta` for relative, `new_bankroll` for absolute.
- **Per-mode DB** (`polybot_paper.db` / `polybot_live.db`); `memory/` shared so the pipeline sees the union.

## 18. Discord

`!status` `!history [n]` `!positions` `!performance` `!pause` `!resume` `!session` `!agents` `!lessons` `!clear [trades|control|all]` `!commands`

## 19. Persistence

`memory/` (outcomes, counterfactuals, ghosts, pipeline_*, calibration), the per-mode SQLite DB, and `settings.yaml` are all git-tracked. `run_polybot.ps1` commits + pushes immediately after the pipeline exits (~11:55 PM ET), then sleeps until 12:01 AM ET for the next session.
