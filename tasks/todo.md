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
      ~100% CF coverage of resolutions. The running process predates the fix until the 12:01 AM
      restart — re-run `python scripts/repair_cf_keying.py --apply` once after restart to sweep
      any chimeras written in the gap (idempotent).
- [ ] After restart, confirm the new telemetry flows: `cross_venue_gap`/`fast_realized_vol_60s`
      + `clob_depth_top5_*`/`clob_book_age_s` non-null in fresh trade_context/ghost/CF records,
      and `state/price_sum_outliers.jsonl` accumulating lines.

## Research (exit engine — the validated edge)

- [ ] E3 latency thesis: recording restored 06-11 (was stripped in the 06-08 cleanup); run the
      right/wrong ITM-scalp split per the E3 design in tasks/goal.md once ~2-3 weeks of post-fix
      CF records exist (~06-25+).
- [ ] E1 cross-book arb: analyze `state/price_sum_outliers.jsonl` per the E1 design after ~7 days
      of recording (~06-18+). E2 thin-book premium: after ~10 days of CLOB depth stamps (~06-21+).
      E4 (perp basis tilt) remains design-only — implement its feed poll only when choosing to
      run it.

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

- 333 resolutions (pre-06-11) have no hold-CF — the keying bug wrote their record under the
  wrong pid and the worst-moment context is unrecoverable. Symmetric-replay coverage of holds
  is ~76% on history, 100% going forward.
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
