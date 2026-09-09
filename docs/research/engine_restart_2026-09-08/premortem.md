# Adversarial pre-mortem -- engine restart 09-08 tests (T1-T4)

Slice `premortem`. Read-only; no computation run here (this is a design-review
of the frozen pre-registration and the Phase-1 map, per task scope). Every
item below names a concrete, executable check the T1-T4 test agents must add
on top of the frozen bars in `docs/research/engine_restart_2026-09-08.md` --
none loosens a bar, all are additive verification.

Format per item: what could go wrong -> the check that would catch it.

---

## 0. General checks -- every test (T1-T4) must run these

1. Clock discipline: all k/timestamp math must use the RECEIPT (rx) clock
   exactly as chainlink_feed.py (running_avg/projected_final_twap, lines
   ~151-250), never payload_ts or exchange ets. Check: grep the test's own
   scratch code for payload_ts/ets used in a k or decision-tick computation.
2. Dead/frozen-book cousins: book-dynamics/dead_book_windows.csv (253
   windows) was built only where a contested book coexists with |s|>=1, so
   it can miss windows dead only while |s|<1. Check: independently flag
   windows via window_paths book_age_up_s/book_age_down_s outliers or a
   micro-tape record count of 1 for the whole window, and diff vs the CSV.
3. One-bet-per-window unit: every $ total, win rate or edge must be counted
   once per window (or once per H-event), never per tick/fill. Check: any
   table with N greater than the number of distinct windows needs a stated
   reason.
4. Walk-forward is really walk-forward: assert no row from ET day d or
   later ever enters the fitted model that scores day d.
5. Day-block bootstrap integrity: B>=2000, blocks by ET DAY not window or
   tick; report the number of unique day-blocks next to every p-value.
6. ANTI + edge<0 control, always: any test claiming an edge but omitting
   either control is not compliant -- treat as "not run," not "passed."
7. No post-hoc cell selection: confirm the pre-declared cell's result was
   computed and recorded before any other grid cell, and the verdict text
   quotes it first.
8. No threshold drift: diff every numeric bar actually applied against the
   literal wording in the frozen pre-registration doc; any deviation voids
   the run.
9. Repo is read-only: confirm ws2_ladder_replay.py (or any repo file) was
   copied into scratch before editing.
10. No live wire: grep the test's command log for
    subscribe/websocket/ws://Iwss://Iplace_order/cancel_order/polybot.main/
    run_polybot -- zero hits required.
11. Book freshness beyond the dead-book list: every book_p used as a
    feature or baseline must come from a two-sided book with
    book_age<=10s, not merely "last BBO change <= t."
12. p99.5 table identity: confirm any margin table used is byte-identical
    to ws2_ladder_replay.r1_tables()['P995'] (deployed TWAP_MARGIN_P995).

---

## 1. T1 -- Zone engine (book-anchored probabilistic model)

Context: B1 tests whether M = sigma(a + b*logit(book_p) + c*r_signed) beats
the recalibrated book on log-loss at k in {58,45,30,20,10}; the D4 Phase-1
run this repeats did NOT exclude the 253 dead-book windows (an explicit
caveat in the pre-registration). B2 backtests a taker leg off M.

### False-positive risks (edge that is not there)

1. p99.5-table leakage on early test days: the deployed TWAP_MARGIN_P995
   table was fit on 08-14..27 (3,695 windows); many B1 OOS days fall inside
   that window (D4 used 14-17 of 23 days, mostly <=08-27), so r_signed is
   normalized by a constant partly fit on those days' own realized errors.
   Check: split the day-block bootstrap into "days inside 08-14..27" vs
   "days strictly after 08-27" and require p<0.05 on the post-08-27 subset
   alone; a result that only survives on overlapping days is leakage.
2. Under-fit "recalibrated book": book_recal is a single logistic on
   logit(book_p). Check: refit book_recal2 with a flexible recalibration
   (isotonic or a spline with 3+ knots) and re-compare combo against it; a
   materially smaller advantage means part of the claim is "beats an
   under-fit book," not projection information.
3. Pooled log-loss can hide inside the saturated majority: 75-99% of books
   are already confident (P>=0.9) by k=58->k=1. Check: split the log-loss
   comparison into "book confident (P outside [0.05,0.95])" vs "book
   contested" and require combo's advantage to survive in the contested
   subset alone.
4. k=58 stands in for k=60 (a 2s peek past the nominal zone edge). Check:
   state this explicitly and, if feasible, add a k=59 comparison point.
5. B2 fires FOK against a stale/undersized ask: the micro tape has no ask
   sizes. Check: join window_paths ask_sz_up/ask_sz_down (valid from
   08-22) at each fire tick, compute the implied share count for a
   $15-notional order, drop/flag fires exceeding top-of-book depth, and
   re-check bars (i)-(vi) on the depth-filtered subset.
