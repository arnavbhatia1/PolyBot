# Completeness critic - engine restart 09-08, T1-T4 vs pre-registration

Slice `critic`. Read-only review of the four test reports, the adversarial pre-mortem,
and `docs/research/engine_restart_2026-09-08.md`. One spot check re-run below
(section 3, item C2/C3). No new data pulled.

## 1. Was every bar evaluated as written? Any loosened, substituted, or PASSed without required N/controls?

**No bar was loosened, substituted, or granted PASS without its required N or controls.**
Checked every bar in T1 (B1 x5 k, B2 i-vi), T2 (H1/H2/H3/H4), T3 (B5a/b/c/d, kill/pass),
T4 (P1 x4 k incl. M'', P2 x8 Bonferroni cells, P3) against the literal wording in the
pre-registration. Every PASS carries its N and day count at or above the stated floor;
every FAIL is FAIL on the literal cell, not a softened neighboring cell. Three
interpretation ambiguities in the frozen text itself were resolved by the tests in the
stricter (more kill-prone) direction, which is defensible but changes the verdict if
read the other way - flagged, not treated as violations:

- T1 section 2.9 / 2.5 - "Kill: B1 fails at k >= 25" is ambiguous between "fails at every
  k>=25" and "fails at any k>=25." Under the pre-registered *bucket* model, only k=58
  fails (k=45, k=30 PASS cleanly); under D4's *exact-k* model class, k=58 *also passes*
  (p=0.023, -0.6% relative) - all three k>=25 cells pass. T1 applies "fails at any," using
  the bucket model, to trigger kill. Under the exact-k class the k>=25 kill would not
  trigger at all. Does not change T1's final disposition only because B2 independently
  fails bars (ii) and (v) (the pre-registration's kill is an OR).
  [T1-zone-engine/REPORT.md sections 2.5, 2.9]
- T3 section 4, section 3 item 11 - the pre-registered book proxy for B5a has no explicit
  staleness bound; T3's PRIMARY reading (30 s stale, one-sided/complement allowed) gives
  the KILL (exc=-0.72pp, t=-1.279). The general pre-mortem's own freshness standard
  (two-sided, age<=10s - the one T1/T4 use) gives the opposite sign (+1.31pp, t=1.745)
  and lands in the pre-registration's own undeclared gap between the kill bar (t<1.0)
  and pass bar (t>=2.0) - i.e. under the stricter, more-standard proxy this cell would
  trigger neither kill nor pass. T3 discloses this but keeps the pre-declared (looser)
  proxy as primary; the KILL is not proxy-invariant.
  [T3-wallets-oos/REPORT.md section 4 "B5a sensitivity re-runs"]
- T2 section 5.2 / 8 - "no flip-fill loss on any relaxed rung" (H3) is ambiguous
  per-window vs per-rung-fill; T2 counts per window (the stricter unit only adds losses,
  never removes one), so the FAIL verdicts for S1/S2 are robust to the reading either way.
  [T2-ladder-hygiene/REPORT.md sections 5.2, 8]

One disclosed literal deviation, not a bar: T3's fresh pull ran at 0.4 s request spacing
against the frozen text's "spacing >= 0.5 s" (an operational rate-limit parameter, traced
to the task brief itself conflicting with the frozen doc). T3 flags it, argues correctly
it cannot bias any reported statistic, and does not silently re-run at 0.5 s.
[T3-wallets-oos/REPORT.md section 3 item 8]

## 2. Pre-mortem checks not run, and does the omission threaten a verdict?

Every one of the pre-mortem's checks is nominally addressed in each test's own checklist
(T1: 24/24, T2: 22/22, T3: 22/22, T4: 18/18 - self-reported, and spot-checked here against
the pre-mortem text line by line; no missing item found in the four checklists). One item
is functionally not run to the depth the pre-mortem specified, though it is now moot for
the adoption decision:

