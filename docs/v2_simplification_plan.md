# PolyBot V2 — Simplification Plan

## Context

V1 has been substantially pruned: the L3c wall layer is gone, legacy fallback methods are gone, historical-justification comments are gone, the Claude prompt is half its original size, and five production safety fixes (atomic DB writes, AuthError detection, startup reconciliation, feed-staleness gate, Chainlink orphan fallback) are in place. The codebase is now ~6k lines for the trading + pipeline path (down from ~7k pre-cleanup).

The remaining complexity is *real* complexity, not bloat. Eight signal layers, ~30 tunable params, and a 7-stage pipeline are still more than one operator can hold in their head at 2 AM. This plan is the fallback if V1's complexity ever costs you sleep, or if a live-mode regime shift produces behavior that needs an LLM to triage.

This is **not a recommended next step** — V1 should run live first and earn its keep or expose its real failure modes. This doc is the option you reach for *if* V1 misfires in production in a way that the existing safety fixes (auto-revert, crisis mode, decay tracking, AuthError exit) can't catch.

---

## Design Principles for V2

1. **One operator can hold the whole model in their head.** Every parameter explainable in one sentence with a known direction-of-effect.
2. **Robustness over theoretical edge.** A simpler model with 80% of the edge but no failure modes you don't understand beats a complex model that needs an LLM to triage.
3. **Adaptive by construction, not by management code.** EWMA realized variance adapts on its own; no `min_atr` floor, no regime-shift auto-scaling, no adaptive compression layer needed on top.
4. **Pipeline tunes 1-3 things, not 30.** Most V1 params are knobs that should never need to move.
5. **Inherit the V1 production safety fixes verbatim.** Atomic DB writes, AuthError, reconciliation, feed staleness, orphan fallback are load-bearing for live money — V2 keeps them all.

---

## What V1 Already Has (No Change Needed)

These are already lean and don't get touched in V2:

- **Production safety fixes** (added in pre-live hardening): atomic open_position+bankroll, AuthError fail-loud, startup share reconciliation, feed-staleness gate, 30-min Chainlink orphan fallback. **All keepers.**
- **Circuit breaker** (`execution/circuit_breaker.py`, 161 lines) — tier-locked floor with concave Kelly scaling. Working as designed.
- **Feed layer** — Coinbase / Kraken / Binance.US / CLOB WS / Chainlink RTDS are clean WebSocket-driven feeds. Keep all of them.
- **Execution layer** (`execution/base.py`, `live_trader.py`, `paper_trader.py`) — fee math is canonical, FOK retry path is correct, HTTP/2 keepalive is in place.
- **DB layer** (`db/models.py`, 283 lines) — single source of truth for bankroll, atomic transaction support added.
- **Discord bot** — `!status`, `!history`, `!pause`, `!resume` are all useful. Auto session-banner already removed.
- **Wall layer (L3c)** — already deleted.
- **Legacy TAEvolver fallbacks** — already deleted; replaced by `LocalRecommender`.

---

## V1 Components Still Targetable for Removal

### Signal layers that can go

| Layer | Current state | V2 disposition |
|---|---|---|
| L1 Student-t CDF | Load-bearing | **Keep** |
| L2 regime autocorrelation | 2 params (`regime_weight`, `regime_lookback`) + a regime-conditional 1.5×/0.5× momentum amplifier | **Drop.** EWMA vol absorbs the regime-shift work. |
| L3 CLOB flow | The only price-independent signal | **Keep** as one weight, optional |
| L3b spot CVD | Marginal added signal; collinear with L3 | **Drop.** Saves a feed (`binance_trades.py`). |
| L3e liquidation pressure | Sparse signal; OI drops are rare | **Drop.** Saves a feed (`bybit_feed.py`). |
| L4 indicator momentum | RSI/MACD/Stoch/OBV/VWAP weighted mix; `momentum_weight ≈ -0.02` (essentially off in V1) | **Drop entirely.** Whole `indicators/` package + the regime-conditional amplifier go. |
| L5 prev_margin carry | A memoryless market means last window's margin shouldn't predict this window | **Drop.** |
| Adaptive probability compression | Reactive correction in V1 | **Drop.** EWMA vol does this work upstream. |

### Vol estimator: ATR → EWMA

V1 uses ATR(7) with three guard rails: `min_atr` floor, `_ATR_FLOOR_FRACTION × rolling_20`, and a regime-shift auto-widen against a 200-sample long-term reference. That's three tuning surfaces stacked because ATR doesn't adapt naturally to vol regimes.

