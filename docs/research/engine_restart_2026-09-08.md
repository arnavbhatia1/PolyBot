# Engine restart — pre-registration and verdicts, 2026-09-08

**Operator directive (09-08):** "restart the engine part of this bot; find the best
engine to make the best prediction in the entire window using data, analytics, other
wallets, feeds, etc. — anything under the sun." This explicitly reopens entry-side
outcome prediction (Hard Rule 1) including the wallet data class WALLETS.md marks
out of mandate. Everything below the "Pre-registered tests" heading was written
BEFORE any test ran; bars are frozen. Phase-1 measurements (the map) are in the
session scratchpad `engine_restart/*/REPORT.md`; load-bearing numbers are restated
here with N.

## Objective

The engine that maximizes expected realized $/day at the $400 go-live bankroll,
over the whole window, subject to the standing bars (REFUTATIONS.md methodology
bans; RESEARCH.md frozen-measurement discipline). "Predicts the outcome" is not the
bar — "beats the book after costs, engine-true, out of sample" is.

## Phase 1 — the map (measured 09-08, 60 s era, 26 ET days 08-13..09-07)

1. **Book calibration.** Slope 0.93–1.11 from elapsed 5 s to 240 s, no decile off by
   more than its Wilson interval (5,063–5,078 windows per elapsed); the open already
   prices the strike lag (Spearman 0.82 with spot−strike; terciles within 1.2 pp).
   Nothing to build at elapsed 5–240 s from public price data. [book-dynamics]
2. **Projection information inside the zone.** Walk-forward logistic on
   [logit(book mid), signed r = (proj−strike)/p99.5(k)] beats the recalibrated book
   on log-loss at k = 58/45/30/20/10 by 0.0135/0.0182/0.0213/0.0259/0.0247, day-block
   bootstrap p ≤ 0.0016, 14–17 of 23 OOS days, N ≈ 6,100 windows per k. Book and
   projection disagree on the side in 3.6 % (k 58) → 1.8 % (k 10) of windows; the
   projection is right 62 % → 90 % of those. [information-structure D4]
   **Caveat carried into T1:** that run did not exclude the 253 dead-book windows
   (placeholder 0.50/0.51 or 0.10/0.90 books), which inflate any model that knows
   the projection. Re-verified in T1 with exclusion.
3. **Contested books.** 8.5 % of windows (542/6,394) are still contested at k ≤ 25;
   in them the projected side wins +7.6/+8.9/+14.3 pp over the book's own P at
   |s| < 0.15 / 0.15–0.3 / 0.3–0.6, k ∈ [6,25), window-cluster-bootstrap CIs exclude
   zero (327/182/106 windows); +1.7/+2.3/+4.3 pp at k ∈ [25,59). The book drifts
   toward the projection at 0.3–2 log-odds per unit s per 2 s. [book-dynamics 3e]
4. **Adverse selection of maker fills.** Lock right in 6,393/6,397 windows; all 4
   losses sit among the 34 windows where the projected winner was dumped ≥ 0.10 in
   the bot zone; 3 of 4 were book reprices (complement ask +0.05 within ±1 s), not
   liquidity pulls; fill-conditional win 14/18 at troughs < 0.80 vs the 80 %
   break-even. Sign-agnostic dump buying loses at every rung (r4 re-derived).
5. **Re-arm defect.** Deployed code re-arms after a floor cancel (`maker_bid.py`
   `_retire` sets `active=None`; main refuses only when a position exists); every
   replay (ws2/r19/r24/r27) arms once per window. Second arms pick the winner
   96.2 % (N 78), third arms 72.7 % (N 11); all loser-side reachable value in 26
   days ($1,932, 3 windows: ep 1787271900, 1787358600, 1787620800) sits in re-arms.
   [information-structure 3e] Unverified against a logged re-arm.
6. **Where the money is.** 99.3 % of winner-side deep value (≤ 0.80, k ∈ [−60,60],
   $2.46M ceded / 26 d) prints in cells the lock forbids; 47 % while the projection
   points at the loser; the lock arrives after the deep print or never in 93 % of
   k ≤ 25 share volume. Tape captures ~8–40 % of post-close volume. [information-structure 3, 5]