6. B2 fires against a stale/frozen/one-sided book from a CLOB reconnect.
   Check: require the ask snapshot at t and t+L to pass the two-sided +
   book_age<=10s freshness gate; report how many fires drop and whether EW
   survives.
7. Outcome-label mismatch at exact ties: "up" must equal
   window_labels.resolved_up (tie -> Up), not a re-derived
   sign(final-strike) comparison. Check: confirm the join source.
8. Grid-peeking on e_min: confirm e_min=0.05's bars were locked before
   e_min in {0.03,0.08} or L=0.70 were computed.
9. Circular monotonicity buckets: confirm the model-edge value used to
   bucket each trade is the walk-forward OOS-fold prediction, never a
   full-sample or same-fold-refit value.

### False-negative risks (real edge missed)

10. Underpowered kill at k>=25: D4 (without dead-book exclusion) found
    p<=0.0016 at 23 days for every k. Check: report effect size next to p;
    a marginal miss (p 0.06-0.10) with matching sign/magnitude should be
    flagged "underpowered" and rerun with all days through 09-08 before
    finalizing.
11. Bucketed-M vs exact-k-M mismatch: T1's model is fit per 4 k-buckets, a
    coarser spec than D4's per-exact-k logistic. Check: also fit the
    exact-k spec and report whether the k>=25 kill decision flips between
    the two model classes.

### Interpretation risk

12. A B1/B2 pass licenses ONLY a taker-side probabilistic engine at k in
    [25,60]. Check: the T1 verdict text must not use a pass to justify
    loosening the ladder's r>=0.6 lock -- that is T2's adverse-selection
    question, decided separately.

---

## 2. T2 -- Ladder hygiene (ws2_ladder_replay.py extensions)

Context: run() today arms once per window and never re-arms
(ws2_ladder_replay.py lines ~218-350); the deployed code re-arms after a
floor cancel (maker_bid.py _retire sets active=None; main.py refuses only
when a position exists). This re-arm behavior is established ONLY by
reading the code, never from a logged re-arm event.

### False-positive risks

1. Re-arm state machine unvalidated against reality: check the paper trade
   DB / trade_history / logs for at least one real observed multi-arm
   window before trusting any H1 dollar delta; if zero exist, the T2
   verdict must say so explicitly regardless of which way H1 comes out.
