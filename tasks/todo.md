# PolyBot — current work

Canonical state: memory `edge-thesis-corrected-baseline.md` (single source of truth).

## Done (2026-06-08)
- [x] 12-day frozen baseline analyzed; microstructure A/B/C = no taker edge (chapter closed)
- [x] Calibrator train/serve domain bug fixed (fit on P(up)+up-outcome, all 3 fit sites) + adopted — validated OOS (+0.053 nats); live at next restart, weight-freeze still ON
- [x] Analyzer fee-base bugs fixed; dollar P&L is the headline metric (`analyze_edge.py` Q0)
- [x] Staying TAKER (edge is taker-native; maker worse post-speed-bump-removal)
- [x] Removed dead code: microstructure recorder + wiring + scripts + local data, and dormant maker infrastructure (taker-only FOK now). 534 tests pass.

## Watch after the restart (do NOT touch weights yet)
- [ ] Confirm the calibrator loaded at the 12:01 AM restart — trade volume should drop to a fraction of prior (selective)
- [ ] Over ~5–7 days track realized $ at the lower volume: `python scripts/analyze_edge.py` (Q0 dollar P&L)
- [ ] Decision: does the calibrated/selective bot stay positive on fresh days? Yes → consider unfreezing weights (let the pipeline tune). Bleeds → reassess.

## Open
- [x] Code changes (calibrator domain fix + dead-code removal) are uncommitted — commit when ready (nightly auto-commit only sweeps settings/memory/db).
