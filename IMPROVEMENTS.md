# IMPROVEMENTS — proposals, by trade lifecycle stage

Anything that changes trading behavior (a signal, a gate, exit math, sizing) must go through
the bot's own backtest/adoption pipeline, not a hand-edit. Each item: the idea, why it should
help, rough effort, whether it needs backtest validation, and any new data it would require.
Sourced from the §1–§19 audit (`tasks/audit/A1–A8.md`) and the dead-code sweep.

## ✅ Implemented this pass (safe, non-behavioral subset)
Applied directly with the suite green; everything else below remains a proposal for the pipeline:
- **Consistency guard tests** (Stage 6) — `f84b6af`: registry↔settings `[P]/[M]` tag check + Discord
  command coverage. Immediately caught 3 more mistags (normal_fraction / late_max_penalty / flip_edge_premium were `[P]` but are MANUAL_ONLY).
- **Feed connection-state telemetry** (Stage 1) — `e05fbdc`: `mark_connected/disconnected` wired into all 6 WS feeds; surfaces in the `feed_health` card.
- **clob_ws shared JSON loader** (Stage 1) — `8177cef`.
- **Gamma-vs-Chainlink resolution-disagreement logging** (Stage 5) — `76a721c`.
- **Crisis trailing-3-day timestamp parse** (Stage 6) — `998f6bd`.
- (`consensus_dead_zone` single-sourcing was already in place via `default_for` — no change needed.)

---

## Stage 1 — Getting data from feeds  (§15, `feeds/*`, §9/§10 staleness)

- **Wire `StalenessTracker.mark_connected/mark_disconnected` into the feeds.** The capability
  exists + is tested but no feed calls it, so `feed_health` can't tell "connected but quiet"
  from "socket down" (an `n=0` snapshot is ambiguous). Call them on WS connect/close in each
  feed. *Effort: low. Backtest: no (telemetry). Data: adds `connected` to feed_staleness.json.*
- **Separate price-arrival cadence from socket-liveness on Coinbase.** `feed_staleness` blends the
  ~1 Hz heartbeat with per-trade ticker, so P50/P95 track liveness and mask quiet-trade gaps. Keep
  the blended metric for liveness but add a ticker-only inter-arrival counter for price-staleness
  fidelity. *Effort: low. Backtest: no. Note: ticker-only alone would raise false "degraded" flags
  in quiet markets — that's why it's additive, not a replacement.*
- **Promote the cross-venue (Coinbase↔Binance) gap from DEBUG to a surfaced metric.** Rollups show
  frequent gaps; persisting P50/P95 (and optionally gating entries on an extreme divergence) turns a
  silent log into a usable signal. *Effort: low–med. Backtest: yes if it gates entries. Data: persist gap stats.*
- **Spike/outlier guard on a WS price print before it updates `_fastest_btc_price`.** A single print
  that jumps >Nσ then reverts can flip P(side) on a tick the resolver never sees. Reject-and-wait on a
  lone outlier. *Effort: low–med. Backtest: replay with/without the filter on stored ticks.*
- **Third corroborating BTC venue (e.g. Kraken/Bitstamp) for the decision-instant price.** A 2-of-3
  agreement check would harden the Coinbase-only choice against a transient venue divergence across the
  strike. *Effort: med. Backtest: hard — needs new feed history first.*
- **New candidate inputs to capture then evaluate:** perp funding rate, options-implied vol (as an ATR
  cross-check / L6 feature), book-depth *dynamics* over time, Chainlink round cadence. *Effort: med.
  Backtest: capture first, then evaluate via the L6/pipeline path.*
- **Unify JSON parsing in `clob_ws`** — it re-implements parsing inline instead of `feeds/_json.loads`.
  *Effort: trivial. Cleanup (consistency / one orjson fast-path).*

## Stage 2 — Signal-engine computation  (§2)