7. **Wallet identity (new data class).** Walk-forward top-decile prior-P&L one-sided
   wallets: bought token wins +2.4/+1.7/+3.7/+6.7 pp over book mid at k 25–60 /
   60–120 / 120–240 / >240 (t 2.8/2.45/4.3/4.95, day-clustered, 13 test days
   excluding the selected h1 windows); skill persistence Spearman +0.37 (N 3,056
   wallets); ~half absorbed by the book within 2 s; follower net of the 7 % taker
   fee +1.7 pp at 120–240 s and +4.0 pp at >240 s, nothing at k ≤ 25. Data-api
   `timestamp` = Polygon block time = match + 2.28 s (p50); indexing lag unmeasured
   (one probe read minutes of lag — n = 4, unverified). The six-pseudonym cluster's
   deep sells are fair (28.5 % vs 32.1 % mid, |t| ≤ 1). [wallets]
8. **External feeds.** Binance UM bookTicker and liquidationSnapshot archives end
   2024-04 / do not exist; spot has no L2 archive; UM bookDepth is 30 s
   percentage-depth (13.8 MB / 25 d). The Chainlink 60 s TWAP stream is not in the
   public Data Streams catalog (bespoke; a vendor resells a direct capture). No
   public real-time feed carries wallet identity. [external-landscape]
9. **Prior art.** None of the eight candidate directions is refuted on 60 s-era
   engine-true or live evidence; the only historical per-rung-need configs were
   30 s-stream artifacts (commits 327125f3/81fdbd0a/28b97bd7, before the 08-18
   rule fix). [prior-art register.json]

## Pre-registered tests (frozen 09-08 before running)

Common protocol, every test: 60 s era only; walk-forward by ET day (train on days
< d; first 3 ET days train-only); no shuffling; one bet per window; event-true books
from the micro tape (BBO changes) — never 1 Hz BBO for scoring; dead-book windows
(`book-dynamics/dead_book_windows.csv`, 253) excluded; liveness filter (≥ 1 raw
report in the trailing 3 s); projection re-implemented as `chainlink_feed.py`
`projected_final_twap` (bridged for the ladder, plain for the taker, as deployed);
p99.5 table = deployed `TWAP_MARGIN_P995`; day-block bootstrap B ≥ 2,000 for every
p-value; both chronological halves reported; ANTI control (same rule, other side)
and edge<0 control bucket mandatory. Grids are reported in full; adoption reads
only the pre-declared cell. Lowering a threshold to make a cell pass voids the run.

### T1 — Zone engine, k ∈ [6,60]: book-anchored probabilistic model

Model M: P(Up) = σ(a + b·logit(book_p) + c·r_signed), book_p = Up mid clamped
[0.01,0.99]. Fitted per k-bucket {[45,60], [25,45), [15,25), [6,15)} walk-forward.

- **B1 (information, re-verification with dead-book exclusion):** M's OOS log-loss
  < recalibrated book's at k ∈ {58,45,30,20,10}, day-block p < 0.05, ≥ 15 OOS days.
  Kill at k ≥ 25 → no probabilistic engine outside the lock.
- **B2 (taker leg):** at each raw tick with k ∈ [6,58], side = argmax M; fire on the
  first tick where P_M(side) − ask(side) − fee ≥ e_min, fee = 0.07·p·(1−p) per share
  at p = ask; the order lands at t + L, L = 0.42 s (paper POST table p50 436 ms
  × 0.95); FOK fills iff ask(side, t+L) ≤ ask(side, t). Hold to resolution.
  ¢/sh = payout − price − fee. Pre-declared cell: **e_min = 0.05**; e_min ∈
  {0.03, 0.08} and L = 0.70 s reported as context. Bars, all required: (i) EW ≥
  +2 ¢/sh on ≥ 100 OOS trades over ≥ 15 OOS days; (ii) net ¢/sh non-decreasing
  across model-edge buckets {<0 (control), [0,3), [3,6), [6,10), ≥10 ¢}; (iii) the
  <0 control bucket ≤ 0; (iv) ANTI ≤ 0; (v) day-block p(EW > 0) < 0.05; (vi) both
  halves positive in dollars at $15 notional per trade. Sub-populations reported
  (not separate arms): contested (book P of the chosen side ∈ [0.5, 0.9]) vs
  confident. Also report: fraction of fires where the 1 Hz top-of-book ask size
  (window_paths, 08-22+) < 15 shares (size risk not modeled by the micro tape).