V2 replaces all of it with one line:
```python
vol = (ewma_realized_variance(returns, lambda=0.94)) ** 0.5
```
EWMA on squared 1-min returns weights recent observations exponentially. When vol drops, the estimate drops; when it spikes, the estimate spikes. No floor needed, no regime-shift detector needed. One parameter (`lambda`) replaces three.

### Sizing knobs that can go

V1 stacks: `kelly_fraction × kelly_edge × circuit_breaker_mult × uncertainty_discount × entry_phase_mult × concurrent_mult × consensus_mult × regime_mult` — eight multipliers, several hardcoded. Half of them are "operates near noise" per the comments.

V2 keeps three: `kelly_fraction × kelly_edge × circuit_breaker_mult`. That's it. Capped at `max_single_position_usd`.

### Exit logic that can go

V1 has `evaluate_hold` + fee-aware threshold + `ExitBoundary` MDP-based optimal threshold + trailing exit ($0.50 / $0.65 / 15%) + flip-trade re-entry + `exit_config` sub-dict (6 params). That's a lot of state for one decision.

V2:
```python
if seconds_remaining < 30 OR model_prob_for_held_side < 0.45:
    scalp at market
else:
    hold to resolution
```
No trailing exits, no patience/urgency adjustments, no flip re-entries.

### Pipeline complexity to cut

V1 pipeline (1987 lines in `scheduler.py`) has: BiasDetector, Platt, distribution-shift KS, SPRT, TAEvolver (Claude + LocalRecommender), WeightOptimizer (per-param walk-forward + regime stratification + combined-interaction check), PipelineTracker (decay tracking + auto-revert), crisis mode + sustained-crisis Kelly halving.

