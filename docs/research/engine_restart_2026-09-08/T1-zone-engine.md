# T1 - Zone engine, k in [6,60]: book-anchored probabilistic model (B1 information, B2 taker leg)

Slice `T1-zone-engine`, run 2026-09-09 against the frozen pre-registration
`docs/research/engine_restart_2026-09-08.md` section T1. Repo read-only; no bot, websocket, collector or
order code was run (PM10 grep: zero hits). Every number below is **measured** on local copies unless tagged
*computed* (a fit / bootstrap) or *inferred*. N is given with every number. All scripts and artifacts are in
this directory (see section 8).

## 0. Verdict

**B1: FAIL as written (passes at k = 45 and 30 only; fails at k = 58, 20, 10). B2: FAIL (bars (ii) and (v) fail
in the pre-declared cell). Kill clause applies: the projection's information is recorded as "real but not tradable
as a taker"; the lock-gated ladder stays the only zone instrument.**

The load-bearing finding is a correction of Phase-1 D4. With the 253 dead-book windows excluded (as the
pre-registration required), model M's out-of-sample log-loss advantage over the recalibrated book collapses from
0.0135-0.0259 (D4) to at most 0.0018 (k = 45/30, -1.7 % / -3.3 % relative, day-block p 0.0002 / 0.0076, 23 OOS
days, N = 5,907 / 5,889 windows) and to nothing at k = 58 (-0.0007, p 0.19), k = 20 (+0.00005, p 0.86) and k = 10
(+0.0002, p 0.92). Re-including the dead-book windows reproduces D4's numbers exactly in magnitude (-0.0138 at
k = 58, -0.0274 at k = 10; section 2.7), so D4's "information" was the placeholder books. What survives lives
entirely in the book-confident population (book P outside [0.05, 0.95]): M sharpens a 0.99 book toward 0.995-0.999
when the projection agrees (k = 45: -8.4 % relative, p 0.0002, 21/23 days). Inside contested books (P in
[0.05, 0.95]) M is not better than the recalibrated book at any k (p >= 0.16; the sign is wrong at 58/59/20/10),
and against an isotonic recalibration of the book the k = 45/30 advantage is not significant either (p 0.29 /
0.65). When book and projection disagree on the side, the projection is right in only 35-42 % of windows
(N = 21-178), the reverse of D4's 62-90 %.

The taker leg at the pre-declared cell (e_min 0.05, L 0.42 s) fires 628 times / 23 OOS days (27/day), fills 326
(52 %; the other 302 saw the ask rise within 0.42 s), and realizes **EW +3.08 c/sh** (win 73.9 %, Wilson
68.9-78.4 %), **$355.6 at $15 notional** (H1 +$156.6 / H2 +$199.0), ANTI -11.25 c/sh (N 150), edge<0 control
-0.65 c/sh (N 5,543). It fails **(ii)**: the sweep partition reads -0.65 / +0.00 / +2.18 / +6.25 / **+4.90** across
{<0, [0,3), [3,6), [6,10), >=10} (the top bucket, N = 19, sits below [6,10), N = 32) and **(v)**: day-block
p(EW <= 0) = 0.072 (23 day blocks, 95 % CI -1.0 .. +7.5 c). The aggregate is fragile in three further ways, all
descriptive: 13 of 23 days are positive and the top three days carry 69 % of the dollars; fills at an improved
ask (the ask fell inside L; N 190) run -1.46 c/sh while unchanged-ask fills (N 136) run +9.41 c/sh, and pricing
every fill at ask(t) instead of ask(t+L) turns the cell to -0.57 c/sh; and the only sub-population that is
individually significant is the 26 trades where the book leaned against the chosen side (mean book P 0.47,
+18.6 c/sh, p 0.011), while the 278 contested trades run +1.42 c/sh (p 0.26). Context cells (e_min 0.08:
+7.2 c/sh, N 209, p 0.023; L 0.70: +3.6 c/sh, N 294, p 0.046) pass (v) but every cell fails (ii); adoption reads
only the pre-declared cell, which fails.

**Scope statement (PM24):** nothing in this report bears on the ladder's r >= 0.6 lock. A B1/B2 pass would have
licensed only a taker-side probabilistic engine at k in [25, 60]; the maker fill / adverse-selection question is
T2's and is not touched here. With B1/B2 failing, the deployed lock-gated ladder remains the only zone instrument.

## 1. Data, population and conventions

