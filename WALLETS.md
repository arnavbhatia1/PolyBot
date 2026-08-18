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
| 0xfc369971… | gesinimen | +$26,314 | 0 | **+26,314** | 82% | 577 | 0.74 | 119s | **born at the rule change**; 74% win at 0.74 median, prefers CONTESTED windows (gap-med $6.1); sign-match 65% — information we don't have |
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

## Standing discipline

Re-run the census after every regime shift and at least weekly during any
paper validation (the 08-14 leaderboard reshuffle was visible in ONE day of
data — faster than any of our own ledgers could tell us the world changed).