V2 pipeline keeps:
1. **Platt refit weekly** with Brier-score holdout gate (proper scoring rule, no walk-forward backtest needed). ~80 lines.
2. **EWMA lambda tune weekly** (optional grid search over {0.90, 0.92, 0.94, 0.96} with realized-vs-predicted MSE). ~40 lines. Skip in v2.0; add only if fixed lambda underperforms.
3. **Daily summary append** to `strategy_log.md` (today's WR, PnL, n_trades, calibrator A/B). No proposals, no LLM, no decay tracking. ~30 lines.

**Total pipeline:** ~150 lines vs V1's ~1987.

The operator reads `strategy_log.md` weekly and decides whether `flow_weight` or `kelly_fraction` should change. Manually. No automation.

---

## V2 Architecture

### Signal model (3 lines)

```python
vol     = ewma_vol(returns_1min, lambda=0.94)
z       = (btc - strike) / (vol * sqrt(minutes_remaining))
prob_up = StudentT(df=5).cdf(z)
```

### Optional flow adjustment (1 weight, in logit space)

```python
logit_p += flow_signal * flow_weight
```
Where `flow_signal` is the existing `book_imbalance × 0.6 + trade_flow × 0.4`. Set `flow_weight = 0` to disable.

### Calibration (last step)

```python
if calibrator:
    prob_up = calibrator.calibrate(prob_up)
```
Same Platt sigmoid as V1, but adoption gate simplified to Brier improvement.

### Sizing

```python
kelly_edge = max(0, (prob - market_price) / (1 - market_price))
size       = bankroll × kelly_fraction × kelly_edge × circuit_breaker_mult
size       = min(size, max_single_position_usd)
```

### Exit

```python
if seconds_remaining < 30 or model_prob_for_held_side < scalp_prob_threshold:
    scalp
else:
    hold
```

---

## V2 Tunable Parameter Surface

The complete operator-knowable list:

| Param | Range | What it does |
|---|---|---|
| `ewma_lambda` | 0.90–0.97 | EWMA decay for realized variance. Lower = faster vol adaptation. |
| `flow_weight` | 0.00–0.10 | Logit-space weight on CLOB flow. 0 disables flow. |
| `student_t_df` | 4–6 | CDF tail fatness. Probably never moves. |
| `kelly_fraction` | 0.05–0.20 | Sizing aggressiveness. Operator-tuned, not pipeline. |
| `min_edge` | 0.02–0.06 | Skip trades below this edge. |
| `min_kelly` | 0.005–0.02 | Skip trades below this Kelly fraction. |
| `scalp_prob_threshold` | 0.40–0.50 | Scalp when held-side prob drops below this. |

**Seven parameters.** Each direction-of-effect is obvious.

Risk caps stay manual-only (same as V1):
- `max_single_position_usd`
- `max_concurrent_positions`
- `circuit_breaker.floor_pct`

---

## Honest Tradeoff

Given how much V1 has already been pruned, the V2 gap is smaller than it would have been pre-cleanup:

| Capability | V1 (current) | V2 | Net |
|---|---|---|---|
| Multi-signal fusion | 6 layers | 1 (CDF) + 1 optional (flow) | Loses ~3-7% theoretical edge |
| Indicator momentum (L4) | Effectively off (`-0.02`) | Removed | ~0% (already noise) |
| Spot CVD (L3b) | Active | Removed | Loses ~0-2% (collinear) |
| Liquidation (L3e) | Active, sparse | Removed | Loses ~0-1% (rare) |
| Regime-shift adaptation | Crisis mode + auto-revert + adaptive compression | EWMA on squared returns | **Improvement** — adapts continuously, not in discrete steps |
| Pipeline auto-tuning | Claude + LocalRecommender + walk-forward | Brier refit + manual review | Loses experimentation; gains operator confidence |
| Operator can debug at 2 AM | No | Yes | **Massive improvement** |

**Estimated Sharpe:** V1 best ~0.18, V2 expected ~0.12–0.16 in good regimes with much narrower variance and zero "the bot scalped at the wrong threshold and left $1.6k on the table" scenarios.

---

## Migration Strategy

### Phase 1: Build v2 in parallel (don't replace v1)

```
polybot_v2/
  main.py                  # Trading loop (~400 lines, vs v1's 2544)
  signal_v2.py             # 3-line signal model (~80 lines)
  vol_estimator.py         # EWMA realized variance (~30 lines)
  pipeline_v2.py           # Refit + log only (~150 lines)
  config/settings_v2.yaml  # ~25 lines
```

Reuse `polybot.feeds.coinbase_feed`, `feeds/kraken_feed`, `feeds/binance_feed`, `feeds/clob_ws`, `feeds/market_scanner`, `feeds/chainlink_feed`, `execution/*`, `db/models`, `discord_bot/*`. They're solid; the simplification is in the model + pipeline only.

### Phase 2: Run v2 in paper for 7-14 days alongside live v1

Different DB file (`polybot_v2.db`), separate Discord channels (`polybot-v2-trades`, `polybot-v2-daily`). Compare on the same trade windows:
- Sharpe ratio
- Win rate
- Drawdown depth
- Calibration error (Brier score)
- **Operator interventions required** — this is the qualitative metric that matters most

### Phase 3: Switch live trading to v2

If v2 Sharpe ≥ 80% of v1's good-regime Sharpe AND has zero operator-intervention crises in the paper window, archive v1 to `polybot_v1_archive/` and rename `polybot_v2/` → `polybot/`.

If v2 underperforms by more than 20%: keep v1 but document v2 as a backup the operator can switch to during regime shifts (since v2 will likely outperform v1 specifically when v1 is misfiring).

---

## File-by-File Disposition

### Already cleaned in V1, no further action

- `feeds/binance_depth.py` (now 92 lines, just WS depth-USD)
- `agents/ta_evolver.py` (now 99 lines, no dead methods)
- `agents/weight_optimizer.py` (now 130 lines, no historical comments)
- `core/signal_engine.py` (now 463 lines, wall layer + history stripped)

### Files to delete (or not port to v2)

- `core/regime.py` — replaced by EWMA
- `core/liquidation.py` — L3e dropped
- `core/garch_vol.py` — replaced by EWMA
- `core/crowd_bias.py` — not used in V2
- `core/gamma_exposure.py` — not used in V2
- `core/sprt.py` — diagnostic only
- `core/alpha_decay.py` — not used in V2
- `core/exit_boundary.py` — V2 exit is one rule
- `core/adverse_selection.py` — kept in V1, drop in V2 (rolling adverse-rate gate is V1-specific)
- `feeds/binance_trades.py` — L3b dropped
- `feeds/bybit_feed.py` — L3e dropped
- `feeds/deribit_iv.py` — IV ratio not in V2 vol estimator
- `feeds/binance_depth.py` — drop if V2 doesn't use depth gate; keep as-is if it does
- `agents/ta_evolver.py` — no LLM-driven tuning in V2
- `agents/local_recommender.py` — no rule-based fallback needed (no Claude in V2)
- `agents/weight_optimizer.py` — no walk-forward backtest in V2
- `agents/bias_detector.py` — V2 logs trends manually
- `agents/pipeline_tracker.py` — no decay tracking in V2
- `agents/pipeline_analytics.py` — no SPRT/KS in V2
- `agents/claude_client.py` — no LLM in V2
- `indicators/*` — entire package, L4 dropped

### Optional kept as offline tools (not in V2 pipeline)

- `agents/counterfactual_tracker.py` — useful for "what if I'd held" post-mortems; expose as `python -m polybot_v2.analyze_counterfactuals`
- `agents/ghost_tracker.py` — same; "what trades did the gates reject"

### Files to rewrite (much simpler)

| V1 | V2 | Lines (V1 → V2) |
|---|---|---|
| `core/signal_engine.py` | `signal_v2.py` | 463 → ~150 |
| `core/calibrator.py` | keep, simplify gate to Brier-only | 99 → ~80 |
| `agents/scheduler.py` | `pipeline_v2.py` | 1987 → ~150 |
| `core/bankroll_strategy.py` | drop drawdown-velocity, keep uncertainty discount only | 63 → ~30 |
| `main.py` | strip flip logic, trailing exits, layer composition, regime classifier | 2544 → ~600 |

### Files kept verbatim (load-bearing)

- `feeds/coinbase_feed.py`, `feeds/kraken_feed.py`, `feeds/binance_feed.py`, `feeds/clob_ws.py`, `feeds/market_scanner.py`, `feeds/chainlink_feed.py`
- `execution/*` (paper, live, base, circuit_breaker, correlation)
- `db/models.py` (atomic transaction support already added)
- `discord_bot/*`
- `config/loader.py`
- `core/order_flow.py` (used by L3 if kept)
- `core/returns.py` (gain_pct math)

---

## Open Questions to Resolve Before Building

1. **Do we keep L3 flow at all?** With the V1 pipeline now logging an empirical directional table per `pipeline_run_log.json`, you have data on whether flow_weight up/down has shown realized live-Sharpe lift. If 30 days of V1 data shows `flow_weight` direction has live delta < 0.005, drop flow entirely — pure CDF.

2. **EWMA lambda — fixed or auto-tuned?** Start fixed at 0.94. Auto-tuning is the kind of management code V2 is supposed to delete, not add.

3. **Counterfactual / ghost trackers — keep as offline analysis tools?** Yes. Useful operator insights ("you would have made $X if you'd held"). One-shot CLI invocation, not in the pipeline.

4. **Adverse-selection gate — keep in V2?** V1's `adverse_selection_threshold` is a real, validated filter (informed-flow detection on rolling 30-s post-fill reversal). It's manual-only and operator-set. Probably worth porting as a single threshold check, no rolling adverse-rate machinery.

5. **Discord bot commands — keep all of them?** Yes; they're already minimal. Don't bring `!session` if you don't use it (you don't auto-fire it anymore). `!clear` is operator-noise — drop.

