# WALLETS.md — the market's living answer key

Census of every btc-updown-5m counterparty since the TWAP era opened
(2026-08-07). Source: data-api `/trades?takerOnly=false` over 2,517 windows
(~4.9M rows, both counterparties per print; 1,989 heavy windows truncated at
the API's 3,500-row ceiling — oldest/earliest-window rows drop first, so
late-window numbers are near-complete). P&L = cash flow + terminal payout from
window_labels. Era split at the 60s-rule cutover (2026-08-14 00:00 UTC).
Re-run: `scratchpad ws3_census.py` / `ws3_behavior.py` (session 08-18);
scripts preserved in RESEARCH.md's tooling note.

## Top wallets by realized P&L (08-07..08-18)

| wallet | name | P&L | pre-60s | post-60s | maker% | windows | median buy px | fill-k med | mechanism read |
|---|---|---|---|---|---|---|---|---|---|
| 0x251c1a28… | 0xAAAAA | **+$108,019** | +62,469 | **+45,550** | 90% | 2,419 | 0.62→0.65 | 134→162s | whole-window informed maker; 65-67% win at ~0.65; survived the rule change intact |
| 0x568b0798… | — | +$51,138 | +39,586 | +11,552 | 100% | 2,339 | 0.66→0.64 | 132→170s | same family of whole-window making, 67-69% win |
| 0x3725d52f… | almach | +$41,589 | +22,225 | +19,364 | 99% | 1,765 | 0.27→0.25 | 57→110s | cheap-side cushion buyer (see Candidate A) |
| 0x0cb03848… | — | +$40,020 | +46,439 | **−6,419** | 95% | 2,218 | 0.35→0.30 | 180s | two-sided flow (49,885 sells); rule-change CASUALTY — win% fell 45%→30% |
| 0xc2ad03f7… | bosona | +$38,765 | +17,794 | +20,972 | 99% | 1,766 | 0.28→0.26 | 60→104s | cushion buyer, same operator profile as almach/mo-money |
| 0x32ed2e54… | mo-money | +$35,695 | +16,926 | +18,769 | 99% | 1,834 | 0.30→0.25 | 65→98s | third of the triplet |
| 0xfc369971… | gesinimen | +$26,314 | 0 | **+26,314** | ~0%* | 577 | 0.74 | 119s | **born at the rule change**; 74% win at 0.74 median, prefers CONTESTED windows (gap-med $6.1); sign-match 65% — information we don't have. *Census's 82% maker read was a tx-group artifact: its 300 newest /activity rows are ALL fee-paying taker BUYs (USDC-delta verified 08-18) — it pays full taker fees and still nets $5.7k/day |
| 0xce50c96b… | honey-spot | +$26,274 | +21,007 | +5,267 | 93% | 190 | 0.74→0.76 | 117→135s | selective mid-window, 73-74% win |
| 0xe0229e10… | JetFadil | +$25,655 | +16,055 | +9,600 | 91% | 2,284 | 0.50 | 146→168s | whole-window maker |
| 0x48ac40fc… | BoneOhio | +$24,383 | +20,328 | +4,054 | 93% | 1,615 | 0.99 | — | the 0.99-wall camper (the lane we refuted live); still alive, 5× smaller post-rule |
| 0x3b840769… | (profile "0x0a2c53bd…") = **1723** | +$16,884 | +16,257 | **+626** | 98% | 308 | 0.69 | 14→32s | the deep_proj reference wallet — see drift section |

## The 08-14 rule change reshuffled the leaderboard

- **Casualties**: 1723 (+$2.3k/day → +$136/day, 20×), 0x0cb038… (+$46k → −$6.4k).
- **Born with the new rule**: gesinimen (+$5.7k/day from day one — arrived
  knowing, or adapted within hours).
- **Rule-agnostic**: 0xAAAAA and the cushion triplet kept earning through the
  switch — their mechanisms don't depend on the resolution window's length.

## 1723 drift vs the frozen 08-14 snapshot

