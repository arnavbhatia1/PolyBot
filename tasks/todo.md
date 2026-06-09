# PolyBot — open work

Canonical state + history: memory `edge-thesis-corrected-baseline.md` and `part-a-audit-findings.md`
(single sources of truth). This file lists only what's still to do.

## Immediate

- [ ] Bot restart pending — running process predates today's fixes; everything loads at the
      automatic 12:01 AM ET restart (`run_polybot.ps1`). Nothing to do unless restarting early.
- [ ] Tonight's first unfrozen pipeline run (06-09 23:45 ET): check adoption decisions and that the
      gate calibrator is non-None. exit_edge_threshold replay is now correctly specified (blended
      threshold + loss-cut exclusion), but legacy CF records lack the loss_cut flag — treat an
      exit_edge_threshold adoption with mild skepticism until ~a week of flagged records exists.
- [ ] Re-freeze trigger: if adoptions degrade realized $, PipelineTracker auto-reverts; manual
      re-freeze = recreate `memory/state/PIPELINE_FROZEN`.

## Watch — calibrated regime (clean days start 06-09 post-12:01 ET)

- [ ] Over ~10-15 clean calibrated days track realized $ at the lower volume:
      `python scripts/analyze_edge.py` (Q0 dollar P&L). Tripwires: cumulative $ > 0, no single day
      >40% of profit, high-confidence trades winning near their calibrated rate.
- [ ] Decision point: calibrated/selective bot stays positive on fresh days → proceed toward
      go-live; bleeds → reassess strategy.

## Go-live gate (unchanged)

- [ ] Statistically significant, day-clustered, OOS-positive $ net of real fee at calibrated volume
      → then start tiny ($50-100) with real USDC as live-data collection (no live DB exists yet;
      everything to date is paper). Realistic timeline ~2-3 weeks from 06-09.

## Deferred / known-and-accepted (context, not tasks)

- L2 direction carries a small Coinbase-vs-Binance basis bias (direction-conditional; calibrator
  can't see it). 200-sample "long-term" ATR buffer spans ~3 min → regime-shift floor + L3b
  vol_factor mostly inert (pipeline tunes a near-no-op in `atr_regime_shift_threshold`).
- Ghost censoring at min_edge/min_kelly boundary: those two knobs are only evaluable upward.
- Circuit breaker sizes 0.40x below $85 bankroll (fresh small live accounts).
- `compute_atr` is production-dead but kept (test pins canonical Wilder math). Correlation ladder
  middle rungs (0.55/0.70) unreachable with fixed rho priors — documented design, kept.
- exit_edge_threshold replay re-prices only toward "hold longer"; simulating earlier scalps would
  need the CF hold-moment tick series — not built, candidates within -0.10..-0.03 rarely need it.
