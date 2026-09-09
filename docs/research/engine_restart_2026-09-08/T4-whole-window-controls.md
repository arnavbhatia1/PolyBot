# T4 — Whole-window controls

Pre-registration: `docs/research/engine_restart_2026-09-08.md`, section T4.
Executed exactly as frozen; no threshold was lowered. All P1/P2 comparisons are
against **book_recal = logistic on [logit(book_p)]** only, matching the T1 B1
bar's own definition of "recalibrated book" (a 1-parameter logistic
recalibration), which P1 explicitly reuses ("B1 bar vs recalibrated book").

## Bottom line

**P1 kills at all four required k (240,180,120,90): M' does not beat the
recalibrated book anywhere — it is measurably worse (higher log-loss) at every
k, in both chronological halves, under two independent z-feature pipelines,
and under an isotonic-recalibration sensitivity check. M'' (the Chainlink-vs-
Binance-relay basis) is UNDECIDABLE at all four k — the recorded Binance relay
stream (`bz`) never starts more than ~85 s before a window's close in the
entire 26-day corpus, so zero windows have a basis observation at k∈{90,120,
180,240}; the feature cannot be computed as specified, not merely "found
weak."**

**P2 kills cleanly: all 8 pre-registered Bonferroni-corrected tests
(imb02, imb1 × k∈{240,180,120,60}) show M1(f) worse than book_recal (higher
log-loss), none pass raw p<0.05 in the pass direction, none pass Bonferroni
α=0.05/8=0.00625. The 5-minute-change context features (not part of the
corrected set) show the same null pattern.**

**P3 is NOT_RUN: the r10 15m-market `.meta.json` files (282 windows, 08-22..31)
carry only `conditionId, outcomePrices, outcomes, clobTokenIds, volume, closed,
orderPriceMinTickSize, orderMinSize` — no question text, slug, or
price_to_beat field in any of the 282 files. `strike_15m` is not recoverable
from the specified local artifacts, and the pre-reg's own fallback clause
applies: "If the strikes are not recoverable, mark P3 NOT_RUN with the
reason."**

Per the pre-reg's kill clause: **P1 and P2 both fail → the whole-window
question is closed by evidence to k ≤ 60**, for the specific 2-3-feature
logistic model class tested here (see Gap/scope note below — this does NOT
reopen or extend the wider 09-01 135-feature/GBT-class closure, which used a
different model class and feature set).

---

## P1 — pre-zone book+z model, k ∈ {240,180,120,90}

### Data pipeline

- **book_p**: `window_paths.db` (1 Hz), nearest row within 0.6 s of decision
  time t=ep+300-k (targets: elapsed 60/120/180/210 s). Required two-sided book
  (bid & ask present on both tokens), mid_up+mid_down∈[0.98,1.02],
  `book_age_up_s`≤10 s AND `book_age_down_s`≤10 s (the deployed freshness
  gate — NOT merely "last BBO change ≤ t"). book_p = mid_up, clamped
  [0.01,0.99]. window_paths.db only has data through 2026-09-01 18:42 UTC
  (`SELECT max(ts)`→1788288162, ep 1788288000 partial) — this caps P1's usable
  corpus to ET days 08-13..09-01 regardless of the 26-day era window list.
- Coverage per k (of 4,962-5,198 era windows in the elapsed-tolerance window):
  book_ok=4,992/5,185 (240 s), 4,967/5,187 (180 s), 4,559/5,195 (120 s),
  3,933/5,197 (90 s) — falling coverage at smaller k reflects the book
  legitimately going one-sided as it locks (matches CLAUDE.md's "lock right in
  6,393/6,397 windows"; not a data defect).
- **z**: reused directly from `information-structure/d2_outside.py`'s
  `outside_samples.parquet` — z=(spot−strike)/rv60, spot=latest raw Chainlink
  report (rx≤t, ≤3 s old), rv60=sqrt(Σ squared consecutive raw-report diffs)
  over reports with rx∈(t−60,t], requires ≥20 reports. "cold" (rv60 undefined)
  excluded: 229-256/7,018 windows per k.
- **dead-book exclusion**: `book-dynamics/dead_book_windows.csv` (253 ep)
  excluded before every fit (see Pre-mortem #2 below for an independent
  cross-check of this list).
- Joint dataset per k after all gates (book_ok & ¬cold & ¬dead): n=4,800
  (k=240), 4,788 (180), 4,384 (120), 3,787 (90).
- **M''**: basis = Chainlink raw spot(t) − Binance relay price(t), relay price
  = latest `bz` report with rx≤t (win_streams.jsonl.gz `bz` stream, same rx
  convention as `common.py bridge_delta`). Checked directly: across all 6,811
  era windows with any `bz` data, the **earliest** `bz` report in any window
  is never more than 84.99 s before that window's close (max over all
  windows: −84.989 s from close; 99th/100th percentile of "earliest report"
  ≈−84.4 s). k=90 requires a report ≤ −90 s from close — **zero windows ever
  have one**; k=120/180/240 are further still. `n_bz_reports_available_at_k`
  = 0 for all four k (see `bz_prices.parquet`, built directly from
  `win_streams.jsonl.gz`). **M'' is UNDECIDABLE at every P1 k by data
  non-existence, not a negative result** — the deployed system only ever
  records the Binance bridge stream in roughly the final 85 s, presumably
  because that's the only window where `chainlink_feed.py` consults it.