| item | value | N |
|---|---|---|
| corpus | `win_streams.jsonl.gz`, era ep >= 1786665600, 26 ET days 08-13..09-07 | 7,018 windows |
| labels | `window_labels.resolved_up` (paper DB; the 100 08-14 windows from `polybot_live.db`, as `ws1_reduce.py` does); asserted equal to `win_streams.up` for all windows; strike/final asserted equal; 0 exact ties | 7,018 |
| decision ticks | `information-structure/cache/ticks.parquet` (raw Chainlink report receipts, 0 < k <= 60); verified against a fresh `common.Window.proj` recomputation on 11,548 sample ticks: k = close - rx exactly, projection diffs 0.0, None-agreement 100 % | 391,602 ticks |
| fixed-k rows (B1) | engine-true projection (plain and bridged) at t = close - k, k in {59,58,45,30,20,10}; agrees with Phase-1 `surf_samples.parquet` to 0.0 at the five shared k | 42,108 rows |
| book | `book-dynamics/micro_b_dedup.parquet` (every BBO price-state change, final 107 s); state at t = last change <= t; book_p = Up mid when the Up token is two-sided, else 1 - Down mid when Down is two-sided, clamped [0.01, 0.99]; undefined otherwise (2.0-2.7 % of windows) | 3,017,645 records |
| liveness | `book-dynamics/liveness.parquet` (per-second raw b-record counts incl. size updates); live3 = a raw record for the token used within [floor(t)-3, floor(t)]; age10 likewise 10 s | 1,187,726 cells |
| dead books | `dead_book_windows.csv` excluded (253, all in corpus) | 253 |
| margin table | `TWAP_MARGIN_P995` from `polybot/core/signal_engine.py` == `ws2_ladder_replay.r1_tables()['P995']` (asserted identical, PM12) | 16 knots |
| projection | plain for M and the taker (as deployed); bridged reported as context | |
| clocks | k = close - rx on OUR receipt clock everywhere; no payload_ts / ets in any k (PM1) | |
| walk-forward | ET day = UTC - 4 h; model for day d fitted on rows with et_day < d (asserted); first 3 ET days (08-13, 08-14, 08-15) train-only; 23 OOS days 08-16..09-07 | |
| halves | H1 = OOS days <= 08-25 (10 days), H2 = 08-26..09-07 (13 days) (Phase-1 boundary) | |
| bootstrap | B = 5,000 resamples of whole ET days; unique day count next to every p | |

Primary B1 population **P**: dead-book CSV excluded AND book_p defined AND projection not cold AND live3.
Coverage at k = 58/45/30/20/10: book_p defined 97.5/97.5/97.6/98.0/98.1 % of windows; live3 among those
99.3/96.4/99.1/96.4/96.4 %; scored rows 5,939/5,907/5,889/5,845/5,828.

