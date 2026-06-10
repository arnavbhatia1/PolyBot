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
.\scripts\run_polybot.ps1                 # daily cycle: trade -> pipeline -> commit -> restart
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

Every 5 min, Polymarket runs a market: will BTC close higher or lower than at the window's start? Two sides — **Up**/**Down** — each an ERC-1155 token trading $0-$1; winning side pays $1/share, loser $0. Chainlink's BTC/USD oracle is the resolution source; Polymarket's Gamma API mirrors it for the slug feed.

The bot picks side, size, when to scale in, and when to sell early. Two modes, same engine/gates/telemetry:

- **`paper`** — realism shim: real CLOB books, FOK semantics (incl. live's retry-on-price-moved), convex slippage, configurable network-fail/latency jitter, $1 min, tick snapping, simulated residual sweep (scalp fee-headroom shares credited to bankroll like live's on-chain sweep, excluded from pnl like live's records). Bankroll in a paper SQLite DB.
- **`live`** — `py-clob-client-v2` FOK orders against the real CLOB via `LiveTrader`; verifies USDC balance + allowance before the first order.

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

- **`student_t_df`** — default 5, clamped >=3 (df<=2 -> undefined variance + a t_scale 1.0->1.73 jump; Gaussian undersizes BTC's fat tails, kurtosis 6-8). The CDF (`student_t_cdf`) and the clamp (`MIN_STUDENT_T_DF`) live in `aux_layers.py`, shared by live + replay so they can't drift.
- **`atr_sigma_ratio`** — default 1.3, tunable 1.2-2.5. The single highest-leverage knob.
- **Autocorrelation-scaled vol** — BTC 1-min returns aren't i.i.d., so `vol_scaled` is multiplied by the AR(1) terminal-SD ratio `sqrt((1+ac)/(1-ac))`, `ac` = lag-1 autocorr (clamped +/-0.5 **before** the sqrt). Positive autocorr (trend) widens spread -> P(Up) toward 0.5; negative (mean-reversion) tightens. `regime` computed **once**, shared by L1/L2/L4/L5.
- **ATR floor**, dynamic: `max(min_atr, 0.30*rolling_20)`; widens to `max(base_floor, long_term_mean*threshold*0.30)` when `rolling_20/long_term_200 < atr_regime_shift_threshold` (0.60) — anti-overconfidence on vol collapse. Buffers hold the last 20/200 ATR **samples**, appended once per `compute_probability` call (every tick — entry *and* hold — **not** per candle).
- **L1 clip** at `1e-6` -> logit +/-13.8, past the final +/-4 clamp (the clamp is the precision floor, not the clip).
- `btc_price` from `_fastest_btc_price`: **Coinbase WS only (<2s)** — lowest-latency and the venue Chainlink resolves against. No Binance fallback (a divergent transient print would flip P(side) on a tick the resolver never sees). Coinbase stale (>=2s) -> decision skipped, not zeroed (Binance read only to log the cross-venue gap — §9).

### L2 — Regime

```
last_return = (live_btc_price - closes[-2]) / closes[-2]
regime      = lag1_autocorr(closes, regime_lookback)
direction   = sign(last_return)
logit += regime * direction * regime_weight * logit_scale
```

Single `lag1_autocorr` helper in `core/returns.py`; `SignalEngine.compute_regime_factor` and `RegimeDetector` both delegate to it. `last_return` mixes the live Coinbase tick against the most recent fully-closed Binance candle (eliminates the L1/L2 minute-boundary mismatch).

### L3 — CLOB flow

```
book_imbalance = (top-5 bid_up + top-5 ask_down - top-5 bid_down - top-5 ask_up) / total
trade_flow     = recency_weighted_net_flow (120s window, 30s half-life decay)
flow_score     = 0.6 * book_imbalance + 0.4 * trade_flow
logit += flow_score * flow_weight * logit_scale     # combined with L3b (redundancy-discounted)
```

Top-5 levels each side by best price.

### L3b — Spot CVD (Coinbase)

Coinbase is the largest US-volume BTC venue and where Chainlink resolves. Per-trade CVD + taker ratio from the WS `ticker` feed (fires per trade with taker side):

```
cvd_60s    = signed Coinbase BTC volume over 60s
vol_factor = clamp(atr / atr_long_term_mean, 0.5, 3.0)   # current volatility regime
cvd_comp   = tanh(cvd_60s / (30 * vol_factor)) * 0.8     # saturation scale tracks regime
taker_comp = (taker_60s - 0.5) * 0.4            # only when >=20 trades in window
spot_flow  = clamp(cvd_comp + taker_comp, +/-1)
logit += spot_flow * spot_flow_weight * logit_scale     # combined with L3 (redundancy-discounted)
```

`compute_spot_flow_signal` in `core/aux_layers.py` (live + replay share it). `vol_factor` scales the `tanh` saturation point to regime — a fixed scale saturates in high-volume regimes, losing resolution when flow is most informative.

**CVD-acceleration gate** (sizing-time guard, not L3b magnitude): `get_cvd_acceleration(recent_s=15, baseline_s=45)` requires >=10 recent trades. Skips entry when `|spot_flow| >= 0.20` **and** `spot_flow * cvd_accel < 0` (signal already peaked).

**Flow-family combine (L3 + L3b).** Book flow and spot CVD watch the *same* BTC move, so naive addition double-counts agreement. Per direction: strongest contribution at full weight, same-direction corroborator discounted by `_FLOW_REDUNDANCY` (0.5); opposing signals offset naturally (no discount — disagreement is information). Combined clamped to **+/-0.50 logits** so neither leg dominates L1.

### L4 — Indicator committee (polarity-split, regime-conditional)

Five indicators (RSI, MACD, Stochastic, OBV, VWAP) from the 1-min candle buffer, raw `score` consumed directly. Groups: **Mean-revert** = RSI, Stochastic, VWAP; **Trend-confirm** = MACD, OBV. Each dot-products with its L4 `weights` dict, then mixes by regime via `t = tanh(regime / regime_momentum_threshold)`:

```
contrarian_mult    = (1 - t) * 0.5
trend_confirm_mult = 0.5 + 0.5 * max(0, t)
score = mean_revert * contrarian_mult
      + |mean_revert| * direction * max(0, t)        # trend regime: polarity FLIPS to sign(last_return)
      + trend_confirm * trend_confirm_mult
score = clamp(score, +/-1)
logit += score * effective_momentum_weight * logit_scale
```

`effective_momentum_weight` = unsigned magnitude scaled `0.5x`->`1.5x` by `|tanh(regime/regime_momentum_threshold)|` (no cliff). `momentum_weight` (0-0.10) caps magnitude; **sign is dead at the L4 level** — reborn per-group in `compute_momentum`: in a revert regime (`t<0`) mean-revert keeps its contrarian sign and trend-confirm is dampened; in a trend regime (`t>0`) mean-revert's sign is **replaced** by `sign(last_1min_return)` and trend-confirm runs full. `regime_momentum_threshold` default 0.15, tunable 0.08-0.25.

### L5 — Previous-window margin carry

```
logit += tanh(prev_resolution_margin / max(atr, 1)) * prev_margin_weight * logit_scale
       * (1 - min(l5_regime_damp_cap, |regime|))
```

Dampener (orthogonality patch): when `|regime|` is high, L2 already encodes the same drift, so L5 contributes only its orthogonal portion. `l5_regime_damp_cap` default 0.7, tunable 0.4-0.9. `prev_resolution_margin` persists with a `saved_at` timestamp in `state/prev_resolution_margin.json`; zeroed if older than 30 min on load.

### L6 — Derived feature library (closed)

A closed library of **3 bounded transforms** of state already tracked by `compute_probability` (`core/derived_features.py`). Every weight defaults to **0.0** — the layer is dead until the pipeline raises one off zero with evidence.

| Feature | Formula | Notes |
|---|---|---|
| `log_atr_ratio` | `clip(log(ATR_short / ATR_long), +/-1.5)` | Vol regime expansion (+) or collapse (-) |
| `autocorr_signed_mag` | `regime * tanh(last_return * 100)` | Direction-aware momentum strength |
| `flow_disagreement` | `tanh(flow + spot_flow)` | Direction-aware flow consensus |

```
l6_total = clamp(sum(derived_weights[name] * logit_scale * feature(ctx)), +/-L6_LOGIT_CAP)   # +/-0.25
logit += l6_total
```

Cap enforced at the call site, and the `claude_client` validator drops any L6 weight-change set whose `sum(|w|)*logit_scale` would breach `L6_LOGIT_CAP` — unbreakable from either side. Adding a feature requires code in `derived_features.py` plus a `ParamSpec` row.

### Calibration (isotonic) — sole overconfidence correction

`IsotonicCalibrator` in `core/calibrator.py`, identity by default. Fits the last 7 days of trades (pool from the calibration train split, >=75 in train else skipped).

Adoption = **single OOB bootstrap-CI gate**: the lower-80% bound of weighted log-loss improvement vs identity, across **300 OOB resamples** with per-bootstrap weight renormalization, must be strictly positive. RNG seeded from `time.time_ns()` each fit. Pre-CI **range check**: `y_min <= 0.50` and `y_max >= 0.55` (else rejected without bootstrapping).

**Tail-overconfidence guards** (baked into `fit()`, so live + replay + both fit sites share them; operator-owned, never pipeline-tuned). Isotonic overfits sparse extreme bins — a few lucky high/low-prob trades pool to ~0/1 and Kelly then max-sizes a "certain" bet that historically wins ~66%. Two layers: **(1) output clamp** — calibrated prob bounded to **[0.15, 0.85]** (`_CAL_OUT_LO/_HI`), a data-justified ceiling (realized win rates top ~0.66-0.69 / bottom ~0.16 at this horizon, so nothing beyond is warranted); never touches an honest fit, caps a slammed tail. **(2) Beta-prior smoothing** — `_PRIOR_FRAC` (0.10) × n pseudo-observations at p=0.5 over `_PRIOR_ANCHORS` (50) anchors (weight scales with pool size), pulling sparse tails toward their realized rate while leaving dense mid-range compression intact. `load()` applies the clamp too, so a legacy slammed `isotonic_params.json` is capped on read. Both also **tighten** the OOB-CI (less tail variance → more robustly adoptable).

`last_fit_diagnostics` (CI bounds, `n_samples`, `bootstrap_n_completed`, `y_min`/`y_max`, `decision`) is stamped to `pipeline_info["calibration"]["fit_diagnostics"]` on every `fit()` reaching the bootstrap stage (both `adopted` and `rejected_ci`); structural early-rejects return `False` without stamping (visible in the reject-site log).

`lowest_learned_prob` / `highest_learned_prob` (`y_thresholds_[0]`/`[-1]`, themselves bounded to [0.15, 0.85] by the clamp) are the per-side "dead side" floors consumed by `evaluate_hold` — §6.

## 3. Entry gates

Edge = `calibrated_model_prob - market_price`. **All** must pass; any single failure -> skip the tick.

| Gate | Threshold | Source |
|---|---|---|
| Chosen-side `prob` | >= `min_model_probability` (default 0.56) | `SignalEngine.evaluate` |
| `edge` | >= `min_edge` (default 0.04, scaled by flip premium — §7) | `SignalEngine.evaluate` |
| `Kelly` (fee-aware) | >= `min_kelly` (default 0.01); `b_eff = b * (1 - fee_rate)` | `SignalEngine._kelly` |
| Spread either side | `spread/2 + EFFECTIVE_FEE_PEAK <= max_spread` (default 0.10); spread unavailable (WS + REST both fail) = skip, fail-closed | `_fetch_market_prices` |
| Book depth | both-sides-thin first (>= `min_book_depth_usd = $50` on at least one side); chosen-side depth must also clear it | `_evaluate_signal_and_enter` |
| Price sum | `price_up + price_down in [0.98, 1.02]` (cross-book no-arb) | `_fetch_market_prices` |
| Book freshness | both sides' WS book snapshots <= `_WS_STALE_S = 10s` old | `clob_ws.both_books_fresh` |
| `edge <= max_edge` | default 0.20 — wider edge = stale phantom price | `_evaluate_signal_and_enter` |
| ATR gate | ATR >= 5th-percentile (lower-bound only) | `IndicatorEngine.atr` |
| SPRT | not `SKIP`; not opposing the chosen side when conf > 60% with >=6 obs; conf >= `min_confidence` (0.02) once >=6 obs | `SPRTAccumulator` |
| Adverse-selection hard skip | `adverse_rate_at_30s >= adverse_selection_threshold` (default 0.80) -> reject | `AdverseSelectionMonitor` |
| Edge-decay | mean 15s post-fill drift (30-min lookback) >= `edge_decay_threshold` (default -0.05). Inactive until >=15 resolved fills | `AdverseSelectionMonitor.get_recent_decay_mean` |
| Layer disagreement | reject when `compute_momentum` opposes the chosen side (>0.5 magnitude) and `edge * 0.5 < min_edge` | inline |
| CVD deceleration | skip if `\|spot_flow\| >= 0.20` AND `spot_flow * cvd_accel < 0` | inline |
| Regime | skip when `RegimeDetector` classifies `quiet` | `RegimeDetector.classify` |
| Net-edge after slippage | `edge - price * est_slip >= min_edge` | `slippage_pct` |
| Pre-submit re-check | walk the ask ladder for FOK VWAP; net edge must clear `[min_edge, max_edge]`. Fresh-BBA fallback (book unavailable): net edge vs `min_edge`, **gross** edge vs `max_edge` (max_edge is a stale-phantom guard, slippage is execution cost) | `compute_buy_vwap` |
| Min order size | size >= $1 (Polymarket CLOB floor; paper mirrors live) | inline |
| Feed staleness | Coinbase <= 30s, Chainlink <= 60s, Binance aggTrade <= 30s, Binance kline <= 45s | inline |

**Adverse selection is sizing-side, not entry-side.** Above the soft penalty floor (`adverse_penalty_floor` default 0.45):

```
kelly_mult = max(0.30, 1 - 1.5 * max(0, adverse_rate_at_30s - 0.45))   # max(adverse_penalty_min, 1 - slope*...)
```

The penalty scales down with the fade rate (at `adverse_rate -> 0.80⁻` it reaches `0.475`); at `adverse_rate >= 0.80` (the hard `adverse_selection_threshold`) the trade is blocked entirely, before sizing. (The 0.30 floor is a clamp, unreachable while trading.) The 30-min lookback is **Bayesian-shrunk to a neutral prior** (n=10, rate=0.5).

**Ghosting:** below-min-prob model skips and the downstream gate vetoes (adverse, edge-decay, edge cap, flip hurdle, SPRT, layer disagreement, CVD decel, net-edge-after-slippage, pre-submit drift) feed a **ghost** into `GhostTracker` (full L1-L5 inputs + aux microstructure); ghosts resolve at the window's close and feed the backtest pool (raising a gate filters the same ghosts from baseline + candidate equally, lowering includes them), priced **fee-aware** via `ghost_gain_pct` — binary payoff net of the entry fee at the recorded price, comparable with realized `gain_pct`. **Not ghosted:** `min_edge`/`min_kelly` rejections inside `SignalEngine.evaluate` (so those two knobs are under-evaluable downward — the pool is censored just below their thresholds) and the non-tunable structural gates (`regime` quiet skip, chosen-side `thin_book_depth`, `min_size` $1 floor), so the pipeline can't adopt a change that re-includes them. The CLOB-microstructure gates (price sum, book freshness, both-sides depth, spread) run only when prices come from the CLOB (`price_source == "clob"`); on the Gamma fallback they're bypassed and the chosen-side depth check + pre-submit re-check are the remaining guards.

## 4. Sizing

Soft multipliers first, then `min()` against hard caps (so the caps dominate), then a $1 floor + a net-edge-after-slippage check:

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

- **Circuit breaker** — tier-locked floor at $100/150/200/300/400/600/800/1000/1500/2000/3000/4000/6000/8000/10000. Floor = locked_tier * `floor_pct` (0.85). Kelly multiplier: `1.0x` at/above the tier; `min_multiplier` (0.40) at/below the floor; **concave (sqrt) interpolation** between. Tier never resets down; ratchets up on a new tier; persists across restart via the `peak_bankroll` DB row (`restore_from_peak`).
- **Time multiplier** — `compute_time_multiplier`. First `normal_fraction` of the window (default 60% -> 0-180s): full Kelly. After: penalty scales by `(1 - conviction)` up to `late_max_penalty` (default 0.30).
- **Consensus multiplier** — `compute_signal_consensus` counts how many of `flow`, `spot_flow`, `cvd_accel_norm` agree with the chosen side (dropping `|signal| < consensus_dead_zone = 0.05`): >=80% -> 1.30x, >=60% -> 1.00x, >=40% -> 0.80x, else 0.60x.
- **Concurrent multiplier (correlation-aware)** — `execution/correlation.py`. Same-side bets correlated, opposite-side hedged; rho is a **fixed prior** (`+0.75` same / `-0.25` opposite; same-market triggers flip logic, not this). Worst rho across opens: > 0.6 -> 0.35, > 0.3 -> 0.55, > -0.2 -> 0.70, <= -0.2 -> 0.90.

### Hard caps

- `max_bankroll_deployed` (default 0.80), enforced twice: sizing clamps the single trade to `cash * 0.80`, and `open_trade` rejects when `deployed + size` would exceed `(cash + deployed) * 0.80` — total deployed cost never passes 80% of equity (free cash + open-position cost).
- `side_depth * max_book_fill_pct` (default 0.50) — under the thin-CLOB upstream gate (at least one side >= $50); if the chosen side is the empty leg of a one-sided book, that's an explicit skip.

## 5. Placing the order

FOK via `py-clob-client-v2`. Up to 3 attempts with jittered exponential backoff — but **only exchange-confirmed rejections retry**. Ambiguous outcomes never resubmit (double-fill guard): an accepted-but-unmatched status (e.g. `delayed`) is cancelled and settled from its trade record; a network failure during the POST checks the WS trade feed for a real fill and otherwise reports unfilled; a confirmed match never re-enters the retry loop even if the fill-price lookup fails (limit price used). HTTP/2 keepalive ping every 5s (against a 60s `keepalive_expiry` pool, so the connection never lapses between pings).

Live mode boot:
1. Client creation requires `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` (boot fails loudly without either); `verify_auth` checks the Safe reachable.
2. USDC balance fetched. Allowance = `min(allowances[spender] for all spenders) / 1e6`.
3. Required allowance: `max_single * max_concurrent_positions * 10` (10x safety), `max_single = bankroll * kelly_fraction` (a max Kelly single bet — **not** the `max_bankroll_deployed` cap). If allowance < required -> logged clean exit, **no retry that day** (outer `while ($true)` restarts next midnight ET; fix allowance first).
4. **Mid-session allowance recheck** — every `_ALLOWANCE_RECHECK_EVERY = 10` successful fills, re-fetch and **warn** (never halts) if it drops below threshold.

Paper boot: skips auth, same `BaseTrader` open/close path; `PaperTrader` simulates book-walk fill + latency + occasional FOK rejection.

Per-trade DB write is atomic (single SQLite transaction): `open_position_and_debit_bankroll`, `close_position(... bankroll_delta=... | new_bankroll=...)`. `bankroll_delta` for a relative credit (scalp), `new_bankroll` for absolute on resolution.

**`fill.fill_size` is always USDC notional** — BUY = requested $ size; SELL = `shares * fill_price`. Logs show the actual fill price (book-walk for paper, FOK for live), never the signal-moment price.

## 6. Holding vs scalping (`evaluate_hold`)

Every tick while we hold, re-run the full model and decide HOLD vs EXIT. `holding_edge = model_prob - market_price_for_side`. The exit threshold blends two curves:

```
itm_ref            = market_mid_for_side or market_price_for_side
itm_depth          = max(0, (itm_ref - 0.5) / 0.5)
deep_loss_floor    = exit_edge_threshold * (1 + 0.5 * itm_depth)
optimal_threshold  = ExitBoundary.compute_exit_threshold(seconds_remaining, fee_rate, market_price_for_side)
effective_threshold = (1 - itm_depth) * max(deep_loss_floor, optimal_threshold)
                    + itm_depth * min(deep_loss_floor, optimal_threshold)
```

ATM trusts the `ExitBoundary` curve; deep ITM weights toward the more patient floor. The blended value is stamped to `last_effective_exit_threshold` so the phantom-bid SELL re-verify (below) gates against the same threshold the scalp used.

**`ExitBoundary.compute_exit_threshold`** in `core/exit_boundary.py`. Binary-payoff math (payoff kinks at $0/$1); a pure function of time / market price / fee (entry price doesn't enter):
- **Deep ITM (`p >= 0.70`):** base time value * `(1 - itm_depth*0.5)` + resolution premium `itm_depth*0.05*(1 - minutes/5)` — wants to hold for $1.
- **Deep OTM (`p <= 0.30`):** base time value * `(1 - otm_depth*0.7)` + urgency premium ramping in the last 2 min — cut losses.
- **ATM:** `0.07 * sqrt(minutes) * 0.4 + fee_cost`.

OTM urgency can push the threshold **positive**, forcing exit even when the model is optimistic — final clamp `[-0.30, urgency_premium > 0 ? 0.30 : -0.01]`.

### Exit branches (in order)

1. **Loss-cut** — `entry_price > 0 AND market_price < entry * loss_cut_fraction (0.65) AND seconds_remaining < loss_cut_time_s (90s) AND BTC on the wrong side of strike by >= 0.5*ATR`. The 0.5*ATR cushion is the whipsaw guard: when BTC sits on the strike and the contract flickers $0.05-$0.70 on thin prints, we don't lock in the bottom. Engine stamps `last_loss_cut_event` in {`""`, `"fired"`, `"whipsaw_blocked"`}; the loop counts those into `gate_stats`.
2. **Deep-loss hold** — `holding_edge < deep_loss_hold_threshold (-0.10) AND market < entry AND model_prob > calibrator.lowest_learned_prob`. The binary residual ($1 if we win) beats locking in the loss at a depressed price. **Override:** when `model_prob <= lowest_learned_prob` the calibrator says this side won essentially never at this raw prob, so selling at market beats ~$0 expected. Identity calibrator returns `0.0` (override disabled); refits update it.
3. **Scalp** — `holding_edge <= effective_threshold` (and not in the deep-loss hold zone), unless BTC is within 0.5*ATR of the strike on the wrong side (same whipsaw cushion as branch 1).
4. **Hold** — otherwise.

No confidence override — math says exit, it exits. (On EXIT, if the WS best_bid looks phantom — the `/price?side=SELL` cross-check comes in below 70% of it — the SELL is re-verified against `last_effective_exit_threshold` first.)

`exit_edge_threshold` is the only operator-touchable exit knob and the **only exit knob the pipeline tunes** (range -0.10..-0.03). On a proposed change, the backtest replays the **counterfactual tracker**'s records through the candidate symmetrically, judged against the same blended effective threshold live fires on (shared `effective_exit_threshold` in `exit_boundary.py`, recomputed from the recorded decision-moment state): a scalp re-prices to its matched hold-to-resolution `gain_pct` (`pnl/size` — §13) when the candidate would **not** have fired it, and a held-to-resolution trade re-prices to its worst-moment hypothetical-scalp `gain_pct` when the candidate **would** have fired there — unless a threshold-independent live branch (whipsaw cushion, deep-loss-hold) would have held it anyway. Without the hold side, a less-patient candidate fires strictly more often than live, the scalp-side path never engages, and every such candidate scores delta == 0 — structurally unevaluable in the direction that matters. Loss-cut closes fire independently of the threshold and are never re-priced (scalp records carry a `loss_cut` flag; records without it replay as ordinary scalps). The CF index reads per-trade files **and rollup arrays** (the nightly rollup runs before the optimizer stage, so most history lives in rollups).

## 7. Flip trading

After a scalp, the bot can re-enter the same window — including the opposite side — **unboundedly** (one position per window; across different windows, `max_concurrent_positions` caps the total). Each re-entry clears the standard entry gates plus a flip premium:

```
flip_premium = flip_edge_premium + 0.005 * max(0, flip_count - 2)
spread_cost  = spread + 2 * fee_rate * p * (1 - p)            # real round-trip
flip_hurdle  = min_edge + max(flip_premium, spread_cost)
```

Flips 1-2 pay only the base `flip_edge_premium` (default 0.015); flip 3 +0.5pp; flip 4 +1.0pp; etc. Or the actual round-trip spread+fee cost, whichever is higher — flips can't churn on micro-edge that won't survive the round trip.

## 8. Resolution

- **Early scalp** — sold before expiry into the book. The bot keeps the difference; counterfactual tracker logs what hold-to-resolution would have paid.
- **Resolution** — window closes; Chainlink decides; winner paid binary **$1**, loser **$0**, credited atomically. Exit price decided **oracle-first** (`_resolved_exit_price`): Gamma's `event_metadata` (Chainlink `final_price` vs `price_to_beat`) is authoritative; absent that, the fallback is Gamma's *coherent* resolved `outcomePrices` (market `closed`, prices sum ~1, one side at an extreme — Gamma rewrites them to the resolved 1/0 post-close). Incoherent prices (stale/phantom) are rejected, not trusted. Live winning redeems settle **non-blockingly**: one balance check per tick (`pending` result until the auto-redeem lands, 60s deadline then trust the raw balance), so the loop keeps managing other positions meanwhile.
- **Never resolves from Binance** — it can diverge from Chainlink by $20-$200 at the close.
- **Chainlink orphan fallback** — if Gamma stays silent ~30 min after entry, read Chainlink directly via `chainlink_feed` and resolve locally. Restart safety: the position stays `open`/`pending_resolution` in the DB (re-evaluated on boot), not a file. `memory/state/orphan_positions.json` is written by a *separate* startup check (`LiveTrader.detect_orphan_positions`) flagging on-chain positions the DB doesn't know about.

## 9. Built-in loss handling

The loss-handling stack lives in §3 (adverse-selection, edge-decay, regime quiet-skip, feed-staleness skips — a Coinbase gap >=2s skips the L1 decision, no Binance fallback), §4 (circuit breaker), and §1 (cross-venue gap logging). Also:
- Circuit-breaker streak counters (3 losses / 3 wins) drive Discord alerts only, never sizing.
- `AdverseSelectionMonitor` state persisted to `state/adverse_state.json` on every fill so restarts inherit the rolling window.
- **CLOB WS heartbeat** — PING every 10s; no PONG for 25s forces a reconnect (checked once per PING cycle, so worst-case detection ~35s).

---

# Part B — Operational Layer (§10-19)

Telemetry, nightly pipeline, param registry, layout, data sources, run commands, invariants, Discord, persistence.

## 10. Live execution telemetry

### Per-decision `trade_context` (stamped into outcome + ghost)

- **Entry facts:** `btc_price`, `strike_price`, `seconds_remaining`, `market_price_up`, `market_price_down`, `closes_tail` (last 2 closes, so the L6 backtest can reconstruct `last_return`), `atr_rolling_20`, `atr_long_term_mean` (so L6 + the L3b `regime_vol_factor` read stamped values, not a re-derived approximation).
- **Probabilities:** `model_probability` (post-calibrator), `model_probability_raw` (pre-calibrator — stored separately so re-fits don't compound).
- **Composite signals:** `flow_score`, `spot_flow_signal`, `regime_autocorr`, `regime_direction`, `prev_resolution_margin`.
- **Microstructure aux:** `coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`.
- **None-vs-0.0 (load-bearing):** every **signal** field is recorded `None` (never `0.0`) when its feed is cold/stale **or its trade buffer doesn't yet span the measurement window (e.g. <60s after a Coinbase reconnect — `CoinbaseFeed.covers`)** — including `flow_score`/`spot_flow_signal`, whose *live* value collapses cold to `0.0` for the logit but whose *recorded* value is `None`. So a recorded `0.0` is genuinely flat flow, not a dead feed; the pipeline replay coerces `None -> 0.0` on read to match live. `coinbase_taker_n` is a **count**: `0` (not `None`) when cold (consumer requires `n >= 20`).
- **SPRT:** `sprt_confidence`, `sprt_status`. **Sizing audit:** `adverse_rate_at_30s`, `adverse_kelly_mult` (recorded for audit; not pipeline-consumed), `entry_phase`, `flip_count`, `is_flip`.

**Ghost rejections share the entry-fact schema** (incl. `entry_phase`/`flip_count`/`is_flip`, and post-cal `model_probability` + `edge` where a signal exists at gate-fire), so by-phase and flip-segmented bias cards see the full ghost population. The sizing snapshot (`size`, SPRT, adverse fields) appears only on pre-submit ghosts — earlier gates fire before sizing runs.

### `edge_decay.deltas` (merged at close, persisted to outcome JSON)

Post-fill drift of the **traded token's own mid** at **5/10/15/30/60s** (positive = our side's price rose = in our favor; baseline and checkpoints share the same axis), captured by `AdverseSelectionMonitor` keyed by `position_id`, merged at close. The live `edge_decay_threshold` gate reads the 15s mean over a 30-min lookback from the monitor's **in-memory** window (restart-inherited via `adverse_state.json`, schema-versioned so a convention change discards old snapshots); the per-outcome `edge_decay.deltas` persisted here is an **audit record**, not consumed by the pipeline. Null windows = trade closed before that checkpoint resolved.

### Gate-skip stats (`memory/state/gate_stats*.json`)

Two files. Live counts persist to `state/gate_stats_current.json` on every resolution (mid-day restarts reload it); at the first record of a new ET day the finished day folds into the lifetime accumulator `state/gate_stats.json` (`counts` + `days_accumulated` + first/last day) and the current file resets. The nightly pipeline reads the current-day file. `loss_cut_fired`/`loss_cut_whipsaw_blocked` audit the 0.5*ATR cushion — stamped per `evaluate_hold` tick (tick-pressure, not distinct events: an underwater position re-stamps every tick until it closes), so read the ratio, not absolute counts.

### Feed staleness telemetry

`feeds/_staleness.StalenessTracker` persists per-feed P50/P95/P99 inter-arrival gaps to `state/feed_staleness.json` every 60s. `feeds/_socket.enable_nodelay` verifies `TCP_NODELAY` on every WS connect. `BiasDetector` reads it into the nightly card as `feed_health` (per-feed `{n, p50, p95, p99, max}` + a `degraded_p95_ge_10s` list), so a degrading feed is surfaced, not misattributed to layer signals.

## 11. Nightly learning pipeline

Runs 23:45 ET (via `run_polybot.ps1`). Five stages; calibrator save deferred to the end so on-disk state stays coherent across crashes. All window boundaries (60d cutoff, holdout split, gate-calibrator window, evolver-context filter, calibration windows) share one per-cycle timestamp. `memory/state/PIPELINE_FROZEN` (file flag) makes the cycle **analysis-only**: data/bias/ghost cards still build, but no calibration change, no weight adoption, no auto-revert — and `loader.save_config` independently refuses writes while frozen.

### Dataset boundaries

- Active dataset bounded to the **last 60 days** before splits (older trades came from probability machines that no longer exist); falls back to full history only if the window has <500 trades.
- Walk-forward folds inside that window: candidates scored on test folds `[60:70][70:80][80:90][90:100]`, each genuinely OOS (the first 60% is chronology context only — nothing refits per fold).
- **7-day holdout** — last 7 days excluded from all folds AND the evolver's per-trade context (outcomes, ghosts, counterfactuals), so the holdout-confirmation gate (and gate calibrator) score candidates on trades they never saw. (Track-record *aggregates* — an adoption's realized 7d-review Sharpe — span the holdout by design; per-trade data never leaks.) **Young-dataset fallback:** if the dataset is younger than 7 days the holdout cut would swallow every trade, emptying the pre-holdout (`opt`) pool. The split disables the holdout (`holdout_active=False`, full pool -> analysis + evolver) **before** the analysis dict is built — the recommender keys off `analysis["overall"]["total_trades"]`, so building it on an empty `opt` pool would silently zero all learning. OOS confirmation is forfeited that cycle; the walk-forward folds still gate adoption.
- **Realized fills only** — `gain_pct = pnl / size` from closed-trade outcomes, `pnl` already netting actual fee + fill price. No mid-price replay; candidates inherit the slippage any live trade paid, and the backtest's Kelly sizing mirrors live `_kelly` exactly (fee-aware `net_b = b*(1-fee)`). Ghosts join the pool priced fee-aware (`ghost_gain_pct` — §3). Rows missing L1 inputs or with a dead ATR are skipped in both arms (live structurally never trades them; the stored side-probability can't be replayed for a candidate).
- **Recency weighting** — `0.94^days_ago` (~11-day half-life) inside the window cutoff.
- **Backtest L1 ATR-floor fidelity (approximate).** Live advances the ATR buffers per decision tick; the backtest advances a local buffer once per stored trade (entry-only). `min_atr`/`atr_regime_shift_threshold` stay backtest-evaluable, and the approximation largely cancels in the baseline-vs-candidate delta. L6 features and the L3b `regime_vol_factor` read the faithfully-stamped `atr_rolling_20`/`atr_long_term_mean`.

### Calibration window

Both calibrators fit on **real trades only** (ghosts excluded) — the calibrator changes live probabilities, so it must learn from fills the bot actually took.

**Two calibrators (decoupled masters)** — one can't serve both live trading (freshest data) and the OOS gate (a window the holdout never saw):
- **Live / production** — `fit` on the **freshest `_CAL_WINDOW_DAYS` (~7d)**, applied to `signal_engine.calibrator` and saved; goes through the full three-gate adoption (stage 3).
- **Gate reference** — a separate fit on the window **immediately before the holdout** (days `[HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS]` back, `self._gate_calibrator`). **All** weight-optimizer backtests score through this, never the live one (`calibrator` is a required arg of the replay helper, so none can silently fall back), keeping the gate OOS. `None` (identity) when the window is thin.

The **live** pool must hold **>=125 trades**, split 60/40 `cal_train`/`cal_val`; `fit` uses **`min_samples=75`** (overriding the class default 150), so `cal_train` >=75, and the Kelly-Sharpe gate (iii) needs **>=50 replayed `cal_val` returns**. The **gate** calibrator fits its whole window unsplit (min 75 usable samples) — it's a fixed reference mapping, not an adopted artifact. Both arms of every weight comparison share it, so its mapping cancels in the adoption delta to first order.

### Stages (in order)

1. **PipelineTracker** — review prior adoptions (7d/14d/30d realized Sharpe); auto-revert underperformers. Each review window finalizes only once its full duration has elapsed; the rollback test runs at every finalization (7d -> 14d -> 30d) and flags only with >=100 post-adoption trades. Revert criterion is **symmetric with adoption** (`actual_sharpe < baseline - ADOPTION_Z_FLOOR * JK_SE`, same `_jk_se`/floor on post-adoption Sharpe + n) — no adopt->dip->revert oscillation. Adoption records store real old/new values (incl. the L4 `weights` dict), so every param reverts; a record touching `kelly_fraction` is deferred while crisis halving is active.
2. **BiasDetector** — per-indicator/side/edge-bucket/regime/time-of-window/phase/flip stats + edge-realization quartiles + execution quality. Runs on `opt_real` (holdout-excluded, **ghosts filtered out**) — no last-7-day leakage, no ghost pollution. Ghosts get their own `ghost_analysis` (`analyze_ghosts`/`by_gate` + by-phase/flip) and feed the optimizer backtest pool (§3), but are excluded from **every** real-performance/display metric (they carry `gain_pct` but no `pnl`, so they'd show a negative Sharpe beside positive P&L).
3. **Calibrator (isotonic)** — fit attempted every cycle, **adopted into production only when it clears all three gates**: (i) per-fit **bootstrap-CI** lower bound > 0 (§2); (ii) beats the *current* calibrator's recency-weighted log-loss on the full cal pool by >= `LOG_LOSS_FLOOR` (0.005 nats); (iii) doesn't reduce Kelly-Sharpe vs current on `cal_val` (>=50 replayed returns, else the night is a no-op). If the current calibrator drifted worse than identity, the new fit is tried against identity directly; identity is restored only when the new fit also fails. Applies live in-memory immediately; weight backtests use the gate-reference calibrator. On-disk save deferred to stage 6. Every exit path stamps an explicit decision + reason (the summary never shows a bare "identity" while a fitted isotonic is serving).
4. **TAEvolver** — `ClaudeRecommender` (Anthropic with full analysis + directional table + structural-probe targets) or `LocalRecommender` (rule-based fallback) returns `{changes, manual_observations}`. The `claude_client` validator reroutes manual-only `changes` -> `manual_observations`, clamps out-of-range values, drops unknown params, drops non-finite L4 weights, and drops combined L6 weight changes breaching the +/-0.25 cap.
5. **WeightOptimizer** — per-param walk-forward backtest; gate decisions live here.
6. **Deferred calibrator save** — after the optimizer stage (when it ran, `save_config` has committed first); also runs when a young dataset skipped the optimizer, so an adoption/revert at 125-199 trades still persists. A crash before this line leaves new weights + the previous-session calibrator: mismatched but each a valid artifact (the reverse — new calibrator + stale weights — is the worse half).

### Adoption gate (WeightOptimizer)

Per candidate change on the 4-fold walk-forward:

```
n_candidate_trades >= MIN_CANDIDATE_TRADES (100)
z = delta_sharpe / JK_SE >= ADOPTION_Z_FLOOR (0.3)   # lag-1 autocorr-adjusted
JK_SE = sqrt((1 + 0.5 * sharpe^2) / n) * sqrt(max(1, 1 + 2*rho1))   # sharpe = baseline; n, rho1 from candidate returns
```

- **Soft abs floor** — `candidate_sharpe < min(0, baseline) - 0.05` blocked (allows less-negative recovery in a regime shift, not an outright collapse). Non-finite Sharpe rejected outright.
- **Fold-consistency floor** — `min(fold_sharpes) >= -0.10` (per-fold Sharpe needs >=3 replayed trades; the floor applies once >=2 such folds exist). Pooled candidate returns include every fold's trades — the same inclusion rule as the baseline.
- **Regime-stratified veto** — per regime bucket once it has **>=8 trades** in the validation fold; shared "no regime degrades >0.10 Sharpe" floor + either **(a)** improves in >=2 of 3 buckets or **(b)** dominant regime improves AND no other degrades >0.10. "Improves" requires a 0.02 Sharpe margin (float-noise can't pass).
- **Holdout confirmation** — baseline vs candidate on the held-out 7-day pool (>=30 trades); `margin = max(0.02, ADOPTION_Z_FLOOR * holdout_jk_se)`, candidate must clear `baseline_h + margin`. `pipeline_info["holdout_active"]` stamped each cycle.

### Combined-holdout interaction check

When `>=2` changes adopt: one combined backtest on the holdout pool (`>= HOLDOUT_MIN_TRADES`), carrying the `exit_edge_threshold` counterfactual override when that change is in the batch. Each already cleared its per-change gates, but two that pass alone can still interfere (shared logit budget, joint clamps).

```
margin = max(0.02, ADOPTION_Z_FLOOR * holdout_jk_se)
if combined_holdout_sharpe < baseline_holdout_sharpe + margin:
    back out the WHOLE batch
```

No iteration; if it fails (or the check itself errors — it fails closed), drop everything — next cycle re-proposes individually with the directional table reflecting this.

### Crisis mode

Triggers on **either**:
- **(a)** baseline Sharpe < 0.10 AND (recent-50 WR < 48% OR recent-50 `avg_loss/avg_win > 2.0`), or
- **(b)** trailing-3-day Sharpe < 0 over >=20 recent trades — catches sustained multi-day collapses the recent-50 smoothing masks.

- **>=3 consecutive crisis cycles** -> halve `kelly_fraction`, floor **`CRISIS_KELLY_FLOOR` (0.04)** — intentionally below the 0.05-0.18 tunable range so crisis sizes more defensively than any optimizer-adoptable state. The loader validates `kelly_fraction` against the same constant, so the persisted floor survives the next boot.
- **Restore on first non-crisis cycle** — original Kelly persisted in `state/crisis_state.json` **before** the cut, so a mid-pipeline crash can't compound the halving; the crisis state resets only after the restore's `save_config` succeeds (a failed save retries next cycle).
- **`kelly_fraction` is locked while halving is active** — the optimizer defers any `kelly_fraction` candidate (`decision="deferred_crisis"`) and the auto-revert path defers any record touching it; both re-enter on the first non-crisis cycle.

### Adaptive exploration + structural probes (`recommender_base`)

- **`EXPLORE_STEPS`** maps each tunable to a base step (e.g. `atr_sigma_ratio = 0.15`, `final_logit_clamp = 0.50`).
- **`_rule_exploratory`** ramps the step up when the directional table shows past probes returned `|bt_delta|` under the noise floor (`max(0.003, 0.3 * baseline_jk_se)`): +50% per dead direction, up to 2.0x with both directions dead; adoptions reset.
- **`STRUCTURAL_PROBES`** fires once per `(param, value)` until evidence appears — any final verdict (adopted, rejected, backed-out) counts as evidence; a crisis-deferred test doesn't: `exit_edge_threshold in {-0.08, -0.05, -0.03}`; L6 turn-on at `0.005` for all three weights so every closed-library feature gets >=1 evaluation. Both recommenders call `_rule_structural_probes()` before `_rule_exploratory`.

L6 directional bookkeeping: the optimizer reads `old_value` from `signal_engine.derived_weights[fname]` for `derived_*_weight` params (not `getattr`, which returns `None` since L6 weights live in a dict).

## 12. What the pipeline can vs cannot touch

The rule: a param is pipeline-tunable only if the realized-fill backtest can faithfully **and** safely score it. Everything the backtest can't simulate (or that's a tail-risk guard) is operator-owned.

### Pipeline-tunable (`PIPELINE_PARAMS` in `polybot/config/param_registry.py`)

| Group | Params | Range |
|---|---|---|
| **L1 / volatility** | `atr_sigma_ratio` | 1.2-2.5 (highest leverage) |
| | `student_t_df` | 3-8 |
| | `min_atr` | 8.0-25.0 |
| **Logit amplifier** | `logit_scale` | 2.0-5.0 |
| **L2-L5 weights** | `regime_weight` (0.01-0.15), `flow_weight` (0.02-0.12), `spot_flow_weight` (0.01-0.15), `prev_margin_weight` (0.01-0.05) | per-param |
| | `momentum_weight` | 0.0-0.10 (magnitude only — sign is dead at L4) |
| **Indicator committee (L4)** | `weights` (RSI/MACD/Stochastic/OBV/VWAP dict) | each >= 0.05, renormalized to sum 1.0; handled as a dict, not a scalar `ParamSpec` |
| **Sizing** | `kelly_fraction` | 0.05-0.18 |
| **Entry gates** | `min_edge`, `min_kelly`, `min_model_probability` | tight bands |
| **Exit** | `exit_edge_threshold` | -0.10..-0.03 |
| **Structural constants** | `regime_momentum_threshold` (0.08-0.25), `final_logit_clamp` (3.0-5.0), `l5_regime_damp_cap` (0.4-0.9), `atr_regime_shift_threshold` (0.40-0.80) | |
| **L6 derived weights** | `derived_log_atr_ratio_weight`, `derived_autocorr_signed_mag_weight`, `derived_flow_disagreement_weight` | 0.0-0.05 each; combined L6 hard-capped at +/-0.25 logits |

### Manual-only (`MANUAL_ONLY_PARAMS`, validator reroutes `changes` -> `manual_observations`)

- **Exit/hold magnitudes outside the curve:** `loss_cut_fraction`, `loss_cut_time_s`, `deep_loss_hold_threshold` — the backtest replays a single stored fill and can't re-simulate these branches; only `exit_edge_threshold` has a counterfactual path (§6).
- **Entry-timing envelope + flip hurdle:** `normal_fraction`, `late_max_penalty`, `flip_edge_premium` — backtest applies raw Kelly + entry gates only (no time-of-window multiplier, no flip hurdle), so changes yield zero delta.
- **Entry-time filters operator owns (protective guards):** `max_edge`, `adverse_selection_threshold`, `edge_decay_threshold`.
- **Risk caps:** `max_concurrent_positions`, `max_bankroll_deployed`. **Circuit breaker:** `circuit_breaker.floor_pct`, `circuit_breaker.min_multiplier`.
- **Indicator periods:** `indicators.{rsi,macd,stochastic,ema,obv,atr}.*` — backtest replays stored scores at the active period; alternate periods need raw candles per snapshot.
- **SPRT:** `sprt.{alpha,beta,observation_interval_s,min_confidence}` — intra-window timing; backtest replays a single stored fill instant.
- **Signal plumbing:** `regime_lookback` (L2 autocorr window), `consensus_dead_zone` (sizing consensus filter).
- **Schedule:** `trading_{start,end}_{hour_et,minute}`.

`is_manual_only(name)` is the single source of truth. If a param appears in both lists (operator error), tunable wins.

## 13. What it deliberately won't do

Guardrails (most enforce a decision made above; collected so a future edit doesn't undo one by accident):
- No Gaussian (§2), no Binance resolution (§8), no big single bets (caps via `max_bankroll_deployed`/`max_book_fill_pct` — edge compounds via frequency).
- No pattern-based exit rules ("RSI > 80, sell") and no confidence override of a scalp — exit is pure edge + time-value math (§6).
- Don't hold a dead side for its binary residual when the calibrator's lowest-learned knot says ~0% — selling at market beats $0 expected (§6).
- `gain_pct = pnl/size` arithmetic, never `log_return`, single source across live + backtest + isotonic fit.
- Entry/exit edge uses the **executable CLOB BBO** — best_ask to buy, best_bid to sell (what a FOK fills against), from the WS BBO with an HTTP `/book` fallback, never the mid. Not `GET /price` as primary (its negRisk cross-match returns phantom prices near expiry); `/price?side=SELL` is only a phantom-bid cross-check on exit. FOK ask-ladder walked for VWAP slippage. Never skip the fee (`rate*shares*p*(1-p)`, `rate=0.07` = `base.DEFAULT_FEE_RATE`, Polymarket Crypto `feeRate`; peak effective 1.75% at p=0.5). `DEFAULT_FEE_RATE` is the **coefficient** inside the `p(1-p)` formula; flat-additive cost gates use `EFFECTIVE_FEE_PEAK` (= `rate*0.25` = 0.0175) instead — never mix them.
- Don't bypass the circuit breaker. Don't delete `polybot/db/polybot_*.db`. Regime direction is `sign(last 1-min return)`, not `sign(prob-0.5)`. Layer adjustments are always logit space, never probability space.

## 14. Project layout

```
polybot/
  main.py                      Trading loop, entry/exit/sizing orchestration
  config/                      settings.yaml, loader.py, param_registry.py (single source of truth)
  core/                        signal_engine, calibrator, order_flow, returns, regime,
                               exit_boundary, sprt, adverse_selection, derived_features,
                               aux_layers (shared model math: student_t_cdf, autocorr_vol_scale,
                               combine_flow_family, regime_vol_factor, compute_spot_flow_signal)
  feeds/                       coinbase_feed (primary BTC + CVD), binance_feed (1m candles, ATR),
                               binance_depth, binance_trades, chainlink_feed (strike + resolution),
                               clob_ws, market_scanner, _socket, _staleness, _json
  indicators/                  rsi, macd, stochastic, obv, vwap, ema, atr + engine
  execution/                   base (BaseTrader, fee math), paper_trader, live_trader,
                               circuit_breaker (tiered floor), correlation
  agents/                      scheduler (orchestrator), outcome_reviewer, counterfactual_tracker,
                               ghost_tracker, bias_detector, ta_evolver, weight_optimizer,
                               pipeline_tracker, pipeline_analytics, claude_client (validator),
                               claude_recommender, recommender_base (EXPLORE_STEPS,
                               STRUCTURAL_PROBES), local_recommender
  memory/                      records: outcomes/, ghost_outcomes/, counterfactuals/ (+ rollups);
                               calibration/ (isotonic_params.json);
                               state/ — rolling state + logs: gate_stats.json (lifetime accumulator)
                               + gate_stats_current.json, adverse_state, crisis_state,
                               feed_staleness, fill_stats, latency_stats, orphan_positions,
                               prev_resolution_margin, cf_watchlist, pipeline_history,
                               pipeline_run_log, strategy_log.md, PIPELINE_FROZEN (flag, §11).
                               Layout centralized in polybot/paths.py (MEMORY_DIR override: POLYBOT_MEMORY_DIR).
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

Trading/pipeline/test commands in Quick Start. Live pre-flight: `python scripts/verify_keys.py` (verify Polymarket creds + USDC balance/allowance; runnable from any directory).

`run_polybot.ps1` is the daily loop: starts 12:01 AM ET, stops trading 11:30 PM ET, runs the pipeline 11:45 PM ET, commits + pushes as it exits (~11:55 PM ET), then sleeps until the next 12:01 AM ET restart — unless the exit slipped past midnight, in which case it restarts immediately instead of losing the day. The commit gate is the process exit code (guards crashes/auth failures; pipeline-internal errors are caught by the scheduler and still exit 0). The outer `while ($true)` survives auth errors but won't retry the same day — fix auth before midnight.

## 17. Invariants (what doesn't drift)

- **Direction sourcing:** empirical directional table only (`pipeline_run_log.json`); no hardcoded per-param priors.
- **Recency weighting** single source `RECENCY_DECAY_PER_DAY` in `pipeline_analytics.py` (`0.94^days_ago`, ~11-day half-life, inside the 60-day cutoff).
- **UTC everywhere** for storage; ET (tz-aware `America/New_York`, not a fixed offset) only for date-bucketing (gate_stats, daily rollup, new-ET-day fold) and trading-window logic.
- **Daily rollup** runs inside the 11:45 PM ET pipeline (`rollup_old_outcomes`/`_ghosts`/`_counterfactuals`), bundling per-trade JSON into `rollup_YYYY-MM-DD.json`; readers glob both per-trade and rollup files (lossless).
- **Shared model math** in `aux_layers.py` (`student_t_cdf` + df clamp, `autocorr_vol_scale`, `combine_flow_family`, `regime_vol_factor`, `compute_spot_flow_signal`) is called by `signal_engine` (live) and `scheduler` (replay) alike, so the optimizer can't tune a model production doesn't run. Replay reconstructs the full L1-L6 logit + calibration identically; the only approximation is the per-trade vs per-tick ATR-floor buffer (§11), which cancels in the delta.
- Other fixed-in-place invariants (cited where they live): `model_probability_raw` separate from calibrated (§10), `gain_pct=pnl/size` never `log_return` (§13), L6 library closed (§2), atomic open/close (§5), per-mode DB + shared `memory/` (§14), circuit-breaker tier persists across restart (§4).

## 18. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all] confirm` `!session` `!pipeline` `!commands`

`!pause` halts new entries only — open positions stay managed (hold/exit/resolution). `!clear` purges Discord chat messages only (never the DB/records) and requires the `confirm` token.

## 19. Persistence

`memory/` (records: outcomes, counterfactuals, ghosts; calibration; and `state/`: pipeline history/run-log + rolling state), the per-mode SQLite DB, and `settings.yaml` are git-tracked, committed + pushed by `run_polybot.ps1` as it exits (§16).
