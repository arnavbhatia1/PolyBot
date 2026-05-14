# Live Regime Adapter — Implementation Plan

## Problem

The pipeline tunes parameters on the last 14 days of trades. This is a strong baseline for "what works on average" but is structurally blind to today's regime. By the time enough chop days accumulate to move the fit, the regime may have flipped back.

**Concrete failure (2026-05-14):** BTC range collapsed to $1,299 vs prior day $2,530. ATR-based L1 confidence stayed high. High-edge (≥0.15) WR dropped from 83% → 59%. Bot kept sizing up on L1 calls that strikes-crossed in chop. Pipeline-tuned `atr_sigma_ratio` couldn't react — it averages across the window.

## Goal

Two-layer architecture:

- **Slow layer (existing):** pipeline-tuned baselines from the 14d window. These are calibrated priors.
- **Fast layer (new):** a `LiveRegimeAdapter` that runs every tick, reads the last 30–60 min of market data, and emits **multipliers** on a handful of critical signal-engine params.

`SignalEngine` consumes `param × adapter_mult` instead of `param`. Baseline stays stable; today's behavior gets shaped by today's data.

## Architecture

```
polybot/core/regime_adapter.py:
  class LiveRegimeAdapter:
    state: rolling deques of (timestamp, btc_price, atr, autocorr,
                              spot_flow, trade outcomes)

    update(btc_price, atr, autocorr, spot_flow) -> None
    record_outcome(prob, correct) -> None
    multipliers() -> RegimeMultipliers
    snapshot() / restore()       # persists to memory/regime_state.json
```

**Wiring:**
- Instantiated alongside `SignalEngine` in `main.py`.
- `update()` called every price tick before signal evaluation.
- `record_outcome()` called from outcome-recording path when a trade resolves.
- `multipliers()` consumed by `SignalEngine.compute_probability` and `evaluate_hold`.
- Snapshot every minute to `memory/regime_state.json`; restore on startup.

**Telemetry:**
- All multipliers logged into `trade_context` per trade.
- Post-cycle pipeline correlates "adapter said X, outcome was Y" — enables tuning the adapter itself later.

## Signals monitored

All from the last 30–60 min of live data:

| Signal | Detects |
|---|---|
| Realized range / ATR-implied range | Low-vol chop where L1 is overconfident |
| Rolling autocorr (5/15/30-min) | Regime instability — L4 unreliable |
| Spot flow persistence (last 10 min) | Real bearish/bullish tape vs noise |
| High-conviction WR (last 20 trades) | Current model miscalibration |
| Strike-cross frequency (last hour) | Direct chop measure |

## Multipliers emitted

Five targeted multipliers, each clamped:

| Param | Range | Trigger |
|---|---|---|
| `atr_sigma_mult` | 0.8×–1.3× | Widen implied vol when realized < implied |
| `kelly_fraction_mult` | 0.5×–1.0× | Shrink size when recent high-conviction WR is poor |
| `max_flow_logit_mult` | 1.0×–1.7× | Let flow override L1 when L1 is failing |
| `logit_clamp_mult` | 0.7×–1.0× | Tighten to ±2.8 when chop detected |
| `momentum_weight_mult` | 0.3×–1.2× | Dampen when autocorr unstable across timescales |

## Build order

One feature at a time. Code, deploy, soak, audit, then next. Otherwise multiple adapter signals fire simultaneously and you can't isolate which one helps.

### Feature 0 — Scaffold
- New module `polybot/core/regime_adapter.py`
- Class skeleton + state deques
- Persist/restore to `memory/regime_state.json`
- Wiring point in `main.py` (per-tick update)
- Wiring point in `SignalEngine` (read multipliers)
- Base telemetry into trade context
- Returns `RegimeMultipliers(all=1.0)` for now — no-op
- **Coding: ~1 day**

### Feature 1 — `atr_sigma_mult` (chop detector)
- Deque of mid prices over last 30/60 min
- Compute high–low realized range
- Ratio against `atr × √minutes`
- Linear map to multiplier (e.g., ratio < 0.7 → mult 1.3)
- Unit tests for the math
- Integration test confirming `compute_probability` consumes the mult
- **Coding: ~0.5 day. Soak: 3–5 days.**

### Feature 2 — `kelly_fraction_mult`
- Deque of `(prob, correct)` updated on trade resolution
- Compute rolling WR among prob ≥ 0.75 in last 20 resolved trades
- WR < 50% → mult 0.5; WR ≥ 70% → mult 1.0; interpolate between
- Wiring touches outcome-recording path in `main.py`
- **Coding: ~0.5–1 day. Soak: 2–3 days.**

### Feature 3 — `max_flow_logit_mult`
- Track sign-stability of spot_flow over last 10 min
- Persistence ≥ 80% same sign → mult 1.5; ≤ 50% → mult 1.0
- **Coding: ~0.5 day. Soak: 2–3 days.**

### Feature 4 — `logit_clamp_mult`
- Reuses chop signal from Feature 1
- Chop detected → tighten clamp from ±4.0 to ±2.8
- **Coding: ~0.5 day. Soak: 2 days.**

### Feature 5 — `momentum_weight_mult`
- Compute autocorr at 5/15/30-min lookbacks
- Concordance rule: all three same sign + |max| > 0.20 → mult 1.2; flipping signs → mult 0.3
- **Coding: ~1 day. Soak: 2–3 days.**

**Total: ~4–5 days active coding, ~3 weeks calendar.**

## Pitfalls

1. **Positive feedback collapse.** Adapter shrinks Kelly because recent high-conviction trades flopped → next trades also flop because nothing changed → sizing collapses to floor permanently. Mitigation: when multipliers stay below 0.7× for >2 hours, force one full-size trade to recheck whether the regime is truly broken or just a bad window.

2. **Overfitting to noise.** 30 minutes is short. Use **two-of-three concordance** rather than any single signal firing the multiplier change.

3. **Don't compound with Platt.** Platt corrects calibration drift across days; adapter corrects regime drift within a day. Keep separable. Adapter should NOT touch `min_model_probability` or anything that interacts with Platt's output.

4. **Tests + state persistence.** Half the coding time. Every signal needs unit tests for the math, integration tests confirming consumption, and snapshot/restore so daily restarts don't reset adapter state.

## Shortcuts

- **Minimum viable: Feature 0 + Feature 1.** ~2 days coding, 5 days soak = 1 week. Captures the biggest win for chop-day failures. Everything else is incremental.

- **Skip the soak periods and ship all 5 simultaneously:** ~5 days total coding, but no way to attribute outcomes to individual adapter signals — same overfitting problem you'd get from making everything pipeline-tunable.

## When NOT to build this

- If `atr_floor_fraction` (already adaptive, lines 170–180 of signal_engine.py) gets pipeline-tuned aggressively enough, it may absorb much of the chop-response need. Worth testing pipeline-tunable `atr_floor_fraction` first as a one-line change.

- If the bot's chop-day Sharpe is acceptable just by tightening `kelly_fraction` baseline (a single pipeline change), the operational complexity of a live adapter may not be worth it.

## Related code

- `polybot/core/signal_engine.py:165–180` — `_effective_atr_floor()`, the existing real-time adapter pattern to model the new code on.
- `polybot/core/signal_engine.py:182–191` — `effective_momentum_weight()`, another existing real-time adapter.
- `polybot/main.py` — per-tick loop, where `update()` gets called.
- `polybot/config/param_registry.py` — would gain entries for adapter clamp ranges (manual-only, not pipeline-tunable).
