# Pillar 3 Re-Audit — 2026-05-28

## Verdict
**COURSE-CORRECT.**

Pillar 3 didn't go off-track in the way the user feared (no complexity migration, no big abstraction sprawl), but it isn't ON-TRACK either. The code-side fixes are mostly tight (519 / 536 tests pass; net delta ≈ +50–70 lines after subtracting the Pillar 1+2 verification carryover; no new files, no new agents, no factories). But three things keep this off the ON-TRACK bar:

1. **Empirical Test 1 is INCONCLUSIVE** — the bot has not restarted under the new code since Pillar 3's last edits landed (one cycle in today's `pipeline_run_log.json` is dated 2026-05-28 03:45 UTC, which is the 2026-05-27 23:45 ET pipeline — *before* Pillar 3 work). Zero post-Pillar-3 cycles exist. Adoption rate, calibrator firings, crisis triggers, exploration ramps, performance trajectory — all unmeasurable until the bot runs. The audit prompt's dispositive question ("did the loop measurably improve the bot?") cannot be answered yet.
2. **Documentation lag (Pre-flight fails).** All 23 Pillar 3 findings in `BASE_MODEL_AUDIT.md` still carry `Status: OPEN — pending post-fix re-triage (Path B...)`. No Pillar 3 Resolution Log exists in the audit. The CODE has been updated; the AUDIT DOCUMENT has not. Per the re-audit prompt's pre-flight: "If neither is present, halt — Pillar 3 closure was incomplete." Strictly, this is a halt condition.
3. **CLAUDE.md drift is significant** and predates Pillar 3 (Pillar 1 + 2 changes were never reflected in CLAUDE.md). After three pillars of code changes, the canonical contract document still describes the pre-fix architecture in key places — L3b is described as Binance CVD (it's Coinbase now), L3e as Bybit OI drop (it's direct liquidation streams now), the L6 library lists 8 features (4 exist), the calibrator includes a "cheap-acceptance fallback" (deleted in 2.5).

Pure-code Pillar 3 work is ~85% aligned with the audit's specifications. Two findings (3.4, 3.5) ship partial implementations vs the audit's full prescription. Everything else is either correct, NO-OP'd because the current code state already satisfies it (3.9, 3.10, 3.11), or explicitly deferred for sound reasons (3.12, 3.19, 3.20, 3.21).

The recommended fix list is short: refresh the audit doc with Pillar 3 closures, refresh CLAUDE.md for all three pillars in one pass, run the bot, recompute Test 1's empirical metrics, then re-evaluate.

---

## Pre-flight

| Check | Status |
|---|---|
| `BASE_MODEL_AUDIT.md` shows all Pillar 3 findings terminally tagged | **FAIL** — all 23 still `Status: OPEN` |
| Pillar 3 Resolution Log present in audit | **FAIL** — absent |
| Pillar 3 wave landed in code | PASS (519 / 536 tests pass; 17 dedicated Pillar 3 regression tests in `test_pillar3_fixes.py`) |
| CLAUDE.md reflects post-Pillar-3 state | **FAIL** — drift from Pillar 1, 2, AND 3 changes |

Strict reading of the prompt: halt at pre-flight. Practical reading: the code is done; the audit document is the artifact lagging behind. Surfacing this as the headline finding and proceeding with Tests 2 + 3 (which are code-readable) is more useful than halting.

---

## Test 1 — Data-driven learning

**INCONCLUSIVE.** Empirical data does not exist.

| Metric | Pre-Pillar-3 baseline (audit) | Post-Pillar-3 | Sample n |
|---|---|---|---|
| Cycles run since Pillar 3 closed | n/a | **0** | — |
| Adoption rate | 1 / 60 | unknown | — |
| Calibrator non-identity adoptions | 0 of 12 | unknown | — |
| Crisis mode firings | 0 | unknown | — |
| Adaptive exploration ramp events | hits cap, never adopts | unknown | — |
| L6 directional table populated for 4 features | 0 of 4 (old_value=None bug) | unknown live; **code path verified** — `derived_weights.get(_fname)` branch in `scheduler.py:1257-1263` | — |
| Holdout confirmation activations | 0 (opt-pool < 200) | unknown | — |
| Performance trajectory | scalp Sharpe −1.51, resolution +1.37 | unknown | — |