- Premise-risk #7 ("the 3,500-row pull cap censored exactly the long-horizon buckets where
  Phase-1 found its largest effects... compare the fresh uncapped pull's effect sizes in
  those same buckets against the OLD capped-corpus numbers to see whether the effect
  shrinks"). T3 fixes the cap going forward (full paging, verified max offset 9500) and
  re-measures OOS, but does not go back and quantify, bucket-by-bucket, how much the
  ORIGINAL 08-01 in-sample finding was itself inflated by the cap defect. Immaterial to
  the current KILL (which stands on the fresh data regardless of why the original finding
  looked as strong as it did), but it leaves unanswered exactly how much of Phase-1's
  headline wallet numbers (map item 7: "+2.4/+1.7/+3.7/+6.7pp") were a censoring artifact
  versus how much has genuinely evaporated out-of-sample.
  [T3-wallets-oos/REPORT.md sections 0, 3]

No omission was found that threatens a verdict currently on record - every kill/fail in
T1-T4 is over-determined by more than one independent check.

## 3. Numbers that contradict each other or the Phase-1 map; spot check re-run

- C1 - wallet skill persistence sign-flips out of sample (highest-severity contradiction
  in the whole set). Phase-1 map item 7 / `wallets/REPORT.md`: Spearman(prior P&L, fresh
  BUY excess) = +0.37 (N=3,056, in-sample). T3's fresh, walk-forward, bar-compliant
  re-measurement: Spearman = -0.0569 (p=0.0008, N=3,444) - opposite sign, and both the
  primary class (B5a, kill) and its own pre-declared edge<0 control (B5d, expected <=0,
  measured +0.86pp, t=2.99, bootstrap P(<=0)=0.000) invert in the same direction. Three
  independent statistics (B5a, B5d, persistence) all flip together - this is a coherent
  inversion, not three unrelated nulls, and it directly contradicts the Phase-1 map's
  central "new data class" claim.
  [T3-wallets-oos/REPORT.md sections 4, 8; engine_restart_2026-09-08.md Phase-1 item 7]
