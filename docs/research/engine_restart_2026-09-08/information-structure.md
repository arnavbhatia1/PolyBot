# information-structure: where prediction information exists across the window (60 s era)

Scout slice of the engine-restart program. Everything below is computed locally from the pulled
recordings; nothing touched the VPS or the bot. All scripts and caches live in this directory.

## 0. Corpus, conventions, and what was re-implemented

**Corpus.** `scripts/research/data/win_streams.jsonl.gz` filtered to `ep >= 1786665600`
(2026-08-14 00:00 UTC): **7,018 labeled windows** over **26 ET days 08-13..09-07** (08-13 is the
4-hour tail of the first UTC day, N=48; 08-17 and 08-21 are partial, N=158/260; the other days
N=282-288). `strike`/`final`/`up` come from `window_labels.price_to_beat/final_price/resolved_up`
(built by `scripts/research/ws1_reduce.py`). 6,811 of 7,018 windows (97.1%) carry the Binance
relay stream (`bz`); the bridge falls back to 0 where it is absent, exactly as the engine does.
Tape = `polybot/memory/recordings/tape_*.jsonl.gz` 08-14..09-07 (08-20 read from the uncompressed
`tape_2026-08-20.jsonl`, which was never gzipped; the uncompressed `micro/tape_2026-08-18.jsonl`
are partial leftovers - the `.gz` files were used). Micro tapes 08-14..09-07 likewise
(`micro_2026-08-20.jsonl` uncompressed, 2.2 GB, full day).

**Projection.** Re-implemented in `common.py` (`Window.proj`) exactly as
`polybot/feeds/chainlink_feed.py:210-247` (`projected_final_twap`) + `:150-177` (`running_avg`,
rx-clock zero-order-hold, anchor = last report at/before `close-60` or the first within 2 s) +
`:179-208` (`spot_bridge_delta`: Binance delta since the raw report payload ts, ring 10 s,
anchor age <= 2 s, |delta| <= 1% of anchor) with the 10 s raw-gap coverage guard (boundary-
inclusive) and the 3 s spot-stale refusal. `w = (t - (close-60))/60`, `spot` = latest raw price
at/before `t`. Margin table = `TWAP_MARGIN_P995` imported from `polybot/core/signal_engine.py:34-39`
through `twap_margin` (`:60-66`), so `r(k) = |proj - strike| / p99.5(k)` uses the deployed knots
(verified identical to `ws2_ladder_replay.r1_tables()["P995"]`). Sign convention: `proj - strike
>= 0` reads Up, as `main.py:1040`. "Cold" = projection None (spot stale, raw hole, cold ring):
2.65% of windows at k=58 rising to 4.59% at k=3 (N=7,018 per k).

**Sanity check of the re-implementation** (`cache/surf_samples.parquet`, bridged, |proj - final|):
k=3 median $0.11 / p99.5 $2.14 (table $3.0), k=6 $0.14 / $3.17 ($4.0), k=10 $0.26 / $5.00 ($7.5),
k=15 $0.52 / $10.0 ($12.5), k=20 $0.94 / $16.2 ($20.0), k=25 $1.37 / $23.4 ($28.5), k=58 $7.26 /
$95.7 ($107.5); N=6,696..6,832 per k. Every 26-day p99.5 sits under the deployed knot, and the
medians match CLAUDE.md "$0.11-0.18" - the reimplementation reproduces the engine estimator.

**Clocks.** `k = close - t` on OUR receipt clock everywhere (tape `ts` is `time.time()` at
receipt, `clob_ws.py:357`; micro `l.rx`, `b.ts` likewise). Exchange `ets` sits 19-172 ms before
receipt (p5..p95, 09-07). The bot decision clock is the raw-report tick (~1 Hz); tick-level
quantities below are evaluated at those ticks (`cache/ticks.parquet`, 391,602 ticks, 0 < k <= 60).

**Day split.** ET day = UTC - 4 h. "first13" = 08-13..08-25 (13 ET days), "rest" = 08-26..09-07
(13 ET days).

**Wilson** 95% intervals in every CSV (`wilson_lo/hi`).