### Walk-forward log-loss, book_recal vs M' (day-block bootstrap B=5,000)

| k | n_eval | n_oos_days | logloss(book_recal) | logloss(M') | mean_diff (M'−book) | boot p | days_improved/17 | **B1 verdict** |
|---|---|---|---|---|---|---|---|---|
| 240 | 4,217 | 17 | 0.598763 | 0.599041 | **+0.000155** | 0.6824 | 8 | **FAIL** |
| 180 | 4,197 | 17 | 0.494274 | 0.494368 | **+0.000092** | 0.2860 | 7 | **FAIL** |
| 120 | 3,823 | 17 | 0.403066 | 0.403138 | **+0.000076** | 0.1820 | 8 | **FAIL** |
| 90  | 3,275 | 17 | 0.371267 | 0.371413 | **+0.000259** | 0.7608 | 10 | **FAIL** |

M' is *worse* than book_recal (positive mean_diff = higher log-loss) at all
four k, not merely non-significant. B1 bar (log-loss < recal book, p<0.05,
≥15 OOS days) fails on the direction of the effect before p-value is even
relevant. Both chronological halves confirm (M' worse than book in both
halves at k=240/180/120; at k=90, M' marginally better in half 1
[08-16..08-23] but worse in half 2 [08-24..09-01], net worse and p=0.76 —
consistent with noise, not a real edge). Full per-day coefficients:
`p1_coefs_by_day.csv`.

**Why**: corr(logit(book_p), z) = 0.70-0.82 across k (highest at k=240,
falling toward k=90); z's own naive directional accuracy (0.67-0.83) tracks
just under the book's naive accuracy (0.68-0.85) at every k. The book already
prices almost all of the spot-vs-strike information z carries — exactly Phase
1 finding #1 ("the open already prices the strike lag; Spearman 0.82 with
spot−strike"), now reconfirmed walk-forward and out of sample at k up to 240 s.

### M'' (basis feature)

UNDECIDABLE at k∈{240,180,120,90} — see data-coverage note above. N=0 usable
windows at every required k. No nearest-faithful substitute was scored inside
the P1 table (k=90 is the closest to the ~85 s recording horizon and still
has 0 coverage); reporting a k=60 descriptive number would be answering a
different, non-pre-registered question and risks being mistaken for a P1
result, so it is omitted here (available on request from `bz_prices.parquet`
if useful context for T1).

### Sensitivity checks (pre-mortem-mandated)

**#13 — z pipeline choice (does the verdict depend on which z you compute?).**
Independent second pipeline: `wp_zproxy.parquet`, built from
`window_paths.chainlink_price`/`chainlink_age_s` (a different recorded field,
1 Hz within-window only, age-gated ≤3 s, ≥20-sample floor — same formula,
disjoint code path and largely disjoint underlying samples from the D2
raw-tick cache). Result: **identical qualitative verdict** — M' with z_wp is
worse than book_recal at all four k (mean diffs +0.000341/+0.000086/
+0.000090/+0.000565; boot p 0.68/0.67/0.23/0.53). **The P1 kill does not flip
under either z definition.**

