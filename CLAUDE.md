# PolyBot

5-min BTC Up/Down trader for Polymarket. Computes P(Up) via a 7-layer model, enters when edge clears noise + Kelly justifies size, then re-evaluates each tick: hold to $1 or scalp.

**This file is the single source of truth — update it in the same commit as any behavioral change.** The pipeline auto-tunes the numeric knobs in §12 against the realized-fill backtest; everything else (model math, gates, sizing, exits, pipeline mechanics, telemetry) is changed by hand, with care and tests.

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
.\run_polybot.ps1                         # daily cycle: trade -> pipeline -> commit -> restart
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

# Part A — Trading Logic (§1-9)

How P(Up) is formed (§2 — the L1-L6 stack + isotonic calibration) and how the bot gates, sizes, orders, exits, flips, resolves, handles losses (§1, §3-9).

## 1. What you're betting on

Every 5 min, Polymarket runs a market: will BTC close higher or lower than at the window's start? Two sides — **Up** / **Down** — each an ERC-1155 token trading $0-$1; winning side pays $1/share, loser $0. Chainlink's BTC/USD oracle is the official resolution source; Polymarket's Gamma API mirrors it for the slug feed.

The bot picks side, size, when to scale in, and when to sell early. Two modes, same engine/gates/telemetry:

- **`paper`** — realism shim: real CLOB books, FOK semantics, convex slippage, configurable network-fail/latency jitter, $1 min, tick-size snapping. Bankroll in a paper SQLite DB.
- **`live`** — `py-clob-client-v2` FOK orders against the real CLOB, wrapped in `LiveTrader`; verifies USDC balance + allowance before the first order.

Schedule (`run_polybot.ps1`) in §16.

## 2. How the bot forms an opinion

Per window, stack evidence in logit space, then sigmoid + isotonic calibration. Every layer except L1 contributes via `weight * logit_scale * signal` (`logit_scale` = global amplifier, default 4.0). Final logit clamped to +/-`final_logit_clamp` (default 4.0) -> prob in [0.018, 0.982].

### L1 — Student-t CDF (core)

"How far is BTC from the strike vs. how much it typically moves in the time remaining?"

```
ac         = clamp(regime, +/-0.5)               # regime = lag-1 autocorr (shared with L2)
vol_scaled = (max(atr, atr_floor) / atr_sigma_ratio) * sqrt(minutes_remaining) * sqrt((1+ac)/(1-ac))
z          = (btc_price - strike) / vol_scaled
t_scale    = sqrt(df / (df - 2))
prob_up    = StudentT_CDF(df, z * t_scale)
```

- **`student_t_df`** — default 5, clamped >=3 (df <= 2 -> undefined variance + a t_scale discontinuity injecting a 1.0->1.73 jump). Gaussian undersizes BTC's fat tails (kurtosis 6-8).
- **`atr_sigma_ratio`** — default 1.3, pipeline-tunable 1.2-2.5. The single highest-leverage knob.
- **Autocorrelation-scaled vol** — BTC 1-min returns aren't i.i.d., so `vol_scaled` is multiplied by the AR(1) terminal-SD ratio `sqrt((1+ac)/(1-ac))`, `ac` = lag-1 autocorr (clamped +/-0.5, the value L2 consumes). Positive autocorr (trend) widens spread -> P(Up) toward 0.5; negative (mean-reversion) tightens. `regime` computed once, shared by L1/L2/L4/L5.
- **ATR floor**, dynamic: `max(min_atr, 0.30 * rolling_20)`; when `rolling_20 / long_term_200 < atr_regime_shift_threshold` (default 0.60) it widens to `max(base_floor, long_term_mean * threshold * 0.30)` (anti-overconfidence on vol collapse). `rolling_20`/`long_term_200` buffer the last 20/200 ATR **samples**, one per `compute_probability` call (every decision tick — entry *and* hold — **not** per 1-min candle).
- **L1 clip** at `1e-6` -> logit +/-13.8, past the final +/-4 clamp (the clamp is the precision floor, not the clip).
- `btc_price` from `_fastest_btc_price`: **Coinbase WS only (<2s)** — lowest-latency feed and the venue Chainlink resolves against. No Binance fallback (a divergent transient print would flip P(side) on a tick the resolver never sees). Coinbase stale (>=2s) -> decision skipped, not zeroed (Binance spot read only to log the cross-venue gap — §9).

### L2 — Regime

```
last_return = (live_btc_price - closes[-2]) / closes[-2]
regime      = lag1_autocorr(closes, regime_lookback)
direction   = sign(last_return)
logit += regime * direction * regime_weight * logit_scale
```

Single `lag1_autocorr` helper in `polybot/core/returns.py`; `SignalEngine.compute_regime_factor` and `RegimeDetector` both delegate to it. `last_return` mixes the live Coinbase tick against the most recent fully-closed Binance candle (eliminates the L1/L2 minute-boundary mismatch).

### L3 — CLOB flow

```
book_imbalance = (top-5 bid_up + top-5 ask_down - top-5 bid_down - top-5 ask_up) / total
trade_flow     = recency_weighted_net_flow (120s window, 30s half-life decay)
flow_score     = 0.6 * book_imbalance + 0.4 * trade_flow
logit += flow_score * flow_weight * logit_scale     # combined with L3b (redundancy-discounted)
```

Top-5 levels each side by best price.

### L3b — Spot CVD (Coinbase)

Coinbase is the largest US-volume BTC venue and where Chainlink resolves. Per-trade CVD + taker ratio from the WS L2 + match feed:

```
cvd_60s    = signed Coinbase BTC volume over 60s
vol_factor = clamp(atr / atr_long_term_mean, 0.5, 3.0)   # current volatility regime
cvd_comp   = tanh(cvd_60s / (30 * vol_factor)) * 0.8     # saturation scale tracks regime
taker_comp = (taker_60s - 0.5) * 0.4            # only when >=20 trades in window
spot_flow  = clamp(cvd_comp + taker_comp, +/-1)
logit += spot_flow * spot_flow_weight * logit_scale     # combined with L3 (redundancy-discounted)
```

