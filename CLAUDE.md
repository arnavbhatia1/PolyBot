# PolyBot

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

**This file is the single source of truth.** Update it with every behavioral change.

**Frozen: the signal/probability model only.** The one locked thing is the math that turns market state into P(Up) — the L1–L6 layer stack, the logit combine/clamp, and the isotonic calibration *transform* as applied to a live decision (§2; implemented in `signal_engine`, `aux_layers`, `derived_features`, `returns`, and `IsotonicCalibrator.calibrate`). That computation, and how it is applied live, does not change. **Everything else is fair game** — entry gates, sizing, ordering, exits, flips, resolution, loss handling (§3–9), the entire nightly pipeline including how and where the calibrator is *fit* (§11), telemetry, and all numeric knobs (§12). The pipeline tunes §12 inside the model; surrounding mechanics are improvable like any other code.

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

# Part A — The Signal Model & Trading Mechanism (§1–9)

**§2 — the model that forms P(Up) — is the frozen core.** Its structural choices (Student-t over Gaussian, polarity-split L4, regime-damped L5, closed L6 library, redundancy-discounted L3+L3b+L3e flow combine, isotonic calibration transform) are locked, as is how the calibrator output is applied to a live decision. §1 is context. §3–9 (entry gates, sizing, ordering, exits, flips, resolution, loss handling) describe current behavior but are **mutable** — improvable like any other non-model code, subject to the same care and tests. The pipeline tunes the numeric knobs in §12.

## 1. What you're betting on

Every 5 minutes, Polymarket runs a market: "Will BTC close higher or lower at the end of this 5-minute window than at the start?" Two sides — **Up** and **Down** — each an ERC-1155 token trading $0–$1. Winning side pays $1/share, loser $0. Chainlink's BTC/USD oracle is the official resolution source; Polymarket's Gamma API mirrors it for the slug feed.

The bot picks which side, how much, when to scale in, and when (if ever) to sell early. Two modes, same engine:

- **`paper`** — full realism shim: real CLOB books, FOK semantics, convex slippage, configurable network-fail / latency jitter, $1 minimum, tick-size snapping. Bankroll in a paper SQLite DB.
- **`live`** — `py-clob-client-v2` FOK orders against the real CLOB. Same engine, gates, telemetry; wraps the trader in `LiveTrader` and verifies USDC balance + allowance before the first order.

Schedule (via `run_polybot.ps1`): starts 12:01 AM ET, stops entries 11:30 PM ET, runs the learning pipeline 11:45 PM ET, commits, restarts.

## 2. How the bot forms an opinion

For each window, the bot computes **P(Up)** by stacking evidence in logit space, then squashes through sigmoid + isotonic calibration. Every layer except L1 contributes via `weight × logit_scale × signal` (`logit_scale` = global amplifier, default 4.0). Final logit clamped to ±`final_logit_clamp` (default 4.0) → prob ∈ [0.018, 0.982].

### L1 — Student-t CDF (core)

"How far is BTC from the strike vs. how much it typically moves in the time remaining?"

```
ac         = clamp(regime, ±0.5)                 # regime = lag-1 autocorr (shared with L2)
vol_scaled = (max(atr, atr_floor) / atr_sigma_ratio) × sqrt(minutes_remaining) × sqrt((1 + ac) / (1 − ac))
z          = (btc_price − strike) / vol_scaled
t_scale    = sqrt(df / (df − 2))
prob_up    = StudentT_CDF(df, z × t_scale)
```

- **`student_t_df`** — default 5, clamped ≥3 (df ≤ 2 → undefined variance + a t_scale discontinuity that injects a 1.0 → 1.73 jump). Gaussian undersizes BTC's fat tails (kurtosis 6–8).
- **`atr_sigma_ratio`** — default 1.3, pipeline-tunable 1.2–2.5. The single highest-leverage knob in the model.
- **Autocorrelation-scaled vol** — BTC 1-min returns aren't i.i.d., so plain `sqrt(minutes_remaining)` misstates the terminal spread. `vol_scaled` is multiplied by the AR(1) terminal-SD ratio `sqrt((1 + ac) / (1 − ac))`, where `ac` is the lag-1 autocorrelation L2 consumes (clamped ±0.5). Positive autocorr (trend) widens spread → P(Up) toward 0.5; negative (mean-reversion) tightens → pushes away. `regime` is computed once and shared by L1, L2, L4, L5.
- **ATR floor**, dynamic: `max(min_atr, 0.30 × rolling_20)`. When `rolling_20 / long_term_200 < atr_regime_shift_threshold` (default 0.60), widens to `max(base_floor, long_term_mean × threshold × 0.30)` so the model isn't overconfident when vol collapses. `rolling_20` / `long_term_200` are buffers of the last 20 / 200 ATR **samples**, one sample appended per `compute_probability` call (per decision tick — entry *and* hold evals) — **not** per 1-min candle. Effective horizon tracks decision cadence, not wall-clock.
- **L1 clip** at `1e-6` → logit ±13.8, past the final ±4 clamp. The clamp is the precision floor, not the clip.
- `btc_price` from `_fastest_btc_price`: **Coinbase WS only (<2s)** — lowest-latency feed and the venue Chainlink resolves against. No Binance fallback: a venue that can diverge across the strike on a transient print would flip P(side) on a tick the resolver never sees. Coinbase stale (≥2s) → decision skipped, not zeroed (Binance spot still read, only to log the cross-venue gap — §9).

### L2 — Regime

```
last_return = (live_btc_price − closes[-2]) / closes[-2]
regime      = lag1_autocorr(closes, regime_lookback)
direction   = sign(last_return)
logit += regime × direction × regime_weight × logit_scale
```

Single `lag1_autocorr` helper in `polybot/core/returns.py` — `SignalEngine.compute_regime_factor` and `RegimeDetector` both delegate to it. `last_return` mixes the live Coinbase tick against the most recent fully-closed Binance candle, eliminating the minute-boundary mismatch between what L1 sees and L2's numerator.