Model **M** (pre-registered): P(Up) = sigma(a + b*logit(book_p) + c*r_signed), r_signed = (proj - strike)/p99.5(k)
toward Up, fitted per k-bucket {[45,60], [25,45), [15,25), [6,15)} on tick-level rows (363,899 filtered ticks;
91,673 / 121,742 / 60,338 / 54,090 per bucket), LogisticRegression(C = 1e4). Last-day coefficients
(09-07): b = 1.01/0.99/1.02/0.94, c = 1.80/2.50/2.89/7.41 by bucket. Tick-level rows are used ONLY for fitting;
every scored quantity is one row per window (PM3). Baselines fitted on the same training rows: `recal` (logistic on
logit(book_p); the bar's baseline), `iso` (isotonic on logit(book_p), clipped [1e-3, 1-1e-3]), `spline` (natural
cubic spline, knots at the 5/25/50/75/95 % quantiles of logit(book_p)), `book` (raw). `combo_x` / `recal_x` etc. =
the same models fitted per exact k on the fixed-k rows of earlier days (D4's design; PM23).

## 2. B1 - information (re-verification with dead-book exclusion)

Bar (frozen): M's OOS log-loss < recalibrated book's at k in {58,45,30,20,10}, day-block p < 0.05, >= 15 OOS days.
p reported two-sided (D4's convention; the one-sided p is also given and never changes a verdict below).

### 2.1 The bar table (population P, plain r, bucket M vs logistic recal, all 23 OOS days)

| k | N windows | days | LL(M) | LL(recal) | diff | rel | p two-sided | p one-sided | days M better | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 58 | 5,939 | 23 | 0.16381 | 0.16453 | -0.00072 | -0.44 % | 0.193 | 0.097 | 15/23 | **FAIL** |
| 45 | 5,907 | 23 | 0.09921 | 0.10098 | -0.00177 | -1.75 % | 0.0002 | 0.0002 | 18/23 | PASS |
| 30 | 5,889 | 23 | 0.05417 | 0.05599 | -0.00182 | -3.25 % | 0.0076 | 0.0038 | 17/23 | PASS |
| 20 | 5,845 | 23 | 0.02751 | 0.02747 | +0.00005 | +0.17 % | 0.861 | 0.431 | 16/23 | **FAIL** |
| 10 | 5,828 | 23 | 0.00664 | 0.00643 | +0.00021 | +3.33 % | 0.920 | 0.540 | 13/23 | **FAIL** |
| 59 (context, PM16) | 5,937 | 23 | 0.17360 | 0.17403 | -0.00043 | -0.25 % | 0.484 | 0.242 | 15/23 | (fail) |

Effect sizes next to p (PM22): at k = 58 the point estimate has D4's sign but is 19x smaller than D4's -0.0135
and is not marginal (p 0.19); at k = 20/10 the sign is reversed. This is not an under-powered miss: the diagnostic
in 2.7 shows D4's magnitude returns as soon as the dead-book windows are re-included. Extending to 09-08 is not
possible on this machine (no 09-08 recordings; `polybot/memory/recordings` ends 09-07).

M vs the RAW book (context): -0.0020 / -0.0049 / -0.0062 / -0.0067 / -0.0093 at k 58/45/30/20/10, p 0.087 /
0.0002 / 0.0002 / 0.0002 / 0.0002 - almost all of which is the recalibration itself (recal vs book: -0.0012 /
-0.0031 / -0.0044 / -0.0068 / -0.0095, p 0.27 / 0.0002 x4).

### 2.2 PM13 - test days strictly after 08-27 (outside the p99.5 table's fit window) vs inside

| k | post-08-27: N / days / diff / p | inside 08-14..27: N / days / diff / p |
|---|---|---|
| 58 | 2,869 / 11 / -0.00160 / 0.087 | 3,070 / 12 / +0.00011 / 0.87 |
| 45 | 2,854 / 11 / -0.00170 / 0.0076 | 3,053 / 12 / -0.00183 / 0.0004 |
| 30 | 2,842 / 11 / -0.00263 / 0.0004 | 3,047 / 12 / -0.00107 / 0.35 |
| 20 | 2,824 / 11 / -0.00045 / 0.33 | 3,021 / 12 / +0.00051 / 0.99 |
| 10 | 2,819 / 11 / +0.00108 / 0.054 (wrong sign) | 3,009 / 12 / -0.00060 / 0.76 |

The k = 45/30 advantage survives on post-08-27 days alone (p 0.0076 / 0.0004, 11 day blocks); it is not
table-leakage. k = 58/20/10 fail on both subsets.

### 2.3 PM15 - book-confident vs contested sub-populations (M vs recal)

| k | confident (P outside [0.05,0.95]): N / diff / rel / p / days better | contested (P in [0.05,0.95]): N / diff / rel / p / days better |
|---|---|---|
| 58 | 4,109 / -0.00175 / -3.8 % / 0.0002 / 21/23 | 1,830 / +0.00159 / +0.37 % / 0.42 / 10/23 |
| 45 | 4,802 / -0.00188 / -8.4 % / 0.0002 / 21/23 | 1,105 / -0.00127 / -0.29 % / 0.49 / 12/23 |
| 30 | 5,327 / -0.00191 / -14.1 % / 0.0004 / 19/23 | 562 / -0.00095 / -0.21 % / 0.88 / 13/23 |
| 20 | 5,559 / -0.00085 / -9.5 % / 0.21 / 20/23 | 286 / +0.01743 / +4.5 % / 0.66 / 13/23 |
| 10 | 5,728 / -0.00082 / -70 % / 0.0002 / 22/23 | 100 / +0.05916 / +19 % / 0.25 / 8/23 |

**The whole B1 signal is inside the saturated majority**: M knows that a 0.99 book with a confirming projection
is 0.995-0.999, and it shaves the tail log-loss on 19-22 of 23 days. Inside contested books M adds nothing at any
k (p >= 0.16 everywhere; positive sign at 58/20/10). The pre-mortem requirement (advantage must survive in the
contested subset alone) is not met at any k.

### 2.4 PM14 - flexible recalibration of the book

| k | M vs isotonic recal: diff / p / days | M vs spline recal: diff / p / days |
|---|---|---|
| 58 | +0.00006 / 0.99 / 12 | -0.00052 / 0.41 / 12 |
| 45 | -0.00099 / 0.29 / 13 | -0.00171 / 0.0004 / 16 |
| 30 | -0.00035 / 0.65 / 12 | -0.00193 / 0.0024 / 17 |
| 20 | -0.00087 / 0.48 / 17 | +0.00004 / 0.87 / 15 |
| 10 | -0.00048 / 0.68 / 17 | +0.00008 / 0.98 / 14 |

Against an isotonic (monotone, non-parametric) recalibration of the book the k = 45/30 advantage is not
significant (p 0.29 / 0.65): most of the "sharpening" M does at the 0.99 tail is reproduced by letting the book's
own tail be recalibrated flexibly. Against a 5-knot spline (smooth) it survives at 45/30. The advantage is
therefore partly "beats an under-fit book" and at most 0.001 log-loss of projection information.

### 2.5 PM23 - exact-k model class (D4's design) vs the pre-registered bucket model

| k | combo_x vs recal_x: diff / p / days | bucket M vs exact-k M (combo - combo_x): diff / p |
|---|---|---|
| 58 | -0.00098 / 0.023 / 17 | +0.00034 / 0.16 |
| 45 | -0.00148 / 0.0088 / 18 | -0.00036 / 0.079 |
| 30 | -0.00188 / 0.0020 / 18 | +0.00009 / 0.85 |
| 20 | -0.00079 / 0.35 / 18 | +0.00080 / 0.081 |
| 10 | -0.00012 / 0.86 / 15 | +0.00012 / 0.42 |

Under the exact-k class k = 58 flips to a pass (p 0.023, -0.6 % relative); k = 45/30 pass; k = 20/10 still fail.
The k >= 25 kill decision therefore depends on the model class at k = 58 only, and the effect there is
-0.6 % / -0.4 % relative under either class. Post-08-27 exact-k: k 58 p 0.064, 45 p 0.020, 30 p 0.0004, 20 p 0.25,
10 p 0.10 (wrong sign). Contested exact-k: no k significant (p >= 0.30).

### 2.6 Population variants and bridged projection (M vs recal, all days)

| variant | k 58 diff / p | k 45 | k 30 | k 20 | k 10 |
|---|---|---|---|---|---|
| P (primary; CSV excl + live3) | -0.00072 / 0.19 | -0.00177 / 0.0002 | -0.00182 / 0.0076 | +0.00005 / 0.86 | +0.00021 / 0.92 |
| P_noLive (CSV excl only) | -0.00071 / 0.23 | -0.00237 / 0.0002 | -0.00236 / 0.0036 | -0.00122 / 0.36 | -0.00162 / 0.35 |
| P_age10 (PM11: two-sided + age <= 10 s) | -0.00070 / 0.23 | -0.00178 / 0.0002 | -0.00224 / 0.0060 | -0.00067 / 0.51 | +0.00001 / 0.97 |
| P_deadAny (CSV + independent detectors) | -0.00032 / 0.53 | -0.00174 / 0.0002 | -0.00149 / 0.012 | -0.00001 / 0.82 | +0.00032 / 0.86 |
| P, bridged r (ladder's projection; context) | -0.00126 / 0.028 | -0.00197 / 0.0002 | -0.00196 / 0.0040 | -0.00003 / 0.82 | -0.00051 / 0.60 |

No variant changes the k = 20/10 verdicts; the bridged projection would pass k = 58 (p 0.028) but the taker
uses the plain projection as deployed and the pre-registration names the plain one for M.

### 2.7 Diagnostic - dead-book windows re-included (why D4 differed)

| population | model | k 58 diff / p | k 45 | k 30 | k 20 | k 10 |
|---|---|---|---|---|---|---|
| no dead exclusion, no liveness, bridged, exact-k (= D4's design) | combo_x vs recal_x | -0.01377 / 0.0008 | -0.01828 / 0.0004 | -0.02132 / 0.0004 | -0.02593 / 0.0012 | -0.02737 / 0.0002 |
| D4 as published | combo vs book_recal | -0.0135 / 0.0016 | -0.0182 / 0.0004 | -0.0213 / 0.0004 | -0.0259 / 0.0012 | -0.0247 / 0.0002 |
| no dead exclusion, live3, plain, bucket M | combo vs recal | -0.01002 / 0.0028 | -0.00175 / 0.0020 | -0.01658 / 0.0052 | -0.00235 / 0.36 | -0.00386 / 0.048 |

D4 reproduces to the third decimal when its population is rebuilt, and the gain disappears when the 253
dead-book windows are excluded (the liveness filter alone does not remove them - frozen placeholder books still
emit size-only updates). Disagreement statistics tell the same story: with dead books re-included the projection
is right in 63/74/77/85/91 % of side disagreements at k 58/45/30/20/10 (D4: 62/73/76/84/90 %); with them excluded
it is right in 35/42/35/38/38 % (N 178/96/55/32/21). The Phase-1 D4 conclusion ("license for a probabilistic engine
in k in [25,60]") was a placeholder-book artifact.

### 2.8 Halves (M vs recal)

H1 (08-16..08-25, 10 days): k 58 +0.00055 / p 0.34; 45 -0.00191 / 0.0012; 30 -0.00031 / 0.85; 20 +0.00130 / 0.85;
10 +0.00054 / 0.91. H2 (08-26..09-07, 13 days): 58 -0.00168 / 0.037; 45 -0.00165 / 0.0016; 30 -0.00296 / 0.0002;
20 -0.00090 / 0.059; 10 -0.00003 / 0.97. Only k = 45 is negative and significant in both halves.

### 2.9 B1 verdict per k

k 58 FAIL (bucket M; exact-k would pass at -0.6 %), k 45 PASS, k 30 PASS, k 20 FAIL, k 10 FAIL. The bar as
written ("at k in {58,45,30,20,10}") is not met. The kill clause "B1 fails at k >= 25" is met at k = 58 under the
pre-registered model class and not met at k = 45/30; under every additive check the k >= 25 advantage is confined
to book-confident windows (2.3) and to a logistic (not isotonic) recalibration baseline (2.4), with a ceiling of
0.002 log-loss (< 0.2 pp of probability on a 0.99 book) - unusable by a taker whose cheapest edge threshold is
3 c. B1 is recorded as FAIL with the k = 45/30 tail-sharpening noted as real but sub-cent.

## 3. B2 - taker leg replay

Rule as frozen: at each raw tick with k in [6,58], side = argmax P_M (bucket M, plain r, OOS walk-forward model
of that ET day); fire on the FIRST tick per window where P_M(side) - ask(side,t) - 0.07*ask*(1-ask) >= e_min;
the order lands at t + L; FOK fills iff ask(side,t+L) <= ask(side,t); price = ask(side,t+L) (the resting ask
matched; the ask(t)-priced variant is reported); hold to resolution; c/sh = payout - price - fee(price); $ at
$15 notional = 15/price shares. Population = B1's P at tick level: 291,142 ticks / 5,969 windows / 23 OOS days
(08-16..09-07) with a model prediction. One fire per window; N trades == N windows in every table (PM3).
The pre-declared cell was computed and written to `b2_lock_predeclared.json` at 2026-09-09T13:59:54Z; the first
context cell was computed at 13:59:55Z (`b2_context_grid.json`) (PM7, PM20).

### 3.1 Pre-declared cell: e_min = 0.05, L = 0.42 s

| quantity | value |
|---|---|
| fires (attempts) | 628 over 23 days = 27.3/day (median 22); per-day range 8-74 |
| fills (trades) | 326 (52 %); 302 unfilled because ask(t+L) > ask(t) (median rise +0.03, p90 +0.10); 0 unfilled for a missing ask |
| trade days / windows | 23 / 326 |
| EW | **+3.08 c/sh**; median +18.9 c; win 241/326 = 73.9 % (Wilson 68.9-78.4 %) |
| dollars at $15 | **+$355.6** total; H1 (08-16..25, N 196) +$156.6, EW +1.64 c; H2 (08-26..09-07, N 130) +$199.0, EW +5.24 c |
| day-block bootstrap | p(EW <= 0) = **0.072**, two-sided 0.144, 95 % CI [-1.00, +7.50] c, 23 day blocks, B = 5,000 |
| ANTI (other side at the same tick) | N 150 fills, EW **-11.25 c/sh**, -$712, other side won 19.3 %, mean price 0.293 |
| edge<0 control (first tick with edge < 0) | N 5,543 fills / 5,931 fires, EW **-0.65 c/sh**, -$784, mean price 0.964, win 95.9 % |
| mirror control (first tick with edge <= -5 c) | N 272 fills, EW -5.95 c/sh, -$368 |
| mean price / P_M(side) / book P(side) | 0.695 / 0.828 / 0.723 |
| k of fires (p10/p50/p90) | 23.6 / 44.7 / 57.4 s; fires by bucket [45,60] 290, [25,45) 253, [15,25) 56, [6,15) 29 |
| book P of chosen side at fire (p5/p25/p50/p75/p95) | 0.495 / 0.665 / 0.765 / 0.84 / 0.905; fires with P < 0.5: 48, [0.5,0.9]: 539, > 0.9: 41 |
| unfilled fires would have won | 84.4 % (vs 73.9 % filled): the FOK is adversely selected |

**Bars, pre-declared cell**

| bar | wording applied | measured | verdict |
|---|---|---|---|
| (i) | EW >= +2 c/sh on >= 100 OOS trades over >= 15 OOS days | +3.08 c, 326 trades, 23 days | PASS |
| (ii) | net c/sh non-decreasing across {<0, [0,3), [3,6), [6,10), >=10} | -0.65 (N 5,543) / +0.00 (4,700) / +2.18 (121) / +6.25 (32) / **+4.90 (19)** | **FAIL** (top bucket below [6,10)) |
| (iii) | <0 control bucket <= 0 | -0.65 c (N 5,543) | PASS |
| (iv) | ANTI <= 0 | -11.25 c (N 150) | PASS |
| (v) | day-block p(EW > 0) < 0.05 | p = 0.072 (23 day blocks) | **FAIL** |
| (vi) | both halves > $0 at $15 notional | +$156.6 / +$199.0 | PASS |

Bucket partition for (ii): the rule at e_min = 0 (first tick with edge >= 0; 5,852 fires / 4,872 fills) bucketed
by the OOS edge at the fire tick, plus the <0 control arm; every window appears once per arm. Own-arm buckets of
the pre-declared trades (edge >= 5 c only): [3,6) = [5,6): +1.48 c (N 121), [6,10): +1.38 c (N 146), >=10:
+10.56 c (N 59) - also not monotone ([6,10) < [3,6)), though the top bucket is the best cell. The anti-predictive
signature REFUTATIONS.md warns about (control = best cell) is absent: the control is the worst bucket in both
partitions. The failure is a non-monotone top with N = 19 (sweep) / N = 146 vs 121 (own arm).

### 3.2 What carries the +3.08 c (descriptive, computed after the lock; no bar changed)

| slice | N | EW c/sh | win | $ | p(EW<=0) | note |
|---|---|---|---|---|---|---|
| contested (book P of side in [0.5, 0.9]) | 278 | +1.42 | 73.0 % | +$177.3 | 0.26 | mean price 0.703, P_M 0.836, book 0.731 |
| book leans against (P of side < 0.5) | 26 | +18.57 | 65.4 % (Wilson 46-81 %) | +$155.9 | 0.011 | mean price 0.451, P_M 0.619, book 0.472; 13 days |
| book confident for (P > 0.9) | 22 | +5.74 | 95.5 % | +$22.5 | 0.14 | 11 days |
| k >= 25 (the engine lane) | 281 | +2.26 | 74.4 % | +$259.7 | 0.18 | H1 +$139.8 / H2 +$119.9; ANTI -8.3 c (N 126) |
| k < 25 | 45 | +8.17 | 71.1 % | +$95.9 | 0.11 | ANTI -26.8 c (N 24) |
| by k-bucket [45,60] / [25,45) / [15,25) / [6,15) | 154 / 127 / 28 / 17 | +4.09 / +0.05 / -3.60 / +27.55 | 76 / 72 / 61 / 88 % | | |
| fills at an improved ask (ask fell inside L) | 190 | **-1.46** | 67.4 % | | | mean improvement 0.064 |
| fills at an unchanged ask | 136 | **+9.41** | 83.1 % | | | |
| every fill priced at ask(t) (conservative) | 326 | **-0.57** | | | 0.61 | CI [-4.8, +4.0] |
| Up side / Down side | 129 / 197 | +2.00 / +3.78 | | | | |

Per day: 13 of 23 days positive in dollars; the top three days (08-31 +$93, 08-20 +$82, 08-22 +$70) carry 69 % of
the total; worst days 09-07 -$39, 08-24 -$38, 08-17 -$29. Calibration of P_M on the filled trades: bins
(0.5,0.6] / (0.6,0.7] realized 65 % / 69 % (P_M 0.57 / 0.66; prices 0.46 / 0.49) - these two bins are the profit;
(0.7,0.8] / (0.8,0.9] / (0.9,0.95] realized 66 / 71 / 80 % against P_M 0.76 / 0.85 / 0.93 (over-confident by
10-14 pp, EW +1.5 / -2.3 / -1.0 c); (0.95,1] realized 90 % vs 0.975 (EW +4.5 c).

### 3.3 PM17 - top-of-book ask size (window_paths, ts >= 08-22, ends 09-01 18:42 UTC)

Size known for 140 of 628 fires (79 of 326 trades; 8 ET days 08-22..09-01). Ask size < 15 shares at
14.3 % of size-known fires (11.4 % of trades); the $15 order exceeds the top-of-book ask size in 21.5 % of
size-known trades (ask size p10/p50/p90 = 13 / 112 / 293 sh). Depth-OK subset (62 trades, 8 days): EW +8.75 c,
win 74 %, +$130.5 but H1 -$9.4 / H2 +$139.9, p 0.093 -> fails (i) on N/days, (v), (vi). The all-size-known
subset (79 trades): +8.21 c, p 0.046, H1 -$9.4. Too small to decide; reported as required.

### 3.4 PM18 - freshness gate at t and t + L

Chosen token two-sided at both t and t+L and a raw book record within 10 s at both: 326 of 326 trades pass
(0 dropped), so the fresh-only EW equals the cell's. The fire population already required liveness (3 s) on the
book_p token.

### 3.5 Context grid (computed after the lock; adoption reads only 3.1)

| cell | fires | trades | fill | EW c/sh | win | $ total | H1 / H2 | p(EW<=0) | ANTI c (N) | bars i/ii/iii/iv/v/vi |
|---|---|---|---|---|---|---|---|---|---|---|
| **e 0.05, L 0.42 (pre-declared)** | 628 | 326 | 52 % | **+3.08** | 73.9 % | +$355.6 | +156.6 / +199.0 | **0.072** | -11.2 (150) | P/**F**/P/P/**F**/P |
| e 0.03, L 0.42 | 1,026 | 489 | 48 % | +0.29 | 76.9 % | +$122.3 | +56.1 / +66.1 | 0.46 | -8.0 (237) | F/F/P/P/F/P |
| e 0.08, L 0.42 | 401 | 209 | 52 % | +7.17 | 74.2 % | +$396.7 | +63.9 / +332.9 | 0.023 | -18.8 (95) | P/F/P/P/P/P |
| e 0.05, L 0.70 | 628 | 294 | 47 % | +3.58 | 72.4 % | +$365.0 | +187.3 / +177.8 | 0.046 | -14.7 (105) | P/F/P/P/P/P |
| e 0.03, L 0.70 | 1,026 | 427 | 42 % | +0.51 | 75.2 % | +$106.7 | +17.3 / +89.4 | 0.42 | -14.1 (152) | F/F/P/P/F/P |
| e 0.08, L 0.70 | 401 | 184 | 46 % | +6.04 | 70.1 % | +$331.5 | +43.4 / +288.1 | 0.047 | -24.9 (66) | P/F/P/P/P/P |

(ii) fails in every cell: at L = 0.42 the sweep partition is the one above; at L = 0.70 it reads -0.44 / -0.17 /
+3.14 / +3.21 / **-0.65** (N 5,497 / 4,468 / 96 / 31 / 18). e_min 0.03 loses its edge (+0.3 c; contested slice
-2.2 c); e_min 0.08 concentrates it (+7.2 c, N 209) but H1 is +$64 against H2 +$333 and (ii) still fails. The grid
is consistent with a small, top-heavy, day-concentrated positive expectation that the frozen bars do not certify.

## 4. Pre-mortem checks (all 24 run)

1. **Clock discipline** - all k use rx (receipt); `ticks.parquet` verified against a fresh `common.Window.proj`
   (chainlink_feed.py replica) on 11,548 sample ticks: k - (close - rx) max |diff| 0.0, projection diffs 0.0; grep of
   t1 scripts for payload_ts/ets: zero uses in any k (`p0_checks.json`, `cmdlog.txt`). PASS.
2. **Independent dead-book detector** - micro (<= 2 raw records in the window, or <= 2 distinct price states while
   contested at k = 30): 356 flags, 158 not in the CSV (134 of them are windows with NO book records at all - they
   drop out via undefined book_p; 32 have a frozen-while-contested book); window_paths (both sides' median book age
   > 60 s in the final 60 s): 200 flags on 5,222 covered windows, 52 not in the CSV; CSV windows missed by the
   detectors: 55 (micro) / 26 (paths). Union of extras: 167; B1 re-run with the union excluded (`P_deadAny`,
   2.6) does not change any verdict. Reported, not adopted as primary.
3. **One row per window** - B1: one row per window per k (N <= 5,939 < 6,765 eligible); B2: N trades == N windows
   (326) in every arm; tick-level rows feed only the fits. PASS.
4. **Walk-forward assert** - `t1lib.walkforward_*` asserts max(train et_day) < test day for every day; log of 23 days
   in `b1_models_P_plain.json` (`wf_log`: e.g. day 08-16 trains on <= 08-15 with 25,449 ticks; 09-07 on <= 09-06 with
   315,593). No shuffling anywhere. PASS.
5. **Bootstrap** - B = 5,000 whole-ET-day resamples everywhere; unique day counts printed next to every p (23 / 11 /
   12 / 10 / 13). PASS.
6. **ANTI + edge<0 control** - computed for the pre-declared cell and every context cell (3.1, 3.5). PASS.
7. **Pre-declared first** - `b2_lock_predeclared.json` stamped 13:59:54Z before `b2_context_grid.json` 13:59:55Z;
   the verdict quotes the pre-declared cell first. PASS.
8. **Bar wording diff** - section 5; no numeric bar deviates; interpretive choices listed and none loosens a bar.
9. **Repo read-only** - `git status` shows only the pre-existing untracked pre-registration file; `ws2_ladder_replay.py`
   was imported read-only for `r1_tables()` and never edited or copied. PASS.
10. **No live wire** - grep of every T1 script and `cmdlog.txt` for subscribe/websocket/ws:///wss:///place_order/
    cancel_order/polybot.main/run_polybot: zero hits. PASS.
11. **book_p freshness** - book_p always comes from a two-sided token (own or complement; the other case is
    undefined and dropped); primary filter live3 (<= 3 s) is stricter than age <= 10 s; the age <= 10 s variant is
    2.6 `P_age10` (no verdict change). PASS.
12. **p99.5 table identity** - `TWAP_MARGIN_P995` == `r1_tables()['P995']` asserted True. PASS.
13. **Post-08-27 split** - 2.2: k 45/30 survive (p 0.0076 / 0.0004, 11 days); k 58/20/10 fail; no leakage claim.
14. **Flexible recal** - 2.4: vs isotonic the k 45/30 advantage is not significant (p 0.29 / 0.65); vs spline it
    is (p 0.0004 / 0.0024). Material shrinkage -> part of the claim is "beats an under-fit book".
15. **Contested vs confident** - 2.3: contested subset shows no advantage at any k (p >= 0.16). FAIL for the claim.
16. **k = 58 stands in for 60** - stated; k = 59 added (2.1): diff -0.0004, p 0.48.
17. **Ask size** - 3.3: 14.3 % of size-known fires have < 15 sh; 21.5 % of size-known trades exceed depth;
    depth-OK subset (N 62) fails (i)/(v)/(vi).
18. **Freshness at t and t+L** - 3.4: 0 of 326 dropped.
19. **Label source** - `window_labels.resolved_up` (paper + live DB), asserted equal for all 7,018 windows; 0 exact
    ties in the corpus. PASS.
20. **e_min 0.05 locked before 0.03/0.08/L 0.70** - timestamps in 7; no bar adjusted afterwards (the post-lock
    splits in 3.2 are descriptive). PASS.
21. **OOS edge for buckets** - the bucket edge is P_M from the (day, bucket) model trained on days < day, evaluated
    at the fire tick; no refit. PASS.
22. **Effect size at k >= 25** - k 58: -0.0007 (-0.44 %), p 0.19, 15/23 days vs D4 -0.0135; not a marginal
    under-powered miss (19x smaller; sign reversed at 20/10); 09-08 extension impossible locally (no recordings);
    the dead-book diagnostic (2.7) accounts for D4.
23. **Exact-k class** - 2.5: k 58 flips to pass (p 0.023, -0.6 %); 45/30 pass in both classes; 20/10 fail in both.
    The k >= 25 decision is class-dependent only at k = 58 and sub-percent in either class.
24. **Scope** - stated in section 0: nothing here licenses loosening the ladder's r >= 0.6 lock (T2's question).

## 5. Bar wording vs bar applied (PM8)

| pre-registration text | applied | deviation |
|---|---|---|
| B1: "M's OOS log-loss < recalibrated book's at k in {58,45,30,20,10}, day-block p < 0.05, >= 15 OOS days" | per k: diff < 0 AND two-sided day-block p < 0.05 AND >= 15 OOS days; recal = logistic on logit(book_p) fitted on the same training rows | none; two-sided p is the stricter reading (one-sided also reported, no verdict differs) |
| "Fitted per k-bucket {[45,60],[25,45),[15,25),[6,15)} walk-forward" | fitted on tick-level rows of earlier ET days per bucket; first 3 ET days train-only | none |
| "dead-book windows excluded; liveness filter (>= 1 raw report in the trailing 3 s)" | CSV excluded; live3 on the book_p token; Chainlink raw liveness is implied by the projection's 3 s spot-stale gate (asserted: max spot age 3.0 s) | none (both readings satisfied) |
| B2: "fire on the first tick where P_M(side) - ask(side) - fee >= e_min ... lands at t + L ... FOK fills iff ask(t+L) <= ask(t)" | as written; price = ask(t+L) (the ask(t)-priced variant reported: -0.57 c) | price not specified in the text; the matched-price convention used, sensitivity reported |
| "(i) EW >= +2 c/sh on >= 100 OOS trades over >= 15 OOS days" | EW = equal-weight mean of c/sh over filled trades; days = days with >= 1 filled trade | none |
| "(ii) net c/sh non-decreasing across model-edge buckets {<0 (control),[0,3),[3,6),[6,10),>=10 c}" | partition = rule at e_min = 0 bucketed by OOS edge at the fire tick + the <0 arm (first tick with edge < 0); own-arm buckets of the 0.05 trades also reported | the text leaves the partition implicit; the only partition that populates all five buckets was used, and both readings fail |
| "(iii) the <0 control bucket <= 0" | EW of the <0 arm | none |
| "(iv) ANTI <= 0" | other side's token at the same fire tick, same FOK rule | none |
| "(v) day-block p(EW > 0) < 0.05" | one-sided p(EW <= 0) from B = 5,000 day-block resamples | none |
| "(vi) both halves positive in dollars at $15 notional" | H1 = OOS days <= 08-25, H2 = later (Phase-1 boundary); $ = 15/price shares x (payout - price - fee) | boundary choice stated |
| "contested (book P of the chosen side in [0.5, 0.9]) vs confident" | as written; confident further split by P > 0.9 / P < 0.5 (descriptive) | none |
| "fraction of fires where the 1 Hz top-of-book ask size < 15 shares" | 14.3 % of size-known fires (140 of 628 have a size; 08-22..09-01 only) | coverage limit of window_paths stated |
| "Kill: B1 fails at k >= 25, or B2 fails (i)-(iv)" | B1 fails at k = 58 (bucket M); B2 fails (ii) and (v) | none |

No threshold was lowered or moved; no cell was selected after the fact.

## 6. Gaps and what was not done

- window_paths (sizes, book ages) ends 09-01 18:42 UTC: the size analysis covers 8 of 23 OOS days; the micro tape has no sizes.
- No 09-08 recordings exist locally; the corpus ends 09-07 (23 OOS days).
- FOK fill mechanics are inferred from BBO prices only: a fill is assumed whenever the resting ask at t+L is <= the limit; queue/size at that ask is unmodeled (PM17 shows 21.5 % of size-known trades exceed top-of-book depth).
- The fee is the deployed 0.07*p*(1-p); no maker rebate, no slippage beyond the top level.
- The bucket model's training rows are autocorrelated ticks; this affects only the fit, not any reported N or p.
- Isotonic baseline predictions are clipped to [1e-3, 1-1e-3]; a different clip moves its log-loss slightly (spline given as the smooth alternative).
- Ties: 0 exact final == strike in the corpus, so the tie->Up rule was never exercised.
- The 100 windows of 08-14 labeled only in `polybot_live.db` have no micro book records (Phase-1 `prep_micro` used paper-DB labels); they drop out via undefined book_p in both Phase 1 and here.

## 7. Structured verdict

B1: k 58 FAIL, k 45 PASS, k 30 PASS, k 20 FAIL, k 10 FAIL -> bar FAIL as written (and kill at k >= 25 met at k = 58
under the pre-registered model class). B2 pre-declared cell: (i) PASS, (ii) FAIL, (iii) PASS, (iv) PASS, (v) FAIL,
(vi) PASS -> FAIL. Disposition per the pre-registration: "real but not tradable as a taker"; the lock-gated ladder
stays the only zone instrument. Phase-1 D4's log-loss result is superseded: it was the 253 placeholder-book windows.

## 8. Artifacts (this directory)

Scripts: `p0_prep.py` (labels, dead detectors, projections, book states), `t1lib.py` (models, walk-forward, bootstrap),
`p1_b1.py` (B1 incl. `diag` mode), `p2_b2.py` (B2). Log: `cmdlog.txt`, `p1.log`, `p2.log`.
Data: `wins.parquet`, `b1_samples.parquet`, `ticks_book.parquet`, `models_P_plain.pkl`, `b1_models_P_plain.json` (walk-forward log + coefficients),
`p0_checks.json` (PM1/PM2/PM12/PM19 evidence).
B1: `b1_results.csv` (all variants x subsets x comparisons), `b1_results_diag.csv` (dead-book re-inclusion), `b1_preds_*.parquet` (per-window OOS predictions).
B2: `b2_lock_predeclared.json` (pre-declared cell, controls, bars, timestamp), `b2_context_grid.json`,
`b2_trades_e05_L042.parquet` (per-trade table: ep, et_day, k, side_up, P_M, book_p, ask_t, ask_tL, filled, price, fee, payout, cps, usd, edge_bucket, pop, anti_*, fresh, size fields),
`b2_control_lt0_L042.parquet`, `b2_sweep_e0_L042.parquet`, `b2_trades_e{003,008}_L042.parquet`, `b2_trades_e{003,005,008}_L070.parquet`.