**#14 — flexible-recalibration baseline (does "beating the book" survive a
better book baseline?).** Fit an isotonic-regression recalibration of raw
book_p (walk-forward, same day splits) as an alternative to the 1-parameter
logistic book_recal. Result: **isotonic recalibration is itself worse than
logistic recalibration** at every k (e.g. k=240: logloss_iso=0.609374 vs
logloss_book_recal=0.598763, boot p=0.0028 in iso's disfavor) — almost
certainly because per-day training sets are small early in the walk-forward
and isotonic has no smoothness prior. Naively comparing M' to this isotonic
baseline would spuriously "pass" (M' beats a worse baseline) at all four k;
**that comparison is invalid and is not credited** — the pre-registered
baseline is the logistic recalibration, against which M' fails outright. This
is flagged explicitly per the adversary's check rather than silently omitted.

**ANTI / edge<0 control (pre-mortem #6).** Not constructed as a separate arm:
P1 is a symmetric log-loss comparison of two calibrated-probability models
with no directional "side" or "fire" decision (unlike B2/H-tests, where ANTI
= same rule, opposite side is well-defined). This mirrors T1's own B1 test
(Phase 1 D4), which also carries no ANTI arm for the same structural reason —
only B2's taker-leg dollar test defines ANTI/edge-bucket controls in the
frozen doc. In lieu of a formal ANTI arm, two structural checks are reported
instead: (a) the z-coefficient sign in the fitted M' is checked per day
(median coefficient positive at all 4 k, i.e. correctly oriented — no
backwards-feature artifact), and (b) days_improved is close to half of OOS
days at every k (7-10/17), consistent with pure noise rather than a
systematic but sub-significant edge in either direction.

---

## P2 — Binance UM bookDepth imbalance, k ∈ {240,180,120,60}

### Data

- Downloaded fresh (read-only public GET, as pre-authorized):
  `https://data.binance.vision/data/futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-<date>.zip`
  for 2026-08-14..2026-09-01 (19 daily files, 13.0 MB zipped, 656,640 rows,
  matches the ~13.8 MB estimate).
- **Timestamp format, stated explicitly**: the `timestamp` column is **a UTC
  datetime string** `"YYYY-MM-DD HH:MM:SS"` — neither Unix ms nor µs. Parsed
  with `pd.to_datetime(..., utc=True)` then converted to epoch seconds.
  **Alignment proof**: row epoch 1786668601.0 → decoded `2026-08-14 00:50:01
  UTC`; the 12 raw rows at that exact original string timestamp
  `2026-08-14 00:50:01` show percentages {-5,-4,-3,-2,-1,-0.2,0.2,1,2,3,4,5}
  with notional 6.41e8 down to 3.71e7 (bid side) and 4.36e7 up to 7.46e8 (ask
  side), i.e. round-trips exactly. 12 rows/timestamp confirmed, 54,720 unique
  timestamps across 19 days (30 s cadence, 2,880/day × 19 = 54,720 exact).
- `imb02=(bid_notional−ask_notional)/(bid+ask)` at %=0.2 (bid=row at −0.2%,
  ask=row at +0.2%); `imb1` likewise at ±1%. `delta_*` = value − value 300 s
  earlier (context only).
- Feature at decision time t = latest bookDepth timestamp ≤ t, required
  ≤30 s stale (one cadence step); p99 staleness 29-30 s at every k (i.e. right
  at the cadence floor — no silent gaps).
- Joined to the same book_p/book_ok/dead-book gates as P1 (window_paths.db
  caps this at 08-13(partial)..09-01 too, 20 unique ET days total, 17 OOS
  after the first-3-train-only rule).
- N per (feature,k): 4,354 (k=240), 4,333 (180), 3,953 (120), 2,378 (60 — the
  two-sided-book gate bites hardest this close to close, as in P1).

### Pre-registered comparison set: exactly 8 (Bonferroni α=0.05/8=0.00625)

**Confirmed count = 8** (imb02, imb1) × (k=240,180,120,60), matching the
frozen doc's stated denominator. The 5-minute-change features (delta_imb02,
delta_imb1) are scored identically below but **reported as context only,
excluded from the denominator**, per pre-mortem #15 (the shared-context task
brief described 4 features; the frozen pre-registration document specifies
only 2 features × 4 horizons = 8 and that number is what the Bonferroni
correction is keyed to — the frozen doc is authoritative).

| feature | k | n_eval | n_oos_days | logloss(book_recal) | logloss(M1) | mean_diff | raw p | days_improved/17 | Bonferroni pass (α=0.00625) |
|---|---|---|---|---|---|---|---|---|---|
| imb02 | 240 | 4,354 | 17 | 0.598760 | 0.599030 | +0.000297 | 0.0156 | 7 | **FAIL** |
| imb1  | 240 | 4,354 | 17 | 0.598760 | 0.599330 | +0.000635 | 0.0064 | 5 | **FAIL** |
| imb02 | 180 | 4,333 | 17 | 0.495027 | 0.495393 | +0.000304 | 0.1780 | 7 | **FAIL** |
| imb1  | 180 | 4,333 | 17 | 0.495027 | 0.495554 | +0.000526 | 0.0228 | 6 | **FAIL** |
| imb02 | 120 | 3,953 | 17 | 0.405294 | 0.406067 | +0.000743 | 0.1504 | 8 | **FAIL** |
| imb1  | 120 | 3,953 | 17 | 0.405294 | 0.405606 | +0.000463 | 0.2032 | 7 | **FAIL** |
| imb02 | 60  | 2,378 | 17 | 0.298970 | 0.299223 | +0.000134 | 0.6580 | 7 | **FAIL** |
| imb1  | 60  | 2,378 | 17 | 0.298970 | 0.299736 | +0.000534 | 0.3596 | 6 | **FAIL** |

Every cell has mean_diff > 0 (M1(f) *worse* than book_recal). Not one cell
even clears the *uncorrected* α=0.05 in the improving direction with the
required log-loss sign (imb1@240's raw p=0.0064 looks small but mean_diff is
positive — i.e. the model is *worse*, and the small p reflects that it is
*consistently* worse across days, not that it beats the book). None pass
Bonferroni. Both-halves check on the two smallest-raw-p cells (imb02@240,
imb1@240) confirms M1(f) worse than book_recal in **both** chronological
halves for both features.

### Context only — 5-minute changes (not in the corrected 8, no threshold applied)

| feature | k | mean_diff | raw p | days_improved/17 |
|---|---|---|---|---|
| delta_imb02 | 240 | +0.000461 | 0.4380 | 6 |
| delta_imb1  | 240 | +0.000673 | 0.0640 | 3 |
| delta_imb02 | 180 | +0.000723 | 0.2396 | 8 |
| delta_imb1  | 180 | +0.000751 | 0.1964 | 9 |
| delta_imb02 | 120 | +0.000243 | 0.5768 | 8 |
| delta_imb1  | 120 | +0.000365 | 0.3848 | 8 |
| delta_imb02 | 60  | −0.000322 | 0.3084 | 11 |
| delta_imb1  | 60  | +0.000446 | 0.2844 | 6 |

Same null pattern (7/8 worse; the one nominally-better cell, delta_imb02@60,
is not close to significant, p=0.31). No feature in this table would change
the P2 verdict even if it had been folded into the corrected set.

**Data-coverage check (pre-mortem #16).** All 8 primary cells clear 17 OOS
days, well above the ≥15 bar — **P2 is not data-coverage-limited**; the kill
is a clean measured FAIL, not an UNDECIDABLE.

**ANTI / edge<0 control**: same structural note as P1 — this is a log-loss
model comparison, not a directional bet; no ANTI arm is defined for this test
shape in the frozen doc (only B2 defines one). No substitute claim of "beats
the book" is being made here to require one.

---

## P3 — nested strike dominance (optional)

**NOT_RUN.** Inspected all 282 `.meta.json` files under
`scripts/research/data/vps-0831/r10_pm_15m/` (08-22..31). Every file's key set
is identical: `{clobTokenIds, closed, conditionId, orderMinSize,
orderPriceMinTickSize, outcomePrices, outcomes, volume}`. `outcomePrices` is
the *resolved* terminal price (e.g. `["1","0"]`), not `price_to_beat`; there
is no question text, slug, or strike field anywhere in the metadata as
recorded. Per the pre-registration's own contingency ("If the strikes are not
recoverable, mark P3 NOT_RUN with the reason"), `strike_15m` cannot be derived
from the specified artifacts, and no other data source was in scope for this
slice (a live Gamma fetch for the 15m strike was not pre-authorized for P3 and
was not attempted). Pre-mortem #17 (cross-check meta strikes against an
independent 60 s-TWAP reconstruction) is therefore N/A — no meta-derived
strike was used for anything.

---

## Pre-mortem checks — full disposition

1. **rx-clock only, never payload_ts/ets, in any k/decision-tick computation.**
   Confirmed by construction: `outside_samples.parquet`'s z is
   `d2_outside.py`'s rx-indexed computation (reused verbatim, not
   recomputed); `bz_prices.parquet` bisects on `bz` entries' `rx` field
   (index 0 of each `[rx,ts,px]` triple), matching `common.py bridge_delta`'s
   own convention; `bookp.parquet`/`wp_zproxy.parquet` use `window_paths.ts`/
   `elapsed_s`, which is the collector's own receipt-time field, not a
   payload or exchange timestamp. No script written for this slice reads
   `payload_ts` or `ets` for any k or decision-time computation (grepped:
   zero hits in `build_bookp.py`, `build_bz.py`, `build_wp_zproxy.py`,
   `build_p2_features.py`, `model_p1.py`, `model_p2.py`).