2. Re-arm refusal condition drift: diff the harness's added refusal logic
   line-by-line against main.py:1048-1055 ("refuse only when a position
   exists"); unit-test one partial-fill case (must block re-arm) and one
   zero-fill floor-cancel case (must allow re-arm).
3. Blind-replay smell test on exact-0.0 deltas: count and manually inspect
   a sample of windows where re-arm ON and OFF produce an exactly-0.0
   dollar delta; confirm they genuinely never re-armed rather than
   exhibiting the banned symmetric/blind-counterfactual pattern.
4. H2's reprice classifier peeking forward: the descriptive dumps.csv
   reprice flag uses a +/-1s window (both directions); a live maintain()
   cancel decision can only see backward. Check: re-derive H2's classifier
   using ONLY trailing (rx<=t) data and report how many of the 4 known-loss
   windows' classification changes vs the descriptive +/-1s version.
5. 4-event fragility in H2's adoption clause: recompute the reprice/pull
   label for all 4 known loss windows at threshold 0.03 and 0.08 (not only
   0.05); report whether any window flips category.
6. Rung-level double counting in H3: sweeps traverse the whole ladder
   within about 1s, so one flip-fill event can hit several relaxed rungs at
   once. Check: confirm H3's "no flip-fill loss on any relaxed rung" and
   dollar/loss-count clauses are tallied per WINDOW, not per rung fill.
7. H4 leaking into T2's own verdict: confirm every H4 number is labeled
   non-adoptive/pending-09-11 and none of it is folded into T2's H1/H2/H3
   pass/fail totals.
8. Post-close reachability overstated under re-arm: the reachability
   replay omits the certain_winner close+5s fail-closed check. Check: if
   H1's re-arm scenario rests into the post-close window, apply the close+5s
   gate explicitly and report whether winner-side post-close dollars shrink.
9. AT_PRICE_QUEUE_SH=135 transferability: that constant was measured at the
   specific round-tick prices the bot already quotes. Check: if H3's need
   schedule changes the effective first-touch rung price, flag any
   extrapolation of the 135-share constant as unverified.

### False-negative risk

10. The ">=2 of 3 known loser windows" faithfulness gate could itself
    under- or over-fit the harness to those 3 windows. Check: after the
    gate passes, hold out ep 1788267900 (the 4th known bot-zone loss from
    book-dynamics, not among the 3 named in the pre-registration) as an
    independent faithfulness check and report whether it reproduces too.

---

## 3. T3 -- Wallet identity, fresh OOS

Context: classes are frozen from data through 09-01; the fresh pull covers
09-02..09-07 with full paging (fixing the 3,500-row/offset<=3000 cap
Phase-1 found). h1 (68 of 362 Phase-1 windows) was selected for
reversal-heavy wall capture and INVERTS the unconditional displacement edge
inside the sample.

### False-positive risks

1. Class-list contamination: regenerate top_directional / bottom_directional
   membership from data strictly through 09-01 and diff byte-for-byte
   against the frozen class-list file used for the fresh pull; any wallet
   whose class differs proves later data leaked into classification.
2. Cap defect recurrence: log the max API offset reached per pulled window
   and confirm every window's paging stopped only on an empty page, never a
   fixed ceiling.
3. Forward-peeking book proxy for k>90: the pre-registered proxy is a
   contemporaneous VWAP within +/-3s (fallback +/-10s) of other
   participants' prints -- a symmetric window that can use trades after the
   wallet's own trade. Check: recompute the excess using an asymmetric
   backward-only VWAP (preceding 3s/10s only) and compare; a materially
   larger excess under the +/- version means momentum leakage, not skill --
   report both and flag the +/- number as an upper bound.
4. Block-time estimation error inside k<=25: for rows without an exact tape
   match, match time is estimated as timestamp minus 2.3s (p10/p90
   1.51/3.08s); a 0.8-1.5s error in a fast final-25s window can flip which
   side of a dump the book-at-match reads as. Check: recompute B5b for
   k<=25 using only exact-tape-matched rows; if the sample is too small,
   report that cell UNDECIDABLE rather than a clean zero.
5. Selection bias reintroduced via the stride rule: confirm the stride-3
   fresh-pull window selection is a fixed epoch-modulo rule written down
   before any outcome was observed, with no filtering by volatility, wall
   capture, or reversal character.
6. Non-B5a cells presented as if pre-registered: confirm two_sided_mm /
   wall_camper / six_cluster results on fresh data, if computed, are
   labeled exploratory context, not folded into the B5a-only kill/pass
   gate.
7. Desk-check scope creep: grep the session command log to confirm the CTF
   Exchange OrderFilled / transactionHash check stayed docs-only -- no live
   WS listener opened, per both the pre-registration and the shared hard
   rule against any websocket subscription.

### False-negative risks

8. Survivorship in the control class: report the fraction of the
   09-01-frozen bottom_directional class with zero trades in 09-02..09-07;
   heavy attrition means B5d's "control <= 0" is tested on a small,
   survived subpopulation, not the full bottom-decile population.
9. B5c positive result without the indexing-lag caveat: Phase-1's
   indexing-lag probe was N=4 and unverified (one read showed minutes of
   lag). Check: any positive B5c number must be reported together with
   this caveat so it is not misread as an operationally buildable edge.
10. Small-N persistence claim: report N explicitly for any fresh-day-only
    persistence Spearman; flag it as far lower power than Phase-1's
    3,056-wallet figure -- do not let a null on 5-6 fresh days read as
    contradicting the original persistence finding.

---

## 4. T4 -- Whole-window controls

### False-positive / definitional risks

1. rv60 clock mismatch: P1 draws book from window_paths (1Hz) but Phase-1's
   z was defined on raw-stream RECEIPT-CLOCK increments (information-
   structure D2, explicitly flagged as clock-sensitive). Check: recompute z
   both ways (raw-tick rv60 vs a window_paths-derived 1Hz proxy) and report
   whether P1's pass/fail flips.
2. Same under-fit-recalibration risk as T1: apply the flexible book-
   recalibration sensitivity check (isotonic/spline vs 1-parameter logistic)
   to P1/P2's baseline before crediting any beat-the-book result.
3. Bonferroni denominator drift: P2's stated correction is 0.05/8 (2 depth
   thresholds x 4 horizons). Check: confirm the actually-scored comparison
   set is exactly 8; flag as invalid any extra "for context" combination
   folded into the same corrected test without inflating the denominator.
4. External-data vintage mismatch: local Binance UM bookDepth stops at
   2026-08-31; window_paths book data may run later. Check: restrict P2's
   evaluation to the overlap window and confirm enough walk-forward OOS
   days remain; otherwise report P2 UNDECIDABLE-by-data-coverage, not a
   clean kill.
5. P3's unverified 15m ground truth: the 15m family has no window_labels
   row; strike/final come only from Gamma .meta.json outcomePrices. Check:
   cross-check a sample against an independently computed 60s-TWAP
   reconstruction from the raw Chainlink stream before trusting any
   dominance-violation count built solely from the meta files.

### False-negative / over-claiming risk

