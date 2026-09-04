# RESEARCH.md — open problems, ranked

The living successor to each session's charter. Every entry carries a
hypothesis, a pre-registered bar, and a status. Every frozen measurement in
the codebase is registered at the bottom with its reopening condition —
re-measuring against a bigger corpus or better estimator is NOT relaxing a
bar; lowering a threshold to make a trade fire is. REFUTATIONS.md is binding;
WALLETS.md is the census. Dates 2026.

## The 08-14 rule-change incident (context for everything below)

Polymarket silently moved resolution from the 30s to the 60s TWAP stream at
08-14 00:00 UTC (market descriptions now cite btc-usd-twap-60s-streams; RTDS
topic `crypto_prices_twap_sixty` bit-matches served finals — live-verified
08-18). The bot traded the wrong stream for 4 days. Both watchers missed it:
the chain invariant (final == next strike) is source-internal, and the
bit-exact tape check existed only inside the heavy sim read that was timing
out nightly, dropped by the ping formatter, plus a per-window drift WARNING
that only ran on held positions and only logged. Fixed on branch
research/60s-rule: feed re-pointed, 60s projection + re-frozen tables, raw
coverage guard, and a dedicated nightly SOURCE watch (mechanism_read) that
turns red the same night. Detection lesson: **an invariant both sides of
which come from the counterparty cannot detect the counterparty changing.**

## The sign record, stated honestly (08-18 walk-forward audit)

In-sample the 60s projection's sign was 873/873 on armed windows. Out-of-fit
(LODO tables, 1,050 armed windows over 5 day-units): **1 flip** — 08-18
13:45, a ~$20 reversal that had cleared 1.147× the p99.5 error at k=24 and
filled the 0.80 rung before the flip-cancel (−$4.50; the only OOS loss).
Per-day exact-binomial 95% upper bounds on the flip rate: 1.2–2.6%. Quote
these numbers, never the in-sample count. ANTI-side controls: −39¢/sh at
~0/400 wins on every split.

## Ranked queue

1. **deep_proj paper validation + the floor decision at ≥14 real-final
   days** — INTERIM floor need 1.0 staged 08-18 (charter fallback): the
   in-sample 0.5 grid could not be validated out-of-fit — 17 OOS fills
   total; the 0.80 rung ran 6/7 vs the break-even+10pp bar (90%) and the
   single adverse event decides every clause. Evidence FOR 0.5 recorded for
   the re-decision: its 4 marginal OOS fills were all wins (+$11.42), the
   one loss cleared 1.0 anyway (clause-iv: zero 0.5-only losses), and 0.5
   accrues the ≥20-fill paper bar ~2.3× faster. The 0.5 shadow's own brief
   realized record (epoch 08-18 16:45Z → 08-19 13:00Z): 2 fills, 2 wins,
   +$20.34 — evidence for the re-decision, excluded from the 1.0 gate.
   Unblock: ≥14 real-final days (~4,000 windows), then walk-forward re-run
   (ws1_oos.py) decides 0.5 vs 1.0 on the full pre-registered bar. The §2 paper bar (≥6d, ≥20
   windows, EW ≥ +5¢/sh, dollars > 0) judges deployment as ever.
   Epoch history: the 08-22 02:40Z epoch (audited-code deploy) accrued 3
   fills / 3 wins / +$20.39 / +36.5¢/sh in 2.5 days — but 08-22..23 rested
   ~260 ladders/day with the 0.80/0.65 rungs STARVED under the 5-share
   minimum (breaker-scaled $2.65/rung at the $132 paper bankroll; 0-4
   ladders/day carried 0.80), so zero fills landed until the bankroll
   recovered. Sub-$150 validation censors the rungs where fills live
   (H1b: win% at fills pins to rung price; fills concentrate at 0.80).
   Re-pinned 08-24 15:40Z at the $400 go-live bankroll so the gate
   measures the deployment that would actually ship. The 3 excluded fills
   are evidence-recorded here.
2. **60s margin-table re-fit at ≥14 real-final days** — the 08-18 freeze
   stands on 970 real-final windows (p99.5 ≈ 5th-from-top order stat) +
   synthetic max-union, MAX from per-tick interval maxima. Re-fit with
   ws1_measure60/ws1_interval_max conventions; also re-decide the ladder
   floor (#1) and the taker's dormancy (#3) from the fresh tables.
   **The 08-20 audit says expect a WIDENING, not a formality.** An
   independent re-derivation reproduced 15/16 frozen p99.5 knots on the
   freeze span, but adding 08-18 — the day the freeze excluded — puts
   several knots outside the envelope: **k=25 $10.0 vs frozen $8.0 (+25%)**,
   k=40 $24 vs $18, k=12 $4.5 vs $3.5, with k=8/20/29/35 also thin. k=25 is
   `maker_k_place_max` and 5 of the first 7 paper fills armed at k≈24.99-25.0,
   so live arming sits exactly where the table is thinnest and `need 1.0` is
   absorbing an under-stated error. 08-20 added a realized case: a filled
   paper window's projection error ran $13.7 at k≈25 (proj 72,780.6 vs final
   72,766.9; −$16.80 loss) — beyond both the frozen $8 and the re-derived
   $10. One tail event, but it landed exactly where fills arm. Tables were deliberately NOT touched by
   the audit — editing knots outside the scheduled re-fit is measurement by
   fiat. Note before re-fitting: `ws1_freeze_tables.py` fitted MAX at grid
   points rather than per-tick interval maxima (under-bounding); fixed 08-20
   to import `ws1_interval_max`, and its input `data/ws1_errors60.csv` must
   be regenerated — the freeze is not currently reproducible from the repo.