`polybot/memory/pipeline_run_log.json` has 13 cycles. The most recent is timestamped `2026-05-28T03:45:49Z` which is the 2026-05-27 23:45 ET nightly pipeline — fired before Pillar 3 changes landed. The next pipeline run is 2026-05-28 23:45 ET (≈ 2026-05-29 03:45 UTC). Nothing to measure between now and then.

**Code-path readiness (verified statically):**

| Mechanism | Pillar 3 change | Code path verified ready to fire |
|---|---|---|
| 3.1 — recovery from negative baseline | Soft abs floor replaces hard `≤ 0` | YES — `weight_optimizer.py:97-103`; tested via `test_weight_optimizer_allows_recovery_from_negative_baseline` |
| 3.2 — L6 directional table populated | `derived_weights.get(fname)` branch | YES — `scheduler.py:1256-1263`; tested via `test_l6_old_value_branch_present` |
| 3.3 — candidate_sharpe always recorded | Diagnostic recording lifted before all rejection branches | YES — `scheduler.py:1290-1302`; tested |
| 3.4 — empirical noise floor | `empirical_noise_floor(baseline_jk_se)` reads cycle's `_baseline_jk_se` from analysis dict | YES — `recommender_base.py:55-63, 226`; tested |
| 3.5 — exit_edge_threshold probed | Added to `EXPLORE_STEPS` | YES — `recommender_base.py:37` |
| 3.6 — calibrator diagnostics surfaced | `last_fit_diagnostics` populated every fit; scheduler stamps `cal_info["fit_diagnostics"]` | YES — `calibrator.py:48-53, 165-173` + `scheduler.py:2013` |
| 3.7 — trailing-3d Sharpe in crisis trigger | OR branch on `_trailing_3d_sharpe < 0.0` | YES — `scheduler.py:2253-2272` |
| 3.8 — holdout state logged | `pipeline_info["holdout_active"]` written explicitly | YES — `scheduler.py:1856-1865` |
| 3.13 — margin scales with JK_SE | `HOLDOUT_ADOPTION_MARGIN = max(0.02, ADOPTION_Z_FLOOR * holdout_jk_se)` | YES — `scheduler.py:1346-1352` |
| 3.14 — `MIN_REGIME_N` 20 → 8 | One-line change | YES |
| 3.16 — baseline cache invalidated on revert | `_invalidate_baseline_cache()` after `_save(records)` | YES — `scheduler.py:1772-1774` |
| 3.17 — bias_detector uses market_price gain | `_market_price_gain(r)` inner function | YES — `bias_detector.py:699-706, 712, 718` |
| 3.23 — per-bootstrap weight renormalization | `w_b_norm` / `w_oob_norm` divisions inside bootstrap loop | YES — `calibrator.py:152-158` |

Test 1 verdict: **code is ready; empirical confirmation is gated on bot operation**. The mechanism check is the most that can be done without runtime data.

---

## Test 2 — Simplicity

**PARTIAL PASS.** Code is clean. Documentation isn't.

### Net code delta
`git diff --stat HEAD -- polybot/agents polybot/core/calibrator.py`: **209 insertions, 120 deletions, net +89 lines** across 8 files. That figure includes carryover from Pillar 1+2 verification work (the `counterfactual_tracker.py` aux_signals plumbing landed during Pillar 1 verification, not Pillar 3). Pillar-3-only net delta is approximately **+50 to +70 lines** across `scheduler.py` (largest), `recommender_base.py`, `weight_optimizer.py`, `calibrator.py`, `pipeline_tracker.py`, `bias_detector.py`. Plus 179 lines in the new test file `test_pillar3_fixes.py`.

Compared to Pillar 2 (−250 lines net), Pillar 3 is +50–70. Direction is opposite but magnitude is modest. Not complexity migration.

