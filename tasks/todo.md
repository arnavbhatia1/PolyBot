# PolyBot — open work

Canonical state + history: memory `edge-thesis-corrected-baseline.md`, `part-a-audit-findings.md`,
`part-b-audit-findings.md` (single sources of truth). This file lists only what's still to do.
Edge verdict + diagnostic evidence: `tasks/goal.md` (2026-06-10, fable_dev).

## Immediate

- [ ] First pipeline run on fable_dev's symmetric exit replay: exit_edge_threshold candidates now
      produce nonzero deltas in both directions (previously structurally 0 for less-patient values)
      — confirm decisions look sane against the sweep numbers in tasks/goal.md.
- [ ] Verify the hold-CF keying fix on live records (fixed 06-10 evening): new hold-type CFs must
      carry the resolving position's id — expect no new duplicate pids in counterfactuals/ and
      ~100% CF coverage of resolutions (was 298 dups + 355 missing from flip re-entry mis-keying).

## Research (exit engine — the validated edge)

- [ ] Latency thesis: test `cross_venue_gap` / `fast_realized_vol_60s` (already in CF aux context)
      as exit-trigger refinements — formal design is E3 in tasks/goal.md; needs ~2-3 weeks of
      post-fix hold-CF records before the split test is meaningful.
- [ ] New-signal experiments E1 (out-of-band price-sum recorder), E2 (CLOB depth + book-age
      stamping), E4 (perp basis tilt) designed and ready in tasks/goal.md — implement the
      recording only when choosing to run one; do not implement speculatively.

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

## Refuted (don't re-chase without new evidence — details in tasks/goal.md)

- ITM patience: the +$1,914 ITM-scalp "headroom" is oracle-only. Every "never scalp above X"
  rule is net negative (-$865 at mp>=0.60, -$150 at mp>=0.75; negative 9-10/14 days); the
  threshold family can't separate wrong from right ITM scalps (he distributions overlap).
  Best boundary tweak (+$156, itm_start 0.65/damp 0.7) failed day-clustered test (t=1.57,
  8/14 days, 63% of gain from one day). Exit boundary stays as-is.
- L1-only entry model: beats the full stack (Brier 0.2334 vs 0.2349) but loses to the market
  price with significance (paired per-day Brier, t=2.23). No entry-side rebuild can help.
  Also refuted as a DELETION (06-10 adoption-replay test): z=-0.30 / day-t=-3.0 on the
  trailing-200 pool, sign flips with calibrator — L2-L6 stay.
- Segmented entry edge: zero of 44 segments (regime/time/ATR/depth/spread/distance, raw + L1)
  beat the market day-clustered; market-better t=3.7-4.4 overall. No pocket exists.
- Near-expiry phantom fade: sides >=0.85 with <90s left win 95.1% vs price 0.955; fading nets
  -0.49/$1 (t=-4.35). Cross-book in-band price-sum arb: $0 (sum truncated at 1.01/1.02).

## Deferred / known-and-accepted (context, not tasks)

- Pre-06-05 counterfactual records carry pnl at the old 0.018 fee basis (outcome restamp never
  touched CF arms; proven exactly — per-pid gaps equal fees*(1-0.018/0.07)). Inflates CF arms
  ~2pp gain on those days in any arm-swapping replay; decays out of the 60-day window; left
  unmutated by choice. Fee-corrected exit-edge numbers reported alongside in tasks/goal.md.
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