- **Per-layer ablation harness.** Generalize the `scripts/holdout_kill_l4.py` pattern to every layer
  (L1–L6) to quantify each layer's marginal log-loss / Sharpe contribution; retire or force-evidence
  near-dead layers. L6 is inert (all weights 0.0) — confirm the structural probes are actually firing it.
  *Effort: med. Backtest: yes (it is a backtest).*
- **Make the L3+L3b redundancy discount (`_FLOW_REDUNDANCY = 0.5`) a probed constant.** It's hardcoded;
  is 0.5 empirically right? Expose it via a ParamSpec + backtest path. *Effort: low + backtest path. Backtest: yes.*
- **Conditional calibration.** A single isotonic map may miss systematic miscalibration buckets (by
  regime, time-of-window, edge size). Evaluate per-regime or per-phase calibrators (or a 2-D map).
  *Effort: med–high. Backtest: yes. Data: already in `trade_context`.*
- **Tighten §2 L4 wording (doc).** "polarity FLIPS / is replaced" understates that the code *cross-fades*
  the contrarian and continuation terms continuously for `0<t<1`. Clarify so it's never refactored into a
  discontinuous switch. *Effort: trivial doc.*
- **De-duplicate `scripts/holdout_kill_l4.py`'s `compute_momentum` reconstruction.** It re-implements the
  L4 math from scratch (drift hazard if `compute_momentum` changes). Import the real function, or delete
  the script once L4 keep/kill is settled. *Effort: low.*
- **Single-source minor defaults** — `consensus_dead_zone` from the registry rather than a local literal;
  document the ATR-floor warm-up thresholds (5/50 samples) in §2. *Effort: trivial.*

## Stage 3 — Entry logic  (§3, §4)

- **Stop passing a cold `cvd_accel_norm` as `0.0` into consensus.** Today it's benign (the 0.05 dead-zone
  drops it), but it violates the §10 "feed-cold ≠ real-zero" contract and would silently count a cold feed
  as a real-zero signal if the dead-zone ever shrank. Omit cold signals from the consensus set instead.
  *Effort: low. Backtest: no (defensive correctness).*
- **Make the concurrent-correlation prior empirical.** ρ is a fixed +0.75 (same-side) / −0.25 (opposite).
  Estimate it from realized same-/opposite-side concurrent-position outcomes. *Effort: med. Backtest: yes.
  Data: already have concurrent-position outcomes.*
- **Regime-/time-conditional entry gates.** Read `gate_stats` for which gates actually bind, then test
  making `min_edge` / `min_model_probability` regime- or time-of-window-conditional. *Effort: med. Backtest: yes.*
- **Audit the sizing multiplier stack for double-counting.** `consensus × adverse × concurrent` may
  penalize the same informed-flow twice (adverse-selection and CVD-decel both react to informed flow).
  Measure the inter-multiplier correlation and de-correlate if needed. *Effort: med analysis. Backtest: yes.*
- **Micro-efficiency:** `slippage_pct` is computed twice with identical args on the entry path (compute
  once); the `thin_book_depth` vs `min_size` telemetry split at a $0.10 seam is arbitrary. *Effort: trivial.*

## Stage 4 — Scalping / holding  (§6, §7)

- **Counterfactual-regret analysis.** Use the counterfactual tracker to detect systematic scalp-too-early
  vs hold-too-long, and feed that bias into the `exit_edge_threshold` probe direction. *Effort: med.
  Backtest: yes (the counterfactual path exists).*
- **Flip net-positivity by `flip_count`.** Segment realized flip outcomes by count to confirm unbounded
  re-entry is net-positive after round-trip cost; tune `flip_edge_premium` / add a cap if not. *Effort:
  low–med analysis. Data: `flip_count` is stamped.*
- **Whipsaw-cushion width.** Probe alternatives to the fixed `0.5×ATR` cushion against the
  `loss_cut_fired` / `loss_cut_whipsaw_blocked` stats. *Effort: med. Backtest: partial (counterfactual).*
- **Exit-boundary vs hold-to-resolution.** Quantify money left on the table by the binary-payoff curve vs
  always-hold, per regime, from the counterfactual tracker. *Effort: med.*