- **Kill:** B1 fails at k ≥ 25, or B2 fails (i)–(iv) → the projection's information
  is recorded as "real but not tradable as a taker"; the lock-gated ladder stays
  the only zone instrument.

### T2 — Ladder hygiene and per-rung schedules (engine-true, `ws2_ladder_replay.py` extended in scratch)

- **H1 re-arms.** Extend `run()` to re-arm after a floor cancel exactly as production
  (idle again → arm when r ≥ need, k ∈ [k_min,k_max], no position booked in the
  window). Faithfulness check first: with re-arm ON at need 0.6 / k_max 25 / R8 /
  $200 the replay must show the three known loser windows (ep 1787271900,
  1787358600, 1787620800) as flip-fills, and at k_max 58 the 09-07 live loss
  (ep 1788743700). If < 2 of 3 reproduce, no claim is made. Then: dollars, fills,
  losses with re-arm ON vs OFF. **Adopt "one arm per window"** iff loser dollars
  removed > winner dollars forgone in BOTH halves.
- **H2 reprice cancel.** While resting, cancel when within the trailing 1 s the
  complement token's best ask rose ≥ 0.05 or our side's best ask fell ≥ 0.05 (micro
  BBO). Adopt iff loser $ removed ≥ 2 × winner $ forgone in both halves, ANTI
  unchanged; report the classification of the four known loss windows.
- **H3 per-rung schedules (pre-declared, three only):** S0 = deployed (uniform
  0.6); S1 = 0.6 on rungs ≥ 0.50, 0.3 on rungs ≤ 0.35; S2 = 0.6 on ≥ 0.50, 0.3 on
  0.35/0.20, 0.15 on ≤ 0.15; each at k_max 25 and 58, re-arm as adopted from H1.
  Adopt S1/S2 over S0 iff dollars ≥ S0 in both halves, loss count ≤ S0, and no
  flip-fill loss on any relaxed rung. ANTI ≤ 0.
- **H4 (descriptive, adoption owned by the 09-11 re-decision):** need {0.6, 0.8,
  1.0} × k_max {15, 25} with re-arm modeled.

### T3 — Wallet identity, fresh out-of-sample

Classes frozen from the existing pulls (data through 09-01): top_directional =
≥ 5 prior windows, one-directional ≥ 70 %, prior P&L top decile; bottom_directional
= bottom decile (control); two_sided_mm; wall_camper; six_cluster (identity). The
frozen class lists are written to disk BEFORE the pull. Fresh pull (read-only
data-api `/trades?takerOnly=false`, ≥ 0.5 s spacing, 429 back-off): btc-updown-5m
windows 09-02..09-07 ET, stride 3, full paging (no offset cap). Book at the trade
instant (block ts − 2.3 s, or the exact tape match): micro BBO for k ≤ 90; outside
it, the contemporaneous VWAP of OTHER participants' prints on the same token within
±3 s (fallback ±10 s), stated as a proxy.

- **B5a:** top_directional BUY excess (bought-token win rate − book price) at k > 60,
  pooled: > 0 with day-clustered t ≥ 2.0 over ≥ 5 fresh ET days.
- **B5b:** same at k ∈ [25,60] and k ≤ 25, reported.
- **B5c:** follower at the tape ask 2.5 s after the match (k ≤ 90 only), net of fee.
- **B5d:** bottom_directional BUY excess ≤ 0 (control); persistence Spearman on the
  fresh days (wallets with ≥ 10 fresh BUY obs).
- **Kill:** B5a t < 1.0 → "wallet information not confirmed out of sample".
  **Pass:** B5a passes → the infra question (on-chain identity latency) escalates
  to the operator as a build proposal; nothing trades off this test.
- Desk check (docs only, no listener): CTF Exchange `OrderFilled` fields, whether
  `proxyWallet` equals the on-chain maker/taker address, free Polygon log/mempool
  access options, expected identity latency.

### T4 — Whole-window controls (closing the "entire window" question)

- **P1 pre-zone:** M′ = logistic on [logit(book_p), z], z = (spot − strike)/rv60
  (information-structure D2 definition), at k ∈ {240,180,120,90}; book from
  window_paths (1 Hz, 5,140 labeled windows to 09-01). B1 bar vs recalibrated book.
  M″ adds the Chainlink-vs-Binance-relay basis (candidate F) on top of M′.