2b. **Measure the GTC place/cancel round trip** — blocking, and the only
   audit finding on the binding gate that no amount of paper accrual can
   settle. `paper_trader._GTC_LATENCY_QUANTILES` pays 56ms/rung and its
   commit claims it is measured, but `latency_stats.json` has no `gtc`
   section and never had one. Reconstructing live fill 334 from tape needs
   ~500ms to reproduce the real outcome: paper credited it $11.21 against
   $3.74 actual, because rungs become matchable roughly twice as fast as
   the real ones and collect fills in the tenths of a second while the
   triggering sweep is still printing (+21.9% over the 5-fill probe). The
   error is two-sided — it flatters winners and punishes losers — so the
   direction of the distortion on ¢/sh is unknown, not conservative.
   Instrumentation shipped 08-21 (both unblock paths): live place/cancel
   record RTTs to `latency_stats.json` (gtc section, raw samples kept),
   every rung stamps `gtc_place_ms`/`gtc_cancel_ms` into the booked
   snapshot, `smoke_gtc_test.py --samples` persists too, and the nightly
   ops watch validates paper's 56ms table against measured p50 (±25%, dark
   until n ≥ 10). **MEASURED 08-28 13:50Z** (`smoke_gtc_test.py --confirm
   --samples 12` on the box, idle path): place p50 57.0 / p90 64.1 / max
   173.8 ms (the max is the cold first call), cancel p50 55.2 / p90 62.8 /
   max 78.0 ms, n=12 each. Paper's table (p50 56 / p90 60 / max 170) is
   inside the ±25% band — the ~500 ms tape reconstruction of fill 334 is
   contradicted for the POST itself. Still open: the in-anger case (five
   sequential POSTs while a sweep prints, event loop busy) — the per-rung
   `gtc_place_ms` stamps on the first live ladder fills answer it.
