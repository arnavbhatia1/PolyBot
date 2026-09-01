# Information program — mid-window outcome prediction, pre-registration

**Authorized 2026-09-01 by the operator, explicitly overriding the entry-side
research closure ("Hard Rule 1") and directing a from-scratch strategy find.**
This document is written BEFORE any model is trained. The bars below are
frozen; changing them after seeing results voids the run.

## Objective

Predict P(final ≥ strike) for btc-updown-5m at decision horizons
k ∈ {240, 180, 120, 60, 30} seconds before close, **better than the order
book prices it at the same instant**, by enough to clear taker costs. The
book is the incumbent champion: the prior program's calibration-perfect
models lost to it on log score in 100% of splits. This program's only
license to exist is NEW data those models never saw.

## Data (Phase A)

| source | content | status |
|---|---|---|
| `window_paths.db` (box copy) | 1 Hz whole-window: both books, depth3, touch sizes, binance_price, **binance_cvd_10s/30s**, chainlink price+age, strike | copying; era slice ≈ 2.2M rows. Known defect: bid-side size/depth cols are worst-level before 08-21 — those columns restricted to 08-21+ |
| tape_*.jsonl | every CLOB print, whole window (taker side, price, size) | on disk, 08-13..31 |
| win_streams / window_labels | strike, final, winner truth | on disk, current |
| **NEW** spot 1s klines (full cols incl. takerBuyVolume) | 1 s taker-flow imbalance, momentum, vol | downloading, 08-13..31 |
| **NEW** perp 1m klines + 5 m metrics (open interest) | basis, OI delta, perp flow | downloading |

## Feature families (fixed now; nothing added after results are seen)

1. Momentum: spot & perp returns over trailing 10/30/60/120/300 s.
2. Vol: realized vol 60/300 s and their ratio; |return|/vol z-scores.
3. Flow: Binance taker-buy imbalance sums 30/60/120 s (1s klines);
   window_paths CVD 10/30 s; perp-spot basis; OI delta (5 m).
4. CLOB flow: signed print flow (taker BUY − SELL) on Up at prices 0.2–0.8,
   trailing 60/120 s, from tape.
5. State: (chainlink − strike) and (book mid − 0.5), each normalized by
   trailing vol; depth/size imbalance (08-21+ only); seconds-of-day (sin/cos).
6. Excluded by construction: anything derived from fills/positions
   (poisoned for entry research), anything using data after decision time.

## Models (fixed now)

L2 logistic regression and sklearn HistGradientBoosting (defaults;
max_iter 300, early stopping on train-tail). No other models, no tuning
loops beyond the fixed small grid {lr 0.05/0.1, leaves 15/31}.

## Protocol

- One row per (window, horizon); decision instant = the window_paths tick
  nearest close−k with both books fresh (< 5 s) and a finite mid.
- Walk-forward by ET day over the 60s era (08-14..31, ~17-18 usable days):
  train on days < d, predict day d. No shuffling, ever.
- Benchmark at the same instant: book-implied P = up-mid (average of
  best bid/ask on Up), clamped to [0.01, 0.99].

## Pre-registered bars

- **B1 (information exists):** model OOS log-loss < book-implied log-loss at
  ≥1 horizon, with day-level block bootstrap p < 0.05, on ≥15 OOS days.
- **B2 (tradable):** taker simulation at the executable touch (ask of the
  favored token) with 0.65 s staleness shift, fee 0.07·sh·p·(1−p), one bet
  per window per horizon, ≥ $5 notional at touch size: EW ≥ +2¢/sh on ≥100
  OOS trades; net ¢/sh monotone across model-edge buckets; the edge<0
  control bucket ≤ 0; anti-side ≤ 0.
- **Kill:** B1 fails at every horizon → the program closes and the entry-side
  closure is re-affirmed with 60s-era + new-data evidence. B1 passes but B2
  fails → recorded as "information without tradability"; a maker-side use
  (quote-skewing) may be proposed separately, not assumed.
- **Ship path if B2 passes:** engine integration proposal + the standard
  paper bar. Nothing trades real money off this document.

## VERDICT (run 2026-09-01, same session; bars above were frozen first)

**B1 FAIL at every horizon. B2 FAIL. The program closes per its own kill
clause.** Walk-forward OOS over 19 ET days, 17,269 (window, horizon) rows,
4,729 windows [data r16_out.txt, r17_out.txt, r17_report.json]:

| k | n OOS | book log-loss | logit | GBT | p(model beats book) |
|---|---|---|---|---|---|
| 30s | 775 | **0.261** | 0.390 | 0.458 | 1.000 / 1.000 |
| 60s | 2,303 | **0.298** | 0.336 | 0.409 | 1.000 / 1.000 |
| 120s | 3,794 | **0.405** | 0.439 | 0.492 | 1.000 / 1.000 |
| 180s | 4,160 | **0.495** | 0.525 | 0.585 | 1.000 / 1.000 |
| 240s | 4,185 | **0.597** | 0.617 | 0.684 | 1.000 / 1.000 |

The book wins on essentially every day at every horizon (bootstrap mass
entirely one-sided). The logistic model receives the book's own mid as a
feature and still scores worse than the book alone — the added features
carry noise, not signal. B2: taker sim at edge ≥ 4¢ runs −2.3 to −3.2¢/sh
at k ∈ [60,240]; the +1.8¢ cell at k=30 fails the monotonicity clause
outright (the edge<0 CONTROL bucket is its second-best cell at +2.5¢ —
the anti-predictive signature the bar exists to catch).

Caveats recorded: `binance_cvd_*` and pre-08-21 size-imbalance columns in
window_paths are NULL-by-design (dead feeds) and were effectively absent;
the same information family was supplied by the 1s taker-buy imbalance
features, which were live. k=30 passes fewer freshness filters (n=775).

**Disposition: the entry-side closure is re-affirmed under the operator's
own 09-01 override — now on 60s-era data, with external flow/OI/basis
features, under pre-registered bars.** Reopening this again requires a
genuinely new DATA CLASS (private/paid flow, cross-venue L2 at depth),
not new models on the same inputs.

## Addendum (pre-registered 09-01, before running): book-anchored family

Operator directed a stronger model attempt. The correct strengthening is
book-anchored nested models, frozen as follows BEFORE the run:

- x0 = logit(book_p). **M0** = logistic on [x0] alone (walk-forward): tests
  whether the book is MISCALIBRATED — beatable with zero new information.
- **M1(f)** = logistic on [x0, f] for each of the 27 features separately:
  per-feature incremental information on top of the price. Significance:
  day-bootstrap, Bonferroni across 27 features × 5 horizons (α 0.05/135 ≈
  0.00037) for any claim; raw p reported for transparency.
- **M2** = GBT on [x0 + all features] (same fixed grid): the anchored
  kitchen sink, for completeness.
- **M3** = logistic on [x0 + top-3 features], the three chosen by TRAIN-set
  incremental value inside each fold (no test contact).
- Bars: same B1/B2 as the main program, applied to this family. Any pass
  additionally requires the winning feature(s) to be stable in sign across
  folds. Kill: no member beats the book at Bonferroni-corrected significance
  → model class is exhausted on this information set; the program stays
  closed and further attempts require new data, not new estimators.

## Addendum VERDICT (run 09-01, bars frozen first): model class exhausted

- **M0 (book recalibration): the book is perfectly calibrated at every
  horizon** — recalibrated log-loss equals or slightly trails the raw book
  everywhere (k=30: 0.2641 vs 0.2610; k=60: 0.2984 vs 0.2983; …; every
  p ≥ 0.64). No zero-information edge exists.
- **Per-feature incremental value: 0 of 135 feature×horizon tests reach even
  nominal p < 0.05**, let alone Bonferroni. Best cell: ret_30 at k=60,
  Δlog-loss +0.0007 (p=0.32) — noise, and two orders of magnitude below
  taker costs even if real. No feature has a stable sign across folds.
- M2 (anchored GBT) overfits badly (0.50–0.90 log-loss); M3 (train-picked
  top-3) trails the book at all horizons and its picks churn fold-to-fold.
- **Kill clause applies in its strongest form: the book at k ∈ [30, 240]s is
  calibrated AND informationally complete against everything derivable from
  public spot/perp 1s flow, OI, basis, and our own CLOB tape. Better
  estimators cannot help — there is nothing to estimate. Reopening requires
  a new data class (private/paid flow), filed as a build proposal.**
  [data r18_out.txt, r18_report.json]

## Honesty notes

The prior closure's methodology lessons are retained as measurement
conventions (event-true books, one bet per window, no 1 Hz-BBO scoring,
monotonicity + control), because dropping them manufactures fake edges —
they are scoring rules, not conclusions. The conclusions themselves are
treated as open: this run assumes nothing about whether the book is beatable.
