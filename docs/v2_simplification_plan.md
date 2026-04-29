# PolyBot V2 — Simplification Plan

## Why This Document Exists

V1 has accumulated 8 signal layers, 30+ tunable parameters, and a learning pipeline with 7 stages. Operating it requires either deep memory of the codebase or an LLM co-pilot to interpret what each weight does. That's a design problem, not an operator problem.

This plan strips PolyBot to its load-bearing core so a single operator can reason about every parameter without consulting documentation. The goal is a system **you can debug at 2 AM without help**.

---

## Design Principles for V2

1. **One operator can hold the whole model in their head.** If a parameter exists, you should be able to explain what it does in one sentence and predict the direction of its effect.
2. **Robustness over theoretical edge.** A simpler model with 80% of the edge but no failure modes you don't understand beats a complex model that needs an LLM to triage.
3. **Adaptive by construction, not by management code.** When the volatility regime shifts, the math should adapt. Adding tracking + auto-revert + crisis mode is a sign the underlying model isn't adapting on its own.
4. **Pipeline tunes 1-3 things, not 30.** Most parameters in V1 are knobs that should never need to move.

---

## Current Architecture Audit

### Load-bearing (keep)

| Component | Why it matters |
|---|---|
| Student-t CDF on z-score | The fundamental signal — distance from strike, scaled by vol |
| Adaptive vol estimate | Knowing how much BTC moves per minute is the entire input |
| CLOB order flow | The only signal independent of price (book imbalance + trade flow) |
| Kelly sizing | Correct positioning given probability + market price |
| Circuit breaker (tier-based floor) | Hard downside protection |
| Resolution from Gamma/Chainlink | Authoritative payout source |
| Adverse selection filter | Genuine informed-flow detector — filters before it costs you |

### Complexity tax (remove or merge)

| Component | Why it goes |
|---|---|
| L2 regime autocorrelation | Replaced naturally by adaptive vol; adds 2 params (`regime_weight`, `regime_lookback`) |
| L3b spot CVD | Marginal added signal on 5-min binary; collinear with L3 CLOB flow |
| L3c wall pressure | Already disabled — gamed by HFT |
| L3e liquidation pressure | Sparse signal; OI drops are rare; not enough data to validate |
| L4 indicator momentum | RSI/MACD/Stoch on 1-min candles is folklore; the regime-conditional 1.5×/0.5× amplifier compounds the noise |
| L5 prev_margin | A memoryless market means last window's margin shouldn't predict this window |
| Flip trading | Adds a whole sub-state machine (`flip_enabled`, `flip_edge_premium`, max-1 logic) for marginal edge |
| Trailing exit ($0.50/$0.65/15%) | Three magic numbers no one can defend |
| Consensus multiplier (very_high/high/medium/low) | 4 thresholds × 4 multipliers = 8 numbers that just amplify whatever L1 already said |
| `exit_config` sub-dict | 6 parameters governing one decision (when to scalp) |
| Adaptive probability compression | Just-added in V1 — but in V2 the adaptive vol does this work upstream |
| Sustained-crisis Kelly auto-reduction | Just-added in V1 — but in V2 the simpler model shouldn't need crisis-mode rescue |

### Pipeline complexity (radically reduce)

V1 pipeline has 7 stages, each with adoption gates, decay tracking, and meta-warnings. V2 keeps:
- Calibrator refit (weekly)
- Vol parameter tune (weekly, optional)
- Strategy log for human review

That's it. No TAEvolver, no WeightOptimizer, no auto-revert, no regime stratification, no SPRT. The bot is simple enough that the operator can decide param changes from the strategy log review.

---

## V2 Architecture

### Signal model (3 lines of math)

```
vol = ewma_realized_variance(price_history, lambda=0.94)  ** 0.5
z   = (btc - strike) / (vol * sqrt(minutes_remaining))
prob_up = StudentT(df=5).cdf(z)
```