The deep_proj geometry was frozen from 1723's 08-14 fill distribution. Since:

| dimension | frozen snapshot (30s era) | current (60s era) |
|---|---|---|
| fill-k (q10/50/90) | 0 / 14 / 27s | 6 / 32 / 57s — **it re-scaled its zone to the doubled window within days** |
| buy price (q25/50/75) | 0.48 / 0.69 / 0.81 | 0.48 / 0.69 / 0.74 — geometry unchanged |
| one-sided windows | 84% | 83% |
| sign-match vs our projection | 86% (win 91% agreeing) | 82% (win 80% agreeing) |
| post-close fill share | 7% | 1% |
| realized P&L | ~$84/window | **~$5.7/window (≈ breakeven)** |

Verdict: 1723 adapted its **timing** correctly and its **economics still
collapsed** — the winner-side deep panic it harvested is largely priced out
under the 60s rule (a 60s average makes late spot whipsaws matter less, so
the book panics less at exactly the moments 1723 monetised). **Stop treating
the 08-14 snapshot as an answer key.** deep_proj's own engine-true record
under the corrected rule (08-18 grid) is now the leg's evidence base, not
1723's ledger.

## Candidate mechanisms (observed in profitable wallets — NOT implemented)

**Candidate A — cushion dip-buyer (the almach/bosona/mo-money triplet).**
Edge statement: panic prints below ~0.30 in the final ~2 minutes win ~12pp
more than their price implies, on EITHER side, with no projection filter —
the triplet's buys run 37-43% win at 0.25-0.30 average, ~$20k/wallet/11d,
and the mechanism carried through the rule change unchanged.
Pre-registered bar: engine-true replay (strictly-below fills, both-sides
rungs 0.10-0.35, no sign filter) over ≥7 60s-rule days must show win% ≥
price+8pp on every rung with ≥10 fills, positive dollars in each of two
disjoint 3-day splits, AND the projection-signed variant must not dominate it
(if sign-gating strictly improves it, it collapses into deep_proj and ships
as a deep_proj config, not a new leg).

**Candidate B — whole-window informed making (0xAAAAA / gesinimen).**
Edge statement: mid-window fills at 0.55-0.85 winning 65-79% — information
about the unresolved outcome that our projection cannot supply (sign-match
19-65%; most fills are outside the averaging zone entirely). This is
entry-side outcome prediction — **Hard Rule 1 territory, out of mandate**.
Recorded so nobody re-derives it as "a new idea": the only sanctioned road in
would be discovering that its edge is mechanical (rebate structure, spread
capture, cross-market inventory) rather than informational. Its scale
(+$45k/11d on $1.5M notional) against measured rebate rates (~0.4%/day of
maker notional ≈ a tenth of its P&L) says informational. Do not build.

## 08-21 H1 decomposition notes (full-era, our own tape)

- Makers as a CLASS net −$7.8k/day post-rule ($6.62M/day notional): ask-side
  makers pay −$24.8k/day to informed buyers; bid-side collects +$17.0k/day
  from wrong-way sellers. Every day-stable pocket is bid-side.
- The mid-window touch-bid wall ($11.9k/day capture, k>60) is 78% five
  wallets: a pseudonym cluster + BoneOhio + the almach triplet — behind
  2.3-8.3k-share shared-price queues, plausibly reward-farming given the
  class-negative first half. Not occupiable (the 0/102 live probe binds).
- Uncensused wall-scale wallet `0x6fc44EC445D73c…` took 36% of the sampled
  terminal counterflow — add at the next census run.
- The 0.99 wall's harvest includes ~$854/day of PRE-close lottery flow
  matched cross-book via the mint adapter — invisible to post-close-only
  reads; a post-close-camping read understates that wallet class's income.

## 08-27 weekly census (08-21..27, 159 sampled windows, r5_report.md)