`compute_spot_flow_signal` in `polybot/core/aux_layers.py` (live + replay share it, can't drift). `vol_factor` scales the `tanh` saturation point to regime — a fixed scale saturates in high-volume regimes, losing resolution when flow is most informative.

**CVD-acceleration gate** (sizing-time guard, not L3b magnitude): `coinbase_feed.get_cvd_acceleration(recent_s=15, baseline_s=45)` requires >=10 recent trades. Skips entry when `|spot_flow| >= 0.20` **and** `spot_flow * cvd_accel < 0` (signal already peaked).

**Flow-family combine (L3 + L3b).** Book flow and spot CVD watch the *same* BTC move, so adding them double-counts agreement (the high-conviction case driving the largest sizing). Per direction: strongest contribution at full weight, same-direction corroborator discounted by `_FLOW_REDUNDANCY` (0.5); opposing signals offset naturally (no discount — disagreement is information). Combined (both legs) clamped to **+/-0.50 logits**, so neither flow leg dominates L1.

### L4 — Indicator committee (polarity-split, regime-conditional)

Five indicators (RSI, MACD, Stochastic, OBV, VWAP) from the 1-min candle buffer, raw `score` consumed directly (no adaptive normalizer). Groups: **Mean-revert** = RSI, Stochastic, VWAP; **Trend-confirm** = MACD, OBV. Each dot-products with its L4 weights (`weights` dict), then mixes by regime via `t = tanh(regime / regime_momentum_threshold)`:

```
contrarian_mult    = (1 - t) * 0.5
trend_confirm_mult = 0.5 + 0.5 * max(0, t)
score = mean_revert * contrarian_mult
      + |mean_revert| * direction * max(0, t)        # trend regime: polarity FLIPS to sign(last_return)
      + trend_confirm * trend_confirm_mult
score = clamp(score, +/-1)
logit += score * effective_momentum_weight * logit_scale
```

`effective_momentum_weight` = unsigned magnitude scaled `0.5x`->`1.5x` by `|tanh(regime / regime_momentum_threshold)|` (no cliff at the threshold). The `momentum_weight` bound (0-0.10) caps magnitude; **sign is dead at the L4 level** — reborn per-group inside `compute_momentum` from regime + realized direction (revert `t<0`: mean-revert keeps its contrarian sign at full power, trend-confirm dampened; trend `t>0`: mean-revert's sign **replaced** by `sign(last_1min_return)` continuation, trend-confirm full). `regime_momentum_threshold` default 0.15, tunable 0.08-0.25.

### L5 — Previous-window margin carry

```
logit += tanh(prev_resolution_margin / max(atr, 1)) * prev_margin_weight * logit_scale
       * (1 - min(l5_regime_damp_cap, |regime|))
```

Dampener (orthogonality patch): when `|regime|` is high L2 already encodes the same drift, so L5 contributes only its orthogonal portion. `l5_regime_damp_cap` default 0.7, tunable 0.4-0.9. `prev_resolution_margin` persists with a `saved_at` timestamp in `memory/state/prev_resolution_margin.json`; zeroed if older than 30 min on load.

### L6 — Derived feature library (closed)

A closed library of **3 bounded transforms** of state already tracked by `compute_probability` (`polybot/core/derived_features.py`). Every weight defaults to **0.0** — the layer is dead until the pipeline raises one off zero with evidence.

| Feature | Formula | Notes |
|---|---|---|
| `log_atr_ratio` | `clip(log(ATR_short / ATR_long), +/-1.5)` | Vol regime expansion (+) or collapse (-) |
| `autocorr_signed_mag` | `regime * tanh(last_return * 100)` | Direction-aware momentum strength |
| `flow_disagreement` | `tanh(flow + spot_flow)` | Direction-aware flow consensus |

```
l6_total = sum(derived_weights[name] * logit_scale * feature(ctx))
l6_total = clamp(l6_total, +/-L6_LOGIT_CAP)        # +/-0.25 logits
logit += l6_total
```

Cap enforced at the call site, and the **`claude_client` validator drops any L6 weight-change set whose `sum(|w|) * logit_scale` would breach `L6_LOGIT_CAP`** — unbreakable from either side. Adding a feature requires code in `derived_features.py` plus a `ParamSpec` row.

### Calibration (isotonic) — sole overconfidence correction

`IsotonicCalibrator` in `polybot/core/calibrator.py`, identity by default. Fits the last 7 days of trades (`>=150 samples` default; pool from the calibration train split, >=75 in train else skipped).

Adoption = **single OOB bootstrap-CI gate**: the lower-80% bound of weighted log-loss improvement vs identity, across **300 OOB resamples** with per-bootstrap weight renormalization, must be strictly positive. RNG seeded from `time.time_ns()` each fit. Pre-CI **range check**: `y_min <= 0.50` and `y_max >= 0.55` (else rejected without bootstrapping).

`last_fit_diagnostics` (`oob_ci_lower_nats`, `oob_ci_median_nats`, `n_samples`, `bootstrap_n_completed`, `y_min`, `y_max`, `decision`) is stamped to `pipeline_info["cal_info"]["fit_diagnostics"]` on every `fit()` reaching the bootstrap stage (both `"adopted"` and `"rejected_ci"`). Structural early-rejects (sample count, zero-weight, sklearn exception, range check) return `False` without stamping; visible in the reject-site log line.

`lowest_learned_prob` (lowest `y_thresholds_[0]` output) is the "dead side" floor consumed by `evaluate_hold` — §6.

## 3. Entry gates

Edge = `calibrated_model_prob - market_price`. **All** must pass; any single failure -> skip the tick.

| Gate | Threshold | Source |
|---|---|---|
| Chosen-side `prob` | >= `min_model_probability` (default 0.56) | `SignalEngine.evaluate` |
| `edge` | >= `min_edge` (default 0.04, scaled by flip premium — §7) | `SignalEngine.evaluate` |
| `Kelly` (fee-aware) | >= `min_kelly` (default 0.01); `b_eff = b * (1 - fee_rate)` | `SignalEngine._kelly` |
| Spread either side | `spread/2 + DEFAULT_FEE_RATE <= max_spread` (default 0.10) | `_fetch_market_prices` |
| Book depth | both-sides-thin gate first (>= `min_book_depth_usd = $50` on at least one side); chosen-side depth must also clear it | `_evaluate_signal_and_enter` |
| Price sum | `price_up + price_down in [0.98, 1.02]` (cross-book no-arb) | `_fetch_market_prices` |
| Book freshness | both sides' WS BBO <= `_WS_STALE_S = 10s` old | `clob_ws.both_books_fresh` |
| `edge <= max_edge` | default 0.20 — wider edge = stale phantom price | `_evaluate_signal_and_enter` |
| ATR gate | ATR >= 5th-percentile (lower-bound only) | `IndicatorEngine.atr` |
| SPRT | not `SKIP`; not opposing the chosen side when conf > 60% with >=6 obs | `SPRTAccumulator` |
| Adverse-selection hard skip | `adverse_rate_at_30s >= adverse_selection_threshold` (default 0.80) -> reject | `AdverseSelectionMonitor` |
| Edge-decay | mean 15s post-fill drift (30-min lookback) >= `edge_decay_threshold` (default -0.05). Inactive until >=15 resolved fills in lookback | `AdverseSelectionMonitor.get_recent_decay_mean` |
| Layer disagreement | reject when `compute_momentum` opposes the chosen side (>0.5 magnitude) and `edge * 0.5 < min_edge` | inline |
| CVD deceleration | skip if `\|spot_flow\| >= 0.20` AND `spot_flow * cvd_accel < 0` | inline |
| Regime | skip when `RegimeDetector` classifies `quiet` | `RegimeDetector.classify` |
| Net-edge after slippage | `edge - price * est_slip >= min_edge` | `slippage_pct` |
| Pre-submit re-check | walk current ask ladder for FOK VWAP; recomputed net edge must clear `[min_edge, max_edge]`. Fresh-BBA fallback (book unavailable) checks net edge vs `min_edge` and **gross** edge vs `max_edge` (`max_edge` is a stale-phantom guard, slippage is execution cost) | `compute_buy_vwap` |
| Min order size | size >= $1 (Polymarket CLOB floor; paper mirrors live) | inline |
| Feed staleness | Coinbase <= 30s, Chainlink <= 60s, Binance aggTrade <= 30s, Binance kline <= 45s | inline |

**Adverse selection is sizing-side, not entry-side.** Above the soft penalty floor (`adverse_penalty_floor` default 0.45):

```
kelly_mult = max(adverse_penalty_min,
                 1 - adverse_penalty_slope * max(0, adverse_rate_at_30s - adverse_penalty_floor))
           = max(0.30, 1 - 1.5 * max(0, adverse_rate - 0.45))
```

So at `adverse_rate = 0.85` the multiplier collapses to 0.30 but the trade still fires — only the hard 0.80 threshold blocks entry. The 30-min lookback is **Bayesian-shrunk to a neutral prior** (n=10, rate=0.5).

Every rejection of a **pipeline-tunable or signal-derived gate** feeds a **ghost** into `GhostTracker` (full L1-L5 inputs + aux microstructure); ghosts resolve at the window's close and feed the backtest pool (raising a gate filters the same ghosts from baseline + candidate equally, lowering includes them). Non-tunable structural gates (`regime` quiet skip, chosen-side `thin_book_depth` vs operator-owned `min_book_depth_usd`, `min_size` $1 floor) reject without ghosting, so the pipeline can't adopt a change that re-includes them.

## 4. Sizing

Hard caps first, soft multipliers second. Then a $1 floor and a real round-trip net-edge sanity check.

```
raw_kelly_size = bankroll * signal.kelly_size
size           = raw_kelly_size * circuit_breaker.kelly_multiplier * time_mult
size          *= consensus_mult * adverse_kelly_mult            # both <= 1.3 / <= 1.0
size          *= concurrent_multiplier(side, market, opens)     # correlation-aware
size           = min(size, bankroll * max_bankroll_deployed)
size           = min(size, side_depth * max_book_fill_pct)
if size < 1.0: skip                                              # CLOB floor
```

### Soft multipliers

- **Circuit breaker** — tier-locked floor at $100/150/200/300/400/600/800/1000/1500/2000/3000/4000/6000/8000/10000. Floor = locked_tier * `floor_pct` (default 0.85). Kelly multiplier: `1.0x` at/above the locked tier; `min_multiplier` (default 0.40) at/below the floor; **concave (sqrt) interpolation** between (a $100/$85 midpoint gives ~0.82x vs 0.70x linear). Tier never resets down; ratchets up when bankroll crosses a new tier.
- **Time multiplier** — `compute_time_multiplier`. First `normal_fraction` of the window (default 60% -> 0-180s): full Kelly. After: penalty scales by `(1 - conviction)` up to `late_max_penalty` (default 0.30).
- **Consensus multiplier** — `compute_signal_consensus` counts how many of `flow`, `spot_flow`, `cvd_accel_norm` agree with the chosen side (dropping signals below `consensus_dead_zone = 0.05`): >=80% -> 1.30x, >=60% -> 1.00x, >=40% -> 0.80x, else 0.60x.
- **Concurrent multiplier (correlation-aware)** — `polybot/execution/correlation.py`. Adjacent windows share regime; same-side bets correlated, opposite-side hedged. rho is a **fixed prior**: `+0.75` same-side, `-0.25` opposite-side (`_CORR_SAME_SIDE`, `_CORR_OPPOSITE_SIDE`). Same-market triggers flip logic, not this multiplier. Worst rho across open positions: > 0.6 -> 0.35, > 0.3 -> 0.55, > -0.2 -> 0.70, <= -0.2 -> 0.90.

### Hard caps

- `bankroll * max_bankroll_deployed` (default 0.80)
- `side_depth * max_book_fill_pct` (default 0.50) — under the thin-CLOB upstream gate requiring at least one side >= $50 depth; if the chosen side is the empty leg of a one-sided book, that's an explicit skip.

## 5. Placing the order

FOK via `py-clob-client-v2`. 3 retries with jittered exponential backoff. HTTP/2 keepalive ping every 5s (against a 60s `keepalive_expiry` pool, so the connection never lapses between pings).

Live mode boot:
1. `verify_auth` checks `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` are set and the Safe is reachable.
2. USDC balance fetched. Allowance = `min(allowances[spender] for all spenders) / 1e6`.
3. Required allowance: `max_single * max_concurrent_positions * 10` (10x safety), where `max_single = bankroll * kelly_fraction` (a max Kelly-sized single bet — **not** the `max_bankroll_deployed` hard cap). If allowance < required -> `AuthError`, clean exit, **no retry that day** (the outer `while ($true)` restarts next midnight ET; fix allowance before then).
4. **Mid-session allowance recheck** — every `_ALLOWANCE_RECHECK_EVERY = 10` submits, re-fetch and warn (or fail) if revoked/run down.

Paper boot: skips auth, same `BaseTrader` open/close path, `PaperTrader` simulates book-walk fill + latency + occasional FOK rejection.

Per-trade DB write is atomic (single SQLite transaction): `open_position_and_debit_bankroll`, `close_position(... bankroll_delta=... | new_bankroll=...)`. `bankroll_delta` for a relative credit (scalp), `new_bankroll` for absolute on resolution.

**`fill.fill_size` is always USDC notional** — BUY = requested $ size; SELL = `shares * fill_price`. Logs show the actual fill price (book-walk for paper, FOK for live), never the signal-moment price.

## 6. Holding vs scalping (`evaluate_hold`)

Every tick while we hold, re-run the full model and decide HOLD vs EXIT. `holding_edge = model_prob - market_price_for_side`. The exit threshold blends two curves:

```
itm_ref            = market_mid_for_side or market_price_for_side
itm_depth          = max(0, (itm_ref - 0.5) / 0.5)
deep_loss_floor    = exit_edge_threshold * (1 + 0.5 * itm_depth)
optimal_threshold  = ExitBoundary.compute_exit_threshold(seconds_remaining, entry_price,
                                                         fee_rate, market_price_for_side)
effective_threshold = (1 - itm_depth) * max(deep_loss_floor, optimal_threshold)
                    + itm_depth * min(deep_loss_floor, optimal_threshold)
```

ATM trusts the `ExitBoundary` curve; deep ITM weights toward the more patient floor.

**`ExitBoundary.compute_exit_threshold`** in `polybot/core/exit_boundary.py`. Binary-payoff math (payoff kinks at $0/$1, unlike European options):
- **Deep ITM (`p >= 0.70`):** base time value * `(1 - itm_depth * 0.5)` + resolution premium (`itm_depth * 0.05 * (1 - minutes/5)`) — wants to hold for $1.
- **Deep OTM (`p <= 0.30`):** base time value * `(1 - otm_depth * 0.7)` + urgency premium ramping in the last 2 minutes — cut losses.
- **ATM:** `0.07 * sqrt(minutes) * 0.4 + fee_cost`.

OTM urgency can push the threshold **positive**, forcing exit even when the model is optimistic — final clamp `[-0.30, urgency_premium > 0 ? 0.30 : -0.01]`.

### Exit branches (in order)

1. **Loss-cut** — `entry_price > 0 AND market_price < entry * loss_cut_fraction (0.65) AND seconds_remaining < loss_cut_time_s (90s) AND BTC on the wrong side of strike by >= 0.5*ATR`. The 0.5*ATR cushion is the whipsaw guard: when BTC sits on the strike and the contract flickers $0.05-$0.70 on thin prints, we don't lock in the bottom. Engine stamps `last_loss_cut_event` in {`""`, `"fired"`, `"whipsaw_blocked"`} per call; the loop counts those into `gate_stats`.
2. **Deep-loss hold** — `holding_edge < deep_loss_hold_threshold (-0.10) AND market < entry AND model_prob > calibrator.lowest_learned_prob`. The binary residual ($1 if we win) beats locking in the loss at a depressed price. **Override:** when `model_prob <= lowest_learned_prob` the calibrator says this side won essentially never at this raw prob, so selling at market beats ~$0 expected. Identity calibrator returns `0.0` (override disabled); refits update it.
3. **Scalp** — `holding_edge <= effective_threshold` (and not in the deep-loss hold zone), **unless BTC is within 0.5*ATR of the strike on the wrong side** — same whipsaw cushion as branch 1.
4. **Hold** — otherwise.

No confidence override. Math says exit, it exits.

`exit_edge_threshold` is the only operator-touchable exit knob and the **only exit knob the pipeline tunes** (range -0.10..-0.03). On a proposed change, the backtest replays the **counterfactual tracker**'s recorded scalp outcomes through the new threshold: trades whose `holding_edge_at_scalp` exceeds the candidate threshold are re-priced using the matched hold-to-resolution `gain_pct` (`pnl/size` — §13).

## 7. Flip trading

After a scalp, the bot can re-enter the same window — including the opposite side — **unboundedly** (one position at a time). Each re-entry clears the standard entry gates plus a flip premium:

```
flip_premium = flip_edge_premium + 0.005 * max(0, flip_count - 2)
spread_cost  = spread + 2 * fee_rate * p * (1 - p)            # real round-trip
flip_hurdle  = min_edge + max(flip_premium, spread_cost)
```

Flips 1-2 pay only the base `flip_edge_premium` (default 0.015); flip 3 pays +0.5pp; flip 4 pays +1.0pp; etc. Or the actual round-trip spread+fee cost, whichever is higher — flips can't churn on micro-edge that won't survive the round trip.

## 8. Resolution

- **Early scalp** — sold before expiry into the book. The bot keeps the difference; counterfactual tracker logs what hold-to-resolution would have paid.
- **Resolution** — window closes; Chainlink decides; winner paid binary **$1**, loser **$0**, credited atomically. Exit price decided **oracle-first** (`_resolved_exit_price`): Gamma's `event_metadata` (Chainlink `final_price` vs `price_to_beat`) is authoritative; absent that, a *coherent* resolved CLOB book (closed, prices sum ~1, one side at an extreme) is the fallback. An incoherent book (stale/phantom print) is rejected, not trusted.
- **Never resolves from Binance** — it can diverge from Chainlink by $20-$200 at the close.
- **Chainlink orphan fallback** — if Gamma stays silent ~30 min after entry, the bot reads Chainlink directly via `chainlink_feed` and resolves locally. Restart safety: the position stays `open`/`pending_resolution` in the DB (re-evaluated on boot), not a file. `memory/state/orphan_positions.json` is written by a *separate* startup check (`LiveTrader.detect_orphan_positions`) flagging on-chain positions the DB doesn't know about.

## 9. Built-in loss handling

The loss-handling stack lives in §3 (adverse-selection, edge-decay, regime quiet-skip, feed-staleness skips — a Coinbase gap >=2s skips the L1 decision, no Binance fallback), §4 (circuit breaker), and §1 (cross-venue gap logging). Facts not stated there:
- Circuit-breaker streak counters (3 losses / 3 wins) drive Discord alerts only, never sizing.
- `AdverseSelectionMonitor` state persisted to `memory/state/adverse_state.json` on every fill so restarts inherit the rolling window.
- **CLOB WS heartbeat** — PING every 10s, force-reconnect if no PONG within 25s.

---

# Part B — Operational Layer (§10-19)

Telemetry, nightly pipeline, param registry, layout, data sources, run commands, invariants, Discord, persistence.

## 10. Live execution telemetry

### Per-decision `trade_context` (stamped into outcome + ghost)

- **Entry facts:** `btc_price`, `strike_price`, `seconds_remaining`, `market_price_up`, `market_price_down`, `closes_tail` (last 2 closes, so the L6 backtest can reconstruct `last_return`).
- **Probabilities:** `model_probability` (post-calibrator), `model_probability_raw` (pre-calibrator — stored separately so re-fits don't compound).
- **Composite signals:** `flow_score`, `spot_flow_signal`, `regime_autocorr`, `regime_direction`, `prev_resolution_margin`.
- **Microstructure aux:** `coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`. Each **signal** field is `None` (never `0.0`) when its feed is missing/stale, so the pipeline tells "feed cold" from "real zero". `coinbase_taker_n` is a **count**: `0` (not `None`) when cold, so its sole consumer (requires `n >= 20`) contributes nothing either way.
- **SPRT:** `sprt_confidence`, `sprt_status`.
- **Sizing audit:** `adverse_rate_at_30s`, `adverse_kelly_mult` (actual Kelly multiplier applied — enables per-bucket retrospective Sharpe), `entry_phase`, `flip_count`, `is_flip`.

**Ghost rejections share the same schema** (incl. `entry_phase`, `flip_count`, `is_flip` stamped at gate-fire time), so by-phase and flip-segmented bias cards see the full ghost population.

### `edge_decay.deltas` (merged at close, persisted to outcome JSON)

Side-signed post-fill mid drift at **5/10/15/30/60s**, captured by `AdverseSelectionMonitor` keyed by `position_id`, merged at close. The 15s mean over a 30-min lookback drives the live `edge_decay_threshold` gate. Null windows = trade closed before that checkpoint resolved.

### Gate-skip stats (`memory/state/gate_stats*.json`)

Two files. Live counts persist to `state/gate_stats_current.json` on every resolution (mid-day restarts reload it); at the first record of a new ET day the finished day folds into the lifetime accumulator `state/gate_stats.json` (`counts` + `days_accumulated` + first/last day) and the current file resets. The nightly pipeline reads the current-day file. Includes `loss_cut_fired`/`loss_cut_whipsaw_blocked` to audit the 0.5*ATR cushion.

### Feed staleness telemetry

`polybot/feeds/_staleness.StalenessTracker` persists per-feed P50/P95/P99 inter-arrival gaps to `polybot/memory/state/feed_staleness.json` every 60s. `polybot/feeds/_socket.enable_nodelay` verifies `TCP_NODELAY` on every WS connect. `BiasDetector` reads `feed_staleness.json` into the nightly card as `feed_health` (per-feed `{n, p50, p95, p99, max}` + a `degraded_p95_ge_10s` list), so a degrading feed is surfaced, not misattributed to layer signals.

## 11. Nightly learning pipeline

Runs 23:45 ET (via `run_polybot.ps1`). Five steps; calibrator save deferred to the end so on-disk state stays coherent across crashes.

### Dataset boundaries

- Active dataset bounded to the **last 60 days** before splits (older trades came from probability machines that no longer exist); falls back to full history only if the window has <500 trades.
- Walk-forward folds inside that window: train 60% / test split across `[60:70][70:80][80:90][90:100]` (each test fold genuinely OOS).
- **7-day holdout** — last 7 days excluded from all folds AND the evolver's context; the two calibrators below exploit this disjoint window so the holdout-confirmation gate scores candidates on trades the gate calibrator never saw. **Young-dataset fallback:** when the dataset is younger than the holdout window the rolling 7-day cut would swallow every trade, leaving the pre-holdout (`opt`) pool below the 200-trade learning floor. The split therefore disables the holdout (full pool → analysis + evolver, `holdout_active=False`) **before** the analysis dict is built — the recommender keys off `analysis["overall"]["total_trades"]`, so building it on an empty `opt` pool would silently zero all learning despite hundreds of trades. The OOS confirmation is forfeited for that cycle (no separate pool exists); the optimizer's own walk-forward folds still gate adoption.
- **Realized fills only** — `gain_pct = pnl / size` from closed-trade outcomes, `pnl` already netting actual fee + fill price. No mid-price replay; candidates inherit the slippage any live trade paid.
- **Recency weighting** — `0.94^days_ago` (~11-day half-life) inside the window cutoff.
- **Backtest L1 ATR-floor fidelity (approximate).** Live advances the ATR buffers per decision tick; the backtest advances a local buffer once per stored trade (entry-only). `min_atr`/`atr_regime_shift_threshold` stay backtest-evaluable, and the approximation largely cancels in the baseline-vs-candidate delta. L6 features and the L3b `regime_vol_factor` read the faithfully-stamped `atr_rolling_20`/`atr_long_term_mean` from `trade_context`.

### Calibration window

Both calibrators fit on **real trades only** (ghosts excluded) — the calibrator changes live-trading probabilities, so it must learn from fills the bot actually took, not rejected ghosts.

**Two calibrators (decoupled masters)** — one can't serve both live trading (freshest data) and the OOS gate (a window the holdout never saw):

- **Live / production** — `IsotonicCalibrator.fit` on the **freshest `_CAL_WINDOW_DAYS` (~7d)**, applied to `signal_engine.calibrator` and saved. Goes through the full three-gate production adoption (stage 3).
- **Gate reference** — a separate fit on the window **immediately before the holdout** (days `[HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS]` back, set once per cycle as `self._gate_calibrator`). All weight-optimizer backtests score through **this** calibrator, never the live one, keeping the gate genuinely OOS. Adopted via `fit`'s bootstrap-CI gate; `None` (identity) when the window is too thin.

Each pool must hold **>=125 trades**, split 60/40 into `cal_train`/`cal_val`; `fit` uses **`min_samples=75`** (overriding the class default 150), so `cal_train` must be >=75, and the Kelly-Sharpe gate ((iii) in stage 3) needs **>=50** `cal_val`. Both arms of every weight comparison share the fixed gate-reference calibrator, so its mapping cancels in the adoption delta to first order.

### Stages (in order)

1. **PipelineTracker** — review of prior adoptions (7d/14d/30d realized Sharpe per adopted version); auto-revert anything that materially underperformed. Revert criterion is **symmetric with adoption** (`actual_sharpe < baseline - ADOPTION_Z_FLOOR * JK_SE`, same `_jk_se`/`ADOPTION_Z_FLOOR` on post-adoption realized Sharpe + trade count), preventing adopt->dip->revert->re-propose oscillation.
2. **BiasDetector** — per-indicator/side/edge-bucket/regime/time-of-window/phase/flip stats + edge-realization quartiles + execution quality. Runs on `opt_real` (the holdout-excluded pool with **ghosts filtered out**) so the analysis dict has no last-7-day leakage **and** no ghost pollution — a ghost is a rejected trade, so counting it in real-performance stats (WR, Sharpe card) conflates "how the strategy did" with "what it declined to do", and since ghosts carry a `gain_pct` but no `pnl` it would show a negative Sharpe beside positive P&L. Ghosts feed the optimizer's *backtest* pool (§3) and get their own `ghost_analysis` (`analyze_ghosts`/`by_gate`); they're excluded from every real-performance/display metric (BiasDetector, trend buckets, all-time card, daily report). The same separation holds for the all-time/daily Discord stats, which are real-trades-only.
3. **Calibrator (isotonic)** — fit attempted every cycle, **adopted into production only when it clears all three gates**: (i) per-fit **bootstrap-CI** lower bound > 0 (§2); (ii) beats the *current* calibrator's recency-weighted log-loss on the full cal pool by >= `LOG_LOSS_FLOOR` (0.005 nats); (iii) does not reduce Kelly-Sharpe vs the current calibrator on `cal_val`. If the current calibrator has drifted worse than identity it reverts to identity (or is replaced directly when the new fit beats identity on both). Adoption applies the live calibrator in-memory immediately; **weight backtests use the separate gate-reference calibrator**. On-disk save deferred to step 6.
4. **TAEvolver** — `ClaudeRecommender` (Anthropic with full analysis + directional table + structural-probe targets) or `LocalRecommender` (rule-based fallback) returns `{changes, manual_observations}`. The `claude_client` validator reroutes manual-only params `changes` -> `manual_observations` and drops combined L6 weight changes that would breach the +/-0.25 cap (§2).
5. **WeightOptimizer** — per-param walk-forward backtest; gate decisions live here.
6. **Deferred calibrator save** — only after `WeightOptimizer.save_config` commits. A crash before this line leaves new weights paired with the previous-session calibrator: mismatched but each a valid artifact (the reverse — new calibrator + stale weights — is the worse half).

### Adoption gate (WeightOptimizer)

Per candidate change on the 4-fold walk-forward:

```
n_candidate_trades >= MIN_CANDIDATE_TRADES (100)
z = delta_sharpe / JK_SE >= ADOPTION_Z_FLOOR (0.3)   # lag-1 autocorr-adjusted
JK_SE = sqrt((1 + 0.5 * sharpe^2) / n) * sqrt(max(1, 1 + 2*rho1))
```

- **Soft abs floor.** `candidate_sharpe < min(0, baseline) - 0.05` is blocked — the loop can adopt a less-negative candidate during a regime shift (recovery), but not an outright collapse.
- **Fold-consistency floor** — `min(fold_sharpes) >= -0.10` (magnitude-aware: a tiny dip is fine, a deep collapse rejects).
- **Regime-stratified veto** — activates per regime bucket once it has **>=8 trades** in the validation fold. Two branches share a "no regime degrades >0.10 Sharpe" floor: **(a)** candidate improves in >=2 of 3 populated buckets; **(b)** dominant regime improves AND no other degrades >0.10.
- **Holdout confirmation** — after clearing the above, baseline vs candidate on the held-out 7-day pool (>=30 trades). `HOLDOUT_ADOPTION_MARGIN = max(0.02, ADOPTION_Z_FLOOR * holdout_jk_se)`; candidate must clear `baseline_h + margin`. `pipeline_info["holdout_active"]` stamped each cycle.

### Combined-holdout interaction check

When `>=2` changes adopt: one combined backtest on the holdout pool (`>= HOLDOUT_MIN_TRADES`). Each already cleared its per-change gates (z-test, fold-consistency, soft-abs floor, regime veto, holdout confirmation) — but two that pass alone can still interfere (shared logit budget, joint clamps).

```
margin = max(0.02, ADOPTION_Z_FLOOR * holdout_jk_se)
if combined_holdout_sharpe < baseline_holdout_sharpe + margin:
    back out the WHOLE batch
```

No iteration; if it fails, drop everything — next cycle re-proposes individually with the directional table reflecting this.

### Crisis mode

Triggers on **either**:
- **(a)** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`), or
- **(b)** trailing-3-day Sharpe < 0 over >=20 recent trades — catches sustained multi-day collapses the recent-50 smoothing masks.

- **>=3 consecutive crisis cycles** -> halve `kelly_fraction`, floor **0.04** (intentionally below the 0.05-0.18 tunable range so crisis sizes more defensively than any optimizer-adoptable state; do not "fix" the discrepancy).
- **Restore on first non-crisis cycle** — original Kelly persisted in `state/crisis_state.json` before the cut, so a mid-pipeline crash can't compound the halving.
- **Optimizer defers `kelly_fraction` while halving is active.** `_run_weight_optimizer` reads `state/crisis_state.json` at entry; if `kelly_reduced=True`, any `kelly_fraction` candidate is marked `decision="deferred_crisis"` and skipped, re-entering the directional table on the first non-crisis cycle.

### Adaptive exploration + structural probes (`recommender_base`)

- **`EXPLORE_STEPS`** maps each tunable to a base step (e.g., `atr_sigma_ratio = 0.15`, `final_logit_clamp = 0.50`).
- **`_rule_exploratory`** ramps step size up when the directional table shows past probes returned `|bt_delta|` under the noise floor (empirical per cycle: `max(0.003, 0.3 * baseline_jk_se)`). Each dead direction adds +50% (cap 3.0x). Adoptions reset the loop.
- **`STRUCTURAL_PROBES`** fires once per `(param, value)` until evidence appears: `exit_edge_threshold in {-0.08, -0.05, -0.03}` (counterfactual data backs a less-strict exit); L6 turn-on at `0.005` for all three weights (`log_atr_ratio`, `autocorr_signed_mag`, `flow_disagreement`) so every closed-library feature gets >=1 evaluation cycle.

Both recommenders call `_rule_structural_probes()` before the rotational `_rule_exploratory`.

### L6 directional bookkeeping

The optimizer captures `old_value` from `signal_engine.derived_weights[fname]` for `derived_*_weight` params (not `getattr(signal_engine, param)`, which returns `None` since L6 weights live in a dict). L6 probes populate the directional table every cycle.

## 12. What the pipeline can vs cannot touch

### Pipeline-tunable (`PIPELINE_PARAMS` in `polybot/config/param_registry.py`)

| Group | Params | Range |
|---|---|---|
| **L1 / volatility** | `atr_sigma_ratio` | 1.2-2.5 (highest leverage) |
| | `student_t_df` | 3-8 |
| | `min_atr` | 8.0-25.0 |
| **Logit amplifier** | `logit_scale` | 2.0-5.0 |
| **L2-L5 weights** | `regime_weight` (0.01-0.15), `flow_weight` (0.02-0.12), `spot_flow_weight` (0.01-0.15), `prev_margin_weight` (0.01-0.05) | per-param |
| | `momentum_weight` | 0.0-0.10 (magnitude only — sign is dead at L4) |
| **Indicator committee (L4)** | `weights` (RSI/MACD/Stochastic/OBV/VWAP dict) | each >= 0.05, renormalized to sum 1.0; adopted via the L4 backtest. Handled as a dict by the optimizer and `claude_client`, not a scalar `ParamSpec`. |
| **Sizing** | `kelly_fraction` | 0.05-0.18 |
| **Entry gates** | `min_edge`, `min_kelly`, `min_model_probability` | tight bands |
| **Exit** | `exit_edge_threshold` | -0.10..-0.03 |
| **Structural constants** | `regime_momentum_threshold` (0.08-0.25), `final_logit_clamp` (3.0-5.0), `l5_regime_damp_cap` (0.4-0.9), `atr_regime_shift_threshold` (0.40-0.80) | |
| **L6 derived weights** | `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_flow_disagreement_weight` | 0.0-0.05 each; combined L6 hard-capped at +/-0.25 logits |

### Manual-only (`MANUAL_ONLY_PARAMS`, validator reroutes `changes` -> `manual_observations`)

- **Exit / hold magnitudes outside the curve:** `loss_cut_fraction`, `loss_cut_time_s`, `deep_loss_hold_threshold` — the backtest replays a single stored fill and can't re-simulate these branches; only `exit_edge_threshold` has a counterfactual path (§6).
- **Entry-timing envelope + flip hurdle:** `normal_fraction`, `late_max_penalty`, `flip_edge_premium` — backtest applies raw Kelly + entry gates only (no time-of-window multiplier, no flip hurdle), so changes yield zero delta (never adoptable).
- **Entry-time filters operator owns:** `max_edge`, `adverse_selection_threshold`, `edge_decay_threshold`.
- **Risk caps:** `max_concurrent_positions`, `max_bankroll_deployed`.
- **Circuit breaker:** `circuit_breaker.floor_pct`, `circuit_breaker.min_multiplier`.
- **Indicator periods:** `indicators.{rsi,macd,stochastic,ema,obv,atr}.*` — backtest replays stored scores at the active period; alternate periods need raw candles per snapshot.
- **SPRT:** `sprt.{alpha,beta,observation_interval_s,min_confidence}` — intra-window timing; backtest replays a single stored fill instant.
- **Schedule:** `trading_{start,end}_{hour_et,minute}`.

`is_manual_only(name)` is the single source of truth. If a param appears in both lists (operator error), tunable wins.

## 13. What it deliberately won't do

Guardrails (most enforce a decision made above; collected here so a future edit doesn't undo one by accident):
- No Gaussian (§2), no Binance resolution (§8), no big single bets (caps via `max_bankroll_deployed`/`max_book_fill_pct` — compounds via frequency).
- No pattern-based exit rules ("RSI > 80, sell") and no confidence override of a scalp — exit is pure edge + time-value math (§6).
- Don't hold a dead side for its binary residual when the calibrator's lowest-learned knot says ~0% — selling at market beats $0 expected (§6).
- `gain_pct = pnl/size` arithmetic, never `log_return`, single source across live + backtest + isotonic fit.
- Entry/exit edge uses the **executable CLOB book BBO** — best_ask to buy, best_bid to sell (what a FOK actually fills against), from the WS BBO with an HTTP `/book` fallback — never the mid. It deliberately does **not** use `GET /price` as primary (its negRisk cross-match returns phantom prices that spike near expiry); `/price?side=SELL` is only a sanity cross-check for a phantom WS bid on exit. The FOK ask-ladder is walked for VWAP slippage. Never skip the fee (`rate * shares * p * (1 - p)`, `rate = 0.018` in `base.DEFAULT_FEE_RATE`).
- Don't bypass the circuit breaker. Don't delete `polybot/db/polybot_*.db`. Regime direction is `sign(last 1-min return)`, not `sign(prob-0.5)`. Layer adjustments are always logit space, never probability space.

## 14. Project layout

```
polybot/
  main.py                      Trading loop, entry/exit/sizing orchestration
  config/                      settings.yaml, loader.py, param_registry.py (single source of truth)
  core/                        signal_engine, calibrator, order_flow, returns, regime,
                               exit_boundary, sprt, adverse_selection, derived_features,
                               aux_layers (compute_spot_flow_signal)
  feeds/                       coinbase_feed (primary BTC + CVD),
                               binance_feed (1m candles, ATR), binance_depth, binance_trades,
                               chainlink_feed (strike + resolution),
                               clob_ws, market_scanner, _socket, _staleness, _json
  indicators/                  rsi, macd, stochastic, obv, vwap, ema, atr + engine
  execution/                   base (BaseTrader, fee math), paper_trader, live_trader,
                               circuit_breaker (tiered floor), correlation
  agents/                      scheduler (orchestrator), outcome_reviewer,
                               counterfactual_tracker, ghost_tracker, bias_detector,
                               ta_evolver, weight_optimizer, pipeline_tracker, pipeline_analytics,
                               claude_client (validator), claude_recommender,
                               recommender_base (EXPLORE_STEPS, STRUCTURAL_PROBES),
                               local_recommender
  memory/                      records: outcomes/, ghost_outcomes/, counterfactuals/ (+ rollups);
                               calibration/ (isotonic_params.json);
                               state/ — rolling single-file state + logs: gate_stats.json
                               (lifetime accumulator) + gate_stats_current.json, adverse_state,
                               crisis_state, feed_staleness, fill_stats, latency_stats,
                               orphan_positions, prev_resolution_margin, cf_watchlist,
                               pipeline_history, pipeline_run_log, strategy_log.md.
                               Full layout centralized in paths.py (MEMORY_DIR override:
                               POLYBOT_MEMORY_DIR).
  discord_bot/                 monitoring + control commands (§18)
  db/models.py                 SQLite (positions, trade_history, bankroll, peak_bankroll).
                               Per-mode: polybot_paper.db / polybot_live.db. memory/ shared.
```

## 15. Data sources

| Source | Feed | What |
|---|---|---|
| Coinbase | `ticker` WS (BTC-USD) | Primary BTC price + per-trade CVD |
| Binance.com | `kline_1m` / `depth20@100ms` / `aggTrade` WS | Candles, ATR, depth, CVD-fallback |
| Polymarket CLOB | WS + `GET /price`, `/book`, `/spread`, `/tick-size` | Books, executable prices, spreads, tick snapping |
| Polymarket Gamma | `GET /events?slug=...` | Discovery + resolution (`event_metadata`) |
| Chainlink (via Polymarket RTDS WS) | `wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink` | Strike capture + resolution price |
| Anthropic | `claude-sonnet-4-6` SDK | Daily learning pipeline |

## 16. Running

Trading/pipeline/test commands in Quick Start. Live pre-flight: `python verify_keys.py` (verify Polymarket creds + USDC balance/allowance).

`run_polybot.ps1` is the daily loop: starts 12:01 AM ET, stops trading 11:30 PM ET, runs the pipeline 11:45 PM ET, commits + pushes as it exits (~11:55 PM ET), then sleeps until the next 12:01 AM ET restart. The outer `while ($true)` survives auth errors but won't retry the same day — fix auth before midnight.

## 17. Invariants (what doesn't drift)

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded per-param priors.
- **Recency weighting** single source `RECENCY_DECAY_PER_DAY` in `pipeline_analytics.py` (`0.94^days_ago`, ~11-day half-life, inside the 60-day cutoff).
- **UTC everywhere** for storage; ET only for date-bucketing (gate_stats, daily rollup) and trading-window logic.
- **Daily rollup** runs inside the 11:45 PM ET pipeline (`rollup_old_outcomes` / `rollup_old_ghosts` / `rollup_old_counterfactuals`), bundling per-trade JSON into `rollup_YYYY-MM-DD.json`.
- **Shared model math** lives in `aux_layers.py` — the L1 vol-autocorrelation scale (`autocorr_vol_scale`), the flow-family combine (`combine_flow_family`), the L3b regime normalization (`regime_vol_factor` + `compute_spot_flow_signal`) — called by `signal_engine` (live) and `scheduler` (replay) alike, so the optimizer can't tune against a model production doesn't run.
- Also fixed (detailed where cited): `model_probability_raw` (§10), `gain_pct = pnl/size` never `log_return` (§13), L6 library closed (§2/§11), `edge_decay.deltas` + `adverse_kelly_mult` + aux-fields-`None`-when-stale (§10), atomic open/close (§5), per-mode DB with shared `memory/` (§14).

## 18. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all]` `!session` `!pipeline` `!commands`

## 19. Persistence

`memory/` (records: outcomes, counterfactuals, ghosts; calibration; and `state/`: pipeline history/run-log + rolling state), the per-mode SQLite DB, and `settings.yaml` are git-tracked, committed + pushed by `run_polybot.ps1` as it exits (§16).