**Why EWMA realized variance instead of ATR:** ATR is a 14-period range estimator that treats the last big bar same as the last small one. EWMA on squared returns automatically gives more weight to recent volatility AND adapts to regime shifts (no `min_atr` floor needed, no `atr_sigma_ratio` divisor needed, no rolling window or long-term reference deque needed). When vol drops, the estimate drops; when it spikes, the estimate spikes. The regime adapts itself.

### Optional flow adjustment (1 weight)

```
logit_p += flow_signal * flow_weight
```

Where `flow_signal` is the existing CLOB order flow score (`book_imbalance × 0.6 + trade_flow × 0.4`). One weight, one parameter.

**Why keep this:** It's the only signal that's not derivable from price. If you have it for free, use it.

### Calibration (one stage)

Platt scaling refit weekly with recency-weighted MLE. Adopt if **Brier score** improves on holdout (not Kelly-Sharpe — Brier is a proper scoring rule that doesn't require a backtest). Identity (a=-1, b=0) when n < 200.

### Sizing (one rule)

```
size = bankroll × kelly_fraction × kelly_edge × circuit_breaker_mult
```

Where:
- `kelly_edge = max(0, (prob - market_price) / (1 - market_price))`
- `circuit_breaker_mult = scales 1.0 → 0.40` between locked tier and floor (unchanged from V1)
- `kelly_fraction` is operator-set (default 0.10), pipeline doesn't touch it

Capped at `max_single_position_usd` (operator risk policy).

### Exit (one rule)

```
if seconds_remaining < 30 OR (model_prob_for_held_side < 0.45 AND time_in_window > 60):
    scalp at market
else:
    hold to resolution
```

No trailing exits, no patience/urgency adjustments, no flip re-entries. If the model says you're now wrong, exit. Otherwise hold.

---

## V2 Tunable Parameter Surface

The complete operator-knowable list:

| Param | Range | What it does |
|---|---|---|
| `ewma_lambda` | 0.90–0.97 | EWMA decay for realized variance. Lower = faster vol adaptation. |
| `flow_weight` | 0.00–0.10 | Logit-space weight for CLOB flow signal. Set 0 to disable flow. |
| `student_t_df` | 4–6 | Tail fatness of the CDF. Probably never moves. |
| `kelly_fraction` | 0.05–0.20 | Sizing aggressiveness. Operator-tuned, not pipeline. |
| `min_edge` | 0.02–0.06 | Skip trades with edge below this. |
| `min_kelly` | 0.005–0.02 | Skip trades with Kelly fraction below this. |
| `scalp_prob_threshold` | 0.40–0.50 | Exit when held-side prob drops below this. |

**That's 7 parameters.** Each one's direction-of-effect is obvious. Operator can reason about all of them without notes.

Risk caps remain manual-only (same as V1):
- `max_single_position_usd`
- `max_concurrent_positions`
- `circuit_breaker.floor_pct`

---

## Pipeline V2

Runs daily. Performs three things:

1. **Refit Platt calibrator** on last 30 days of outcomes with `0.97^days` recency weighting. Adopt if Brier improves on holdout AND has ≥200 samples.

2. **Tune EWMA lambda** (optional). Grid search `lambda ∈ {0.90, 0.92, 0.94, 0.96}` against last 14 days of realized vs predicted vol. Pick the lambda with lowest MSE. No adoption gate — just track and use the best.

3. **Append daily summary** to `strategy_log.md`: today's WR, total PnL, n_trades, calibrator A/B, current ewma_lambda. **No proposals**, no Claude analysis, no decay tracking. The operator reviews this weekly and decides whether to change `flow_weight` or `kelly_fraction` manually.

**Total pipeline code:** ~200 lines instead of ~3,000.

---

## What V2 Loses vs V1

Honest accounting:

| Capability | V1 has it? | V2? | Net |
|---|---|---|---|
| Multi-signal fusion | Yes | Reduced to 1 signal | Loses ~5-10% theoretical edge |
| Indicator momentum (L4) | Yes | No | Loses ~0-3% (likely noise anyway) |
| Spot CVD (L3b) | Yes | No | Loses ~0-2% (collinear with L3) |
| Auto-detection of regime shifts | Sort of (via crisis mode) | Yes (via EWMA) | **Improvement** |
| Adoption decay tracking | Yes | No | Loses post-hoc analysis tool |
| LLM-driven param tuning | Yes | No | Loses what was net-negative this week |
| Operator can reason about everything | No | Yes | **Massive improvement** |

**Estimated Sharpe tradeoff:** V1 best ~0.18, V2 expected ~0.10–0.15 in good regimes, but with much narrower variance. V2 won't have $1,605-on-the-table-from-bad-scalp issues because it has one exit rule.

---

## Migration Strategy

### Phase 1: Build v2 in parallel (don't replace v1)

Create `polybot_v2/` package alongside `polybot/`. Same `db/`, same feeds, same execution layer. Only the signal engine, sizing, exit, and pipeline are rewritten.

```
polybot_v2/
  main.py                  # Trading loop (~300 lines, vs v1's 2000+)
  signal_v2.py             # The 3-line signal model
  vol_estimator.py         # EWMA realized variance
  pipeline_v2.py           # Calibrator refit + lambda tune + log
  config/settings_v2.yaml  # ~20 lines
```

Reuse `polybot.feeds.*`, `polybot.execution.*`, `polybot.db.*`, `polybot.discord_bot.*`. Those are solid; the simplification is in the model + pipeline only.

### Phase 2: Run v2 in paper mode for 7-14 days

Keep v1 running in paper mode in parallel (different DB / Discord channel). Compare:
- Sharpe ratio
- Win rate
- Drawdown depth
- Calibration error
- Operator interventions required (the qualitative metric that matters most)

### Phase 3: Switch live trading to v2

If v2 Sharpe ≥ 80% of v1's good-regime Sharpe AND has zero operator-intervention crises, retire v1. Archive `polybot/` to `polybot_v1_archive/` and rename `polybot_v2/` to `polybot/`.

If v2 underperforms v1 by more than 20%: keep v1 but document v2 as a backup the operator can switch to during regime shifts (since v2 will likely outperform v1 specifically when v1 is misfiring).

---

## File-by-File Changes

### Files to delete (or not port to v2)
- `core/regime.py` — replaced by EWMA
- `core/liquidation.py` — L3e dropped
- `core/garch_vol.py` — replaced by EWMA
- `core/crowd_bias.py` — not used in V2
- `core/gamma_exposure.py` — not used in V2
- `core/sprt.py` — diagnostic only, not needed
- `core/alpha_decay.py` — pipeline doesn't track per-signal decay in V2
- `core/exit_boundary.py` — V2 exit is a simple rule
- `feeds/binance_trades.py` — L3b dropped
- `feeds/binance_depth.py` — keep only if V2 retains L3 flow
- `feeds/bybit_feed.py` — L3e dropped
- `feeds/deribit_iv.py` — IV ratio not in V2 vol estimator
- `agents/ta_evolver.py` — no LLM-driven tuning in V2
- `agents/weight_optimizer.py` — no walk-forward backtest in V2
- `agents/bias_detector.py` — V2 logs trends to strategy_log, no auto-detection
- `agents/pipeline_tracker.py` — no decay tracking in V2
- `agents/pipeline_analytics.py` — no SPRT/KS in V2
- `agents/counterfactual_tracker.py` — kept optional as analysis tool, not in pipeline
- `agents/ghost_tracker.py` — kept optional as analysis tool

### Files to rewrite (much simpler)
- `core/signal_engine.py` → `signal_v2.py` (~150 lines vs ~600)
- `core/calibrator.py` → keep, simplify adoption gate to Brier-only (~80 lines)
- `agents/scheduler.py` → `pipeline_v2.py` (~200 lines vs ~1900)
- `core/bankroll_strategy.py` → simplify uncertainty discount, drop drawdown-velocity (~100 lines vs 250)
- `main.py` → strip flip logic, trailing exits, layer composition (~600 lines vs ~2200)

### Files to keep as-is
- `feeds/coinbase_feed.py`, `feeds/kraken_feed.py`, `feeds/binance_feed.py`, `feeds/clob_ws.py`, `feeds/market_scanner.py`, `feeds/chainlink_feed.py`
- `execution/*` (paper, live, base)
- `db/models.py`
- `discord_bot/*`
- `config/loader.py` (config file format simplifies but loader is fine)
- `indicators/*` (only if V2 keeps any indicator-based signal — if not, delete)

---

## Open Questions to Resolve Before Building

1. **Do we keep L3 (CLOB flow) or strip to pure-CDF?** Test both V2 variants in paper mode and compare. The simpler-still version with no flow signal at all might be the right answer if flow_weight optimal value drifts toward 0 in backtest.

2. **EWMA lambda — fixed or auto-tuned?** Start with fixed 0.94 (typical for daily vol; 5-min may want different). Add the grid-search-by-MSE only if fixed lambda underperforms.

3. **What about the indicator engine?** If V2 has zero indicator signal, the entire `indicators/` package becomes dead code. Decision: drop it. If we ever want to re-introduce a feature it's a clean rebuild, not pulling in the existing baggage.

4. **Counterfactual / ghost tracking — keep as offline analysis?** These produce useful operator insights ("you would have made $X if you'd held instead of scalping"). Worth keeping as a standalone CLI tool (`python -m polybot_v2.analyze_counterfactuals`) even if not in the pipeline.

5. **Discord bot commands — keep all of them?** `!status`, `!history`, `!pause`, `!resume`, `!session` are all useful. `!clear` and per-trade signal-strength alerts are operator-noise — consider trimming.

---

## Estimated Implementation Effort

| Phase | Effort |
|---|---|
| Set up `polybot_v2/` package structure | 1 day |
| `vol_estimator.py` (EWMA) | 0.5 day |
| `signal_v2.py` (3-line model + flow) | 1 day |
| Simplified `calibrator.py` (Brier gate) | 0.5 day |
| `pipeline_v2.py` (just refit + log) | 1 day |
| Strip `main.py` (remove flips, trailing exits, layer composition) | 2 days |
| Wire up tests | 1 day |
| Paper-mode parallel run setup | 0.5 day |
| **Total to first paper run** | **~7-8 days of focused work** |

Plus 7-14 days of paper-mode comparison before any live switch.

---

## What This Doesn't Solve

- **Market microstructure changes.** If Polymarket's CLOB liquidity vanishes or fees change, V2 is just as exposed as V1.
- **The Polymarket platform risk.** Smart contract bugs, depegs, regulatory action — same risk for both.
- **The fundamental edge question.** If 5-minute BTC binary on Polymarket has no edge to extract (after fees + slippage), no model architecture saves you. V2 just makes that fact easier to discover quickly.

---

## Decision Required

This plan represents a 1-2 week rebuild. Before starting:

1. **Confirm the goal.** Is operator-comprehensibility worth a 10-20% theoretical Sharpe haircut?
2. **Confirm the timing.** V1 just had three new self-adaptive features added today. Run those for a week first to see if V1 self-stabilizes — if it does, the pressure to rebuild drops.
3. **Confirm the scope.** The doc above is "minimal viable V2." More aggressive simplifications (no flow, no calibrator, just CDF + Kelly) are possible but riskier.

If you greenlight: branch off `main`, create `polybot_v2/`, build phase 1, paper-test 7 days, decide.

If you don't: keep this doc as a fallback. If V1 has another regime-shift crisis where the management code can't keep up, you'll know what the simpler alternative looks like.
