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
   until n ≥ 10). Still open until real samples exist: run
   `smoke_gtc_test.py --confirm --samples 12` on the box, or wait for the
   first live ladder. Until then the ≥20-fill bar is measured against an
   unvalidated fill clock.
3. **Lock-dip taker: DORMANT-pending-regime** (`taker_enabled: false`,
   staged 08-18). The 60s rule killed its supply: 4 winner-side max-lock
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
   1723's ledger). An adder after a bar passes, never a strategy.
10. **Census cadence** — re-run WALLETS.md census weekly during validation
    and after any regime break; the 08-14 leaderboard reshuffle was visible
    in one day of counterparty data.
11. **Sibling markets' in-window deep flow** — unmeasured, low priority.
    What IS refuted is post-close camping on the siblings (30s era). A
    measurement would mirror ws2_supply over sibling tapes; nothing licenses
    it before btc-5m's own paper bar passes.

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
floor inside it). Every remaining millisecond we own is already bought;
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
| TWAP_MARGIN_P995/_MAX | signal_engine.py | 08-18 | 970 real-final windows (08-14..17) + 1,651 synthetic max-union; rx-clock ZOH + 10s coverage guard; MAX = per-tick interval maxima | ≥14 real-final days, or any resolution-rule change (SOURCE gate red) |
| ladder need | 1.0 (interim) | 08-18 | walk-forward audit: 0.5 unvalidatable OOS at 17 fills; evidence both ways in queue #1 | ≥14 real-final days → ws1_oos re-run decides |
| k_place [6,25] | settings | 08-18 | k>25 REFUTED by kinematics (REFUTATIONS.md) | a mechanism that prices avalanche sweeps (Candidate A), never extension of the sign-gated ladder |
| taker_enabled | false (dormant) | 08-18 | 4 dips / 1 reachable / 1,184 windows vs ≥1-per-3-days bar | queue #3 re-arm condition |
| HOSTILE thresholds | p50<$6, photo<$1 >15% | 08-18 | percentile-ported from 30s-era positions; 1,186 60s windows | regime distribution shift (nightly line drifting) |
| kill-rule sparsity guard | ≥5 fills in trailing 4d | 08-18 | measured false-trip on the engine-true series | fills/day regime change making 5 too strict/loose |
| AT_PRICE_QUEUE_SH | 135 sh | 08-17 | live book watch (49 windows) vs sweep-consumed med 31 (11 days, stable); nightly ops watch alarms at p75 > 135 | live at-price fills paper refuses to credit |
| RAW_GAP_MAX_S | 10s | 08-18 (re-derived) | conditional p99.5 err $0.79 at gap≤10; cliff ≥15-30s | 60s-era hole population change |
| twap_k_min_s | 6.0 | 08-12 scar (30s era) | k=1.1 realized max-tier breach | queue #6 above |
| bz relay lag | p50 0.421s | 08-18 | 74,184 bz records rx−ts | new relay behavior |
| GTC/taker latency tables | paper_trader | 08-07..08 | box-measured; nightly ops watch alarms at ±25% POST p50 drift | any Polymarket pipeline change (smoke tests) |
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