2. **Independent dead/frozen-book detector, diffed against the 253-row CSV.**
   Built from `book-dynamics/micro_b_dedup.parquet` (final-90s BBO changes):
   (a) crude "≤1 distinct record on either side" detector flags 642/6,884
   micro-tape windows — mostly *legitimately* decided-early markets (a book
   that locks at 0.99/0.01 and never changes again is healthy, not dead), so
   this alone is not a good proxy (419 of the 642 are outside the existing
   253-row CSV, consistent with "healthy-locked" rather than "placeholder").
   (b) Refined to match the documented dead-book signature (frozen near
   0.50/0.51, not near a decided extreme): 245 windows flagged,
   **216/253 (85%) overlap with `dead_book_windows.csv`**, 29 additional
   candidates not in the CSV, and 37 CSV entries not caught this way (likely
   the placeholder-oscillates-around-0.10/0.90-with-many-dedup-rows pattern
   the "≤1 record" test structurally can't see). **Sensitivity re-run**:
   excluding the extra 29 candidates on top of the existing 253 at k=240
   changed **zero** rows in the final P1 dataset (n=4,800 unchanged,
   logloss unchanged to 6 decimals) — these 29 candidates don't survive the
   independent book_ok gate (two-sided/sum/freshness) anyway. **No effect on
   the verdict; reported per the check's own instruction regardless.**
3. **Every $ total/win rate/N counted once per window.** P1/P2 have no dollar
   totals. Every N in every table is a distinct-(ep,k) row count;
   `bookp.parquet` is built with `drop_duplicates("window_id", keep="first")`
   per k, so each window contributes exactly one row per k, never one per
   tick. Confirmed no N exceeds the corresponding distinct-ep count at that k.
4. **Walk-forward, no leakage.** `tr=(et_day<d)`, `te=(et_day==d)`, first 3
   sorted days skipped as train-only, in every one of `model_p1.py`,
   `model_p2.py`, and the sensitivity scripts. Verified separately that
   Python string-sorting `et_day` (e.g. "08-13".."09-07") exactly matches
   epoch-chronological order for this date range (no month/year wraparound in
   the corpus) — `sorted(unique et_day) == epoch-sorted(unique et_day)` holds
   exactly.
5. **Day-block bootstrap, B≥2,000, whole ET days, day-block count reported.**
   B=5,000 throughout (`day_block_boot()` resamples `len(unique days)` days
   with replacement, `B` times). `n_oos_days` is printed next to every
   p-value in every table above (17 for P1 and P2 uniformly).
6. **ANTI + edge<0 controls for every claimed edge.** No edge is claimed by
   either P1 or P2 (both kill). Both are symmetric log-loss model
   comparisons, a test shape the frozen doc does not define an ANTI/edge
   bucket for (only B2 does); addressed explicitly above rather than silently
   omitted, with substitute sanity checks (coefficient-sign, days-improved
   near 50%, both-halves) reported in their place.
7. **Pre-declared cell computed and quoted first.** P1's k order in every
   script and table is (240,180,120,90), matching the frozen doc's literal
   list order; P2's is imb02→imb1 within each k, k order (240,180,120,60),
   also literal order. Verdict text above quotes the full frozen-order grid,
   not a cherry-picked cell — there is no single "adoption cell" concept in
   P1/P2 (unlike T1's B2 e_min=0.05), since these are omnibus/Bonferroni
   grids where the kill rule applies to the whole set.
8. **Every numeric bar diffed against the doc's literal wording.** P1: "M's
   OOS log-loss < recalibrated book's..., day-block p<0.05, ≥15 OOS days" —
   applied exactly, using the doc's own definition of "recalibrated book"
   (1-parameter logistic), not isotonic or any other flexible recalibration.
   P2: "Bonferroni over features × horizons (α 0.05/8)" — applied as
   α=0.05/8=0.00625 exactly, denominator 8 confirmed (see #15). No threshold
   was lowered or substituted anywhere in this slice.
9. **`ws2_ladder_replay.py` copy-before-edit.** N/A — not used in this slice
   (P1/P2 need no p99.5 margin table or ladder replay; see #12).
10. **No subscribe/websocket/place_order/polybot.main in this session's own
    commands.** Grepped this session's command history mentally and via the
    scripts themselves: zero occurrences. The only network calls made were
    plain HTTPS GETs to `data.binance.vision` for the pre-authorized public
    daily zip files (P2's own data source, explicitly in scope).
11. **book_p only from a two-sided, book_age≤10s book.** Enforced exactly as
    `book_ok = two_sided & sum_ok & fresh` in `build_bookp.py`, using
    `book_age_up_s`/`book_age_down_s` (the deployed freshness field), not
    "last BBO change ≤ t." Coverage counts reported per k above.
12. **p99.5/margin-table byte-identity.** N/A — neither P1 (z uses rv60, not
    p99.5) nor P2 (no margin table at all) touches `TWAP_MARGIN_P995`
    anywhere. Explicitly noted rather than silently skipped.
13. **rv60/z recomputed two ways; pass/fail flip check.** Done — see "Why"
    and Sensitivity #13 above. **Does not flip**; both pipelines kill P1 at
    every k.
14. **Flexible-recalibration (isotonic) sensitivity on the book baseline.**
    Done — see Sensitivity #14. Isotonic recal is *worse* than the
    pre-registered logistic recal; the naive "M' beats isotonic" comparison
    is explicitly flagged as invalid and not credited toward any pass.
15. **P2's scored set is exactly 8, extras not folded in.** Confirmed by
    construction (`PRIMARY_FEATURES=[imb02,imb1]` × `KS` (4) = 8 rows in
    `p2_primary_results.csv`; deltas are in a separate
    `p2_context_results.csv` with no Bonferroni column at all).
16. **P2 restricted to actual Binance-UM-bookDepth-covered dates; OOS day
    count checked.** Bookdepth data covers 2026-08-14..09-01 UTC (verified:
    `bd_min`=1786665601, `bd_max`=1788307171); joined against window_paths.db
    (itself capped at 09-01). Result: 20 unique ET days total, 17 OOS at
    every one of the 8 primary cells — **comfortably above the ≥15 bar. P2 is
    a clean measured FAIL, not UNDECIDABLE-by-coverage.**
17. **Cross-check meta strikes against an independent TWAP reconstruction.**
    N/A — P3 is NOT_RUN; no meta-derived strike was used for anything to
    cross-check.
18. **Scope of the P1/P2 kill, stated explicitly.** This kill closes the
    narrow 2-3-feature logistic-model class tested here (book_p + z; book_p +
    Binance-depth-imbalance) at k∈{240,180,120,90/60}. It does **not** extend
    or reaffirm the wider 09-01 135-feature/gradient-boosted-tree-class
    program closure referenced in MEMORY.md ("book perfectly calibrated,
    0/135 features") — that was a different model class over a different
    (larger) feature set, evaluated separately. The two closures are
    consistent in direction but are not the same evidence and should not be
    cited interchangeably.

---

## Gaps / scope notes

- P1/P2 are both hard-capped to ET days **08-13(partial)..09-01** by
  `window_paths.db`'s own coverage ceiling (`max(ts)`=2026-09-01 18:42 UTC),
  not by anything in this slice's design. The frozen doc's own P1 line
  anticipates this ("book from window_paths (1 Hz, 5,140 labeled windows to
  09-01)"). 09-02..09-07 era days (present in `win_streams.jsonl.gz` and used
  by other T4-adjacent tests) are unavailable for P1/P2 specifically because
  they need the 1 Hz book snapshot, which this cache doesn't carry past
  09-01. This is a pre-existing data-collection gap, not something this
  slice can close without a fresh (out-of-scope) live collection.
- M'' is UNDECIDABLE rather than killed — a real, verified data-availability
  fact (the `bz` stream literally does not exist before ~85 s pre-close in
  the recorded corpus), not a negative result about the candidate-F basis
  feature's usefulness. Testing it properly would require either recording
  `bz` further back from close (an infra change, out of scope for a
  read-only research slice) or restricting to k≤85 s, which is inside T1's
  zone, not P1's stated pre-zone range.
- No live-book, live-Binance, or live-Polymarket calls were made anywhere in
  this slice; the only network activity was the pre-authorized Binance
  UM bookDepth historical-archive downloads for P2.
