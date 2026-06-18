# LIVE_READINESS_STATUS — interim (updated 2026-06-18)

**This is NOT the verdict file.** `LIVE_READINESS_VERDICT.md` (go/no-go + a
Phase-0 FOK-only ROI number) cannot be honestly produced yet — the binding gate
is **data-blocked**, see below. This file is the reconciliation between
`live_readiness.md` (the falsification plan) and the current real state.

## Bottom line

**NO-GO right now — and not because anything is broken.** The go-live gate is
**Phase 0: a significantly-positive FOK-only per-trade EV on the post-gut,
post-fix machine**, and the clean data needed to measure it has only just started
accruing. The best estimates that *do* exist are ≤ 0 or contaminated:

- Full-history scalp-vs-hold CF: **+$563** (actual −$7567 vs hold −$8130,
  scalp-optimal 1390/2478 = 56%). **Do not inherit** — dominated by inventory
  the *deleted* entry stack sourced (pre-gut).
- Post-gut, **pre-fix** estimate (the 30-agent audit): **−$182/7d vs always-hold,
  t_day −0.68 (not significant).** This is the relevant prior, and it is the
  number the two 06-17 suppressor-bug fixes were meant to move.
- Post-**fix** clean number: **not yet measurable** (clock started today).

So the disciplined reading of the checklist's own logic ("Phase 0 fails → STOP,
everything downstream is moot") is: we are at the *start* of the Phase 0
measurement window, not past it.

## The data clock (the thing we are actually waiting on)

- Edge-relevant fixes #1 (loss-cut HOLD when holding_edge>0 + per-candle ATR
  dedup, commit `c7520006`, 11:16 ET) and #3 (winning-redeem safe-pending,
  `31aa856d`, 10:57 ET) landed 06-17 morning.
- Running paper bot (**pid 42396, started 06-17 13:19 ET**) is on that code →
  **clean post-fix edge data begins 06-17 13:19 ET.**
- Gate requires **≥10 clean ET days with a day-clustered t ≥ 2**. First full
  clean day is 06-18 → **earliest meaningful re-measurement ~06-27/28**, and
  only deploys if the edge comes back significantly positive. If it doesn't, the
  −$182 was real and go-live stays off.
- **Progress (06-18): clean day 1 of ~10.** All measurement excludes pre-fix days
  via `CLEAN_EPOCH` (06-17 13:19 ET) in `shadow_exit_model.py`. Day-1 is healthy
  operationally (clean restart on the fixed code, recorder + CF stream flowing, no
  outage); the one-day CF sliver is mildly positive but **noise** (df=0) and leans
  on favorable ITM hold resolutions, not scalp alpha. Nine clean days to the gate.

## Can the clock be compressed to read at 6/22? (investigated 06-17) — NO

Asked: squish the wait so the gate reads at 6/22 instead of ~06-27/28. Two
independent blockers, both verified (code re-read + 4-agent workflow):

1. **The fix cannot be back-applied to already-recorded data.** The CF replay
   harnesses (`sweep_exit_policy.py`, `shadow_exit_model.py`) **reuse the
   `holding_edge`/`model_prob` stamped in each record at decision time** — they
   never recompute the L1 probability (zero `compute_probability`/`evaluate_hold`
   calls in `scripts/`; `window_paths` stores only BBO/depth/coinbase/strike, no
   candle/ATR inputs to reconstruct it). So replaying the 06-11→06-17 backlog
   measures **fix #1 (branch logic, re-decided in code) on top of the *buggy*
   stamped probability** — fix #2 (ATR→prob) does *not* flow through. The
   corrected-prob policy can only be measured on **new** data, and clean post-fix
   data started 06-17 13:19 ET (~1 day exists now; ~6 by 6/22).
