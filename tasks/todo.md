# PolyBot — open work

Canonical state + history: memory `edge-thesis-corrected-baseline.md`, `part-a-audit-findings.md`,
`part-b-audit-findings.md` (single sources of truth). This file lists only what's still to do.
Edge verdict + diagnostic evidence: `tasks/goal.md` (2026-06-10, fable_dev).

## Immediate

- [ ] First pipeline run on fable_dev's symmetric exit replay: exit_edge_threshold candidates now
      produce nonzero deltas in both directions (previously structurally 0 for less-patient values)
      — confirm decisions look sane against the sweep numbers in tasks/goal.md.

## Research (exit engine — the validated edge)

- [ ] ITM patience: 409/653 ITM scalps would have resolved $1 (model pessimism mid-window wrong
      63%). Explore a stronger ExitBoundary resolution premium; validate via symmetric replay only.
- [ ] Latency thesis: test `cross_venue_gap` / `fast_realized_vol_60s` (already in CF aux context)
      as exit-trigger refinements.

- [ ] Bot restart pending — the running process predates today's Part B fixes; everything loads at
      the automatic 12:01 AM ET restart (`run_polybot.ps1`). Nothing to do unless restarting early.
- [ ] Tonight's pipeline run (23:45 ET) is the first on the fully fixed pipeline: confirm the
      nightly auto-commit actually commits (it was silently dead since 05-28 — fixed today), the
      calibration line in the summary is truthful (decision + reason, never bare "identity"), and
      adoption decisions look sane.
- [ ] exit_edge_threshold adoptions: mild skepticism until ~06-16 — pre-flag CF records lack the
      loss_cut flag and replay as ordinary scalps until a week of flagged records accumulates.
- [ ] Re-freeze lever if adoptions degrade realized $: recreate `memory/state/PIPELINE_FROZEN`
      (now a true analysis-only switch: no calibration change, no adoption, no auto-revert).

## Watch — calibrated regime (clean days started 06-09)

- [ ] Over ~10-15 clean calibrated days track realized $ at the lower volume:
      `python scripts/analyze_edge.py` (Q0 dollar P&L). Tripwires: cumulative $ > 0, no single day
      >40% of profit, high-confidence trades winning near their calibrated rate.
- [ ] Decision point: calibrated/selective bot stays positive on fresh days → proceed toward
      go-live; bleeds → reassess strategy.

## Go-live gate (unchanged)

- [ ] Statistically significant, day-clustered, OOS-positive $ net of real fee at calibrated volume
      → then start tiny ($50-100) with real USDC as live-data collection (no live DB exists yet).
      Realistic timeline ~2-3 weeks from 06-09.

## Deferred / known-and-accepted (context, not tasks)

- Ghost censoring at min_edge/min_kelly boundary: those two knobs are only evaluable upward.
- L2 direction carries a small Coinbase-vs-Binance basis bias (direction-conditional; calibrator
  can't see it). 200-sample "long-term" ATR buffer spans ~3 min → regime-shift floor + L3b
  vol_factor mostly inert.
- exit_edge_threshold replay is symmetric (fable_dev): hold records re-price to their worst-moment
  hypothetical scalp when the candidate would fire (whipsaw + deep-loss-hold branches respected).
  Remaining approximation: the snapshot is the worst-holding_edge moment, not a tick series — a
  candidate could fire at another moment where the boundary sat higher. Accepted.
- Replay scores the recorded side only (live picks the best side per tick) — inherent to
  realized-fill replay. Tracker rollback compares backtest baseline vs realized fills (different
  populations) — accepted, the z-margin is symmetric.
- Circuit breaker sizes 0.40x below $85 bankroll (fresh small live accounts). `compute_atr` is
  production-dead but kept (test pins canonical Wilder math). Correlation ladder middle rungs
  unreachable with fixed rho priors — documented design.
