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
   11 days) while the book-resting watch grew 55→135. The shipped 135
   over-states typical queues → paper under-credits at-price fills →
   conservative, correct direction. The nightly ops watch now alarms when
   trailing p75 exceeds the constant (the unsafe direction). Reopen only if
   live deep fills land that paper refuses to credit (recalibrate from
   `filled_at_px` live/paper attribution, never from book snapshots).
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