---

## Estimated Implementation Effort

Given V1 is already cleaner than the original plan assumed:

| Phase | Effort |
|---|---|
| Set up `polybot_v2/` package | 0.5 day |
| `vol_estimator.py` (EWMA) | 0.5 day |
| `signal_v2.py` (3-line model + optional flow) | 0.5 day |
| Simplified `calibrator.py` (Brier gate) | 0.5 day |
| `pipeline_v2.py` (refit + log) | 1 day |
| Strip `main.py` (the largest task) | 2-3 days |
| Wire up tests | 1 day |
| Paper-mode parallel run setup | 0.5 day |
| **Total to first paper run** | **~6-7 days of focused work** |

Plus 7-14 days of paper-mode comparison before any live switch.

---

## What V2 Doesn't Solve

- **Market microstructure changes.** If Polymarket CLOB liquidity collapses or fees spike, V2 is exposed identically.
- **Polymarket platform risk.** Smart contract bugs, USDC depeg, regulatory action — same risk for both.
- **The fundamental edge question.** If 5-min BTC binary on Polymarket has no extractable edge after fees and slippage, no model architecture saves you. V2 just makes that conclusion easier to reach quickly.

---

## Decision Trigger

Don't build V2 preemptively. Build it if any of the following happens in the next 30 days of live V1:

1. **A regime shift causes V1 to misfire and the existing safety code (auto-revert, crisis mode, AuthError) doesn't catch it.** This is the canonical "V1 is too complex to debug" failure.
2. **The Claude pipeline costs more than it earns.** Track adoption decay vs API cost. If `format_decay_analysis` shows >50% adoptions decaying for 4+ consecutive weeks, the LLM is overfitting and V2's manual-tune model wins.
3. **You spend more than 1 hour/week debugging V1 behavior** to understand why it did something. Operator-comprehensibility was the goal; if V1 fails at it, switch.

Otherwise: V1 with its current fixes is what you have. V2 is the documented escape hatch.

If you greenlight: branch off `main`, create `polybot_v2/`, build phase 1, paper-test 7-14 days, decide.

If you don't: this doc stays as the fallback. The trigger conditions above are objective; revisit them at the end of each month of live trading.
