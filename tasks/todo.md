# PolyBot — open work

Canonical state + history: memory `edge-thesis-corrected-baseline.md`, `part-a-audit-findings.md`,
`part-b-audit-findings.md` (single sources of truth). This file lists only what's still to do.

## Immediate

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
- [ ] Optional hardening: set `DISCORD_ADMIN_IDS` in `polybot/config/.env` — Discord commands are
      open to all channel members until it's set.

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
- exit_edge_threshold replay re-prices only toward "hold longer"; simulating earlier scalps would
  need the CF hold-moment tick series — not built, candidates within -0.10..-0.03 rarely need it.
- Replay scores the recorded side only (live picks the best side per tick) — inherent to
  realized-fill replay. Tracker rollback compares backtest baseline vs realized fills (different
  populations) — accepted, the z-margin is symmetric.
- Circuit breaker sizes 0.40x below $85 bankroll (fresh small live accounts). `compute_atr` is
  production-dead but kept (test pins canonical Wilder math). Correlation ladder middle rungs
  unreachable with fixed rho priors — documented design.