3. **Lock-dip taker: DORMANT-pending-regime** (`taker_enabled: false`,
   staged 08-18). **Re-run 08-31 on 17 days of winner books (ws3_dips,
   deployed re-fit MAX tables): headline 58 events / 38 FOK-reachable is an
   ARTIFACT — 29 of the 38 sit exactly inside the CLOB maintenance/outage/
   reconnect windows (08-26 04:05-07:35Z, 08-30 18:05-18:15Z, 08-19
   04:15-09:50Z; all printless in the tape-coverage table), entry pinned at
   0.50/0.51 with durations of exactly ~9 s / ~30 s = a frozen or empty book
   scanned to the end of the zone while Chainlink stayed up. The deployed
   book gates (freshness, price-sum, depth) refuse those moments, and during
   cancel-only mode an FOK cannot be placed. Genuine events: the 08-24
   14:20/14:25 avalanche windows (entries 0.83-0.96, sub-second to 4.7 s)
   plus 0.94-0.96 flickers — ~1 reachable WINDOW per 1-2 weeks, far below
   the ≥1-per-3-days bar. Dormancy STANDS. Scanner debt: ws3_dips needs a
   book-freshness + price-sum gate replica before its counts are trusted
   again; its RTT constant 0.436 also predates the 08-17 50 ms hold cut.** The 60s rule killed its supply: 4 winner-side max-lock
   dips / 1 FOK-reachable over 1,184 windows (bar: ≥1 reachable per 3
   days); production max-tier fired zero times; the p99.5-tier sim showed
   the tier's known fragility (one −95.3¢ breach vs thirteen +5¢ wins).
   Zero wrong-side max locks corpus-wide (the never-breach premise holds).
   Re-arm condition: re-run ws3_dips.py at the ≥14-day re-fit or after any
   regime break; re-arm only if reachable dips ≥ 1/3 days AND the harness
   clears +2¢/sh after the real taker fee. The bridge changes dip
   qualification by −2/+0 events (fresher spot removes marginal locks) —
   re-check at adoption time (#4).
4. **Bridged projection for the taker** — 08-18 measurement: bz-bridge p99.5
   ≤ plain at 12/13 knots (one ~tie), max never wider on real-final days;
   kline-sim (validated ≡ live bz at p99 $0.000) tighter at ALL knots on the
   full corpus but max WIDER at k=25/35 on synthetic days. Bar to adopt: on
   ≥14 real-final days, paired, bz p99.5 ≤ plain at EVERY k ≥ 6 AND bz max ≤
   plain max at every k ≥ 6. Until then the taker stays plain (tables are
   plain-measured). Status: measured, provisionally positive, not shipped.
5. **Candidate A — cushion dip-buyer** (WALLETS.md): both-sides deep rungs
   0.10-0.35, no sign filter. Bar written in WALLETS.md (win ≥ price+8pp per
   rung ≥10 fills, positive dollars in two disjoint 3-day splits, and the
   sign-gated variant must not dominate). Status: observed in three wallets
   through both eras; not implemented.
6. **twap_k_min_s 6.0** — carried from the 30s era by charter decision. At
   ≥14 real days, measure k ∈ [2,6) knots on the 60s rule (08-18 read:
   p99.5 ≈ $0.7-0.9, max ≈ $1.5-1.6 gated — pinnable-looking but the 30s era
   taught exactly this overconfidence at low k). Proposal-only either way.
   08-31 replay evidence (r9): k_place [2,25] arms cleanly (sign 4,187/4,187)
   but adds exactly ONE fill per need in 18 days, both from the era's
   pathological first hour on 08-13 — no economic case to revisit the scar.
7. **Queue-constant estimator discrepancy** — sweep-consumed depth at deep
   levels is stable (med 19-46 sh, p75 62-120, pooled med 31, NO trend across
   11 days) while the book-resting watch grew 55→135. Re-measured 08-21 on
   the full 60s era (8 days, 56,523 sweeps): med 29 / p75 77 — unchanged,
   still well under the constant. The shipped 135
   over-states typical queues → paper under-credits at-price fills →
   conservative, correct direction. The nightly ops watch now alarms when
   trailing p75 exceeds the constant (the unsafe direction). Reopen only if
   live deep fills land that paper refuses to credit (recalibrate from
   `filled_at_px` live/paper attribution, never from book snapshots).
   Fill-rule verification against live/paper fill PAIRS stays blocked: zero
   live ladder fills exist post-era — unblocks on the first live flow.
8. **Candidate A — cushion dip-buyer** (WALLETS.md): both-sides deep rungs
   0.10-0.35, no sign filter. Bar written in WALLETS.md. NOTE from the WS4
   kinematics: any such leg eats the avalanche sweeps deliberately — its
   economics must price them (the triplet's do; win% ≈ price+12pp INCLUDES
   sweep losses). Status: observed in three wallets through both eras; not
   implemented.
9. **Maker rebates at scale** — proven real (~0.4%/day of maker notional on
   1723's ledger). An adder after a bar passes, never a strategy. 08-31
   arithmetic (venue_truth doc): the liquidity-rewards program pays the
   ladder pattern exactly $0 on three independent clauses (one-sided ≡ 0 at
   mid>0.90; min_size 50 sh; max_spread 1.5-4.5¢ vs our 10-70¢ distances),
   maker rebate per full sweep ≈ $0.52 < the $1 daily payout floor, and the
   program's published window ends 08-31. Rebates flip none of the symmetric
   MM / touch-bid / post-close refutations.
10. **Census cadence** — re-run WALLETS.md census weekly during validation
    and after any regime break; the 08-14 leaderboard reshuffle was visible
    in one day of counterparty data.
11. **Sibling markets' in-window deep flow** — **CLOSED 08-31**: measured on
    btc-15m (the only credible sibling), $137/day total pie ex-outlier over
    282 sampled windows → REFUTATIONS.md entry. Reopen only on a liquidity
    regime change in the 15m family (volume24hr sustained > ~$5M/day).

## 08-31 charter — verdicts (docs/research/{venue_truth,ceiling}_2026-08-31.md)

Corpus: 60s era extended to 08-30 (win_streams 6,427 windows; tape complete
every day incl. the re-pulled full 08-27). Scripts r6-r11 in scripts/research/,
outputs in data/vps-0831/. Headlines, each detailed in the docs:

- **Venue truth**: 60s rule stable (tripwire = per-market Gamma
  `resolutionSource`, flipped bit-exactly at the 08-14 boundary — proposal:
  compare it at discovery, feed `_on_source_mismatch`); fees/tick/min-size
  unchanged; rewards pay our pattern exactly $0 (one-sided ≡ 0 at mid>0.90,
  min_size 50, max_spread 1.5-4.5¢) and the $1M program expires 08-31 —
  re-census after 09-01; CLOB had 3 order-placement outages 08-30/31 (one
  4.4h cancel-only) — paper cannot see placement halts.
- **Ceiling (r7)**: oracle (winner known, k≤25 arm, engine-true fills,
  volume-conserving) = **$382/day at $60**, decaying ~60% in capture
  efficiency by $2,000; the paper fill rule overstates capture above ~$150
  budget (linear forever) — fine at $60 (7% gap).
- **The lock excludes ~98% of the pie (r11)**: 0% of sampled deep sell volume
  arrives at ≥1.0× p99.5 (median 0.02-0.03×; 68% anti-side in 08-28..31);
  engine-true locked-sweep rate 4/4,047 armed. The honest ceiling of the
  deployed system is the locked slice: $5.6/day era, ~$3/day current supply.
- **Supply fell ~2.9× (r7)**: deep-sell value $2,651/day (08-21..27) →
  $923/day (08-28..30); coverage complete; concentration of sellers top-5
  48% → 76%; the six-pseudonym cluster (seabears/pinkypanda/porkypie12/
  grumbong/wundawally/spork30 — WALLETS.md) is ~40% of it, break-even
  inventory-flatteners, stationary across both weeks.
- **Frontier flat (r8, r9)**: need 1.0 → 4 fills/18d +$100.75 (0 fills
  08-28..30); 0.75 → 8 fills +$201.50, 100%, 0 flip-fills; 0.5 → 20 fills
  85% at the 0.80 rung; 1.25 → 4 fills (dollar diffs = sweep lottery);
  k_place [2,25] adds exactly 1 fill per need, both from the era's first
  hour on 08-13 — the k≥6 scar stands (#6 evidence updated). ANTI −$153k.
- **btc-15m in-window ladder REFUTED** (r10 → REFUTATIONS.md): $137/day
  total pie ex-outlier; #11 below is CLOSED.
- **One survivor**: the ≥28-day floor re-decision with a 0.75 arm under a
  bar pre-registered 08-31 — docs/research/proposal_floor_redecision_2026-09-11.md.
  Kill line pre-registered there: bar-completion > 120 days at trailing rates,
  or deep-sell value < ~$450/day trailing-7d → escalate as kill-market.

## 09-01 post-expiry check (the census the 08-31 charter scheduled)

- **Rewards did NOT expire**: the live window's `/rewards/markets/{cid}` shows
  `rewards_config[].rate_per_day: 10000` with `start_date 2026-09-01` — the
  program renewed/continued past its published "through August" window. (Field
  trap for the census notebook: the rate is under `rewards_config[].rate_per_day`;
  a `rates` key reads null on the same record.) No subsidy shakeout to expect.
- **All whole-window MMs present on 09-01** (48-window stride-4 sample,
  163,596 rows): 0x0cb, Bonereaper, hot-garbage, wqewqa, antsaslyku,
  AdanaKebab, x-MoneyForWhiskas, 1000monkeys, hdueilqhsdn, iR5oIct — 35-47 of
  48 windows each; 0.99-wall flow intact (BoneOhio 64k sh, 0x50f7 53k,
  ≥0.985 BUY total 504k sh in-sample). The six-pseudonym cluster is active
  all day (~2k rows each). No structural opening appeared.
- **Deep supply collapsed further**: winner-side deep sells (k∈[-60,25],
  px ≤ 0.80) = 158 sh / $43.94 ceded / 2 of 48 windows → **~$176/day pace,
  ~7% of the 08-21..27 level and beneath the pre-registered $450/day
  trailing-7d kill line's pace**. Watch the trailing mean this week; if it
  crosses, the escalation in proposal_floor_redecision_2026-09-11.md fires.
- **Deeper-rung replay (r13, engine-true, 09-11 evidence only)**: adding a
  0.15 rung at need 1.0 → +$140.62 vs +$100.75 on the same 4 fills (0.10 too
  → +$197.67); ANTI clean; ALL added dollars come from the single 08-24
  full sweep — N=1 sweep, recorded for the re-decision, not adopted.
- Spot books 13:00-13:30 ET ran thin mid-window (touch 8-520 sh) but a
  1.5 s-cadence probe through two closes shows the **0.99 wall building
  exactly on the 08-13 pattern**: stacking from k≈43s, 1.7k → 44k sh by the
  close, 135k sh post-close. The 0/102 time-priority refutation stands
  unchanged; the earlier thin snapshot was mid-build on a quiet window
  [data r14b_wallprobe.txt].

## 09-04 status read (first 2.8 days on floor 0.6 / six rungs / 25%)

- Realized: **0 fills in 781 windows** (616 ladders rested: 558 hold-expiry
  no-seller, 21 floor cancels "sign inside noise", 21 post-close unverified,
  16 cold); 0 CRITICAL/tracebacks; nightly push landing (09-02/03/04 auto
  commits on origin). At the replay rate (0.83/day) P(0 in 2.8 d) ≈ 10%.
- **Supply rebounded**: deep-sell ceded value 09-01 $1,126 / 09-02 $1,874 /
  09-03 $748 (coverage 198/287 on 09-03 — reconnect holes); trailing-7d mean
  **$1,007/day vs the $450 kill line** — not met. [data r23_supply_watch.json]
- **Why no fills despite supply (the r11 mechanism, now timed)**: 13 windows
  printed the winner < 0.50 in the resting span; all 13 first deep prints land
  at k = 14–25 s (i.e. at/before the placement window opens) in windows with
  |final − strike| $1–5 (two at $9–16). The ladder rested in 3 of them — armed
  **9–15 s AFTER the deep print** (13:09:43Z ep 1788267900 k=16 vs sweep k=24.8;
  19:39:51Z ep 1788377700 k=9 vs 23.0; 15:19:52Z ep 1788448500 k=7 vs 21.6).
  Sub-$5 gaps cannot clear 0.6 × p99.5(25) = $17 at k=25; the lock arrives
  after the dump by construction. Paper fill path is NOT at fault.
- **r24 k_max frontier at need 0.6 ($100, six rungs, re-fit tables)**: k_max
  25 → 15 fills +$621; 30 → 14 +$626; 40 → 17 +$647; 58 → 21 +$715 — all
  100% wins, **0 flip-fills at every k_max** (the 08-18 kinematics refutation
  ran on the thin frozen tables; the re-fit margins at k > 25 are 2–4× wider,
  so only decided windows arm early). Halves at 58: +618/+98. ANTI −$299k/−$323k.
  Need 1.0: k_max moves 4 → 5 fills. Recorded as evidence for the 09-11
  re-decision (k_place row); NOT applied — the marginal 6 fills are a
  different population from the observed misses, which no lock-gated k_max
  can reach.

## 09-01 operator-directed exploit pass (docs/research/exploit_pass_2026-09-01.md)

- **Floor frontier (r19), engine-true, 3 latency assumptions**: need 0.60 →
  15 fills / 18 d, 15/15 wins, +$311.25, 0 flip-fills, halves +293/+18,
  invariant under 300/500 ms GTC latency; 0.50 → 20 fills 85% at the 0.80
  rung, 2 flip-fills −$96; **≤ 0.35 negative or half-negative** (flip-race).
  0.60 is the defensible live floor (3.75× fills, 100% record, ~24-day bar
  clock). Post-hoc grid caveat recorded; satisfies the 08-31 0.75 bar's
  clauses. Deployment is an operator decision; not applied.
- **Stale-quote race on Binance tick data (r21)**: book reprices 79 ms
  median after a jump, competing takers at 42 ms, MTM EV negative at every
  achievable L → faster-info REFUTED by physics (REFUTATIONS.md entry
  updated with the modern numbers; the 0.33 s figure is obsolete).
- Queue position at $400: negative by arithmetic (+1¢/win vs −99¢/breach at
  a k≈43 s posting horizon; ~1,700 sh queue ahead, ~200 sh/window fills).

## 09-01 information program (operator-authorized Hard Rule 1 override)

Run from scratch the same day it was authorized: pre-registered design +
frozen bars in docs/research/info_program_2026-09-01.md, then Phase A
(window_paths era slice 2.5M rows, spot 1s klines with taker-buy flow,
perp 1m + OI metrics, CLOB print flow from tape) and Phase B (walk-forward
logit + GBT vs the book) in one pass. **B1 FAIL at all five horizons —
the book beats both models on OOS log score with one-sided bootstrap mass;
B2 FAIL (−2.3..−3.2¢/sh; the one positive cell fails monotonicity via a
positive control bucket). Program closed by its own kill clause; the
entry-side closure in REFUTATIONS.md is re-affirmed with 60s-era,
new-external-data evidence.** Scripts r15-r17; artifacts info_dataset.parquet,
r17_report.json. Data debt noted: window_paths `binance_cvd_*` are
NULL-by-design (dead-feed columns) — any future flow features must come
from external data, as here.

## 08-27 optimization pass — pre-registered bars (written BEFORE measurement)

Corpus: 60s era 08-14..27 (~14 real-final days, the #2 unblock). Every
change below is re-measurement or a stricter filter, never a threshold
lowered to make a window fire. OOS = alternating-ET-day halves; ANTI-side
control mandatory. Engine-true only (ws2_ladder_replay conventions).

- **R1 table re-fit (RESEARCH.md #2, due)**: p99.5 = fitted p99.5 rounded up
  to $0.5 per knot on real-final windows; MAX = per-tick interval maxima
  ∪ synthetic (widen-only), rounded up to $1, monotone. Adopt as the frozen
  tables if reproducible from the repo. Then re-decide the floor (#1) with
  ws1_oos: 0.5 replaces 1.0 only on the #1 bar.
  **VERDICT 08-27: ADOPTED — every knot widens 2-4×** (r1_report.md; 3,695
  real-final windows, 15 ET days; the chain reproduces the 08-18 freeze
  16/16 on its own span). k=25 $8 → $28.5, k=12 $3.5 → $9, k=6 $1.5 → $4;
  MAX k=25 $24 → $100. LODO k=25 $27.5-29.9 on all 15 folds — regime, not
  a day: median |final−strike| ran $5-28/day on the freeze week and
  $43-106/day since. The frozen p99.5 was exceeded on 11.1% of k=25
  samples (design 0.5%); frozen MAX breached per-tick in 75 windows incl.
  one wrong-side max-tier lock (08-21 00:25Z). **Floor: need 1.0 STANDS;
  0.5 fails #1 clauses i (0.80 rung 84.2% vs 90%) and iv (a 0.5-only arm
  swept 4 rungs, −$18).** Consequence, stated plainly: re-fit × 1.0 still ARMS ~3,200 of 3,700
  windows but FILLS 4 in 14 days (4/4 wins, +$37.78 ws1_oos / +$100.75
  ws2 replay) — by the time the sign clears an honest floor the book has
  priced the winner and nobody sells into 0.80; the fill pocket is ~0.3
  windows/day in this regime; the shipped frozen × 1.0 took
  39 fills / 30 wins / +$22.14 on the same days with 9 sign flips and
  halves of +$68.80 / −$46.74 — a floor that fails 11% of the time, paid in
  the losses this week. Deployed 08-27 with a fresh validation_epoch.
- **R2 placement window**: k_place_max ∈ {15, 20, 25} at need 1.0. A tighter
  k_max is adopted only if EW/sh AND total dollars improve in BOTH halves,
  ANTI ≤ 0, and the fill count stays ≥ 70% of k=25's (a filter that removes
  the fills removes the record). Otherwise 25 stands.
  **VERDICT 08-27: 25 STANDS** (r23_tables.md): k15 and k20 both lose
  half B (k15 +$3 vs +$97.75; k20 +$15.46 vs +$97.75) — fails the
  both-halves clause at need 1.0 and again at 0.5.
- **R3 per-rung verdicts**: a rung is dropped from the seed only if its own
  fills are net-negative in BOTH halves with ≥ 15 fills per half; budget
  re-weighting toward a rung only if that rung's win% ≥ price + 5pp in BOTH
  halves. Both must hold on the re-fit tables, not the frozen ones.
  **VERDICT 08-27: NO CHANGE — undecidable at honest tables** (re-fit ×
  1.0: 4/1/1/1/1 fills per rung, all wins; no rung reaches 15 fills per
  half). The FROZEN reference row is the autopsy of the past two weeks:
  0.80 rung 76.9% (be 80) −$18; 0.65 rung 52.9% (be 65) −$37.84; 0.50
  rung 36.4% (be 50) −$36; only 0.35/0.20 paid (+$18.86/+$132) — under
  a thin floor the mid rungs were adverse-selected bleeders and one deep
  rung carried the ledger. Descriptive, for the ≥28-day re-decision only:
  re-fit × need 0.75 = 8 fills, 100%, +$201.50, halves +$104/+$98, 0
  flips; re-fit × 0.5 = 19 fills, 84.2%, +$225.63 (0.80 rung 84.2% vs 85
  be+5). Eight fills decide nothing — recorded, not adopted. Fills nest
  across needs (1.0 ⊂ 0.75 ⊂ 0.5) and every need's dollars are the
  full-sweep windows where the winner printed below $0.20 while the ladder
  rested (tape-verified); fill-on-flip lost 100% of the time it occurred
  (2/2 at 0.5, 5/5 frozen). At current rates the ≥20-fill paper bar needs
  ~74 days at need 1.0 (~38 at 0.75, ~16 at 0.5) — a bar clock, not a
  reason to lower the floor. Tape
  coverage caveat: reconnect holes on 08-19/21/26 (242-250 of 288 windows
  with prints) under-count fills for every row.
- **R4 Candidate A (cushion dip-buyer)**: bar as written in WALLETS.md —
  both-sides rungs 0.10-0.35, no sign filter; win% ≥ price + 8pp per rung
  at ≥ 10 fills, positive dollars in two disjoint 3-day splits, and the
  sign-gated variant must not dominate (else it is a deep_proj config).
  **VERDICT 08-27: REFUTED** (r4_report.md; 3,787 windows, every variant:
  6-rung and 3-rung bands, k_place [6,25]/[6,60]/[6,120]). Every rung wins
  LESS than its own price (0.35 rung 22% vs 43% needed; 0.10 rung 4.5%);
  −$1,852 at k[6,25], −$11,844 at k[6,120]; 0 of 11 three-day blocks
  positive. Clause 3 fires both ways: the projection-favoured slice is the
  only money (+$245 on 20 fills vs −$2,176 on 739 anti-projection fills) —
  what survives IS deep_proj. The triplet's real seat is the k>60 touch-bid
  queue wall (H1), unreachable print-through. Method note: unconditional
  both-sides resting crosses dead tokens at k≤25 (0.01/0.99 books) — a
  resting rule (rung strictly below the token's last print) was required;
  the unconditional −$77k row is kept as the record.
- **R5 field + census**: weekly census (#10, due) over 08-21..27; web scan
  for Polymarket rule/fee/reward changes. Findings are context — nothing
  here changes config without one of R1-R4 passing.

## 08-21 charter — pre-registered bars (written BEFORE measurement)

Corpus: the full 60s era (08-14..21, ~8 ET days of tape/micro/window_paths,
pulled to `scripts/research/data/vps-0821/`). Dollar bars anchor at the $400
go-live bankroll. At most TWO surviving mechanisms get proposed for
implementation; the rest rank here. Verdicts land in this file the same
session.

- **H1 market P&L decomposition**: a flow pocket qualifies only if (i) total
  systematic flow ≥ $200/day, (ii) the capturing position is one we can
  occupy — no shared-price queue seniority (08-13 live probe is binding) and
  no sub-410ms reaction requirement, (iii) our capturable slice ≥ $10/day at
  $400 under print-through fill physics, (iv) it persists in both era halves
  (08-14..17 vs 08-18..21) and survives a shuffled-window control.
  **VERDICT 08-21** (h1_report.md; 3.32M prints, fee_bps 0 on all; makers as
  a class net −$7.8k/day on $6.62M/day notional — every day-stable pocket is
  bid-side; pipeline reproduced all three known pockets, incl. our own seat:
  signal-free deep bids k∈(6,25] are worth −$100/day — the edge is entirely
  the projection):
  - **STAGED (the one proposal): 0.85-0.95 rungs on the deep_proj ladder,
    k∈[6,25]** — winner-side maker capture on 0.8-1.0 bids is +$1,423/day,
    positive 8/8 days, both halves, shuffle-destroyed (outcome edge — the
    projection supplies it), print-through physics, ~$18/day per rung at
    $400. Staging condition BEFORE any settings change: ws2_ladder_replay
    engine-true over the full era with the extended rungs — the flip-race is
    the killer at high prices (a 0.95 rung's whole margin is 5%), so the
    replay's fill-on-flip accounting decides, then the normal §2 paper bar.
    Warning prior: 1723 ran −5.9¢/sh at 0.95 pre-rule.
    Pre-registered replay bar (written before the run): each NEW rung needs
    ≥10 fills, win% ≥ its price + 5pp, and positive dollars over the era;
    the extended ladder's total dollars must be ≥ the baseline ladder's;
    the ANTI-side extended ladder must be ≤ 0. Rungs judge individually —
    a failing rung dies alone; all three failing kills the proposal.
    **REPLAY VERDICT 08-21: KILLED — all three rungs fail individually**
    (h1b_extended_rungs.md; engine-true, 2,066 windows / 1,827 armed, $60
    go-live budget): 0.95 → 39 fills 92.3% (needs 100) −$8.62; 0.90 → 24
    fills 87.5% (needs 95) −$5.00; 0.85 → 17 fills 82.4% (needs 90) −$3.97.
    Extended total < baseline at both budgets; ANTI −$75.9k (the projection
    is real — the high rungs lose anyway). Mechanism confirmed: sign
    accuracy 99.8% on armed windows, but win% AT FILLS pins to the rung
    price — the fill channel adversely selects the sign failures; all three
    rungs' losses are the same 3 flip windows placed at k≈24.4-24.7, and one
    95¢ loss erases 19 wins. Re-decision evidence for #1: the baseline 0.80
    rung ran exactly break-even (80.0% on fills) on this corpus — the
    price-margin geometry, not the sign, is what pays. Also structural: at
    the $150 paper bankroll an 8-rung split ($2.81/rung) starves every rung
    ≥0.65 under the 5-share exchange minimum.
  - REFUTED — mid-window bid wall ($11.9k/day, k>60, the market's largest
    pocket): same-book touch bids behind 2.3-8.3k-share shared-price queues
    (the exact 0/102 live refutation; symmetric-MM ban); top-5 wallets take
    78%, plausibly reward-farming (makers class-negative in h1).
  - REFUTED — terminal lottery counterflow ($854/day, final 6s): 98.9% of
    the capture is the winner-book 0.99 wall matched cross-book via the mint
    adapter — joining a refuted shared-price wall + a k<6 certainty claim
    (unpinnable floor); the same seat at k∈(6,25] nets −$165/day.
  - REFUTED — underdog-ask longshot tax (+$14.1k/day mean at 0.0-0.2):
    sign-flips daily (−$32k on 08-17) — not systematic; needs pair-minting.
  - BLOCKED — post-close bands: the tape unsubscribes p50 +12s after close;
    unblock = a post-close collector (occupation refuted regardless).
  - Calibration: k>25 lock bids reproduced their refutation (incumbents ate
    −$3.3k/−$6.3k flip-race days).
- **H2 cross-window seam** (adjacent refutation: open head-start, 30s era —
  any contradicting evidence routes through this file, never straight to
  code): N's projection as N+1's strike, traded against N+1's opening book.
  Bar: ≥ 300 windows with an executable opening ask, EW ≥ +5¢/sh after taker
  fee at ≥ $5 FOK size, net ¢/sh monotone across model-edge buckets with an
  edge<0 control, anti-side ≤ 0, ≥ $10/day at $400.
  **VERDICT 08-21: REFUTED** (h2_report.md, 1,972 windows; chain invariant
  verified 1,956/1,956). Held-out EW −2.1..−4.4¢/sh at every δ ∈ [2,30]s —
  the favored side wins 56-64% but its ask (0.57-0.63) already exceeds
  breakeven: the book prices the incoming strike within 2s of open.
  Monotonicity fails at all δ; the edge<0 control cell wins MOST at 3 of 5 δ
  (the 30s-era anti-predictive failure mode, reproduced). Larger |d| loses
  more. Pre-open leak NOT MEASURABLE: the recorder subscribes N+1's tokens
  at open (1/1,972 windows with pre-open BBO; 2 boundary-jitter prints);
  unblock = subscribe N+1 ≥60s early in the micro-tape, re-measure at ≥14d.
- **H3 complement structure**: (a) pure arb: Up-ask + Down-ask < 1 − both
  legs' taker fees at ≥ $2/leg executable on event-true books; bar: ≥ 1
  event/day AND ≥ $5/day at depth. (b) cheaper-route: 1 − bid_down vs ask_up
  at our ladder arm times; bar: ≥ 1 tick median improvement on ≥ 20% of
  arms. Control for both: ±30s time-shuffled books must produce ≈ zero.
  **VERDICT 08-21: BOTH REFUTED** (h3_report.md). (a) zero post-fee
  violations in 973,302 synchronized ask pairs; event-true violations are
  book-update transients only — 949 events / 8 days, median < 1ms, max
  21ms, total violating time 0.14s, $0.00/day realizable. The ±30s control
  manufactures 6.8-8.9× the event-true rate — any arb this scanner "finds"
  on unsynchronized books is staleness. (b) median route improvement
  $0.0000; ≥1 tick on 0.01% of arm-seconds (bar: 20%); parity is enforced
  tick-tight (47.7% of prints have a complementary-priced print within
  ±0.5s, 10-20× the shifted base rate). Route choice is illusory.
  Fill-realism side-finding: unmirrored complement-BUY prints at
  rung-compatible prices bound paper's invisible-fill undercount at ≤14-17%
  of deep flow — CONSERVATIVE direction, unconfirmed as real (all 5 era
  live fills had own-token prints); unblock = reconcile the next live
  ladder session's fills against own-token tape prints.
- **H4 sell-at-certainty inventory** (exit refutation adjacent — different
  mechanism: boundary-verified certain winners only, never spot-lens): bar:
  net expected ≥ +$5/day at $400 (freed capital × next-ladder-arm
  probability × ladder EW, minus haircut), p95 haircut ≤ 2¢/sh incl. taker
  fee, ≥ 100 boundary-certain windows, rule keys on `certain_winner` only.
  **VERDICT 08-21: REFUTED on timing** (h4_report.md). Auto-redeem credits
  winners at p50 +100s / p90 110-130s after close (5,736 on-chain REDEEM
  rows, 6 wallets) vs the close+275s redeploy deadline — capital is back
  before the next ladder arms. The haircut itself passes (0.99 exec p50,
  1.07¢/sh incl. fee, depth ample), but per freed dollar it costs 1.07% vs
  a 0.19% benefit (5.5×, bankroll-invariant): net −$0.75/day at $400.
  Reopen only if redeem p50 degrades past 275s AND ladder EW/$ × P(arm)
  exceeds ~1.1% — both, EW noise alone cannot close a 5.5× gap.

## Latency: the stop line (08-21, do not gold-plate past this)

Measured from every stamped fill in existence (36 live fills, 08-05..13;
report in the session data dir): owned compute — wake→eval + positions +
tick + context — runs ~3ms p50 / ~5ms p99 on current code. End-to-end
report-rx→submit p50 ≈ 1.05s decomposes as raw-report age at decision
(~1s: the raw stream ticks ~1Hz and fires wake on book events between
reports) + owned ~5ms + sign 4.3ms + POST ~303ms (itode ~250ms policy
floor inside it at measurement time; Polymarket cut that hold to 50ms on
08-17 — the floor moved, nothing we own did). Every remaining millisecond
we own is already bought;
the residuals are Polymarket's deliberate taker hold, Chainlink delivery
(1.6-1.8s, REFUTATIONS.md: we are not the fast participant), and the raw
cadence itself. The 25ms owned budget + WARNING regression guard the
floor; further latency engineering buys nothing measurable — stop here.

## 08-18 recalibration audit — closed items (evidence in place)

- **WS0 boundary-trust clock**: payload-ts by construction (rx never enters
  `_record_boundary`); CLAUDE.md §1 now states it. All post-migration paper
  evidence is clock-clean.
- **WS2 regime stack**: HOSTILE thresholds percentile-ported (photo band
  $2→$1, p50 floor $8→$6; massacre days still flag under both). Gate stays
  alert-only (REFUTATIONS.md — target population empty, second
  confirmation). Kill rule gained the ≥5-fills sparsity guard (measured
  false-trip on the engine-true series).
- **WS4 k>25**: REFUTED with kinematics (REFUTATIONS.md) — sweeps are
  same-second avalanches; flip-race loss probability exceeds every rung's
  price margin; [6,58] never beats [6,25] OOS.
- **Fees re-verified post-rule (08-18)**: maker zero-fee exact on 274/274
  USDC deltas; taker curve at fee/model median 1.000 on 326 rows. The
  ladder's fee-free economics stand.
- **RAW_GAP_MAX_S re-derived on 60s-era data**: conditional p99.5
  reconstruction error at gap ≤10s is $0.79 (inside the $1 k=2 knot); the
  danger cliff starts ≥15-30s (med $2.74, max $27 past 30s). 10s confirmed —
  no longer a carried 30s-era constant.
- **Reconstruction noise vs arming**: 23.9% of need-1.0 arms sit within the
  p90 target noise ($0.22) of the floor vs 31.9% within one knot-refit step
  ($0.5) — noise moves fewer decisions than re-fit granularity; the only
  noise-band fill was a +$34.40 win. No change.

## Frozen-measurement register

| constant | value | frozen | corpus / estimator | reopening condition |
|---|---|---|---|---|
| TWAP_MARGIN_P995/_MAX | signal_engine.py | 08-27 (re-fit) | 3,695 real-final windows (08-14..27) + 1,651 synthetic max-union; rx-clock ZOH + 10s coverage guard; MAX = per-tick interval maxima; LODO k=25 $27.5-29.9; freeze-span reproduction 16/16 | ≥28 real-final days, or any resolution-rule change (SOURCE gate red), or a nightly regime line sustaining gaps p50 < $10 for 7 days (the calm regime that fit the 08-18 knots) |
| ladder need | **0.6** (7 rungs incl. 0.15/0.10; budget frac **0.40** since 09-04) | 09-01 / 09-04 (operator directive) | r19 frontier on 18 real-final days: 0.6 → 15/15 wins, +$311, 0 flip-fills, invariant under 56/300/500 ms; 0.5 → 2 flip-fills; ≤0.35 negative. r22: +0.15 rung 2/2 (+$61 at $60). Adopted outside the ≥28-day schedule by explicit operator decision; the 08-27 1.0 verdict and its clauses stand on the record | the 09-11 ws1_oos re-run judges 0.6 on the same clauses; any flip-fill loss at 0.6 re-opens the floor immediately |
| k_place **[6,58]** | settings | **09-04 (operator directive)** | r24 on the re-fit tables at need 0.6: k_max 25/30/40/58 → 15/14/17/21 fills, all 100%, **0 flip-fills at every k_max**, ANTI −$300k+; the 08-18 kinematics refutation ran on the thin frozen tables (k=25 $8 vs $28.5 now) — wider margins mean only decided windows arm early | any flip-fill loss at k>25 re-opens the row immediately; 09-11 ws1_oos re-run judges it |
| taker_enabled | false (dormant) | 08-18 | 4 dips / 1 reachable / 1,184 windows vs ≥1-per-3-days bar | queue #3 re-arm condition |
| HOSTILE thresholds | p50<$6, photo<$1 >15% | 08-18 | percentile-ported from 30s-era positions; 1,186 60s windows | regime distribution shift (nightly line drifting) |
| kill-rule sparsity guard | ≥5 fills in trailing 4d | 08-18 | measured false-trip on the engine-true series | fills/day regime change making 5 too strict/loose |
| AT_PRICE_QUEUE_SH | 135 sh | 08-17 | live book watch (49 windows) vs sweep-consumed med 31 (11 days, stable); nightly ops watch alarms at p75 > 135 | live at-price fills paper refuses to credit |
| RAW_GAP_MAX_S | 10s | 08-18 (re-derived) | conditional p99.5 err $0.79 at gap≤10; cliff ≥15-30s | 60s-era hole population change |
| twap_k_min_s | 6.0 | 08-12 scar (30s era) | k=1.1 realized max-tier breach | queue #6 above |
| bz relay lag | p50 0.421s | 08-18 | 74,184 bz records rx−ts | new relay behavior |
| GTC/taker latency tables | paper_trader | GTC re-measured 08-28 (n=12 idle: place p50 57 / cancel p50 55 ms) and the paper table re-derived from those samples 08-31 (the old 08-07 table tripped the KS watch on a ~1 ms shift); POST table = 07-08 ledger (n=20), no re-derivation code | nightly ops watch: POST p50 ±25%, GTC p50 ±25% + KS D ≤ 0.30 | POST table reopened 08-27 (taker hold 250→50 ms on 08-17) — only live taker POSTs add samples; GTC in-anger stamps from the first live ladder |
| kelly_fraction 0.08, maker_bankroll_frac 0.15 | settings | pre-era | post-gate playbook | after a §2 bar pass |
| fee model 0.07 / 0.0175 | base.py | 07-22, re-verified 08-18 post-rule | 1,751 live fills + 600 post-rule USDC deltas vs documented curve | Polymarket fee change |

## Tooling

Everything lives in `scripts/research/` (see its README for the run order and
the `data/` layout): `ws1_reduce.py` (micro-tape → per-window streams),
`ws1_measure60.py` (error tables, all estimators), `ws1_freeze_tables.py`,
`ws1_interval_max.py` (per-tick MAX knots — the shipped convention),
`ws1_boundary_autopsy.py` (which stream instant equals the served final — run
FIRST on any mechanism alarm), `ws1_oos.py` (walk-forward/LODO floor
validation — the ≥14-day re-decision runs through this), `ws2_ladder_replay.py`
(engine-true replay, parametrized tables/needs/k/eps + ANTI controls),
`ws2_regime.py` (threshold porting + kill-rule sim), `ws2_supply*.py`
(panic-supply attribution), `ws3_dips.py` + `ws3_books_reduce.py` (taker dip
supply — the re-arm read), `ws4_k25.py` (sweep kinematics + flip race),
`ws3_census.py` / `ws3_behavior.py` / `ws3_queue.py` (WALLETS.md),
`klines_download.py` /
`pm_trades_download.py` (Binance 1s mirror; data-api both-counterparty pull,
offset caps at 3,500 rows/window).