Files (all in this directory): `d1_surface.csv`, `d1_surface_usd.csv`, `d2_outside_surface.csv`,
`d2_outside_summary.csv`, `d3_money_cells.csv`, `d3_blind_winrate.csv`, `d3_summary.csv`,
`d3_coverage.csv`, `d3_coverage_vs_dataapi.csv`, `d3_flipfill_windows.csv`, `d3_arm_events.csv`,
`d3_rearm_summary.csv`, `d4_logloss.csv`, `d4_disagree.csv`, `d4_disagree_by_r.csv`,
`d5_timing_quantiles.csv`, `d5_timing_hist.csv`, `d5_first_deep_per_window.csv`.
Caches (`cache/`): `surf_samples.parquet`, `ticks.parquet`, `windows.parquet`, `deep_prints.parquet`,
`outside_samples.parquet`, per-day `bbo_*.parquet` (BBO state sampled at integer k 90..-10),
`cl_l_*.parquet` (every raw Chainlink report), `prints_*.parquet` (every print of a labeled
window's two tokens, k in [-125, 305]).

---

## 1. Calibration surface P(win | r, k) inside the zone  (`d1_surface.csv`, `d1_surface_usd.csv`)

One sample per (window, k); bridged projection (the ladder's); `variant=plain` also in the CSV.

P(win) for the projection sign, all 26 days (N in the second table):

| k | [0,0.1) | [0.1,0.2) | [0.2,0.3) | [0.3,0.45) | [0.45,0.6) | [0.6,0.8) | [0.8,1.0) | [1.0,1.5) | >=1.5 |
|---|---|---|---|---|---|---|---|---|---|
| 3  | 0.843 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 6  | 0.882 | 0.987 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 10 | 0.867 | 0.973 | 0.983 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 15 | 0.860 | 0.964 | 0.988 | 0.996 | 0.995 | 0.993 | 1.000 | 1.000 | 1.000 |
| 20 | 0.851 | 0.956 | 0.983 | 0.994 | 0.994 | 0.995 | 0.997 | 1.000 | 1.000 |
| 25 | 0.850 | 0.932 | 0.978 | 0.991 | 0.995 | 0.998 | 0.996 | 1.000 | 1.000 |
| 30 | 0.818 | 0.945 | 0.973 | 0.995 | 1.000 | 0.997 | 0.996 | 1.000 | 1.000 |
| 35 | 0.825 | 0.921 | 0.983 | 0.994 | 0.997 | 0.997 | 1.000 | 1.000 | 1.000 |
| 40 | 0.812 | 0.911 | 0.982 | 0.986 | 0.997 | 0.997 | 1.000 | 1.000 | 1.000 |
| 45 | 0.792 | 0.935 | 0.968 | 0.987 | 0.995 | 0.998 | 0.996 | 1.000 | 1.000 |
| 50 | 0.783 | 0.933 | 0.968 | 0.991 | 0.991 | 0.997 | 1.000 | 1.000 | 1.000 |
| 58 | 0.774 | 0.923 | 0.945 | 0.979 | 0.994 | 0.998 | 1.000 | 1.000 | 1.000 |

N per cell:

| k | [0,0.1) | [0.1,0.2) | [0.2,0.3) | [0.3,0.45) | [0.45,0.6) | [0.6,0.8) | [0.8,1.0) | [1.0,1.5) | >=1.5 |
|---|---|---|---|---|---|---|---|---|---|
| 3  | 51 | 60 | 50 | 69 | 63 | 94 | 77 | 195 | 6037 |
| 6  | 68 | 75 | 75 | 75 | 92 | 102 | 116 | 216 | 5894 |
| 10 | 143 | 113 | 115 | 134 | 158 | 156 | 161 | 432 | 5305 |
| 15 | 215 | 193 | 160 | 231 | 195 | 280 | 271 | 602 | 4583 |
| 20 | 336 | 273 | 229 | 321 | 321 | 400 | 358 | 817 | 3687 |
| 25 | 452 | 352 | 323 | 431 | 428 | 506 | 449 | 910 | 2904 |
| 30 | 595 | 415 | 409 | 569 | 521 | 588 | 507 | 945 | 2223 |
| 35 | 714 | 534 | 517 | 658 | 575 | 652 | 514 | 892 | 1730 |
| 40 | 797 | 640 | 595 | 766 | 596 | 660 | 532 | 833 | 1378 |
| 45 | 937 | 790 | 632 | 817 | 651 | 663 | 489 | 789 | 1041 |
| 50 | 1174 | 929 | 776 | 877 | 693 | 607 | 465 | 620 | 678 |
| 58 | 1423 | 1061 | 841 | 941 | 652 | 587 | 378 | 510 | 439 |

Readings (measured):
- The lock frontier is far inside the certain region: at k=25, r in [0.6,0.8) wins 0.998 (N=506,
  Wilson 0.989-1.000); r in [0.45,0.6) 0.995 (N=428); [0.3,0.45) 0.991 (N=431); even [0.2,0.3)
  0.978 (N=323). In r units the surface is close to k-invariant for r >= 0.3 (>= 0.979 everywhere,
  N >= 69).
- Information at low r: at k=58, r < 0.1 (|disp| < $10.75) the sign is still right 0.774
  (N=1,423, Wilson 0.752-0.795); r in [0.1,0.2) 0.923 (N=1,061). The margin table is a
  p99.5 bound; the median error is 7-15x smaller, so most of the k>25 population is decided
  long before it clears 0.6 x p99.5.
- Stability: every cell with r >= 0.45 is >= 0.983 in BOTH halves (first13 / rest; see
  `d1_surface.csv split=first13|rest`); e.g. k=25 [0.6,0.8): 0.996 (N=231) vs 1.000 (N=275).
  The low-r cells move by 1-3 pp between halves (in the CSV).
- Dollar view (`d1_surface_usd.csv`, bridged): |disp| >= $32 wins >= 0.993 at every k (N=3,471..
  3,569); $16-32: 0.949 at k=58 -> 0.999 at k=20; $8-16: 0.878 (58) -> 0.995 (20) -> 1.000 (<=10);
  $4-8: 0.792 (58) -> 0.987 (20) -> 1.000 (<=6); $0-1: 0.656 (58) -> 0.796 (20) -> 0.956 (3).
- Plain vs bridged: plain errors are 3-10% wider at p99.5 (table above); the plain surface is in
  the CSV (`variant=plain`).

---

## 2. Outside the zone, k in {300,240,180,120,90,60}  (`d2_outside_surface.csv`, `d2_outside_summary.csv`)

Definitions (stated): `spot(t)` = latest raw Chainlink report with rx <= t, refused if older than
3 s; `disp = spot - strike`; **rv60(t) = sqrt(sum of squared consecutive raw-report price
differences over the reports with rx in (t-60, t])**, i.e. the trailing-60 s realized dollar
volatility of the raw stream (the std of a 60 s random-walk move); requires >= 20 reports in the
span (else cold); `z = disp / rv60`. Raw stream from the micro tapes: 1,962,199 reports
08-14..09-07. Cold: 229-256 windows per k (3.4-3.8%).

Summary (N = non-cold windows):

| k | N | accuracy of sign(disp) | rv60 median $ | median abs disp $ | median abs z |
|---|---|---|---|---|---|
| 300 (open) | 6,780 | 0.569 | 14.4 | 7.05 | 0.52 |
| 240 | 6,762 | 0.676 | 16.6 | 18.0 | 1.24 |
| 180 | 6,789 | 0.747 | 15.9 | 25.2 | 1.78 |
| 120 | 6,771 | 0.822 | 15.8 | 28.9 | 2.10 |
| 90 | 6,789 | 0.865 | 15.4 | 31.5 | 2.36 |
| 60 | 6,778 | 0.922 | 15.3 | 33.0 | 2.49 |

P(win | |z| bin, k) with N:

| k | [0,0.25) | [0.25,0.5) | [0.5,1) | [1,1.5) | [1.5,2) | [2,3) | >=3 |
|---|---|---|---|---|---|---|---|
| 60  | 0.600 (395) | 0.751 (370) | 0.809 (712) | 0.909 (729) | 0.944 (590) | 0.984 (1130) | 0.991 (2852) |
| 90  | 0.507 (428) | 0.602 (400) | 0.749 (762) | 0.829 (671) | 0.890 (699) | 0.931 (1138) | 0.968 (2691) |
| 120 | 0.520 (458) | 0.598 (413) | 0.690 (869) | 0.766 (805) | 0.837 (724) | 0.906 (1133) | 0.941 (2369) |
| 180 | 0.515 (538) | 0.596 (490) | 0.633 (971) | 0.692 (922) | 0.754 (829) | 0.826 (1320) | 0.892 (1719) |
| 240 | 0.527 (782) | 0.582 (720) | 0.628 (1294) | 0.680 (1137) | 0.717 (993) | 0.768 (1124) | 0.812 (712) |
| 300 | 0.514 (1861) | 0.531 (1422) | 0.588 (2022) | 0.627 (1030) | 0.676 (373) | 0.778 (72) | - (0) |

Dollar bins are in the CSV (`variant=usd`): e.g. |disp| >= $32 wins 0.990 at k=60 (N=3,471),
0.848 at k=180 (N=2,818), 0.689 at k=300 (N=589).

Readings: information accrues roughly linearly in accuracy from 57% at the open to 92% at the
zone edge; the |z| >= 3 cells are stable across halves (k=60: 0.994/0.989; k=180: 0.898/0.888;
k=240: 0.797/0.822). At the open, `spot - strike` is the last-30 s momentum against the TWAP and
is right 56.9% (Wilson 55.7-58.1%) - a real but small signal that prior work (09-01 program)
found already in the book. NOT tested here: whether the book at k in [60,300] already prices these
z cells (deliverable 4 covers k <= 58 only).

---

## 3. Where the money is  (`d3_money_cells.csv`, `d3_blind_winrate.csv`, `d3_summary.csv`, coverage files)

Population: every tape print of a labeled window's tokens with price <= 0.80 and k in [-60, 60]:
**639,358 prints in 6,500 windows** (winner token 238,804; loser token 400,554). Attribution:
`r_signed` = displacement toward the PRINTED token / p99.5(k), bridged projection at the print
receipt time (0 < k <= 60). For k <= 0 the engine projection is undefined; those prints carry
the projection at the close instant (margin clamped at the k=2 knot, $2.5) and a `pc_certain`
flag (final sixty-report already received, rx from `boundaries.json`). k bins: [45,60] [25,45)
[15,25) [6,15) [3,6) (0,3) [-5,0] [-20,-5) [-60,-20). Value ceded = sum size x (1 - price).

**Engine-true reachability** (`reachable`): the ladder state machine replayed on the raw ticks -
arm when idle and 6 <= k <= 25 and |r| >= 0.6 (effective +56 ms), cancel when resting and (cold or
r toward the resting side < 0.6) (effective +54 ms), a ladder resting at the close holds to
close + 60 s (the `certain_winner` fail-closed at close+5 s is NOT replayed). Re-arming after a
cancel is allowed, which is what the deployed code does (`maker_bid.py:331` sets `active = None`
after `_retire`; `main.py:1048-1055` only refuses when the window already has a position).

### 3a. Totals (26 ET days)

| metric | prints | shares | $ ceded | share of winner $ | $/day |
|---|---|---|---|---|---|
| winner-token prints <= 0.80, k in [-60,60] | 238,804 | 5,258,155 | 2,459,669 | 1.000 | 94,603 |
| of which pre-close (k > 0) | 238,611 | 5,246,087 | 2,452,114 | 0.997 | 94,312 |
| pre-close in lock-FORBIDDEN cells (k > 25 or r < 0.6 or cold) | 238,119 | 5,210,713 | 2,435,149 | **0.990** | 93,660 |
| pre-close in lock-ALLOWED cells (k <= 25 and r >= 0.6) | 492 | 35,373 | 16,965 | 0.0069 | 653 |
| post-close (k <= 0), tape-recorded | 193 | 12,068 | 7,554 | 0.0031 | 291 |
| post-close with the final capture already received | 146 | 9,611 | 6,648 | 0.0027 | 256 |
| engine-true reachable (ladder resting on the winner at print time) | 543 | 34,217 | 16,402 | **0.0067** | 631 |
| taker-SELL prints only (the flow that fills a resting bid) | 21,932 | 624,716 | 302,375 | 0.123 | 11,630 |
| loser-token prints <= 0.80, k in [-60,60] | 400,554 | 30,523,690 | 27,973,850 | - | 1,075,917 |
| loser prints engine-true reachable (resting on the LOSER = flip-fill exposure) | 135 | 3,646 | 1,932 | - | 74 |

The "$94.6k/day" headline is dominated by k in [25,60] prints at 0.5-0.8 in contested windows -
fair-priced two-sided trading, not panic. Restricting to prints <= 0.50 leaves $1.44M (N=66,138,
777 windows) and to taker-SELL prints <= 0.80 in k in [0,25] leaves the flow the r7/r11 census
measured. The cell table separates these.

### 3b. Winner-token $ ceded by (k bin, r toward winner)

| k bin | anti(<0) | [0,0.1) | [0.1,0.2) | [0.2,0.3) | [0.3,0.45) | [0.45,0.6) | [0.6,0.8) | [0.8,1.0) | [1.0,1.5) | >=1.5 | cold |
|---|---|---|---|---|---|---|---|---|---|---|---|
| [45,60] | 716,075 | 403,239 | 100,972 | 26,498 | 12,713 | 1,672 | 739 | 239 | 51 | - | 21,830 |
| [25,45) | 439,223 | 262,545 | 73,233 | 20,949 | 10,081 | 2,290 | 1,981 | 75 | 10 | - | 26,126 |
| [15,25) | 115,787 | 55,913 | 22,487 | 6,668 | 2,408 | 874 | 970 | 157 | 7 | 9,353 | 6,647 |
| [6,15) | 30,972 | 31,077 | 7,236 | 5,900 | 3,259 | 1,284 | 724 | 39 | 110 | 351 | 3,315 |
| [3,6) | 3,639 | 4,292 | 3,482 | 133 | 331 | 131 | 1,394 | 32 | 43 | - | 283 |
| (0,3) | 3,664 | 4,590 | 230 | - | 0 | - | 73 | 1,557 | 3 | 2,151 | 3 |
| [-5,0] | 20 | 32 | - | - | - | - | - | - | 5 | 32 | 43 |
| [-20,-5) | - | - | - | - | - | - | - | - | - | - | 6,445 |
| [-60,-20) | - | - | - | - | - | - | - | - | - | - | 978 |

(Post-close rows read "cold" when the close-instant projection was None; the post-close
attribution is informational, see coverage below.) Per-cell N prints, N windows, shares,
taker-SELL split, <= 0.50 / <= 0.20 sub-bands, and reachable shares are all in `d3_money_cells.csv`.

**Reading**: 47% of winner $ is ceded while the projection points at the LOSER (`anti`, $1.31M),
another 31% while r < 0.1. Only $16,965 (0.69%) sits in the cells the current lock allows, and
the engine-true replay reaches $16,402 (0.67%) - the "~98% unreachable" claim is confirmed and
sharpened: **99.3% of winner-side deep value is outside the lock cells, and 98.6% of it prints
while the projection is either wrong-signed or inside r < 0.2.**

### 3c. Loser-token traps, same cells (r toward the LOSER)

| k bin | anti(<0) | [0,0.1) | [0.1,0.2) | [0.2,0.3) | [0.3,0.45) | [0.45,0.6) | [0.6,0.8) | [0.8,1.0) | [1.0,1.5) | cold |
|---|---|---|---|---|---|---|---|---|---|---|
| [45,60] | 7,738,924 | 345,147 | 51,878 | 9,947 | 3,591 | 993 | 51 | - | - | 208,317 |
| [25,45) | 9,007,399 | 190,905 | 40,390 | 7,358 | 3,237 | 274 | 13 | - | - | 310,512 |
| [15,25) | 3,978,194 | 48,598 | 10,602 | 1,832 | 1,142 | 682 | 1,228 | 11 | 336 | 145,724 |
| [6,15) | 3,151,750 | 25,922 | 6,487 | 4,488 | 288 | 134 | 293 | 25 | - | 102,546 |
| [3,6) | 1,091,763 | 2,895 | 508 | - | - | - | - | - | - | 39,090 |
| (0,3) | 1,049,168 | 10,528 | 1,002 | - | - | - | - | - | - | 51,854 |
| [-5,0] | 293,619 | 1,358 | 99 | - | - | - | - | - | - | 5,572 |

### 3d. Blind-bidder win rate by shares (prints on the PROJECTION-side token, r_signed >= 0)

`winner_shares / (winner + loser shares)` per cell; N windows (winner / loser) in parentheses.

| k bin | [0,0.1) | [0.1,0.2) | [0.2,0.3) | [0.3,0.45) | [0.45,0.6) | [0.6,0.8) | [0.8,1.0) | [1.0,1.5) | >=1.5 |
|---|---|---|---|---|---|---|---|---|---|
| [45,60] | 0.60 (1027/603) | 0.69 (547/217) | 0.75 (240/79) | 0.80 (106/32) | 0.68 (28/5) | 0.93 (9/1) | 1.00 (5/0) | 1.00 (3/0) | - |
| [25,45) | 0.64 (635/370) | 0.70 (364/136) | 0.75 (170/47) | 0.79 (101/22) | 0.89 (25/7) | 0.99 (15/1) | 1.00 (5/0) | 1.00 (2/0) | - |
| [15,25) | 0.62 (208/121) | 0.74 (128/39) | 0.85 (61/14) | 0.77 (36/6) | 0.66 (12/3) | 0.67 (6/3) | 0.96 (2/1) | 0.04 (1/1) | 1.00 (1/0) |
| [6,15) | 0.61 (90/56) | 0.64 (59/15) | 0.68 (36/11) | 0.92 (26/4) | 0.91 (12/1) | 0.78 (2/2) | 0.70 (1/1) | 1.00 (1/0) | 1.00 (1/0) |
| [3,6) | 0.66 (21/10) | 0.89 (10/1) | 1.00 (4/0) | 1.00 (4/0) | 1.00 (2/0) | 1.00 (2/0) | 1.00 (1/0) | 1.00 (1/0) | - |
| (0,3) | 0.45 (12/7) | 0.47 (6/2) | - | 1.00 (1/0) | - | 1.00 (1/0) | 1.00 (1/0) | 1.00 (1/0) | 1.00 (2/0) |

**This is the central structural fact for a probabilistic engine**: the per-window surface in
section 1 says the sign is right 99.5% at (k in [15,25), r in [0.6,0.8)), but CONDITIONAL ON A
DEEP PRINT ON THAT TOKEN the share-weighted win rate in the same cell is 0.67 (N = 6 winner
windows / 3 loser windows). Deep prints on the favored token happen precisely in the windows
where the projection is about to be wrong - adverse selection of the fill, not miscalibration of
the sign. Cell Ns are tiny (1-15 windows at r >= 0.45, k <= 25), so these rates are
descriptive; the direction is consistent everywhere: fill-conditioned win rate << unconditional
P(win) at the same r.

### 3e. Re-arms are where the flip-fill exposure lives (`d3_rearm_summary.csv`, `d3_arm_events.csv`, `d3_flipfill_windows.csv`)

| arm index in window | N arms | side == winner | median k at arm | winner $ reachable | loser $ reachable | windows with winner fills / loser fills |
|---|---|---|---|---|---|---|
| first arm | 6,358 | 0.9995 | 24.3 | 15,825 | 0 | 17 / 0 |
| second arm (after a floor cancel) | 78 | 0.9615 | 16.1 | 577 | 591 | 3 / 1 |
| third arm | 11 | 0.727 | 13.0 | 0 | 1,341 | 0 / 3 |
| fourth | 3 | 1.000 | 8.5 | 0 | 0 | 0 / 0 |

3 windows carry loser-side reachable prints, all during re-arms: ep 1787271900 (08-20, armed Up
at k=24.7 on disp +$25.5, floor-cancelled at k=19.4, re-armed at k=18.1, Up printed 0.60-0.75 at
k=13.9 while r toward Up was still 0.78, final -$6.65), ep 1787358600 (08-21, re-armed Up at
k=16.6, Up printed 0.22-0.67 at k=18.0, final -$2.3), ep 1787620800 (08-24, third arm, k=12.4-13.0).
`scripts/research/ws2_ladder_replay.py:262-292` arms ONCE per window and never re-arms, so the
r19/r24/r27 "0 flip-fills at k_max 25" statements hold for the replay convention, not for the
deployed code path. Unverified: no paper/live log of an actual re-arm was inspected here; the
claim rests on reading `maker_bid.py:298-331` + `main.py:1040-1075`.

### 3f. Coverage caveat: the tape under-records post-close prints  (`d3_coverage.csv`, `d3_coverage_vs_dataapi.csv`)

Tape prints in the first 5 s post-close vs the final 5 s pre-close: ratio 0.18-0.68 by UTC day
(median 0.36; e.g. 09-07: 134 vs 615 prints in 289 windows). Against the data-api pulls
(`vps-0831/r6_pm_trades`, 91 windows 08-28..31; `r12_pm_trades`, 51 windows with tape overlap;
the API lists each fill on both sides, 2.9 rows per transactionHash, so only RELATIVE ratios are
meaningful): tape shares / API shares = 0.506 (r6) and 0.447 (r12) in the final 25 s pre-close;
0.119 / 0.037 in post[0,5); 0.096 / 0.185 in post[5,60); 0.000 in post[60,120). Relative to the
pre-close ratio, **the tape captures roughly 8-24% of post-close volume in the first 5 s and
19-41% in [5,60) s, and nothing after 60 s** (the WS subscription moves to the next window).
Every post-close number in this report (and the paper trader post-close fills, which come from
the same WS callback) is a lower bound by a factor of ~3-10.

---

## 4. Book vs projection in the zone, walk-forward  (`d4_logloss.csv`, `d4_disagree.csv`, `d4_disagree_by_r.csv`)

Book: micro `b` records (every BBO change, final 90 s only) replayed to the state at t = close - k;
`book_p` = mid of the Up token clamped to [0.01, 0.99] (Down-side complement if Up missing);
1.0-1.3% of windows have no book at these k. **k=58 stands in for k=60**: the engine projection
is undefined at exactly k=60 (`t <= close-60` returns None). Models fitted on strictly earlier ET
days (first 3 ET days train only; evaluated on 23 ET days, N=6,072..6,172 windows per k):
`book` raw; `book_recal` logistic on logit(book_p) (control); `proj_cell` = section-1 cell table
with Laplace smoothing, signed by the projection; `proj_logit` logistic on signed r; `combo`
logistic on [logit(book_p), signed r]. Day-block bootstrap B=5,000 on per-day mean log-loss
differences, two-sided p.

| k | book | book_recal | proj_cell | proj_logit | **combo** | combo - book (p) | combo - book_recal (p) | days combo < book_recal (of 23) |
|---|---|---|---|---|---|---|---|---|
| 58 | 0.1855 | 0.1846 | 0.2029 | 0.2101 | **0.1711** | -0.0144 (0.0012) | -0.0135 (0.0016) | 16 |
| 45 | 0.1268 | 0.1241 | 0.1277 | 0.1352 | **0.1059** | -0.0208 (0.0002) | -0.0182 (0.0004) | 17 |
| 30 | 0.0831 | 0.0791 | 0.0709 | 0.0756 | **0.0578** | -0.0253 (0.0002) | -0.0213 (0.0004) | 17 |
| 20 | 0.0617 | 0.0563 | 0.0384 | 0.0411 | **0.0304** | -0.0313 (0.0002) | -0.0259 (0.0012) | 17 |
| 10 | 0.0398 | 0.0308 | 0.0122 | 0.0109 | **0.0062** | -0.0336 (0.0002) | -0.0247 (0.0002) | 14 |

Brier follows the same order (combo 0.0502 -> 0.0017 from k=58 to k=10; book 0.0555 -> 0.0106).
Projection-only vs book: p = 0.08-0.79 at k >= 30 (not better), p <= 0.009 at k <= 20 (better).

Disagreement on the favored side (book mid vs projection sign):

| k | N | disagree | frac | book right | proj right | proj right frac | mean abs r at disagreement | mean abs(book_p - 0.5) |
|---|---|---|---|---|---|---|---|---|
| 58 | 6,142 | 221 | 3.6% | 85 | 136 | 0.615 | 0.22 | 0.067 |
| 45 | 6,124 | 175 | 2.9% | 48 | 127 | 0.726 | 0.42 | 0.074 |
| 30 | 6,090 | 135 | 2.2% | 33 | 102 | 0.756 | 0.94 | 0.049 |
| 20 | 6,085 | 134 | 2.2% | 22 | 112 | 0.836 | 1.87 | 0.051 |
| 10 | 6,046 | 107 | 1.8% | 11 | 96 | 0.897 | 5.27 | 0.044 |

By |r| at k=58: disagreements at r < 0.1 (N=132) go 57 proj / 75 book; at r >= 0.1 (N=89) 79
proj / 10 book. At k=45: r < 0.1 (N=85) 42/43; r >= 0.1 (N=90) 85/5.

**Verdict**: the projection alone does NOT beat the book at k >= 30 (p = 0.08-0.79) - the book is
a better single predictor there - but the projection carries information the book lacks at EVERY
k including 58 and 45: the combination beats the raw book and the recalibrated book at all five k
with p <= 0.0016, improving on 14-17 of 23 days. When they disagree on the side, the projection is
right 62% at k=58 (N=221, Wilson 55-68%) rising to 90% at k=10, and it is almost always right when
the disagreement has r >= 0.1. This is a license for a probabilistic engine in k in [25, 60] -
with the caveat in section 3d that fill-conditioned probabilities differ from window-conditioned
ones, and that these disagreements are 2-4% of windows.

Caveats: the book mid at 1 tick spread is a coarse probability (0.01 resolution near the
extremes, clamped at 0.01/0.99, which is why `book_recal` beats `book` at k <= 45); the logistic
fit is a two-feature linear model, not an engine; log-loss gains are on the whole population, not
on fill-weighted outcomes.

---

## 5. Timing of information vs supply  (`d5_timing_quantiles.csv`, `d5_timing_hist.csv`, `d5_first_deep_per_window.csv`)

gap = t(lock) - t(print), lock = first raw tick with r toward the winner >= 0.6 at k in [6,25]
(the current lock); "never" = the window never locks on the winner in [6,25]. Share-weighted.

| population | prints | windows | shares | $ ceded | lock never | lock AFTER print | lock before print | gap p10/p50/p90 (s) |
|---|---|---|---|---|---|---|---|---|
| winner <= 0.80, k in [-60,60] | 238,804 | 1,646 | 5.26M | 2.46M | 0.363 | 0.629 | 0.008 | 15.0 / 30.8 / 43.1 |
| same, taker-SELL only | 21,932 | 1,493 | 625k | 302k | 0.379 | 0.602 | 0.020 | 14.1 / 29.6 / 42.4 |
| winner <= 0.50, k in [-60,60] | 66,138 | 777 | 2.03M | 1.44M | 0.415 | 0.576 | 0.009 | 16.5 / 32.3 / 44.0 |
| **winner <= 0.80, k in (0,25]** | 29,186 | 350 | 715k | 332k | **0.683** | **0.255** | 0.062 | -4.4 / 9.0 / 15.3 |
| winner <= 0.50, k in (0,25] | 7,090 | 170 | 256k | 187k | 0.683 | 0.249 | 0.068 | -4.2 / 8.7 / 14.7 |
| post-close k in [-60,0] (tape) | 193 | 12 | 12k | 7.6k | 0.990 | 0 | 0.010 | - |

Gap histogram for winner <= 0.80, k in (0,25] (share fraction): never 0.68; [10,15) 0.11;
[5,10) 0.08; [2,5) 0.03; [15,20) 0.04; [0,2) 0.01; lock already resting (gap < 0) 0.06 total.

Alternative locks (in the CSV): relaxing k to any k <= 58 turns "never" 0.683 -> 0.554 and
"after" 0.255 -> 0.378 (the lock still arrives after the print); floor 0.3 in [6,25]: never
0.473, after 0.391, before 0.136; floor 1.0: never 0.842.

First winner print <= 0.50 per window with k in (0,25] (N=170 windows): k of the first deep print
p10/p50/p90 = 16.1 / 24.5 / 25.0 - 141 of 170 fall in (20,25], i.e. the deep flow is already
running when the placement window opens. Extending to k in (0,60] (N=775 windows), 650 first
deep prints are at k in (50,60] and only 125 inside k <= 50: the eventual winner is simply the
priced underdog at the zone edge in most of these windows (|final - strike| median $5.57, p10 $0.54).
For the 170-window k <= 25 population |final - strike| median $1.26 (p25 $0.44, p75 $3.77): sub-$5
gaps cannot clear 0.6 x p99.5(25) = $17.1, so the lock never arrives in 67.1% of them; when it
does, it arrives 3.4-17.4 s after the first deep print (p10-p90; median 14.7 s). This reproduces
and extends RESEARCH.md 09-04 "armed 9-15 s after the deep print" (3 windows there; 56 here).

---

## 6. What this map says to the design phase (inferred from the measurements above)

1. The window-level surface (section 1) is nearly saturated: r >= 0.3 is >= 0.979 everywhere;
   the current lock (0.6) throws away nothing in sign accuracy and everything in supply. But
   the fill-conditioned rate (3d) is the number a rung engine must price from, and it is 0.6-0.9
   in the cells that carry volume - a probabilistic engine cannot use section 1 alone.
2. The projection adds information over the book at all k in [10,58] (section 4, p <= 0.0016),
   so an engine in [25,60] has license; the size of the edge is a log-loss improvement of
   0.013-0.026 over the recalibrated book, concentrated in the 2-4% of windows where they disagree.
3. 99.3% of winner-side deep value prints in cells the lock forbids and 47% while the projection
   points the other way (3b). The reachable design space is "bid on the side the book disfavors
   at k in [25,60] when the combo model disagrees with it" - exactly the population of 3d where
   the blind win rate is 0.6-0.8 by shares.
4. The tape misses 60-90% of post-close volume (3f); any post-close leg must be sized from the
   data-api, not from the tape or from paper fills.
5. Re-arms after a floor cancel are where the deployed code flip-fill exposure sits (3e) and
   the ws2 replay does not model them; reconcile before any replay-based claim of "0 flips".

## 7. Not done / not verified

- Deliverable 4 is at k=58 not 60 (projection undefined at 60); no book-vs-spot test at k > 60.
- The `certain_winner` fail-closed (close + 5 s) is not in the reachability replay; post-close
  reachability is therefore slightly over-stated (and the tape under-records it, see 3f).
- The BBO state at k is the last recorded change <= t; 1.0-1.3% of windows had no change in
  [k=90, k] and were dropped from section 4.
- Cell win rates in 3d with < 10 windows are descriptive only.
- The re-arm behaviour of the deployed code (3e) was established by code reading, not by a
  logged re-arm event.
- Section 2 defines rv60 on the raw stream receipt-clock increments; a payload-clock or
  1 s-kline definition would change z by a scale factor, not the ordering.
- No wallet attribution, no data-api pulls, no external feeds were used in this slice.