2. **5–6 day-clusters can't carry the gate.** The day-clustered t has df =
   n_days−1. At 5 days (df=4) the gate's own t≥2 is **p=0.058 one-sided — not even
   95% significant** (t_crit=2.13); power to detect a modest edge (d=0.6) is
   ~0.30. At 10 days (df=9) t=2 → p=0.038, power ~0.54. **More windows/day (288 vs
   35 traded) does *not* help** — it raises within-day precision, not the
   between-day df that drives the t (intra-day 5-min windows are serially
   correlated and collapsed to one number/day by design). The binding constraint
   is calendar days; nothing manufactures the missing 5.

**Earliest VALID read: ~06-27 eve / 06-28** (10 clean days 06-18→06-27). This
respects `live_readiness.md`'s "do not relax a kill bar to pass it."

**What 6/22 *can* legitimately give:** a **non-binding directional preview** —
restrict the CF pool to records ≥ 06-17 13:19 ET, report day-mean CF EV
(`actual−cf` / `scalp_was_optimal`, never a naive signed sum) with the day count
and sub-threshold t shown. It is **asymmetric**: a clearly-negative preview can
justify *abandoning early* (saving days); a positive preview can **never**
greenlight early — the binding read still needs 10 days.

## Phase reconciliation

| Phase | live_readiness gate | Status | Evidence |
|---|---|---|---|
| **0 — true live baseline** | FOK-only per-trade EV positive + day-clustered sig | **BLOCKED — clock just started** | post-fix data begins 06-17 13:19 ET; pre-fix prior −$182/7d not-sig; method (`diagnose_edge.py` scalp-vs-hold, `sweep_exit_policy.py`) in place but must strip passive/maker fills + reprice at FOK VWAP for the live number |
| **1 — execution-realism gap** | modeled slip ≥ realized; modeled fill-rate ≤ realized | **NOT STARTED** (correctly — sequenced after Phase 0; running it on pre-fix tape measures the wrong code) | tape recorder live; needs held-out window slip/fill calibration vs CLOB prints |
| **2 — adverse selection** | net exit markout vs counterparties > 0 | **NOT STARTED** | `wallet_stats` pipeline live + classifying (donors −3..−11¢, sharps +12..+37¢); exit-markout-by-class netting not yet computed |
| **3 — robustness / regime** | exit edge positive+sig in ≥2/3 vol regimes | **BLOCKED on Phase 0 data** | separate from the exit-VALUE model shadow (below) |
| **5 — live integrity** | dry-run at $1 floor, zero integrity failures | **CODE DONE, dry-run NOT RUN** | double-fill guard, Chainlink-first resolution, safe-pending redeem, live GTC passive exit all in place; the $1-floor live dry-run is the open work |
| **6 — go/no-go + ramp** | synthesize 0–5 | **PENDING** (verdict gated on Phase 0 clearing) | — |

**Adjacent experiment — exit-value model (gates nothing; being killed early):**
the freestanding two-head model competes with ExitBoundary on price-derived
features; `deployed` stays False and its shadow verdict is **not a go-live step.**
On clean post-fix data it loses every day (first 2 clean days: ExitBoundary +$49
ALL / +$340 ITM vs model +$1 / +$304 — model behind −$48 ALL, −$35 ITM).
**Operator decision: abandon it at day 3–4** rather than the full 5-day shadow —
zero cost, since ExitBoundary is already the live policy. If a model is pursued it
should be the floored ExitBoundary overlay (todo.md) — can't hurt, upside not
guaranteed — and only after the gate clears.

## Single largest unresolved risk (named, per Definition of Done)

**The exit edge may not survive on the fixed code — Phase 0 comes back ≤ 0 or not
significant.** The pre-fix post-gut prior is −$182/7d (not significant); the
+$690/14d that motivated the strategy was a pre-gut artifact of deleted-entry-stack
inventory. If the ≥10-day post-fix re-measurement doesn't return a
significantly-positive edge, there is no live strategy to deploy. This — not any
execution detail — is the binding go/no-go risk.

## Next action (does not deploy capital)

**Let the clock run.** Re-measure the post-fix Phase 0 FOK-only EV at ~06-27/28
(≥10 clean days, t_day≥2), stripping passive/maker fills and repricing at FOK
VWAP. This is the gate — and the only thing that matters next.