- **P2 external:** Binance UM bookDepth (30 s) depth imbalance at ±0.2 % and ±1 %
  as M1(f) on top of x0 = logit(book_p) at k ∈ {240,180,120,60}; Bonferroni over
  features × horizons (α 0.05/8).
- **P3 nested strike (optional, r10 15m pulls 08-22..31):** at shared closes,
  count 5m/15m trade-price dominance violations ≥ 0.03 in the final 60 s and the
  shares behind them.
- **Kill:** P1/P2 fail → the whole-window question is closed by evidence to k ≤ 60.

## Verdicts (run 2026-09-09; bars above were frozen first)

**Every candidate engine failed its pre-registered bar. The whole-window prediction
question is closed by evidence at every horizon, and the largest Phase-1 claim was
an artifact.** Full reports: `engine_restart_2026-09-08/*.md` (Phase-1 scouts,
pre-mortem, T1–T4, critic); re-arm harness `scripts/research/r28_rearm_replay.py`.

| test | pre-declared cell | result | verdict |
|---|---|---|---|
| T1 B1 information (dead books excluded) | M vs recalibrated book, k 58/45/30/20/10 | Δlog-loss −0.0007 (p 0.19) / **−0.0018 (p 0.0002)** / **−0.0018 (p 0.008)** / +0.0001 (p 0.86) / +0.0002 (p 0.92); N ≈ 5,830–5,940 windows per k, 23 OOS days | FAIL as written (passes only at k 45/30) |
| T1 B2 taker leg | e_min 0.05, L 0.42 s | 628 fires / 326 fills / 23 days; EW +3.08 ¢/sh, win 73.9 %, +$355.6 at $15; ANTI −11.25 ¢ (N 150); control −0.65 ¢ (N 5,543); (ii) monotonicity FAIL (≥10 ¢ bucket +4.90, N 19, below [6,10) +6.25); (v) day-block p 0.072 FAIL; (vi) halves +$156.6 / +$199.0 | **KILL** |
| T2 H1 faithfulness | re-arm ON reproduces the 3 named loser windows | 3/3 (arms 3, 2, 3) + a 4th re-arm loss ep 1787678700 (08-25, −$200); 09-07 live loss reproduces at k_max 58 | PASS |
| T2 H1 adoption (one arm per window) | loser $ removed > winner $ forgone, both halves | H1-half $475 removed vs $6.25 forgone (5 re-arm fills: 4 losses, 1 win); H2-half 0 vs 0 (no re-arm fill at k_max 25) | FAIL by the letter (second half empty) |
| T2 H2 reprice cancel | loser $ removed ≥ 2× winner $ forgone | trigger precedes all 3 losses + the live loss (lead 0.09–1.10 s) but removes 10 of 12 winner fills: $100 removed vs $1,070 forgone | **KILL** |
| T2 H3 schedules | S1 / S2 vs S0 (k 25, re-arm ON) | S0 12 fills / 11 wins / +$995; S1 13 / 8 / 5 losses / +$797; S2 16 / 9 / 7 losses / +$836; relaxed-rung flip-fill loss windows 5 / 7 | **KILL** |
| T3 B5a wallet class, fresh OOS | top-decile one-sided BUY excess at k > 60, 09-02..09-07 | −0.72 pp, day-clustered t −1.28 (6 days, 28,683 obs, 542 windows); control class (bottom decile) **+0.86 pp, t +2.99**; persistence Spearman **−0.057** (was +0.37 in-sample); follower net of fee negative in every bucket | **KILL** (inverted, not merely null) |
| T4 P1 pre-zone | [logit(book), z] vs recalibrated book, k 240/180/120/90 | worse at every k (Δ +0.0001..+0.0003, p 0.18–0.76, 17 OOS days, N 3,275–4,217); basis model UNDECIDABLE (Binance relay never recorded > 85 s before close) | **KILL** |
| T4 P2 external | Binance UM bookDepth imbalance at ±0.2 % / ±1 %, 4 horizons | worse in 8/8 cells; nothing at Bonferroni | **KILL** |
| T4 P3 nested strike | 5m/15m dominance | 15m pulls carry no strike field | NOT RUN |

### Corrections to the Phase-1 map (record these; they supersede the items above)