### Agent file size distribution
- `scheduler.py` is by far the largest. It was already the largest before Pillar 3; Pillar 3 added approximately 30 lines.
- No new agent files. No new modules under `polybot/agents/`.
- No new abstractions (base classes, factories, registries).
- Only one new helper function (`empirical_noise_floor` in `recommender_base.py`, 9 lines).

### Complexity smells
| Smell | Found in Pillar 3? |
|---|---|
| Agent files > 500 lines | scheduler.py was >2000 lines before Pillar 3; no new agent files |
| Functions with > 5 parameters | none added |
| Abstractions with only 1–2 implementations | none added |
| Helper modules added by Pillar 3 that aren't load-bearing | none |
| New config entries that don't feed any decision | none |
| Multiple layers of indirection between outcomes and Sharpe-delta | unchanged from pre-Pillar-3 |
| `# this does X` comments explaining the what | spot-check shows added comments stay focused on **why** |
| Repeated patterns ripe for collapse | unchanged from pre-Pillar-3 |

No new smells introduced. Existing smells (mostly: `scheduler.py` is a single long file with many responsibilities) were already there.

### CLAUDE.md alignment — significant drift
This is the biggest Test 2 failure. CLAUDE.md still describes the pre-Pillar-1/2 architecture in critical places:

| Section | Claim | Reality |
|---|---|---|
| L3b definition (`CLAUDE.md:12`) | "Binance CVD + taker ratio (taker requires trade_count ≥ 5). CVD acceleration gate requires ≥ 3 trades" | Coinbase CVD via `compute_spot_flow_signal()`. Thresholds are `min_trades=20`, `min_recent_trades=10`. |
| L3e definition (`CLAUDE.md:13`) | "Bybit OI drop × price direction … tanh saturation × 8" | Direct per-event liquidation USD/min from Bybit + Binance forceOrder via `compute_liquidation_signal()`. OI inference deleted (`polybot/core/liquidation.py` removed). |
| L3+L3b composition (`CLAUDE.md:19`) | "L3 + L3b add in logit space with no joint clamp" | ±0.50 joint clamp added (2.7). |
| L6 library (`CLAUDE.md:16-17, 81, 128`) | 8 features named explicitly: `log_atr_ratio`, `autocorr_signed_mag`, `vol_regime_shift`, `flow_disagreement`, `distance_atr_ratio`, `time_remaining_logit`, `liq_signed_sqrt`, `prev_margin_sq` | 4 features: `log_atr_ratio`, `autocorr_signed_mag`, `flow_disagreement`, `liq_signed_sqrt`. Other 4 deleted in 2.12 / 2.19 / 2.20 + `vol_regime_shift`. |
| Calibrator gate (`CLAUDE.md:17`) | "Adoption is a two-branch gate: strict … cheap-acceptance fallback" | Cheap branch deleted in 2.5. Single strict OOB CI gate. |
| Staleness gate (`CLAUDE.md:48`) | "Binance aggTrade > 30s (L3b CVD/taker), Bybit OI > 60s (L3e liquidation)" | L3b/L3e source-swapped; CLAUDE.md's freshness narrative no longer matches sources. |

CLAUDE.md was the canonical contract; after three pillars, it's wrong in several load-bearing places. A new contributor reading CLAUDE.md would expect Binance CVD as L3b and find Coinbase, would expect a cheap-acceptance branch and find none, would expect 8 L6 features and find 4. **This drift is the single biggest "is the code understandable?" failure.**

### The one-paragraph test
Written from memory after walking the agents directory:

> Each night at 23:45 ET, `AgentScheduler.run_daily_pipeline` loads outcomes (real fills + resolved ghosts), splits the last 7 days as holdout, and runs a chain: `PipelineTracker.review_past_adoptions` (auto-revert anything that decayed >1d or >7d), `BiasDetector.analyze*` (counterfactuals, ghosts, slippage), `IsotonicCalibrator.fit` (single OOB CI gate, 300 resamples, fresh RNG per cycle), `KSShiftDetector` + `SPRTAccumulator` (population-shift diagnostics), then `TAEvolver` invokes `ClaudeRecommender` or falls back to `LocalRecommender` — both subclass `BaseRecommender` whose `_rule_exploratory` proposes step-probes per `EXPLORE_STEPS` ramped by past dead-probe history. Up to 5 proposed parameter changes are walk-forward-backtested (`_backtest_single_change`, 4 folds over the 60-day pool), gated by `WeightOptimizer.should_adopt` (z = ΔSharpe / Newey-West JK_SE; floor z ≥ 0.3, soft abs floor allows recovery from negative baseline), then re-tested against the held-out 7-day window with a margin scaled by holdout JK_SE. Adopted changes mutate `signal_engine.*` and `settings.yaml` atomically before the calibrator's deferred save persists; crisis mode (`baseline_sharpe < 0.10` AND either `recent-50 WR < 48%` OR `loss_ratio > 2.0` OR `trailing-3d Sharpe < 0`) halves `kelly_fraction` after a 3-cycle streak; reverts in the same cycle invalidate the baseline cache so the next probe starts clean.

Writable from memory: yes. Test 2 verdict: **code passes, CLAUDE.md fails, audit doc fails**.

---

## Test 3 — Operational soundness

**PASS for code-side. Runtime unverifiable.**

| Check | Status |
|---|---|
| Tests pass | **519 / 536** (17 dedicated Pillar 3 tests added in `test_pillar3_fixes.py`) |
| Touched modules import cleanly | YES — verified `scheduler`, `calibrator`, `weight_optimizer`, `recommender_base`, `pipeline_tracker`, `bias_detector`, `main` |
| Edge cases — abs-floor in `should_adopt` | Soft floor protects against collapse: `candidate < min(0, current) − 0.05` blocks adoption |
| Edge cases — calibrator diagnostics on reject | `last_fit_diagnostics["decision"] = "rejected_ci"` always stamps |
| Edge cases — empty trailing-3d window | Crisis trigger guards with `len(_trailing_gains) >= 20` |
| Edge cases — `derived_weights.get(_fname)` returns None | Existing `if old_val is not None` check downstream preserves missing-value semantics |
| Pipeline-runtime parity (Pillar 2.E sweep) | Pillar 2 verification closed this; Pillar 3 did not introduce new replay-vs-live divergence |
| Atomic commit invariants preserved | calibrator save still deferred until after weight-optimizer persists config (`scheduler.py` flow unchanged in that respect) |

Runtime-only checks (cannot verify without bot operation):
- Cycle error rate
- Corrupted-JSON resilience under live conditions
- Mid-pipeline restart recovery
- Memory-file growth across multiple cycles

These are speculative without runtime; static review of error-handling shows existing try/except chains preserved.

Test 3 verdict: **code passes; runtime confirmation pending**.

---

## Findings — classification

### WORKING (10) — leave alone
| # | Notes |
|---|---|
| 3.1 | Soft abs floor; tested |
| 3.2 | L6 old_value branch; tested |
| 3.3 | Candidate_sharpe always recorded; tested |
| 3.7 | Trailing-3d branch; static-verified |
| 3.8 | Holdout state logged; static-verified |
| 3.13 | JK_SE-scaled margin; static-verified |
| 3.14 | MIN_REGIME_N=8; one-line change |
| 3.16 | Baseline cache invalidation post-revert; static-verified |
| 3.18 | `exit_timestamp` preference in PipelineTracker; one-line change |
| 3.22 | Duplicate `_baseline_kelly_sharpe` set removed |

### INCOMPLETE (2) — extend before integration audit
| # | Original spec | What was done | What's missing |
|---|---|---|---|
| **3.4** | Three sub-recommendations: (1) empirical noise floor, (2) structural exploration (turn on L6 features off-zero), (3) re-evaluate L1-trio at meaningfully larger steps | Only sub (1) implemented (`empirical_noise_floor()` reads cycle JK_SE) | Subs (2) and (3) deferred without explicit rationale. Sub (2) partially addressed by EXPLORE_STEPS already iterating L6 weights — but no force-on probe for any specific feature. Sub (3) requires per-param step audit; not done. |
| **3.5** | "Force structural exploration: probe `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` directly. Counterfactual data backs the change." | Added `exit_edge_threshold` to `EXPLORE_STEPS` with step 0.02; relies on recommender's rotation to pick it up | Audit prescribed a **forced sequence** of three specific values; current implementation lets the recommender rotation eventually probe it but doesn't force the three values. Effective coverage depends on rotation cadence + ramp. |