## Stage 5 — Aftermath of a trade  (§8, §10)

- **Background the winning-redeem poll.** Live `_resolve_bankroll` awaits up to 60 s synchronously inside
  the per-tick management loop, freezing management of any concurrent position. Make it a background task
  or cap the wait. *Effort: med. (Real latency issue once `max_concurrent_positions > 1`.)*
- **Monitor Gamma-vs-Chainlink resolution agreement.** Resolution is now oracle-first with a coherent-book
  fallback (`95387f4`); log/alert when the book fast-path and the oracle would disagree — a cheap feed-health
  signal and a guard against oracle drift. *Effort: low.*
- **Tighten L1 ATR-floor backtest fidelity.** §11 admits it's "approximate" (one snapshot per trade vs live
  per-tick buffers). Stamp the rolling-20 / long-term-200 ATR buffers per decision tick so the backtest
  matches live across vol-regime transitions. *Effort: med. Data: stamp the buffers into `trade_context`.*
- **Consolidate the FOK ladder-walkers.** `compute_buy_vwap`, `_estimate_fok_walk`, and `_precheck_rejects`
  each walk the ask ladder; unify into one shared function to remove the §17 shared-math drift hazard.
  *Effort: med.*

## Stage 6 — Learning pipeline  (§11, §12)

- **Parse the crisis trailing-3-day cutoff instead of lexicographic ISO-string compare** (the lone
  non-parsed date comparison in the pipeline; works today but inconsistent with the parse-everywhere
  convention). *Effort: trivial.*
- **Add two guard tests:** a registry↔`settings.yaml` `[P]/[M]` tag-consistency test (would have caught the
  `deep_loss_hold_threshold` mistag) and a Discord command-coverage test (every command in the help text +
  docstring resolves to a handler — would have caught the phantom commands). *Effort: low. Strengthens the suite.*
- **Single-source `DEFAULT_FEE_RATE`.** The literal `0.018` recurs as default args in `core` (signal_engine,
  exit_boundary, counterfactual_tracker) because `core` can't import `execution` without a layering
  inversion. Relocate the constant to a core-level module so every layer references one definition (update
  the §13 "`base.DEFAULT_FEE_RATE`" reference accordingly). *Effort: med. The operative fee is already
  single-sourced via call sites; this closes the latent drift in the fallbacks.*
- **Calibrate the edge half-life.** Measure how fast the realized edge actually decays and tune the 60-day
  window + `0.94^days` (~11-day) recency to match — microstructure edge may decay faster or slower. *Effort:
  med analysis.*
- **Review adoption/revert frequency.** Read `pipeline_run_log` to confirm the gates aren't too strict
  (nothing ever adopts) or too loose, and that the structural probes are firing (`exit_edge_threshold ∈
  {−0.08,−0.05,−0.03}`, L6 turn-on at 0.005). *Effort: low.*

## Stage 7 — Feedback into the next day  (§11 adoption→prod, §19, §16)

- **Monitor live-vs-gate calibrator divergence.** The freshest-7-day live calibrator can chase noise even
  with the OOS gate split; alert when the live and gate-reference maps diverge materially, and consider
  widening the live window if it proves unstable. *Effort: low (monitor).*
- **Warm the ATR buffers across the midnight restart.** The rolling-20 / long-term-200 ATR buffers start
  cold each restart, so the first post-restart windows run on a partial floor (regime discontinuity at the
  boundary). Persist + reload them. *Effort: med. Data: persist the ATR buffers.*
- **Rename/annotate `log_return`.** It's telemetry-only (never in sizing/backtest/calibration per §13/§17),
  but the name invites the exact misuse the guardrail forbids. Rename to `log_return_telemetry` or add a
  prominent note at the definition. *Effort: trivial.*
- **Carry feed connection-state across the restart** (depends on Stage-1 `mark_connected` wiring): the
  `feed_health` card could then distinguish a feed slow to warm from one that's down on the first morning ticks.