6. A P1/P2 null is a narrower closure than it sounds: P1/P2 use a 2-3
   feature logistic; the 09-01 program's original closure used a
   135-feature/GBT-class test. Check: state explicitly, if P1/P2 kill, that
   the closure applies only to the specific z / Binance-depth-imbalance
   features tested here, not the wider 09-01 closure.

---

## 5. Premise risks -- the Phase-1 map itself, and what artifact to re-check

1. D4's log-loss (information-structure/d4_logloss.csv) was computed
   WITHOUT excluding the 253 dead-book windows -- the pre-registration's own
   stated caveat. Re-check by diffing T1's re-verified numbers against this
   file to quantify how much of the reported gain the dead-book windows
   contributed.
2. The dead-book detector itself may under-count: book-dynamics/
   dead_book_windows.csv was built only where a contested book coexists
   with |s|>=1 (book-dynamics' own stated gap). Re-check by rebuilding an
   independent detector from window_paths book_age_*_s outliers and diffing
   the two exclusion sets.
3. The re-arm claim rests entirely on code reading (maker_bid.py:298-331,
   main.py:1040-1075), never on an observed live/paper re-arm
   (information-structure 3e and engine-map both say this explicitly).
   Re-check the paper trade DB/logs for any actual multi-arm window before
   trusting any re-arm dollar figure in T2's verdict.
4. Contested-edge cells are measured at MID (book-dynamics/
   contested_edge_clusterboot.csv), not the executable ask, and not net of
   about 0.4s taker RTT or the 0.07*p*(1-p) fee. Re-derive the same cells
   net of spread-crossing, RTT and fee before treating B1's pass as
   informative about B2's chances -- this is the single most likely place a
   "T1 licenses a taker" story evaporates on contact with B2.
5. The maker ladder's fill-conditional win rates rest on a "trough < rung"
   proxy (book-dynamics/dumps_fill_conditional.csv, information-structure/
   d3_blind_winrate.csv) indistinguishable in BBO data from an MM
   cancellation, with ZERO live ladder fills to calibrate it. Re-check
   against the handful of real recorded paper fills in
   polybot_paper_audit.db (CLAUDE.md cites 1 fill since 08-27, 36 and 16
   fills in earlier epochs) as a sanity cross-check, small as that N is.
6. The h1 wallet pull (68 of 362 windows) inverts the unconditional
   displacement edge inside the sample (loses 4-5pp vs the population's
   +1-2pp; wallets scout "h1 selection bias"). Re-check that no downstream
   T3 analysis silently pools h1 data into the "fresh" 09-02..09-07
   comparison.
7. The 3,500-row pull cap censored exactly the long-horizon buckets
   (120-240s, >240s) where Phase-1 found its largest, most Bonferroni-
   significant wallet effects (wallets/pull_inventory.json capped-window
   lists). Re-check by comparing the fresh (uncapped) pull's effect sizes
   in those same buckets against the old capped-corpus numbers to see
   whether the effect shrinks once the censoring artifact is removed.
8. Wallet class thresholds (top/bottom decile, >=70% one-directional,
   >=5 prior windows) were fixed a priori in-session but not pre-registered
   outside this session -- the wallets scout's own admission. Re-check
   whether any earlier version of s4_measure.py used different thresholds
   before settling on today's, which would indicate implicit tuning against
   visible partial results.
9. Book calibration slope >1 after 240s is attributed to the 0.99 tick
   cap, not information (book-dynamics section 2) -- but the near-saturated
   P(win) surface (information-structure section 1) could interact with the
   tie -> Up resolution convention at the low-r tail. Re-check that T1's
   outcome label exactly reproduces window_labels.resolved_up's tie
   convention, not a re-derived sign comparison.
10. Engine-map's exact reproduction of RESEARCH.md's 09-08 r27 number
    (17 fills / +$1,882.72) proves internal consistency between two
    invocations of the SAME fill rule, not that the fill rule is correct.
    Do not let T2 cite this reproduction as external validation of the
    fill model in any verdict.

---

## 6. Priority ranking (most likely to flip a verdict)

1. p99.5-table leakage on pre-08-27 test days (T1 #1) -- could single-
   handedly explain B1's whole k>=25 story either way.
2. Contested-edge-at-mid vs net-of-costs (premise #4 / T1 B2 checks #5-6) --
   most likely single reason B2 fails even if B1 passes.
3. Re-arm state machine unvalidated against any real occurrence (T2 #1,
   premise #3) -- every H1/H2 dollar figure inherits this.
4. Forward-peeking wallet book proxy (T3 #3) and the 3,500-row-cap
   censoring of exactly the significant buckets (premise #7) -- together
   could halve or erase the wallet story's headline numbers.
5. Dead-book detector under-coverage (general #2, premise #2) -- a cheap
   check that could silently move every book-dynamics/information-structure
   number cited by T1/T2.
