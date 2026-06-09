# PolyBot — current work

Canonical state: memory `edge-thesis-corrected-baseline.md` (single source of truth).

## IN PROGRESS (2026-06-09): Calibration tail-overconfidence fix — iron-tight
Top-knot slam (raw>=0.97 -> ~1.0) found on first clean day; oversizes false-certainty trades.
Fix = isotonic + Beta-prior smoothing (prior=0.10*n, 50 anchors) + output clamp [0.018,0.982].
- [x] Offline prototype on real history: realized raw>=0.97 ~= 0.66; fix lands 0.97->0.68
- [x] PRIOR_FRAC=0.10 locked — verified on live-sized 1663 pool: slam gone, dLL preserved
      (+0.073 vs +0.075), OOB-CI lower bound IMPROVES (+0.052 vs +0.023, less overfit)
- [x] Implement in calibrator.py (augment-and-fit shared by fit + bootstrap; clamp [0.15,0.85];
      output clip in calibrate(); load() clamps legacy slam; operator-owned constants)
- [x] Tests: 4 new guards (slam-impossible, output bounded, mid-range preserved, load caps legacy) — 538 pass
- [x] Re-fit + re-stamp isotonic_params.json (y_max 1.0->0.85, n=1574 freshest-7d; adopted, +0.080 / CI +0.059)
- [x] Re-scored 06-09 slams: 6/8 overpriced Up "certainties" now SKIP (-EV); slam-cohort stake shrinks 8.1x
- [x] Docs: CLAUDE.md §2 + MODEL_IMPROVEMENTS.md updated
- [ ] RESTART the bot — on stale pre-fix code (started 10:52 AM 06-08); restart loads new guards +
      re-stamped curve (load-clamp caps the old curve immediately regardless)

## Done (2026-06-08)
- [x] 12-day frozen baseline analyzed; microstructure A/B/C = no taker edge (chapter closed)
- [x] Calibrator train/serve domain bug fixed (fit on P(up)+up-outcome, all 3 fit sites) + adopted — validated OOS (+0.053 nats); live at next restart, weight-freeze still ON
- [x] Analyzer fee-base bugs fixed; dollar P&L is the headline metric (`analyze_edge.py` Q0)
- [x] Staying TAKER (edge is taker-native; maker worse post-speed-bump-removal)
- [x] Removed dead code: microstructure recorder + wiring + scripts + local data, and dormant maker infrastructure (taker-only FOK now). 534 tests pass.
- [x] Reconciled MODEL_IMPROVEMENTS.md with the thesis (doc-only, no live code): proven edge = sizing+exit; calibration is Tier 1 (sizing lever); forecasting items (L6/vol/new feeds) demoted to Tier 3 "low ceiling, hard-gated".

## Watch — calibrated regime is LIVE (do NOT touch weights yet)
- [x] Calibrator confirmed loaded — restart 2026-06-08 ~10:52 AM ET, banner "Calibration: on … learned from 1,663 trades" (n matches saved params). Code committed.
- [ ] First CLEAN calibrated day = 2026-06-09 (today 06-08 is mixed: ~141 of 148 trades were pre-calibration before 10:52). Don't score today as a calibrated day.
- [ ] Over ~5 clean calibrated days track realized $ at the lower volume: `python scripts/analyze_edge.py` (Q0 dollar P&L). Volume should drop to a fraction of prior.
- [ ] Decision: does the calibrated/selective bot stay positive on fresh days? Yes → consider unfreezing weights (let the pipeline tune; re-tune min_edge down for honest probs). Bleeds → reassess.

## Go-live gate (unchanged)
- [ ] Statistically significant, day-clustered, OOS-positive $ net of real fee at calibrated volume → then start tiny with real USDC (no live DB exists yet; everything to date is paper).