### L3 — CLOB flow

```
book_imbalance = (top-5 bid_up + top-5 ask_down − top-5 bid_down − top-5 ask_up) / total
trade_flow     = recency_weighted_net_flow (120s window, 30s half-life decay)
flow_score     = 0.6 × book_imbalance + 0.4 × trade_flow
logit += flow_score × flow_weight × logit_scale     # combined with L3b + L3e (redundancy-discounted)
```

Top-5 levels each side by best price. Trade flow is exponential decay with a 30s half-life inside the 120s window.

### L3b — Spot CVD (Coinbase)

Coinbase is the largest US-volume BTC venue and is where Chainlink resolves. Per-trade CVD + taker ratio from the WS L2 + match feed:

```
cvd_60s    = signed Coinbase BTC volume over 60s
vol_factor = clamp(atr / atr_long_term_mean, 0.5, 3.0)   # current volatility regime
cvd_comp   = tanh(cvd_60s / (30 × vol_factor)) × 0.8     # saturation scale tracks regime
taker_comp = (taker_60s − 0.5) × 0.4            # only when ≥20 trades in window
spot_flow  = clamp(cvd_comp + taker_comp, ±1)
logit += spot_flow × spot_flow_weight × logit_scale     # combined with L3 + L3e (redundancy-discounted)
```

`compute_spot_flow_signal` in `polybot/core/aux_layers.py` so live and replay can't drift. `vol_factor` scales the `tanh` saturation point to the current regime (a fixed scale would saturate in high-volume regimes, losing resolution when flow is most informative).

**CVD-acceleration gate** (sizing-time guard, not L3b magnitude): `coinbase_feed.get_cvd_acceleration(recent_s=15, baseline_s=45)` requires ≥10 recent trades. Skips entry when `|spot_flow| ≥ 0.20` **and** `spot_flow × cvd_accel < 0` — the signal has already peaked.

**Flow-family combine (L3 + L3b + L3e).** Book flow, spot CVD, and liquidations watch the *same* BTC move, so summing additively double-counts when they agree — the exact high-conviction case that drives the largest sizing. Instead, per direction: the strongest contribution enters at full weight, same-direction corroborators discounted by `_FLOW_REDUNDANCY` (0.5); opposing signals offset naturally (no discount — disagreement is information). Combined result (all three legs) clamped to **±0.50 logits**, so no flow leg or correlated cluster can dominate L1.

### L3e — Direct futures liquidations (Binance)

Per-event `btcusdt@forceOrder` from Binance futures; each message is one order with side, qty, price.

```
long_usd  = sum of price × qty for order.side == SELL (closing longs → price-down event)
short_usd = sum of price × qty for order.side == BUY  (closing shorts → price-up event)
vol_factor = clamp(atr / atr_long_term_mean, 0.5, 3.0)
liq        = tanh((short_usd − long_usd) / (50_000 × (btc_price / 65_000) × vol_factor))   # scale tracks price + vol
liq × liquidation_weight × logit_scale     # enters the L3+L3b+L3e flow combine (see L3b), not added separately
```

Sign: **short liquidation → price-up (+)**, **long liquidation → price-down (−)**. Helper `compute_liquidation_signal` in `aux_layers.py`, shared with replay. Cascade scale tracks `btc_price` (vs a $65k reference) and the current vol regime — a fixed USD threshold would silently recalibrate as BTC's price level drifts.

### L4 — Indicator committee (polarity-split, regime-conditional)

Five indicators (RSI, MACD, Stochastic, OBV, VWAP) from the 1-min candle buffer, raw `score` consumed directly (no adaptive normalizer). Two groups:

- **Mean-revert:** RSI, Stochastic, VWAP
- **Trend-confirm:** MACD, OBV

Each dot-products with its L4 weights (`weights` dict), then mixes by regime via `t = tanh(regime / regime_momentum_threshold)`:

```
contrarian_mult   = (1 − t) × 0.5
trend_confirm_mult = 0.5 + 0.5 × max(0, t)
score = mean_revert × contrarian_mult
      + |mean_revert| × direction × max(0, t)        # in trend regime, polarity FLIPS to sign(last_return)
      + trend_confirm × trend_confirm_mult
score = clamp(score, ±1)
logit += score × effective_momentum_weight × logit_scale
```

`effective_momentum_weight` = unsigned magnitude scaled `0.5×` → `1.5×` by `|tanh(regime / regime_momentum_threshold)|` (no cliff at the threshold). The pipeline's `momentum_weight` bound (0–0.10) caps magnitude; **sign is dead at the L4 level** — reborn per-group inside `compute_momentum` from regime + realized direction.

- **Revert regime** (`t < 0`): mean-revert keeps its contrarian sign at full power; trend-confirm dampened.
- **Trend regime** (`t > 0`): mean-revert's sign is **replaced** by `sign(last_1min_return)` (continuation, not a direction-agnostic flip); trend-confirm at full power.

`regime_momentum_threshold` default 0.15, pipeline-tunable 0.08–0.25.

### L5 — Previous-window margin carry

```
logit += tanh(prev_resolution_margin / max(atr, 1)) × prev_margin_weight × logit_scale
       × (1 − min(l5_regime_damp_cap, |regime|))
```

The dampener is the orthogonality patch: when `|regime|` is high, L2 already encodes the same drift, so L5 contributes only its orthogonal-info portion. `l5_regime_damp_cap` default 0.7, tunable 0.4–0.9. `prev_resolution_margin` persists with a `saved_at` timestamp in `memory/prev_resolution_margin.json`; older than 30 min on load → carry zeroed (previous window no longer adjacent).

### L6 — Derived feature library (closed)

A frozen library of **4 bounded transforms** of state already tracked by `compute_probability` (`polybot/core/derived_features.py`). Every weight defaults to **0.0** — the layer is dead until the pipeline raises one off zero with evidence.

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