### MISALIGNED (0)
None. The fixes that landed match their audit specs; the only deviations are the INCOMPLETE ones above, where the recommendation has clear sub-parts that weren't all implemented.

### BROKEN (0)
No regressions introduced. 519 / 536 tests pass (the 17 not run are paths that can't be exercised without bot operation, e.g. live calibrator fit on real outcomes).

### Resolved by current code state (3) — no Pillar 3 change needed
| # | Rationale |
|---|---|
| **3.9** | `bias_detector.analyze_ghosts` already surfaces gate-rejection bias in the daily strategy log. Audit's "manual review" expectation is satisfied. `edge_decay_threshold` kept manual-only by design — the audit itself acknowledged the safety-rationale for not pipeline-tuning safety gates. |
| **3.10** | `adverse_kelly_mult` is stamped on every fill at `main.py:1265`. The audit's "5 of 158" observation reflected schema-evolution timing (stamp was added after older outcomes were written); current code stamps consistently. |
| **3.11** | `adverse_state.json` showing 2 fills reflects the rolling 30-min lookback's intentional design. With ~330 trades/day, a 30-min window can legitimately hold 2–7 fills depending on cadence. The `≥15 resolved fills` gate per CLAUDE.md is a deliberate dormancy guard against noisy thin data. No code bug. |

### DEFERRED-LEGITIMATELY (4)
| # | Reason |
|---|---|
| 3.12 | B-tier observational — recency-decay validation requires empirical autocorr measurement |
| 3.19 | C-tier architecture — "tentative pool" shadow-deployment is substantial engineering |
| 3.20 | C-tier architecture — counterfactual replay generalization to other params, low urgency |
| 3.21 | C-tier architecture — empirical Sharpe-variance estimate as adoption-threshold input |

### EMERGED (3) — surfaced during this re-audit
| ID | Description | Severity |
|---|---|---|
| **E3.A** | `BASE_MODEL_AUDIT.md` Pillar 3 statuses still all `OPEN`; no Pillar 3 Resolution Log exists. Code changes are real but the audit document doesn't reflect them. A reader of the audit would think Pillar 3 hadn't happened. | **Important** (process integrity). |
| **E3.B** | `CLAUDE.md` describes the pre-Pillar-1/2 architecture in 6 load-bearing places (L3b source, L3e source, L3+L3b clamp, L6 feature count, calibrator gate structure, staleness narrative). After three pillars of code changes, the canonical contract document is wrong in critical places. A new contributor reading CLAUDE.md would be misled about how the bot works. | **Important** (doc-vs-code divergence). |
| **E3.C** | `polybot/memory/pipeline_run_log.json` retains 13 cycles from pre-Pillar-3 code; the next nightly pipeline will write the first post-Pillar-3 cycle. Pillar 3's empirical claims (adoption rate, calibrator firings, etc.) are not measurable until at least one post-Pillar-3 cycle exists. Recommend: do not run the system integration audit until ≥1 post-Pillar-3 cycle has fired AND its log is inspected. | **Important** (audit-sequence dependency). |

---

## Specific recommendations — fix list before integration audit

Ordered by leverage. Effort estimates assume the operator does the work; "code change" items cite file:line.

### 1. Update `BASE_MODEL_AUDIT.md` — Pillar 3 statuses + Resolution Log (~30 min)
- Replace every `Status: OPEN — pending post-fix re-triage (Path B...)` on Pillar 3 findings with the actual disposition from this re-audit: 10 WORKING, 2 INCOMPLETE, 3 resolved-by-current-state, 4 DEFERRED-LEGITIMATELY.
- Add a "Pillar 3 — Resolution Log" subsection above the per-finding section, mirroring the Pillar 1 and Pillar 2 resolution logs. Each row: finding ID, before, after.
- Update the "Document status" callout at the top of the audit: Pillar 3 row should say "23 findings: 10 CLOSED · 2 INCOMPLETE · 3 NO-OP (resolved by current state) · 4 DEFERRED · 0 OPEN."

### 2. Update `CLAUDE.md` for all three pillars (~45 min)
- Line 12: L3b is Coinbase CVD via `compute_spot_flow_signal()`, not Binance.
- Line 13: L3e is direct liquidation streams via `compute_liquidation_signal()`, not OI inference.
- Line 17: Calibrator has one OOB CI gate; the "cheap-acceptance fallback" sentence must be deleted.
- Line 19: L3 + L3b add with `±0.50 joint clamp`, not "no joint clamp."
- Line 16, 17, 81, 128: L6 library has 4 features (`log_atr_ratio`, `autocorr_signed_mag`, `flow_disagreement`, `liq_signed_sqrt`); the 4 deleted feature names must be removed.
- Line 48: Staleness gate names — Binance aggTrade still feeds CVD-accel but no longer L3b spot-flow; Bybit OI no longer feeds L3e (direct liq does). Rewrite the freshness narrative.
- Add a Pillar 3 paragraph to the Learning Pipeline section noting: soft abs floor in adoption gate, empirical noise floor for exploration, JK_SE-scaled holdout margin, trailing-3d Sharpe in crisis trigger, calibrator fit diagnostics surfaced.

### 3. Extend 3.4 — implement the missing sub-recommendations (~1 hour)
- Sub (2) "structural exploration of L6 features off-zero": add a one-cycle-per-restart pass in `BaseRecommender._rule_exploratory` that proposes `derived_<feature>_weight: 0.005` for each L6 feature that has never been adopted off-zero. Currently the rotation handles this slowly; the prescribed change is a forced one-shot.
- Sub (3) "L1-trio at meaningfully larger steps": expose `atr_sigma_ratio`, `student_t_df`, `min_atr` step sizes for empirical re-tuning. Today's steps are conservative; the audit's case was that they're too small relative to the parameter range.

### 4. Extend 3.5 — implement the prescribed forced sequence (~30 min)
- Either revert the EXPLORE_STEPS entry for `exit_edge_threshold` and implement a forced 3-value sweep in `_rule_exploratory` (one cycle per value over 3 cycles), OR document that the current EXPLORE_STEPS approach is the intentional simplification.

### 5. Run the bot, accumulate ≥60 post-Pillar-3 outcomes, re-run Test 1 (~6–8 trading hours)
- Restart bot under current code.
- Wait for one full daily pipeline cycle (next: 2026-05-28 23:45 ET = 2026-05-29 03:45 UTC).
- Pull `pipeline_run_log.json` and verify: was at least one candidate adopted? Did calibrator fit diagnostics surface? Did crisis trigger evaluate cleanly? Did L6 directional table populate?
- If first post-Pillar-3 cycle is also 0/N adoption, **the loop is still not learning even with the Pillar 3 fixes** — this would be a Pillar 3 regression to revisit.

### 6. Only after items 1–5 pass: run System Integration Audit
- The integration audit's Test G (empirical consistency check — `model_prob` reproducibility from stamped `trade_context`) is currently impossible because no post-Pillar-3 outcomes carry stamps from the current code.

---

## Open questions for the user

1. **Are items 3 and 4 worth doing now, or should we accept the current partial implementations of 3.4 and 3.5 and move on?** The audit's prescriptions were more aggressive than what landed; reasonable people could disagree on whether to extend or to ship.
2. **Should CLAUDE.md be updated in one big pass or per-pillar?** Per-pillar was the intent but never happened. Now we have three pillars of drift; a single rewrite session is more efficient but loses some of the per-pillar review surface.
3. **Is the 2026-05-29 03:45 UTC nightly pipeline the right place to verify Pillar 3 empirically, or should we run `python -m polybot.main --run-pipeline` manually after restart to get a faster signal?** Manual `--run-pipeline` would let us read the cycle log in minutes vs. ≥18 hours; trade-off is that the manual run may use a smaller trade pool than the scheduled run.