- **0xAAAAA and JetFadil both STOPPED at 2026-08-20 15:43Z — the identical
  minute, zero rows since**: one operator, two pseudonyms. gesinimen
  collapsed to ~$2k/day notional. 0x0cb03848 (the 08-18 "casualty") is the
  week's top earner again as a two-sided MM (~18% of market notional).
  Cushion triplet and BoneOhio unchanged; 1723 at break-even (−$192).
- **Our seat** (winner-side 0.65-0.80 bids, k∈[6,25]): ~123 sh/window, 78%
  win across all occupants, +$1,063 in the sample — small and near
  break-even at the 0.80 end, positive at 0.65-0.75. One deep_proj
  look-alike (0x44832d0d: 326 fills, 66% win, px 0.74-0.80, k med 32s)
  earns; two look-alikes lose. No new occupant.
- **0x6fc44… confirmed** (Caring-Kingfish): 100% BUY at 0.99 straddling the
  close, ~$55k/day notional, 61% btc-5m — a pure winner-wall buyer.
- Field (sourced, r5_report.md): taker delay 250→50ms since 08-17; RTDS
  serves the 30s stream again; CLOB maintenance degraded trading 08-25/26
  04:00-07:30 UTC (inside our day); fees/tick/min-size/rewards unchanged.
- Coverage caveat: 142/159 windows hit the 3,500-row cap (oldest surviving
  row k≈218s) — whole-window makers under-counted, late window complete.

## 08-31 weekly census (08-28..31, 87 sampled windows, r6_census_0831.json)

- **Leaderboard**: 0x0cb03848 still #1 (+$5,864 sample, two-sided whole-window
  MM); new whole-window names AdanaKebab / x-MoneyForWhiskas / 1000monkeys /
  trinity42; Bonereaper and antsaslyku persist; BoneOhio +$1,549, positive
  every day; 1723 negative (−$414); gesinimen small (+$382, 9 windows);
  0xAAAAA/JetFadil still zero rows since 08-20 15:43Z.
- **Our seat ran hot for its occupants**: 0x44832d0d (EZTRADENL4) 47 pocket
  fills at 98% win, 0x239e726f 34 at 97%, LuiaLeQuartier (a loser last week)
  9 at 84% — the pocket paid this week, on very few sweeps.
- **The deep supply is a persistent six-pseudonym cluster + churn**: seabears,
  pinkypanda, porkypie12, grumbong, wundawally, spork30 sell the winner token
  at px 0.55–0.67, k∈[11,24], in BOTH weekly samples — ≈40% of deep sell
  volume, each ≈ break-even overall (inventory flattening, not panic); their
  deep sells are maker-side asks, which still cross any higher resting bid.
  Top-5 seller concentration rose 48% → 76% as supply fell — single-operator
  risk on the supply side is now material [data r6_census_{0821,0831}.json].
- **Deep supply fell ~2.9×**: winner-side deep sell value in the resting span
  $2,651/day (08-21..27, sweep-day outlier included) → $923/day (08-28..30);
  tape coverage complete on those days [data r7_supply_by_day.json].
- **Displacement read (r11)**: 0% of sampled deep sell volume arrives at
  ≥1.0× the re-fit p99.5 — median sell at 0.02–0.03 margin multiples; in
  08-28..31, 68% of ceded value was anti-side (reversal windows). The
  lock-gated ladder is structurally outside ~98% of this flow.
- 09-01 re-check: the rewards program RENEWED into September ($10k/day on the
  live window; the rate sits under `rewards_config[].rate_per_day` — a `rates`
  key on the same record reads null, a new endpoint trap). All whole-window
  MMs present on 09-01; the 0.99 wall builds on the 08-13 pattern (1.7k → 44k
  sh by the close, 135k post-close; r14b probe); deep supply fell again —
  158 sh / $44 ceded across 48 sampled windows (~$176/day pace, ~7% of
  08-21..27) [data RESEARCH.md 09-01 note; r12_pm_trades].

## Standing discipline

Re-run the census after every regime shift and at least weekly during any
paper validation (the 08-14 leaderboard reshuffle was visible in ONE day of
data — faster than any of our own ledgers could tell us the world changed).
