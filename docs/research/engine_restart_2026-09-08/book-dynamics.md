# Book dynamics through the window — the counterparty's side of the engine question

Scout: `book-dynamics`. Date: 2026-09-08. Repo read-only; all outputs under
`C:/Users/abhat/AppData/Local/Temp/claude/C--Users-abhat-Personal-PolyBot/dc8ad5cd-5d51-4fc7-920c-a96d4dbabc90/scratchpad/engine_restart/book-dynamics/`.
Every number below is **measured** on the data named unless marked *computed* (a fit) or *inferred*.

## 0. Data actually used (N)

| source | what | N |
|---|---|---|
| `scripts/research/data/vps-0831/window_paths.db` (the widest copy; preferred over vps-0821's 2,088-window file) | 1 Hz to elapsed ~255 s, then 5 Hz; BBO, top-3 depth, touch size, book age, Chainlink price | era rows (ts >= 1786665600, btc-updown-5m): **2,495,326 rows / 5,281 windows**, 2026-08-14 00:00 -> 2026-09-01 18:42 UTC; labeled: 2,430,192 rows / **5,140 windows** |
| `polybot/memory/recordings/micro_2026-08-14..09-07.jsonl(.gz)` | `k="b"` BBO records (final 90 s) for labeled tokens | **267,054,778 records -> 3,017,645 distinct price-state changes** (size-only updates dropped), **6,884 windows**, 25 days |
| `scripts/research/data/win_streams.jsonl.gz` | raw Chainlink stream `l` per window (rx, payload ts, price), Binance relay `bz` | **7,018 era windows** (of 8,726) |
| `scripts/research/data/polybot_paper.db` `window_labels` | resolved_up, price_to_beat (strike), final_price, token ids | 17,220 rows; era labels used for every join |
| projection grid (`proj_grid.csv`) | engine-exact projection at k in {58,55,50,45,40,35,30,27,25,22,20,18,16,15,14,12,10,9,8,7,6,5,4,3,2,1} | 182,468 rows; **175,364 with a valid projection**; **167,533 with a live book state** |

Known data defects handled: (a) **253 "dead-book" windows** on 08-19, 08-26, 08-30/31, 09-01..03 where the CLOB
subscription delivered one placeholder book (0.50/0.51 or 0.10/0.90) and never updated — confirmed in both the
micro tape and window_paths (98.3% agreement on 832 matched ticks; window_paths `book_age_up_s` median 291 s).
Listed in `dead_book_windows.csv`; excluded from every "contested book" statistic. (b) window_paths bid/ask-size
columns are worst-level before the 08-21 fix — touch sizes are reported for ts >= 2026-08-22 only. (c) 08-17 has
163 windows (outage day).

## 1. Conventions

- **Book P(Up)** = mean of the available {mid_up, 1 - mid_down}; log-odds L = logit(P) clipped to [0.005, 0.995]
  (the book cannot quote past 0.99, so L saturates at +/-5.29). "Clean" (deliverable 1) = both tokens two-sided,
  mid_up + mid_down in [0.98, 1.02], both book ages <= 10 s.
- **Elapsed sampling**: nearest window_paths row within +/-0.6 s (1 Hz zone) or +/-0.15 s (5 Hz zone, >= 255 s).
- **Projection**: exact replica of `ChainlinkFeed.running_avg` / `projected_final_twap`
  [polybot/feeds/chainlink_feed.py:143-166, 210-249]: receipt-clock ZOH average over [close-60, t], seed = last
  report at/before the start or first within 2 s, 10 s coverage guard boundary-inclusive, spot stale > 3 s -> None,
  PLAIN (no Binance bridge; bridged also written to `proj_grid.csv`, not analysed). **s = (proj - strike)/p99.5(k)**,
  p99.5 = piecewise-linear `TWAP_MARGIN_P995` [polybot/core/signal_engine.py:34-39], strike = served `price_to_beat`.
- **Micro book state at t** = last BBO price change <= t for that token; **live** = >= 1 raw record in
  [floor(t)-3, floor(t)] (`liveness.parquet`). **Contested** = |L| < 3 (P in (0.047, 0.953)).
- **Dump event** (deliverable 3): a BBO record whose numeric bid is >= 0.10 below max(bid in force at t-1 s, bids in
  (t-1, t)); one event per episode; episode ends at the first bid >= ref - 0.02 (recovery) or at the close (censored);
  depth = ref - trough; NaN bids never trigger. **reprice** flag = complement's ask rose >= 0.05 or own ask fell
  >= 0.05 within +/-1 s (a market move, not a liquidity pull). s_rel = s signed toward the dumped side (s_rel > 0 <=>
  the dumped side is the projected winner).
- k buckets: [1,6) [6,10) [10,15) [15,25) [25,40) [40,59]. Wilson 95% intervals on counts; tick-level N overstates
  independence — window counts are given everywhere and the headline contested edge carries a window-cluster
  bootstrap (2,000 resamples).

## 2. Deliverable 1 — calibration of book P(Up) by elapsed time

Files: `calibration_summary.csv`, `calibration_by_elapsed.csv` (deciles + fixed bins, both filters).
N per elapsed ~ 5,063-5,078 labeled windows; Up base rate 0.498.

| elapsed s | n | Brier | logloss (base 0.693) | cal. slope +/- se (*computed*, logit fit) | frac P extreme (<=0.02 or >=0.98) | frac both tokens two-sided |
|---|---|---|---|---|---|---|
| 5 | 5,063 | 0.237 | 0.667 | 0.93 +/- 0.06 | 0.000 | 0.995 |
| 30 | 5,069 | 0.224 | 0.637 | 0.97 +/- 0.04 | 0.000 | 0.995 |
| 60 | 5,064 | 0.209 | 0.603 | 0.98 +/- 0.04 | 0.000 | 0.996 |
| 120 | 5,068 | 0.167 | 0.500 | 1.04 +/- 0.03 | 0.017 | 0.991 |
| 180 | 5,073 | 0.122 | 0.382 | 1.03 +/- 0.03 | 0.151 | 0.911 |
| 240 | 5,077 | 0.058 | 0.190 | 1.11 +/- 0.04 (clean 1.07 +/- 0.04, n=2,763) | 0.539 | 0.578 |
| 270 | 5,073 | 0.024 | 0.082 | 1.18 +/- 0.05 (clean 1.04 +/- 0.06, n=994) | 0.819 | 0.230 |
| 285 | 5,075 | 0.0135 | 0.046 | 1.38 +/- 0.09 (clean 1.28 +/- 0.13, n=416) | 0.908 | 0.116 |
| 294 | 5,078 | 0.0100 | 0.034 | 1.59 +/- 0.16 | 0.951 | 0.057 |
| 299 | 5,078 | 0.0090 | 0.030 | 1.89 +/- 0.35 | 0.962 | 0.040 |

- Intercepts are -0.005 ... -0.05 (no directional bias). Decile and fixed-bin realized rates sit inside the Wilson
  interval of the bin mean at every elapsed for every bin with n >= 200; the largest deviations are scattered in
  sign (e.g. 180 s bin 0.60-0.70: realized 0.59 [0.53,0.65] vs 0.65, n=284; 240 s bin 0.60-0.70: 0.71 [0.62,0.78]
  vs 0.655, n=129). **The 09-01 "perfectly calibrated" claim reproduces at 5-240 s.**
- Slope > 1 after 240 s is the **0.99 tick cap**, not information: 82-96% of books are at an extreme and resolve
  correctly > 99.9% of the time (see section 3 "book wrong"). Calibration inside the final 30 s is therefore only
  measurable on the 4-12% of windows still contested, where n is 116-416 and slopes are 1.04-1.28 (+/- 0.06-0.41).

## 3. Deliverable 2 — does the book move toward the projection?

Files: `response_samples.csv` (167,533 ticks), `response_regression.csv`, `response_by_sbin.csv`,
`response_disagreement.csv`, `response_outcome_models.csv`, `book_wrong_at_k.csv`,
`response_regression_contested_live.csv`, `contested_honest_cells.csv`, `contested_edge_clusterboot.csv`.

**3a. Saturation first.** With a live book, the projected side's mean P is 0.973-0.990 wherever |s| >= 0.6 at
every k bucket (`response_by_sbin.csv`). The fraction of live books that are "confident" (P >= 0.9 on a side)
is 75.3% at k=58, 90.3% at k=30, 94.9% at k=25, 96.1% at k=6, 98.7% at k=1 (`book_wrong_at_k.csv`, n ~ 6,300-6,640
windows per k). Whenever the lock fires, the book already agrees.

**3b. Full-grid regression dL(2 s) ~ s_clip + L(t)** (window-clustered SE): b_s is -0.010 ... -0.035 and b_L
+0.006 ... +0.015 at every k bucket (t ~ 5-24). These are **not interpretable as "book moves away from the
projection"**: s and L are near-collinear (both saturated on the same side in > 90% of ticks) and L is clipped
at +/-5.29 so the confident side cannot move further. Reported for completeness only; the nonparametric table
is the evidence.

**3c. Nonparametric drift toward the projection** (`response_by_sbin.csv`, live books, 2 s horizon):

| k bucket | s in (0, 0.3]: mean dL (n) | s in (-0.3, 0]: mean dL (n) | s in (0.3, 0.6] (n) | s in (0.6, 1.0] (n) | frac of non-zero moves toward the projection, abs(s) < 0.6 |
|---|---|---|---|---|---|
| [6,10) | +0.457 (472) | -0.386 (504) | +0.096 (417) | +0.056 (513) | 0.73-0.90 |
| [10,15) | +0.222 (624) | -0.221 (650) | +0.102 (515) | +0.002 (576) | 0.73-0.85 |
| [15,25) | +0.186 (1,822) | -0.209 (1,736) | +0.108 (1,320) | +0.012 (1,605) | 0.74-0.82 |
| [25,40) | +0.156 (2,641) | -0.131 (2,661) | +0.076 (1,888) | +0.015 (1,935) | 0.70-0.81 |
| [40,59) | +0.100 (6,500) | -0.115 (6,759) | +0.076 (3,477) | +0.015 (2,474) | 0.68-0.73 |

The book drifts toward the projection's sign only while the projection is *small* (|s| < 0.6); beyond that the
book is already there and the drift is <= 0.015 log-odds per 2 s.

**3d. Contested live books** (|L| < 3, dead-book windows removed): 12,256 ticks / 2,242 windows at |s| < 0.6.
Regression dL(2 s) ~ s_clip + L (`response_regression_contested_live.csv`):

| k bucket | n ticks / windows | b_s given L (t) | b_L (t) | R2 |
|---|---|---|---|---|
| [6,10) | 304 / 110 | +2.12 (4.9) | +0.28 (5.4) | 0.35 |
| [10,15) | 451 / 204 | +1.37 (4.6) | +0.19 (6.5) | 0.21 |
| [15,25) | 1,424 / 425 | +0.86 (4.0) | +0.13 (7.3) | 0.12 |
| [25,40) | 2,474 / 915 | +0.52 (3.3) | +0.10 (9.0) | 0.10 |
| [40,59) | 7,537 / 2,228 | +0.34 (4.4) | +0.06 (9.3) | 0.05 |

Units: log-odds per unit s per 2 s. When the book is still contested it moves toward the projection at
0.3-2 log-odds per unit s every 2 s, faster as k shrinks; the positive b_L is momentum (the book continues in
its current direction).

**3e. Is the projection information the book has not priced?** Realized win rate of the *projected* side minus
the book's P for that side, contested live books, window-cluster bootstrap 95% (`contested_edge_clusterboot.csv`):

| k range | abs(s) bin | ticks / windows | book P (proj side) | realized | edge | boot 95% |
|---|---|---|---|---|---|---|
| [6,25) | [0, 0.15) | 1,507 / 327 | 0.713 | 0.789 | **+7.6 pp** | [+4.1, +11.3] |
| [6,25) | [0.15, 0.3) | 451 / 182 | 0.787 | 0.876 | **+8.9 pp** | [+2.9, +14.4] |
| [6,25) | [0.3, 0.6) | 200 / 106 | 0.788 | 0.930 | **+14.3 pp** | [+9.5, +19.0] |
| [25,59) | [0, 0.15) | 7,046 / 1,671 | 0.735 | 0.752 | +1.7 pp | [0.0, +3.4] |
| [25,59) | [0.15, 0.3) | 2,120 / 1,002 | 0.846 | 0.869 | +2.3 pp | [-0.1, +4.6] |
| [25,59) | [0.3, 0.6) | 778 / 432 | 0.873 | 0.917 | +4.3 pp | [+1.2, +7.0] |
| [6,10) | [0, 0.15) | 205 / 73 | 0.669 | 0.824 | +15.5 pp | [+7.2, +23.2] |
| [15,25) | [0, 0.15) | 998 / 311 | 0.732 | 0.793 | +6.1 pp | [+2.2, +9.8] |

Per-cell detail (book-P bin x abs(s) bin x k) in `contested_honest_cells.csv`: e.g. k [15,25), |s| < 0.15, book
P 0.5-0.65 -> realized 0.68 (n=139/82 windows); k [15,25), |s| < 0.15, book P 0.8-0.953 -> 0.948 vs 0.892
(n=503/229). Interpretation: **inside a contested book the projection's sign is worth +6-14 pp over the mid at
k in [6,25) and +2-4 pp at k >= 25.** But the contested state is rare: 2,259 of 6,474 live windows (34.9%) are
ever contested in the final minute, **542 of 6,394 (8.5%) at k <= 25, 73 of 6,341 (1.2%) at k <= 6**. The edge is
measured at mid, before spread-crossing, and not against a fill model. The outcome models in
`response_outcome_models_contested.csv` (a2b) were computed before dead-book removal and are superseded by this table.

**3f. Book confidently wrong** (`book_wrong_at_k.csv`): P >= 0.9 on the eventual loser — k <= 6: 0-1 of ~6,300
(<= 0.016%); k=15: 6/6,214 (0.10%); k=25: 23/6,058 (0.38%); k=40: 41/5,726 (0.72%); k=58: 74/5,000 (1.48%).
In those windows the projection's sign was right in 0-7% (median |s| 0.10-0.21): **the projection does not see
the late reversals that catch the book either.** After dead-book removal the "book disagrees with a locked
projection" state is 0.2-1.4% of |s| >= 0.6 ticks (14-86 windows per bucket) and is the same placeholder/frozen
artifact class, not a tradeable disagreement.

## 4. Deliverable 3 — panic anatomy (deep dumps in the final 60 s)

Files: `dumps.csv` (every event), `dumps_by_k_s.csv`, `dumps_marginals.csv`, `dumps_fill_conditional.csv`,
`dumps_k_hist.csv`, `dumps_per_day.csv`, `botzone_check.log`.

**Population**: 13,768 token series scanned (6,884 windows); **6,758 events in 1,792 windows (26.0%)**, 270/day,
1,285 windows dumped on both sides. 83.8% of events occur at |s_rel| < 0.3; 2.7% with no projection.
k histogram (5 s bins): [55,60) 1,815 - [50,55) 1,063 - [45,50) 815 - [40,45) 629 - [35,40) 518 - [30,35) 442 -
[25,30) 404 - [20,25) 302 - [15,20) 289 - [10,15) 234 - [5,10) 140 - [0,5) 103.

**Did the dumped side win?**

| slice | n events / windows | dumped side won (Wilson) | median / p90 recovery s | censored | median ref bid -> trough |
|---|---|---|---|---|---|
| all | 6,758 / 1,792 | 0.598 [0.586, 0.609] | 3.4 / 52.8 | 22.8% | 0.79 -> 0.46 |
| k [0,6) | 131 / 48 | 0.840 [0.767, 0.893] | 0.17 / 1.8 | 9.9% | 0.98 -> 0.69 |
| k [6,10) | 112 / 61 | 0.884 [0.812, 0.931] | 0.37 / 4.3 | 5.4% | 0.98 -> 0.755 |
| k [10,15) | 234 / 110 | 0.812 [0.757, 0.857] | 0.24 / 8.9 | 9.4% | 0.92 -> 0.68 |
| k [15,25) | 591 / 289 | 0.756 [0.720, 0.789] | 1.07 / 19.3 | 14.9% | 0.91 -> 0.63 |
| k [25,40) | 1,364 / 645 | 0.662 [0.637, 0.687] | 2.3 / 33.7 | 19.2% | 0.84 -> 0.56 |
| k [40,60] | 4,326 / 1,668 | 0.529 [0.515, 0.544] | 5.6 / 56.6 | 26.6% | 0.71 -> 0.34 |
| s_rel >= 1 | 63 / 31 | 1.000 [0.943, 1] | 0.17 / 3.8 | 3.2% | 0.99 -> 0.84 |
| s_rel [0.6, 1) | 110 / 74 | 0.900 [0.830, 0.943] | 1.4 / 16.5 | 8.2% | 0.98 -> 0.775 |
| s_rel [0.3, 0.6) | 464 / 326 | 0.856 [0.821, 0.885] | 3.6 / 30.9 | 10.8% | 0.96 -> 0.74 |
| s_rel (-0.3, 0.3) | 5,664 / 1,597 | 0.584 [0.571, 0.596] | 3.5 / 53.7 | 23.4% | 0.77 -> 0.44 |
| s_rel (-0.6, -0.3] | 210 / 152 | 0.205 [0.156, 0.264] | 7.5 / 54.7 | 42.9% | 0.30 -> 0.035 |
| s_rel <= -0.6 | 64 / 39 | 0.172 (11/64) | — | 45% | 0.34-0.46 -> 0.03 |
| reprice (market moved) | 3,674 / 1,632 | 0.503 [0.487, 0.519] | 10.9 / 57.1 | 34.8% | 0.77 -> 0.23 |
| one-sided (liquidity pull) | 3,084 / 1,130 | 0.710 [0.694, 0.726] | 0.56 / 13.8 | 8.5% | 0.81 -> 0.62 |

Two populations: **reprices** (54%) are coin flips that do not recover (the market moved); **one-sided pulls**
(46%) revert in ~0.6 s and the dumped side wins 71%. The fill window for a resting bid is therefore ~0.2-1 s on
liquidity pulls and effectively "never recovers" on reprices.

**The bot's zone** (s_rel >= 0.6, k in [6,25]): **62 events in 34 windows over 25 days (1.4 windows/day)**;
dumped side won 90.3% [80.5, 95.5] by event, **30 of 34 by window**; median recovery 0.28 s, p90 5.6 s; ref bid
0.99, trough median 0.83; trough < 0.80 in 39% of events, < 0.65 in 27%, < 0.50 in 16%, < 0.35 in 15%.
By window: trough < 0.80 in 18 windows -> **14 won (77.8%)**; trough < 0.65 in 12 -> 9 won (75%).
**Adverse selection, measured**: unconditionally the projection side lost in **4 of 6,397** windows that ever
show |s| >= 0.6 at k in [6,25] (0.06%), and **all 4 of those are among the 34 dumped windows** (11.8%). The four
(`botzone_check.log`): ep 1787271900, 1787358600, 1787620800, 1788267900 — k 15-23 s, s_rel 0.61-0.84 at the
dump (none >= 1.0), final within $1.4-16 of the strike, 3 of 4 flagged reprice. Windows with s_rel >= 0.6 dumps
at k in [6,15): 19, all won.

**Fill-conditional win rate** (dumped side wins | trough < rung; `dumps_fill_conditional.csv`), pooled k [6,25),
all regimes (replicates r4 "Candidate A"): rung 0.90 -> 0.785 (n=937/337 w), 0.80 -> 0.722 (711/282, -7.8 pp vs
rung), 0.65 -> 0.618 (461/209, -3.2), 0.50 -> 0.494 (320/167, -0.6), 0.35 -> 0.324 (222/130, -2.6), 0.20 -> 0.152
(158/117, -4.8), 0.10 -> 0.048 (126/114, -5.2). Pooled k >= 25: 0.80 -> 0.522 (5,169/1,655, -27.8 pp), 0.50 ->
0.329 (-17.1). Sign-agnostic dip buying loses at every rung, and heavily at k >= 25. With the projection filter
0.3 <= s_rel < 0.6 at k [15,25): 0.90 -> 0.901 (71/54), 0.80 -> 0.788 (33/32), 0.65 -> 0.533 (15/15); at k [40,60]:
0.80 -> 0.781 (178/155), 0.65 -> 0.683 (104/94), 0.50 -> 0.525 (59/58) — roughly fair. With s_rel >= 0.6 at
k [15,25): 0.90 -> 0.769 (26/17), 0.80 -> 0.615 (13/11); at k [6,10) and [10,15): 100% (36 events / 19 windows);
at k [25,40): 0.90 -> 0.968 (31/26), 0.80 -> 0.917 (12/10); at k [40,60]: 0.90 -> 0.92 (50/39), 0.80 -> 0.871 (31/28).
Caveat: "trough < rung" assumes the resting bid is hit when the best bid drops below it; a quote cancellation
drops the bid without a fill. No live ladder fills exist to validate the mapping.

## 5. Deliverable 4 — spread and depth through the window

Files: `spread_depth_by_elapsed.csv`, `final90_side_split.csv`, `spread_depth_by_day_midwindow.csv`.
N per 30 s bucket ~ 151k-157k rows / 5,24x-5,25x windows (5 Hz buckets 130k-463k rows).

- **Spread**: median 0.01 in every bucket; p90 0.01; spread >= 0.05 in 0.3-0.7% of rows. When both sides exist the
  spread is one tick from open to close. The share of rows with spread <= 0.01 falls from 96-97% (0-120 s) to 82%
  (210-240 s) and 51% (295-300 s) only because a side goes missing (see below).
- **Top-3 depth (USD)**: Up-bid median $235 (0-30 s) -> $193 (30-60) -> $167 (90-120) -> $154 (150-180) -> $138
  (210-240); Up-ask median $698 -> $762 -> $851 -> $1,066 -> $886; p10 bid depth $134 -> $32; fraction of rows with
  min(top-3 bid depth of either token) < $50: 3.6% (0-30) -> 11.5% (120-150) -> 26% (210-240) -> 23% (295-300).
- **One-sided / empty**: some side of some token missing in 0.5% (0-90 s) -> 2.2% (120-150) -> 5.9% (150-180) ->
  14.6% (180-210) -> 30.1% (210-240) -> 66.7% (240-270) -> 83.0% (270-285) -> 92.1% (285-295) -> **95.7% (295-300)**.
  A token with BOTH sides missing: 0.4-0.5% flat (feed outages). mid-sum outside [0.98,1.02]: <= 0.03%. Book age
  > 10 s: 2.5% early -> 25% at 295-300.
- **Which side vanishes** (`final90_side_split.csv`, labeled windows, n ~ 5,100 per bucket): the **loser's bid**
  — missing 14.6% at 180-210, 30% at 210-240, 52% at 240-255, 70% at 255-270, 81% at 270-280, 91% at 285-290,
  **96% at 298-300**. The winner's bid/ask are missing only 0.4-0.5%. The winner bids >= 0.99 in 15% of rows at
  180-210 -> 31% (210-240) -> 53% (240-255) -> 70% (255-270) -> 81% (270-280) -> 90% (285-290) -> 96% (298-300);
  winner bid <= 0.80 in 39% -> 27% -> 16% -> 10% -> 7.1% -> 4.7% -> 3.6%; <= 0.50 in 18% -> 13% -> 8.7% -> 6.4% -> 4.8% ->
  3.9% -> 3.4%. Loser ask median 0.11 (180-210) -> 0.05 (210-240) -> 0.01 from 240 s.
- **The 0.99 wall**: winner top-3 bid depth median $208 (180-210) -> $243 (240-255) -> $447 (255-270) -> $1,487
  (270-280) -> $3,094 (280-285) -> $5,494 (285-290) -> $7,980 (290-295) -> $12,302 (298-300); winner touch bid size
  (post-08-22) 225 sh -> 1,008 -> 7,148 -> 12,366 -> 22,737. Loser top-3 bid depth stays $41-$181.
- **Day drift** (mid-window 60-240 s): Up-bid top-3 depth median $80 (08-14) -> $150 (08-19..21) -> $270-400
  (08-22..28) -> $140-230 (08-29..31); missing-any 3-15% by day.

## 6. Deliverable 5 — cross-window structure at the open (elapsed 5 s)

Files: `open_correlations.csv`, `open_regressions.csv`, `open_by_lag_gap.csv`, `open_by_prev_margin.csv`,
`open_book_context.csv`, `open_context.csv`. Book at 5 s available for 5,063 windows, all two-sided.
Definitions: `lag_gap` = raw Chainlink price at open+5 s - served strike (the 60 s TWAP lags spot);
`move60` = raw price at the open - raw price 60 s before it; `d_prev` = previous window's final - strike.

| variable | n | Pearson r with logit P(5 s) | Spearman | point-biserial r with outcome |
|---|---|---|---|---|
| lag_gap | 4,939 | **0.714** | **0.824** | 0.176 |
| move60 | 4,826 | 0.491 | 0.574 | 0.117 |
| d_prev (previous window margin) | 5,048 | 0.024 (p=0.09) | 0.040 | -0.018 (p=0.19) |

- Tercile calibration (n ~ 1,609-1,683 per tercile): move60 low/mid/high -> book 0.419/0.503/0.576 vs realized
  0.416/0.506/0.575; lag_gap -> 0.388/0.503/0.608 vs 0.382/0.512/0.602; d_prev -> 0.496/0.498/0.504 vs
  0.500/0.504/0.492. All within 1.2 pp.
- Logistic up ~ logit P(5 s) + var/sd (*computed*): lag_gap z = 1.62 (b 0.072 +/- 0.045), move60 z = 0.55,
  d_prev z = -1.73 (b -0.050 +/- 0.029). Log-likelihood gain over book-only <= 0.0003 nats/window.
- By lag_gap bin: e.g. > +$40: realized 0.739 [0.67, 0.79] vs book 0.686 (n=199); < -$40: 0.346 [0.28, 0.41] vs
  0.317 (n=205) — inside the intervals. Sanity: our rx-clock 60 s average of the raw stream over [open-60, open]
  differs from the served strike by median $0.11, p90 $0.56 (n=5,001).
- **Conclusion**: the book at 5 s already prices the strike lag (spot vs the lagging TWAP strike) and correctly
  ignores the previous window's outcome; there is no exploitable mispricing at the open under the 60 s rule.

## 7. What this says for the engine design

1. **The book is the engine for elapsed 5-240 s.** Calibration slope 0.93-1.11, no decile off by more than the
   Wilson interval, and it already prices the strike lag at the open (section 6). Nothing here contradicts the 09-01
   "0 of 135 features" result.
2. **In the final minute the book is decided in > 90% of windows**; the loser's bid side is gone in 70-96% of
   samples after 255 s and the winner sits at 0.99 behind a $1.5k-$12k wall. The only place the book can still
   be "slow" is the 8.5% of windows contested at k <= 25 (1.2% at k <= 6).
3. **Inside those contested windows the projection is real information**: +6 to +14 pp over mid at k in [6,25)
   (cluster-bootstrap CIs exclude zero), +2 to +4 pp at k >= 25, and the book moves toward it at 0.3-2 log-odds per
   unit s per 2 s. This is a taker-shaped opportunity (buy the projected side at the ask while the book is at
   0.5-0.9), sized by 327-542 windows per 25 days, before spread and fill costs — a design candidate, not a
   validated edge.
4. **The maker ladder's fills are adversely selected.** The lock is right 99.94% of the time unconditionally, but
   the dumps that fill it are exactly where it fails: 4 of 34 dumped windows lost (all four at 0.61 <= s < 0.84,
   k 15-23, three flagged as reprices); fill-conditional win rate 77.8% (14/18) at troughs below 0.80 against an
   80% break-even. A floor of 1.0 (not 0.6) would have excluded all four losers in this corpus, at the cost of
   most of the 62 events; the reprice flag (complement ask +0.05 within +/-1 s) separates coin-flip reprices from
   71%-win liquidity pulls and is the natural cancel/skip signal.
5. **Sign-agnostic dump buying is dead again** (rung 0.80 wins 72% at k [6,25), 52% at k >= 25) — G-M holds; the
   only profitable slice of dumps is the projection side, as r4 found.

## 8. What was not done / cannot be verified here

- No fill model: "trough < rung" is a proxy for a fill; MM cancellations look identical in BBO data. No live
  ladder fills exist to calibrate it.
- The bridged projection was computed (`proj_grid.csv`) but not analysed in section 3.
- Contested-book edges are at mid; spread crossing (1 tick when two-sided, but the ask can be 0.02-0.10 above mid
  when one-sided), latency (~0.4 s taker RTT) and the 0.07*p*(1-p) taker fee are not subtracted.
- Tick-level Wilson intervals overstate precision; cluster-bootstrap intervals are given only for the headline edge.
- window_paths coverage ends 09-01 18:42 UTC (5,140 labeled windows); micro-tape analyses run to 09-07.
- Dead-book windows were identified by a contested book coexisting with |s| >= 1; windows dead only while |s| < 1
  would survive that filter (they would bias the contested edge upward; the liveness filter removes those with no
  records at all).
- Per-wallet attribution of dumps and the counterparty census are other scouts' slices.

## 9. File inventory

Scripts: `common.py` (conventions, projection replica, p99.5 table), `prep_paths.py`, `prep_micro.py`,
`prep_proj.py`, `micro_load.py`, `liveness.py`, `a1_calibration.py`, `a2_response.py`, `a2b_contested.py`,
`a3_dumps.py`, `a3b_fill_window.py`, `a4_spread_depth.py`, `a4b_side_split.py`, `a5_open.py`.
Caches: `paths_era.parquet`, `micro_b_<day>.parquet` x25, `micro_b_era.parquet`, `micro_b_dedup.parquet`,
`liveness.parquet`, `proj_grid.csv`, `response_samples.csv`, `open_context.csv`, `dead_book_windows.csv`.
Result CSVs: `calibration_summary.csv`, `calibration_by_elapsed.csv`, `response_regression.csv`,
`response_by_sbin.csv`, `response_disagreement.csv`, `response_outcome_models.csv`, `book_wrong_at_k.csv`,
`response_regression_contested.csv`, `response_regression_contested_live.csv`, `response_univariate_contested.csv`,
`response_outcome_models_contested.csv` (superseded), `contested_up_rate_by_sign_s.csv` (pre-dead-removal),
`contested_projside_by_abs_s.csv` (pre-dead-removal), `contested_honest_cells.csv`, `contested_edge_clusterboot.csv`,
`dumps.csv`, `dumps_by_k_s.csv`, `dumps_marginals.csv`, `dumps_fill_conditional.csv`, `dumps_k_hist.csv`,
`dumps_per_day.csv`, `spread_depth_by_elapsed.csv`, `spread_depth_by_day_midwindow.csv`, `final90_side_split.csv`,
`open_correlations.csv`, `open_regressions.csv`, `open_by_lag_gap.csv`, `open_by_prev_margin.csv`,
`open_book_context.csv`. Logs: `a1.log a2.log a2b.log a3.log a3b.log a4.log a4b.log a5.log degenerate_check.log
contested_final.log botzone_check.log prep_*.log`.