- C2 - Phase-1's "$1,932 reachable" figure does not match the engine-true replayed dollar
  loss in the same windows; SPOT-CHECKED here. `information-structure/REPORT.md` (the
  row "loser prints engine-true reachable (resting on the LOSER = flip-fill exposure)":
  135 prints, 3,646 shares, $1,932 "$ ceded", 3 windows) is a count of raw CLOB print
  value at the resting price, not what the paper-trading fill rule (strictly-below-full-
  fill; one blended booking per window) actually books. T2's engine-true replay of the
  SAME 3 named windows (1787271900/1787358600/1787620800) books -$100.00 / -$100.00 /
  -$75.00 = -$275.00 total - about 7x smaller than the "$1,932" figure the
  pre-registration quotes when introducing the re-arm-defect problem ("all loser-side
  reachable value... ($1,932...) sits in re-arms"). Confirmed by re-reading
  `information-structure/REPORT.md` section 3a/3e directly: the "$1,932" column is
  "$ ceded" from raw prints, a theoretical-exposure number, not a bookable P&L number -
  spot check confirms these are two different, legitimately-defined quantities that the
  pre-registration's own prose conflates by juxtaposing them; a reader taking "$1,932" as
  "what the bug costs" over-states the realized stakes by about 7x.
  [information-structure/REPORT.md section 3a, 3e; T2-ladder-hygiene/REPORT.md section 3.1]
- C3 - Phase-1's implied 4th loss window is wrong; the real 4th loss is a different,
  previously unflagged window. The pre-registration's own pre-mortem (item T2-#10)
  directed T2 to hold out ep 1788267900 as "the 4th known bot-zone loss from
  book-dynamics." T2's replay, plus a direct spot check of `book-dynamics/proj_grid.csv`
  for that window run in this pass, CONFIRMS T2's finding that this is NOT a real loss:
  the bridged (deployed) signed displacement never clears the 0.6 floor inside k in
  [6,25] - grid values at k=25/22/20 are 0.536/0.246/-0.473; it only exceeds 0.6 at k=27,
  outside the placement window. book-dynamics' original classification used the *plain*
  projection, not the deployed *bridged* one, which is why it looked like a 4th loss.
  Meanwhile T2 independently discovered a real, previously unflagged 4th re-arm loss, ep
  1787678700 (08-25 ET, -$200.00), hidden from the pre-registered protocol run because
  `dead_book_windows.csv` wrongly marks it dead (it has a live, decided book with real
  prints). Net effect: Phase-1's roster of "the" loss windows is wrong in two independent
  ways for the same reason (plain-vs-bridged mismatch on one, a dead-book
  misclassification on the other), and the pre-registration itself was built on the wrong
  roster.
  [T2-ladder-hygiene/REPORT.md sections 3.1, 3.3, 2.3; spot check this pass:
  `book-dynamics/proj_grid.csv` rows for ep=1788267900, k in {20,22,25,27,30},
  s_bridged = -0.473 / 0.246 / 0.536 / 0.626 / 0.831 - confirms <0.6 throughout k in [6,25]]
- C4 - a shared upstream artifact defect (`dead_book_windows.csv`) is documented by T2
  but not cross-corrected in T1 or T4. T2 quantifies it precisely: 28 of the 253 CSV rows
  have live, decided books (226-2,122 BBO changes each), including 2 of the 3 named loser
  windows and 7 of the 17 baseline profitable fill windows (+$793.66 of the reported
  +$1,882.72). T1 (section 2.6, `P_deadAny`) and T4 (pre-mortem #2) each ran their OWN
  independent dead-book detector, but both only add more exclusions on top of the CSV
  (167 more windows for T1, 29 more for T4) - neither tests removing T2's specific 28
  wrongly-included windows, i.e. nobody re-ran T1's B1/B2 log-loss and taker-leg numbers,
  or T4's P1, on the corrected `dead_confirmed.csv` (225 rows) that T2 built. Given T1/T4's
  N is in the thousands, 28 windows is very unlikely to flip a verdict, but the claim
  "T1/T4 are robust to this defect" has never actually been tested with T2's specific
  correction - only with each test's own, different, superset exclusion.
  [T2-ladder-hygiene/REPORT.md section 2.3, `dead_confirmed.csv`;
  T1-zone-engine/REPORT.md section 2.6; T4-whole-window-controls/REPORT.md pre-mortem #2]

No contradiction was found in the raw log-loss magnitudes across T1/T4 (they decrease
monotonically from k=240 (~0.599) to k=58 (~0.164) to k=10 (~0.0066) - internally
consistent with more information closer to close) or in corpus sizes (7,018 era windows,
26 ET days 08-13..09-07, cited identically by T1 and T2).

## 4. Claims load-bearing for an adoption decision that rest on a single window, a proxy, or code reading

- The entire re-arm mechanism's real-world existence rests on production logs from a
  STALE configuration, not the one being adopted/rejected. T2's log cross-check (134 real
  multi-arm windows, confirming re-arms genuinely happen) covers only 07-13..08-27, when
  the deployed config was need 2.0 to 1.0, the OLD p99.5 table, 5 rungs - not today's need
  0.6 / current table / 8 rungs / k_max 25. In that entire logged period, zero fills were
  ever booked on a re-arm (arm>=2) - meaning every dollar figure behind H1/H2/H3's
  adoption calls (loser $ removed, winner $ forgone) is a paper-replay artifact with no
  live or logged confirmation, at any configuration, that a re-arm has ever actually
  filled, let alone at the currently deployed settings.
  [T2-ladder-hygiene/REPORT.md sections 1.1, 7 item 13, 9]
- T1's taker-leg PASS bars are carried by a small, non-representative slice. Of the
  +$355.6 total, +$155.9 (44%) comes from just 26 of 326 trades (8%) where the book
  already leaned against the model's chosen side; unchanged-ask fills (N=136, +9.41 c/sh)
  and improved-ask fills (N=190, -1.46 c/sh) diverge by more than 10 c/sh depending on an
  unspecified pricing convention (ask(t) vs ask(t+L)) that flips the whole cell's sign
  (+3.08 to -0.57 c/sh); 13 of 23 days are positive and the top 3 days carry 69% of the
  dollars. The cell already fails on its own bars (ii)/(v), so this doesn't change the
  disposition, but it means the PASSed sub-bars (i, iii, iv, vi) should not be read as
  "the taker leg nearly worked" - most of its apparent edge is a few days and a few
  outlier trades.
  [T1-zone-engine/REPORT.md sections 3.1, 3.2]
- T3's on-chain-identity claim ("proxyWallet == on-chain maker/taker, EMPIRICALLY
  VERIFIED") is N=1. One decoded OrderFilled transaction, cross-checked against two RPC
  providers that happen to agree with each other on the same transaction - not two
  independent transactions. Phrasing ("empirically verified, not merely inferred")
  over-states an n=1 confirmation; it is consistent with the documented contract design
  (correctly cited), but the "empirical" claim itself is a single data point.
  [T3-wallets-oos/REPORT.md section 6]
- T3's kill hinges on a single 6-day, non-stationary week (see C1 above and the section 1
  proxy-sensitivity point): the whole B5a/B5d/persistence inversion is measured on
  09-02..09-07, with a documented within-week sign flip (first 3 days positive, exc
  +0.31/+0.15/+0.80pp; last 3 negative, -2.52/-2.48/-2.17pp) and a half-2 t-stat of -28.7
  built on only 3 day-clusters (T3 itself warns not to over-read that magnitude). The kill
  bar is met on the literal pooled cell, but the entire "wallet information not confirmed
  OOS" conclusion is one calendar week's data, not yet a stable measurement.
  [T3-wallets-oos/REPORT.md section 4 "Both chronological halves", section 8]

## 5. What the operator needs before acting

1. Rebuild `dead_book_windows.csv` from T2's `dead_confirmed.csv` (225 rows) and re-run
   T1's B1/B2 and T4's P1 against it, specifically restoring the 28 windows T2 showed are
   live/decided books (2 named losers + 7 fill windows + likely others in T1/T4's larger
   corpora) - a cheap re-run, not a new data pull, and the one concrete check nobody has
   done yet (section 3, C4).
2. September-dated production logs (or a short live/paper observation window) at the
   CURRENT deployed config (need 0.6, current p99.5 table, k_max 25, 8 rungs), to confirm
   at least once that a re-arm can actually fill under today's settings before trusting
   any H1/H2/H3 dollar figure as more than a paper-replay hypothesis (section 4). Nothing
   in T1-T4 substitutes for this; it is a live-data gap, not a code change.
3. A decision on which "recalibrated book" is the pre-registration's actual baseline for
   every B1-style bar (T1, P1, P2): a 1-parameter logistic recalibration (what all four
   tests used) vs. a flexible one (isotonic/spline). T1 section 2.4 shows the k=45/30 PASS
   does not survive an isotonic baseline (p=0.29/0.65); T4 shows isotonic is itself worse
   than logistic recalibration in its own corpus. This is a definitional gap in the frozen
   doc, not something a test can resolve on its own, and it determines whether T1's only
   two PASS cells count as information or as "beats an under-fit book."
4. More OOS days before re-deciding T1's k=58 model-class dependency and T3's wallet
   kill: T1's k=58 flips PASS/FAIL depending on bucket vs exact-k model class at a
   sub-1%-relative margin (section 1); T3's kill rests on 6 days with a documented
   mid-week reversal. Neither is resolvable with more replay of the same data - both need
   calendar days that don't exist locally yet (T1: no 09-08 recordings exist locally; T3:
   only 6 fresh ET days exist at all).
5. No code change is indicated by this critic pass - T1-T4 killed cleanly enough
   (independently over-determined, per section 2) that the pre-registration's own
   dispositions stand: "projection real but not tradable as a taker" (T1), no
   ladder-hygiene change adopted (T2, though see #2 above before treating that as final),
   wallet identity not confirmed OOS (T3), whole-window question closed to k<=60 for the
   tested feature classes only (T4, explicitly scoped - does not reopen or reaffirm the
   wider 09-01 135-feature closure).

## 6. Artifacts

Spot check: `book-dynamics/proj_grid.csv` filtered to ep=1788267900 (pandas one-liner,
output used in section 3 C3 above). No new script files were needed beyond this
generator; every other number in this report is read directly from the four test
REPORT.md files and `information-structure/REPORT.md` (the "$1,932" row) cited above.