Cap enforced at the call site, and the **`claude_client` validator drops any L6 weight-change set whose `Σ|w| × logit_scale` would breach `L6_LOGIT_CAP`** — unbreakable from either side. Closed library: adding a feature requires code in `derived_features.py` plus a `ParamSpec` row.

### Calibration (isotonic) — sole overconfidence correction

`IsotonicCalibrator` in `polybot/core/calibrator.py`. Identity by default. Fits on the last 7 days of trades (`≥150 samples` default, pool from the calibration train split; ≥75 in train, else skipped).

Adoption is a **single OOB bootstrap-CI gate**: the lower-80% bound of weighted log-loss improvement vs identity, across **300 OOB resamples** with per-bootstrap weight renormalization, must be strictly positive. RNG seeded from `time.time_ns()` each fit so the CI tracks real sampling variance.

Pre-CI **range check**: `y_min ≤ 0.50` and `y_max ≥ 0.55` (else the curve doesn't span the decision region — rejected without bootstrapping).

`last_fit_diagnostics` (`oob_ci_lower_nats`, `oob_ci_median_nats`, `n_samples`, `bootstrap_n_completed`, `y_min`, `y_max`, `decision`) is stamped to `pipeline_info["cal_info"]["fit_diagnostics"]` on every `fit()` reaching the bootstrap stage — both accept (`decision="adopted"`) and CI-reject (`decision="rejected_ci"`). Structural early-rejects (sample count, zero-weight, sklearn exception, range check) return `False` without stamping; those are visible in the reject-site log line.

`lowest_learned_prob` (lowest `y_thresholds_[0]` the calibrator can output) is the "dead side" floor consumed by `evaluate_hold` — §6.

## 3. Entry gates

Edge = `calibrated_model_prob − market_price`. **All** must pass; any single failure → skip the tick.

| Gate | Threshold | Source |
|---|---|---|
| Chosen-side `prob` | ≥ `min_model_probability` (default 0.56) | `SignalEngine.evaluate` |
| `edge` | ≥ `min_edge` (default 0.04, scaled by flip premium — §7) | `SignalEngine.evaluate` |
| `Kelly` (fee-aware) | ≥ `min_kelly` (default 0.01); `b_eff = b × (1 − fee_rate)` | `SignalEngine._kelly` |
| Spread on either side | `spread/2 + DEFAULT_FEE_RATE ≤ max_spread` (default 0.10) | `_fetch_market_prices` |
| Book depth | both-sides-thin gate first (≥ `min_book_depth_usd = $50` on at least one side); chosen-side depth must also clear it | `_evaluate_signal_and_enter` |
| Price sum | `price_up + price_down ∈ [0.98, 1.02]` (cross-book no-arb) | `_fetch_market_prices` |
| Book freshness | both sides' WS BBO ≤ `_WS_STALE_S = 10s` old | `clob_ws.both_books_fresh` |
| `edge ≤ max_edge` | default 0.20 — wider edge = stale phantom price | `_evaluate_signal_and_enter` |
| ATR gate | ATR ≥ 5th-percentile (lower-bound only) | `IndicatorEngine.atr` |
| SPRT | not `SKIP`; not opposing the chosen side when conf > 60% with ≥6 obs | `SPRTAccumulator` |
| Adverse-selection hard skip | `adverse_rate_at_30s ≥ adverse_selection_threshold` (default 0.80) → reject | `AdverseSelectionMonitor` |
| Edge-decay | mean 15s post-fill drift (30-min lookback) ≥ `edge_decay_threshold` (default −0.05). Inactive until ≥15 resolved fills in lookback | `AdverseSelectionMonitor.get_recent_decay_mean` |
| Layer disagreement | reject when `compute_momentum` opposes the chosen side (>0.5 magnitude) and `edge × 0.5 < min_edge` | inline |
| CVD deceleration | skip if `\|spot_flow\| ≥ 0.20` AND `spot_flow × cvd_accel < 0` | inline |
| Regime | skip when `RegimeDetector` classifies `quiet` | `RegimeDetector.classify` |
| Net-edge after slippage | `edge − price × est_slip ≥ min_edge` | `slippage_pct` |
| Pre-submit re-check | walk current ask ladder for FOK VWAP; recomputed net edge must clear `[min_edge, max_edge]`. Fresh-BBA fallback (book unavailable) checks net edge vs `min_edge` and **gross** edge vs `max_edge` (`max_edge` is a stale-phantom guard, slippage is execution cost) | `compute_buy_vwap` |
| Min order size | size ≥ $1 (Polymarket CLOB floor; paper mirrors live) | inline |
| Feed staleness | Coinbase ≤ 30s, Chainlink ≤ 60s, Binance aggTrade ≤ 30s, Binance kline ≤ 45s | inline |

**Adverse selection is sizing-side, not entry-side.** Above the soft penalty floor (`adverse_penalty_floor` default 0.45):

```
kelly_mult = max(adverse_penalty_min,
                 1 − adverse_penalty_slope × max(0, adverse_rate_at_30s − adverse_penalty_floor))
           = max(0.30, 1 − 1.5 × max(0, adverse_rate − 0.45))
```

So at `adverse_rate = 0.85` the multiplier collapses to 0.30 but the trade still fires — only the hard 0.80 threshold blocks entry. The 30-min lookback is **Bayesian-shrunk to a neutral prior** (n=10, rate=0.5) so the gate stays calibrated in low-volume hours and the rate doesn't snap to 1.0 after one bad fill.

Every rejection of a **pipeline-tunable or signal-derived gate** feeds a **ghost** rejection into `GhostTracker` with full L1-L5 inputs + aux microstructure; ghosts resolve at the same window's close and feed the pipeline's backtest pool. Raising a gate filters the same ghosts out of baseline + candidate equally; lowering one includes them. Non-tunable structural gates (`regime` quiet skip, chosen-side `thin_book_depth` vs the operator-owned `min_book_depth_usd`, `min_size` $1 floor) reject without ghosting — the pipeline can't adopt a change that re-includes them.

## 4. Sizing

Hard caps first, soft multipliers second. Then a $1 floor and a real round-trip net-edge sanity check.

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

- **Circuit breaker** — tier-locked floor at $100/$150/$200/$300/$400/$600/$800/$1000/$1500/$2000/$3000/$4000/$6000/$8000/$10000. Floor = locked_tier × `floor_pct` (default 0.85). Kelly multiplier: `1.0×` at/above the locked tier; `min_multiplier` (default 0.40) at/below the floor; **concave (sqrt) interpolation** between (shallow drawdowns penalize lightly — a $100/$85 midpoint gives ~0.82× vs 0.70× linear — deep ones aggressively). Tier never resets down; ratchets up when bankroll crosses a new tier.

- **Time multiplier** — `compute_time_multiplier`. First `normal_fraction` of the window (default 60% → 0–180s): full Kelly. After: penalty scales by `(1 − conviction)` up to `late_max_penalty` (default 0.30). High-conviction late entries barely penalized; ATM late entries take the full hit.

- **Consensus multiplier** — `compute_signal_consensus` counts how many of `flow`, `spot_flow`, `cvd_accel_norm` agree with the chosen side (after dropping signals below `consensus_dead_zone = 0.05`):
  | Agree % | Mult |
  |---|---|
  | ≥ 80% | 1.30× |
  | ≥ 60% | 1.00× |
  | ≥ 40% | 0.80× |
  | else | 0.60× |

- **Concurrent multiplier (correlation-aware)** — `polybot/execution/correlation.py`. Adjacent 5-min BTC windows share regime + microstructure; same-side concurrent bets are highly correlated, opposite-side naturally hedged. ρ is a **fixed prior**: `+0.75` same-side, `−0.25` opposite-side (`_CORR_SAME_SIDE`, `_CORR_OPPOSITE_SIDE`). Same-market triggers flip logic, not this multiplier. Worst ρ across open positions:
  | Worst ρ | Mult |
  |---|---|
  | > 0.6 | 0.35 |
  | > 0.3 | 0.55 |
  | > −0.2 | 0.70 |
  | ≤ −0.2 | 0.90 |

### Hard caps

- `bankroll × max_bankroll_deployed` (default 0.80)
- `side_depth × max_book_fill_pct` (default 0.50) — under the thin-CLOB upstream gate requiring at least one side ≥ $50 depth; if the chosen side is the empty leg of a one-sided book, that's an explicit skip.

## 5. Placing the order

FOK via `py-clob-client-v2`. 3 retries with jittered exponential backoff. HTTP/2 keepalive ping every 5s (against a 60s `keepalive_expiry` pool, so the connection never lapses between pings).

Live mode boot:
1. `verify_auth` checks `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` are set and the Safe is reachable.
2. USDC balance fetched. Allowance = `min(allowances[spender] for all spenders) / 1e6`.
3. Required allowance: `max_single × max_concurrent_positions × 10` (10× safety), where `max_single = bankroll × kelly_fraction` (a max Kelly-sized single bet — **not** the `max_bankroll_deployed` hard cap). If allowance < required → `AuthError`, clean exit, **no retry that day** (the outer `while ($true)` restarts next midnight ET; fix allowance before then).
4. **Mid-session allowance recheck** — every `_ALLOWANCE_RECHECK_EVERY = 10` submits, re-fetch and warn (or fail) if revoked/run down.

Paper boot: skips auth, same `BaseTrader` open/close path, `PaperTrader` simulates book-walk fill + latency + occasional FOK rejection.

Per-trade DB write is atomic (single SQLite transaction): `open_position_and_debit_bankroll`, `close_position(... bankroll_delta=... | new_bankroll=...)`. `bankroll_delta` for a relative credit (scalp), `new_bankroll` for absolute on resolution.

**`fill.fill_size` is always USDC notional** — BUY = requested $ size; SELL = `shares × fill_price`. Logs show the actual fill price (book-walk for paper, FOK for live), never the signal-moment price.

## 6. Holding vs scalping (`evaluate_hold`)

Every tick while we hold a position, re-run the full model and decide HOLD vs EXIT. `holding_edge = model_prob − market_price_for_side`. The exit threshold blends two curves:

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

**`ExitBoundary.compute_exit_threshold`** in `polybot/core/exit_boundary.py`. Binary-payoff math (payoff kinks at $0/$1, unlike European options):
- **Deep ITM (`p ≥ 0.70`):** base time value × `(1 − itm_depth × 0.5)` + resolution premium (`itm_depth × 0.05 × (1 − minutes/5)`) — wants to hold for $1.
- **Deep OTM (`p ≤ 0.30`):** base time value × `(1 − otm_depth × 0.7)` + urgency premium ramping in the last 2 minutes — cut losses.
- **ATM:** `0.07 × √minutes × 0.4 + fee_cost`.

OTM urgency can push the threshold **positive**, forcing exit even when the model is optimistic — final clamp `[−0.30, urgency_premium > 0 ? 0.30 : −0.01]`.

### Exit branches (in order)

1. **Loss-cut** — `entry_price > 0 AND market_price < entry × loss_cut_fraction (0.65) AND seconds_remaining < loss_cut_time_s (90s) AND BTC on the wrong side of strike by ≥ 0.5×ATR`. The 0.5×ATR cushion is the whipsaw guard: when BTC sits on the strike and the contract flickers $0.05–$0.70 on thin prints, we don't lock in the bottom. Engine stamps `last_loss_cut_event` ∈ {`""`, `"fired"`, `"whipsaw_blocked"`} per call; the loop counts those into `gate_stats`.

2. **Deep-loss hold** — `holding_edge < deep_loss_hold_threshold (−0.10) AND market < entry AND model_prob > calibrator.lowest_learned_prob`. The binary residual ($1 if we win) beats locking in the loss at a depressed price. **Override:** when `model_prob ≤ lowest_learned_prob`, the calibrator says this side won essentially never at this raw prob — selling at market beats ~$0 expected. Identity calibrator returns `0.0` (override disabled); refits update it dynamically.

3. **Scalp** — `holding_edge ≤ effective_threshold` (and not in the deep-loss hold zone), **unless BTC is within 0.5×ATR of the strike on the wrong side** — same whipsaw cushion as branch 1, so a borderline strike-side flip can't scalp out a position whose binary residual is still ~coin-flip.

4. **Hold** — otherwise.

No confidence override. Math says exit, it exits.

`exit_edge_threshold` is the only operator-touchable exit knob and the **only exit knob the pipeline can tune** (range −0.10..−0.03). On a proposed change, the backtest replays the **counterfactual tracker**'s recorded scalp outcomes through the new threshold: trades whose recorded `holding_edge_at_scalp` is above the candidate threshold are re-priced using the matched hold-to-resolution PnL.

## 7. Flip trading

After a scalp, the bot can re-enter the same window — including the opposite side — **unboundedly** (one position at a time). Each re-entry clears the standard entry gates plus a flip premium:

```
flip_premium = flip_edge_premium + 0.005 × max(0, flip_count − 2)
spread_cost  = spread + 2 × fee_rate × p × (1 − p)            # real round-trip
flip_hurdle  = min_edge + max(flip_premium, spread_cost)
```

Flips 1–2 pay only the base `flip_edge_premium` (default 0.015); flip 3 pays +0.5pp; flip 4 pays +1.0pp; etc. Or the actual round-trip spread+fee cost, whichever is higher — flips can't churn on micro-edge that won't survive the round trip.

## 8. Resolution

- **Early scalp** — sold before expiry into the book. The bot keeps the difference; counterfactual tracker logs what hold-to-resolution would have paid.
- **Resolution** — window closes; Chainlink decides; winning side pays $1/share, losing side $0. PnL credited atomically.
- **Never resolves from Binance** — it can diverge from Chainlink by $20–$200 at the close (the worst time to trust spot).
- **Chainlink orphan fallback** — if Gamma is silent 30+ min past expiry, the bot reads Chainlink directly via `chainlink_feed` and resolves locally. Restart safety: the position stays `open`/`pending_resolution` in the DB until resolved (re-evaluated on boot), not a file. `memory/orphan_positions.json` is written by a *separate* startup check (`LiveTrader.detect_orphan_positions`) flagging on-chain positions the DB doesn't know about.

## 9. Built-in loss handling

The stack: circuit breaker (§4), adverse-selection gate (§3), edge-decay gate (§3), regime quiet-skip (§3), feed-staleness skip (§3 — a Coinbase gap ≥2s skips the L1 decision, no Binance fallback), cross-venue gap logging (§1). Facts not stated above:
- Circuit-breaker streak counters (3 losses / 2 wins) drive Discord alerts only, never sizing.
- `AdverseSelectionMonitor` state persisted to `memory/adverse_state.json` on every fill so restarts inherit the rolling window.
- **CLOB WS heartbeat** — PING every 10s, force-reconnect if no PONG within 25s.

---

# Part B — Mutable Operational Layer (§10–19)

The surrounding scaffolding — telemetry, nightly pipeline, param registry, layout, data sources, run commands, invariants, Discord, persistence. The pipeline tunes numeric values declared in §12 against the realized-fill backtest; structural changes to telemetry or pipeline mechanics are operator decisions.

## 10. Live execution telemetry

### Per-decision `trade_context` (stamped into outcome + ghost)

- **Entry facts:** `btc_price`, `strike_price`, `seconds_remaining`, `market_price_up`, `market_price_down`, `closes_tail` (last 2 closes, so the L6 backtest can reconstruct `last_return`).
- **Probabilities:** `model_probability` (post-calibrator), `model_probability_raw` (pre-calibrator — stored separately so re-fits don't compound).
- **Composite signals:** `flow_score`, `spot_flow_signal`, `liquidation_pressure`, `regime_autocorr`, `regime_direction`, `prev_resolution_margin`.
- **Microstructure aux:** `coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`, `binance_liq_long_usd_min`, `binance_liq_short_usd_min`. Each **signal** field is `None` when its feed is missing/stale, never `0.0` — so the pipeline can tell "feed cold" from "real zero." `coinbase_taker_n` is a **count**, not a signal: `0` (not `None`) when cold, while its paired `coinbase_taker_60s` is `None`, so the sole consumer (requires `n ≥ 20`) contributes nothing either way.
- **SPRT:** `sprt_confidence`, `sprt_status`.
- **Sizing audit:** `adverse_rate_at_30s`, `adverse_kelly_mult` (the actual Kelly multiplier applied at sizing — enables per-bucket retrospective Sharpe), `entry_phase`, `flip_count`, `is_flip`.

**Ghost rejections share the same schema**, including `entry_phase`, `flip_count`, `is_flip` stamped at gate-fire time, so the pipeline's by-phase and flip-segmented bias cards see the full ghost population.

### `edge_decay.deltas` (merged at close, persisted to outcome JSON)

Side-signed post-fill mid drift at **5/10/15/30/60s**, captured by `AdverseSelectionMonitor` keyed by `position_id`, merged into the outcome JSON at close. The 15s mean over a 30-min lookback drives the live `edge_decay_threshold` gate. Null windows = trade closed before that checkpoint resolved.

### `gate_stats_YYYYMMDD.json` (per ET day, in-process accumulator)

Persists on every position resolution to a date-keyed file; `gate_stats.json` mirrors the current day. Mid-day restarts preserve counts (the in-process dict reloads on first record). Rollover at midnight ET. Includes `loss_cut_fired` / `loss_cut_whipsaw_blocked` to audit the 0.5×ATR cushion.

### Feed staleness telemetry

`polybot/feeds/_staleness.StalenessTracker` persists per-feed P50/P95/P99 inter-arrival gaps to `polybot/memory/feed_staleness.json` every 60s. `polybot/feeds/_socket.enable_nodelay` verifies `TCP_NODELAY` via `getsockopt` on every WS connect. `BiasDetector` reads `feed_staleness.json` into the nightly card as `feed_health` (per-feed `{n, p50, p95, p99, max}` + a `degraded_p95_ge_10s` list), so a feed creeping from P50≈1s to P95≈25s is a surfaced fact, not a distribution shift the optimizer misattributes to layer signals.

## 11. Nightly learning pipeline

Runs 23:45 ET (via `run_polybot.ps1`). Five steps; calibrator save deferred to the end so on-disk state stays coherent across crashes.

### Dataset boundaries

- Active dataset bounded to the **last 60 days** before splits (older trades came from probability machines that no longer exist). Falls back to full history only if the 60-day window has <500 trades.
- Walk-forward folds inside that window: train 60% / test split across `[60:70][70:80][80:90][90:100]` (each test fold genuinely OOS).
- **7-day holdout** — last 7 days excluded from all folds AND the evolver's context. Two calibrators (see Calibration window): the **live** calibrator fits the freshest `_CAL_WINDOW_DAYS` for production, while a separate **gate-reference** calibrator fits the disjoint window immediately before the holdout (days `[HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS]` back). Weight backtests score candidates through the gate reference, so the holdout-confirmation gate evaluates them on trades that calibrator never saw.
- **Realized fills only** — `gain_pct = pnl / size` from closed-trade outcomes, `pnl` already netting actual fee + fill price. No mid-price replay; candidates inherit the slippage any live trade paid.
- **Recency weighting** — `0.94^days_ago` (~11-day half-life) inside the window cutoff. Microstructure edge decays in days, not weeks.
- **Backtest L1 ATR-floor fidelity (approximate).** Live advances the rolling-20 / long-term-200 ATR buffers per decision tick; the backtest holds one snapshot per trade, so `_kelly_bankroll_returns` advances a local buffer once per stored trade (entry-only). `min_atr` and `atr_regime_shift_threshold` stay backtest-evaluable, and the approximation largely cancels in the baseline-vs-candidate delta — but absolute fidelity drifts during vol-regime transitions and on regime-bucketed subsets. (L6 features and the L3b/L3e `regime_vol_factor` read the faithfully-stamped `atr_rolling_20` / `atr_long_term_mean` from `trade_context`.)

### Calibration window

**Two calibrators (decoupled masters).** A single calibrator can't serve both live trading (wants the freshest data) and the OOS gate (needs a window the holdout never saw), so the pipeline fits two:

- **Live / production** — `IsotonicCalibrator.fit` on the **freshest `_CAL_WINDOW_DAYS` (≈7d)**, applied to `signal_engine.calibrator` and saved. Live trading sizes on this, so it tracks current microstructure instead of trailing 7–14 days behind. Goes through the full three-gate production adoption (stage 3).
- **Gate reference** — a separate fit on the window **immediately before the holdout** (days `[HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS]` back, set once per cycle as `self._gate_calibrator`), disjoint from the holdout. The weight-optimizer backtests — baseline, walk-forward folds, `_backtest_recommendations` / `_backtest_single_change`, holdout confirmation, combined check — score candidates through **this** calibrator, never the live one, so the gate stays genuinely OOS even as the live calibrator fits the freshest data. Adopted via `fit`'s own bootstrap-CI gate; `None` (identity) when the window is too thin → backtests run at identity (no behavior change from before the split when that window is empty).

Each pool must hold **≥125 trades**, split 60/40 into `cal_train` / `cal_val`; `fit` uses **`min_samples=75`** (overriding the class default 150), so `cal_train` must be ≥75, and the Kelly-Sharpe gate ((iii) in stage 3) needs **≥50** `cal_val`. See §2 for the per-fit bootstrap-CI gate. Both arms of every weight comparison share the same fixed gate-reference calibrator, so its mapping is common-mode and cancels in the adoption Δ to first order.

### Stages (in order)

1. **PipelineTracker** — review of prior adoptions (7d/14d/30d realized Sharpe per adopted version); auto-revert anything that materially underperformed. Revert criterion is **symmetric with adoption**: `actual_sharpe < baseline − ADOPTION_Z_FLOOR × JK_SE`, using the identical `_jk_se` and `ADOPTION_Z_FLOOR` on the post-adoption realized Sharpe + trade count. Shared z-floor and n-aware SE prevent adopt → noise-dip → revert → re-propose oscillation.
2. **BiasDetector** — per-indicator/side/edge-bucket/regime/time-of-window/phase/flip stats + edge-realization quartiles + execution quality. Runs on `opt_outcomes` only (excludes holdout) so the analysis dict has no last-7-day leakage.
3. **Calibrator (isotonic)** — fit attempted every cycle, **adopted into production only when it clears all three gates**: (i) per-fit **bootstrap-CI** lower bound > 0 (§2); (ii) beats the *current* calibrator's recency-weighted log-loss on the full cal pool by ≥ `LOG_LOSS_FLOOR` (0.005 nats); (iii) does not reduce Kelly-Sharpe vs the current calibrator on `cal_val`. If the current calibrator has drifted worse than identity it reverts to identity (or is replaced directly when the new fit beats identity on both log-loss and sizing). The §2 CI gate is necessary but **not sufficient**. Adoption applies the live calibrator in-memory immediately for production; the **weight backtests use the separate gate-reference calibrator** (Calibration window), not the just-adopted live one. The on-disk save is deferred to step 6.
4. **TAEvolver** — `ClaudeRecommender` (Anthropic with full analysis + directional table + structural-probe targets) or `LocalRecommender` (rule-based fallback) returns `{changes, manual_observations}`. The `claude_client` validator reroutes manual-only params `changes` → `manual_observations`. **Combined L6 weight changes are dropped if `Σ|w| × logit_scale` would breach ±0.25.**
5. **WeightOptimizer** — per-param walk-forward backtest; gate decisions live here.
6. **Deferred calibrator save** — only after `WeightOptimizer.save_config` commits. A crash before this line leaves new weights paired with the previous-session calibrator: mismatched but each a valid, coherent artifact. Saving the calibrator first risked a brand-new calibrator paired with stale weights — the worse half.

### Adoption gate (WeightOptimizer)

Per candidate change on the 4-fold walk-forward:

```
n_candidate_trades ≥ MIN_CANDIDATE_TRADES (100)
z = Δ_sharpe / JK_SE ≥ ADOPTION_Z_FLOOR (0.3)        # lag-1 autocorr-adjusted
JK_SE = sqrt((1 + 0.5 × sharpe²) / n) × sqrt(max(1, 1 + 2·ρ₁))
```

- **Soft abs floor.** `candidate_sharpe < min(0, baseline) − 0.05` is blocked — the loop can adopt a less-negative candidate during a regime shift (recovery), but not an outright collapse.
- **Fold-consistency floor** — `min(fold_sharpes) ≥ −0.10`. Magnitude-aware: a tiny dip is fine, a deep collapse rejects.
- **Regime-stratified veto** — activates per regime bucket once it has **≥8 trades** in the validation fold. Two branches share a "no regime degrades >0.10 Sharpe" floor: **(a)** candidate improves in ≥2 of 3 populated buckets; **(b)** dominant regime improves AND no other degrades >0.10.
- **Holdout confirmation** — after clearing the above, baseline vs candidate on the held-out 7-day pool (≥30 trades). `HOLDOUT_ADOPTION_MARGIN = max(0.02, ADOPTION_Z_FLOOR × holdout_jk_se)`; candidate must clear `baseline_h + margin`. `pipeline_info["holdout_active"]` stamped each cycle.

### Combined-holdout interaction check

When `≥2` changes adopt: one combined backtest on the holdout pool (same data, requires `≥ HOLDOUT_MIN_TRADES`). Each change here has already cleared per-change z-test, fold-consistency, soft-abs floor, regime veto, and per-change holdout confirmation — but two that pass alone can still interfere (shared logit budget, joint clamps).

```
margin = max(0.02, ADOPTION_Z_FLOOR × holdout_jk_se)
if combined_holdout_sharpe < baseline_holdout_sharpe + margin:
    back out the WHOLE batch
```

No iteration — the per-change gates already did directional filtering; the question is purely "does the joint set survive on fresh data at the same z-floor?" If not, drop everything; next cycle re-proposes individually with the directional table reflecting this.

### Crisis mode

Triggers on **either**:
- **(a)** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`), or
- **(b)** trailing-3-day Sharpe < 0 over ≥20 recent trades — catches sustained multi-day collapses the recent-50 smoothing masks.

- **≥3 consecutive crisis cycles** → halve `kelly_fraction`, floor **0.04** (intentionally below the 0.05–0.18 tunable range so crisis sizes more defensively than any optimizer-adoptable state; do not "fix" the discrepancy).
- **Restore on first non-crisis cycle** — original Kelly persisted in `crisis_state.json` before the cut, so a mid-pipeline crash can't compound the halving.
- **Optimizer defers `kelly_fraction` while halving is active.** `_run_weight_optimizer` reads `crisis_state.json` at entry; if `kelly_reduced=True`, any `kelly_fraction` candidate is marked `decision="deferred_crisis"` and skipped. The claim re-enters the directional table on the first non-crisis cycle but can't override the safety floor while crisis is engaged.

### Adaptive exploration + structural probes (`recommender_base`)

- **`EXPLORE_STEPS`** maps each tunable to a base step (e.g., `atr_sigma_ratio = 0.15`, `final_logit_clamp = 0.50`).
- **`_rule_exploratory`** ramps step size up when the directional table shows past probes returned `|bt_delta|` under the noise floor (empirical per cycle: `max(0.003, 0.3 × baseline_jk_se)`). Each dead direction adds +50% (cap 3.0×). Adoptions reset the loop.
- **`STRUCTURAL_PROBES`** fires once per `(param, value)` until evidence appears: `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` (counterfactual data backs a less-strict exit); L6 turn-on at `0.005` for all four weights (`log_atr_ratio`, `autocorr_signed_mag`, `flow_disagreement`, `liq_signed_sqrt`) so every closed-library feature gets ≥1 evaluation cycle.

Both recommenders call `_rule_structural_probes()` before the rotational `_rule_exploratory`.

### L6 directional bookkeeping

The optimizer captures `old_value` from `signal_engine.derived_weights[fname]` for `derived_*_weight` params (not `getattr(signal_engine, param)`, which returns `None` since L6 weights live in a dict). L6 probes populate the directional table every cycle.

## 12. What the pipeline can vs cannot touch

### Pipeline-tunable (`PIPELINE_PARAMS` in `polybot/config/param_registry.py`)

| Group | Params | Range |
|---|---|---|
| **L1 / volatility** | `atr_sigma_ratio` | 1.2–2.5 (highest leverage) |
| | `student_t_df` | 3–8 |
| | `min_atr` | 8.0–25.0 |
| **Logit amplifier** | `logit_scale` | 2.0–5.0 |
| **L2-L5 weights** | `regime_weight` (0.01–0.15), `flow_weight` (0.02–0.12), `spot_flow_weight` (0.01–0.15), `liquidation_weight` (0.01–0.10), `prev_margin_weight` (0.01–0.05) | per-param |
| | `momentum_weight` | 0.0–0.10 (magnitude only — sign is dead at L4) |
| **Indicator committee (L4)** | `weights` (RSI/MACD/Stochastic/OBV/VWAP dict) | each ≥ 0.05, renormalized to sum 1.0; adopted via the L4 backtest. Handled as a dict by the optimizer and `claude_client`, not a scalar `ParamSpec`. |
| **Sizing** | `kelly_fraction` | 0.05–0.18 |
| **Entry gates** | `min_edge`, `min_kelly`, `min_model_probability` | tight bands |
| **Exit** | `exit_edge_threshold` | −0.10..−0.03 |
| **Structural constants** | `regime_momentum_threshold` (0.08–0.25), `final_logit_clamp` (3.0–5.0), `l5_regime_damp_cap` (0.4–0.9), `atr_regime_shift_threshold` (0.40–0.80) | |
| **L6 derived weights** | `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_flow_disagreement_weight`, `derived_liq_signed_sqrt_weight` | 0.0–0.05 each; combined L6 hard-capped at ±0.25 logits |

### Manual-only (`MANUAL_ONLY_PARAMS`, validator reroutes `changes` → `manual_observations`)

- **Exit / hold magnitudes outside the curve:** `loss_cut_fraction`, `loss_cut_time_s`, `deep_loss_hold_threshold` — the backtest replays a single stored fill and can't re-simulate these branches; only `exit_edge_threshold` has a counterfactual path (§6).
- **Entry-timing envelope + flip hurdle:** `normal_fraction`, `late_max_penalty`, `flip_edge_premium` — backtest applies raw Kelly + entry gates only, modeling neither the time-of-window multiplier nor the flip hurdle, so changes yield zero delta (never adoptable).
- **Entry-time filters operator owns:** `max_edge`, `adverse_selection_threshold`, `edge_decay_threshold`.
- **Risk caps:** `max_concurrent_positions`, `max_bankroll_deployed`.
- **Circuit breaker:** `circuit_breaker.floor_pct`, `circuit_breaker.min_multiplier`.
- **Indicator periods:** `indicators.{rsi,macd,stochastic,ema,obv,atr}.*` — backtest replays stored scores at the active period; alternate periods need raw candles per snapshot.
- **SPRT:** `sprt.{alpha,beta,observation_interval_s,min_confidence}` — intra-window timing; backtest replays a single stored fill instant.
- **Schedule:** `trading_{start,end}_{hour_et,minute}`.

`is_manual_only(name)` is the single source of truth. If a param appears in both lists (operator error), tunable wins.

## 13. What it deliberately won't do

Guardrails (most enforce a decision made above; collected here so a future edit doesn't undo one by accident):
- No Gaussian (§2), no Binance resolution (§8), no big single bets (caps via `max_bankroll_deployed` / `max_book_fill_pct` — compounds via frequency).
- No pattern-based exit rules ("RSI > 80, sell") and no confidence override of a scalp — exit is pure edge + time-value math (§6).
- Don't hold a dead side for its binary residual when the calibrator's lowest-learned knot says ~0% — selling at market beats $0 expected (§6).
- `gain_pct = pnl/size` arithmetic, never `log_return`, single source across live + backtest + isotonic fit.
- Entry-edge math uses `GET /price?side=BUY` (executable), never raw book prices; books are walked only for FOK VWAP slippage. Never skip the fee (`rate × shares × p × (1 − p)`, `rate = 0.018` in `base.DEFAULT_FEE_RATE`).
- Don't bypass the circuit breaker. Don't delete `polybot/db/polybot_*.db`. Regime direction is `sign(last 1-min return)`, not `sign(prob−0.5)`. Layer adjustments are always logit space, never probability space.

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

`run_polybot.ps1` is the daily loop: starts 12:01 AM ET, stops trading 11:30 PM ET, runs the pipeline 11:45 PM ET, commits + pushes as it exits (~11:55 PM ET), then sleeps until the next 12:01 AM ET restart. The outer `while ($true)` survives auth errors but won't retry the same day — fix auth before midnight.

## 17. Invariants (what doesn't drift)

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded per-param priors.
- **Recency weighting** single source `RECENCY_DECAY_PER_DAY` in `pipeline_analytics.py` (`0.94^days_ago`, ~11-day half-life, inside the 60-day cutoff).
- **UTC everywhere** for storage; ET only for date-bucketing (gate_stats, daily rollup) and trading-window logic.
- **Daily rollup** runs inside the 11:45 PM ET pipeline (`rollup_old_outcomes` / `rollup_old_ghosts` / `rollup_old_counterfactuals`), bundling per-trade JSON into `rollup_YYYY-MM-DD.json`.
- **Shared model math** lives in `aux_layers.py` — the L1 vol-autocorrelation scale (`autocorr_vol_scale`), the flow-family combine (`combine_flow_family`), and the L3b/L3e regime normalization (`regime_vol_factor` + `compute_spot_flow_signal`/`compute_liquidation_signal`) — called by `signal_engine` (live) and `scheduler` (replay) alike, so the optimizer can't tune against a model production doesn't run.
- Also fixed (detailed where cited): `model_probability_raw` (§10), `gain_pct = pnl/size` never `log_return` (§13), L6 library closed (§2/§11), `edge_decay.deltas` + `adverse_kelly_mult` + aux-fields-`None`-when-stale (§10), atomic open/close (§5), per-mode DB with shared `memory/` (§14).

## 18. Discord

`!status` `!history [n]` `!positions` `!performance` `!pause` `!resume` `!session` `!agents` `!lessons` `!clear [trades|control|all]` `!commands`

## 19. Persistence

`memory/` (outcomes, counterfactuals, ghosts, pipeline_*, calibration), the per-mode SQLite DB, and `settings.yaml` are git-tracked. `run_polybot.ps1` commits + pushes immediately after the pipeline exits (~11:55 PM ET), then sleeps until 12:01 AM ET for the next session.