- **Item 2 was a dead-book artifact.** Re-including the 253 placeholder-book windows
  reproduces D4 to the third decimal; excluding them shrinks the gain 19× (to
  ≤ 0.0018 at k 45/30) and the projection is right in only 35–42 % of
  book-vs-projection disagreements (N 178/96/55/32/21), not 62–90 %. What survives is
  a sub-cent tail-sharpening inside already-decided books; inside contested books
  the model never beats the recalibrated book (p ≥ 0.16 at every k), and against an
  isotonic recalibration the k 45/30 gain is not significant (p 0.29 / 0.65).
  Item 3's contested-book edges (measured at mid) do not survive an executable test.
- **Item 5's "$1,932 loser-side reachable" was print value, not bookable loss.** The
  engine-true booking of the same windows is −$275 (−$475 with the 4th window the
  dead-book list hid). ep 1788267900 is NOT a bot-zone loss under the deployed
  bridged projection (r peaks 0.58; the ladder armed the winner) — book-dynamics
  had used the plain projection.
- **`dead_book_windows.csv` over-includes.** 225 of 253 are confirmed dead (≤ 2 BBO
  records, placeholder state); 28 have live books (226–2,122 BBO changes),
  including 2 of the 3 loser windows and 7 of the 17 baseline fill windows
  (+$793.66). T2's verdicts are invariant to the list; T4's own two-sided/age gate
  already excluded them; T1 tested a stricter union, not the restoration of the 28
  (critic finding 3). `T2_dead_confirmed.csv` is the corrected list.
- **Item 7's wallet effect did not carry forward one week.** The class ranking
  inverted (top decile −0.72 pp, bottom decile +0.86 pp, persistence −0.057). The
  kill is proxy-sensitive: with a strict two-sided ≤ 10 s book (23 % coverage) the
  cell reads +1.31 pp, t 1.75 — neither kill nor pass. On-chain identity
  (OrderFilled maker/taker == data-api proxyWallet) verified on one transaction.

### What stands

1. **The deployed lock-gated ladder is the only zone instrument with a license.**
   No taker, probabilistic-rung, wallet-following or pre-zone engine cleared its bar.
2. **Production re-arms; every replay claim assumes one arm per window.** Confirmed
   in code (`maker_bid._retire` → `active=None`; main refuses only on an existing
   position) and in 134 logged multi-arm era windows (07-13..08-27; 0 fills on
   arm ≥ 2 under the then-config). At need 0.6 / k_max 25 the replay's 5 re-arm
   fills in 26 days are 4 losses (−$475) and one +$6.25 win, all 08-20..08-25; the
   harness reproduces 34 of 66 logged floor re-arms (production evaluates at 4 Hz),
   so this is a lower bound. The pre-registered adoption rule failed only because
   the second half carries no re-arm fill. **Operator decision:** either make
   production one-arm-per-window (aligning the deployed code with the semantics
   every r19/r24/r27 frontier number was computed under) or carry H1 to a
   re-decision once September accumulates re-arm events. Nothing here is an edge;
   it is the evidence chain's consistency.
3. **Methodology lessons, binding from here:** (a) dead/placeholder books must be
   excluded by BBO activity, never by a contested-while-|s|≥1 heuristic; (b) a
   ladder replay that does not model production re-arms cannot claim "0 flip-fills";
   (c) any taker replay must state the fill-price convention — ask(t) vs ask(t+L)
   flips T1's cell from +3.08 to −0.57 ¢/sh; (d) fill-conditioned and window-level
   probabilities differ by tens of pp in the cells that carry volume.
4. **Open, not killed:** Chainlink-vs-Binance basis at k > 85 s (the relay stream is
   recorded only inside the zone — an instrumentation change, not a model);
   5m/15m nested-strike dominance (no 15m strikes in the local pulls). Neither has
   a mechanism argument strong enough to justify the instrumentation on its own.

### Critic's load-bearing caveats (engine_restart_2026-09-08/critic.md)

Wallet inversion on three independent statistics; T2 dollars are paper-replay with
no observed re-arm fill at the current config; the dead-book correction was not
cross-applied to T1; the Phase-1 loss roster was wrong in two ways; T1's passing
sub-bars ride on 26 trades and a pricing convention; T3's kill sits in the
undeclared gap between kill and pass under a stricter book proxy. Every kill is
over-determined despite these gaps; no code change is indicated by the bars.
