# Proposal — floor re-decision at ≥28 real-final days, with need 0.75 as a third arm

Status: pre-registered 2026-08-31, BEFORE the deciding run. Runs on or after
**2026-09-11** (≥28 real-final 60s-era days). This is the only Phase-2
candidate that survived the 08-31 charter's register and replay checks.
Nothing changes in config before the run; adopting any floor today would be
lowering a threshold to make trades fire (RESEARCH.md preamble ban), and the
ladder-need register row's reopening condition is exactly this ≥28-day re-run.

## Mechanism (who loses and why they keep losing)

Same leg, same mechanism: sellers transact the eventual winner ≤ 0.80 in the
final 25 s; the projection identifies the winner with measured 100% sign on
armed windows (4,047/4,047, 18 days). The floor trades certainty for fill
rate: 1.0 → 4 fills/18d ($5.6/day), 0.75 → 8 fills/18d ($11.2/day, 100% win,
0 flip-fills, both OOS halves positive), 0.5 → 20 fills/18d (85% win at the
0.80 rung vs its 85 bar — marginal) [data r8_replay_out.txt].

## Register check

- Ladder need = 1.0 frozen 08-27; reopening condition "≥28 real-final days
  re-run" — this proposal IS that reopening, on schedule, not early.
- 0.75 was recorded 08-27 as descriptive evidence for this re-decision
  (r23: 8 fills 100%, halves +$104/+$98, 0 flips). Not a DEAD ENDS entry.
- Extended rungs, k>25, k<6, both-sides rungs: all separately
  refuted/scar-locked; this proposal touches only `maker_ladder.need`.

## Runnable replay spec

Corpus: all real-final windows 08-14 → run date (win_streams rebuilt via
`ws1_reduce.py`; tape complete). Script: `ws1_oos.py` walk-forward/LODO on the
then-current tables, arms {0.5, 0.75, 1.0} × k[6,25] × $60; plus
`ws2_ladder_replay.run` per-rung economics and ANTI controls, conventions
identical to r23/r8. Expected N at 09-11 (era rates): ~5-6 fills at 1.0,
~12-13 at 0.75, ~30 at 0.5; at the 08-28..30 supply regime roughly half that.

## Pre-registered thresholds (0.75 adoption bar; 0.5 keeps its standing #1 bar)

Adopt 0.75 over 1.0 only if ALL of:
1. every rung with ≥5 OOS fills wins ≥ its price + 10pp;
2. positive dollars in both alternating-ET-day halves;
3. ANTI ≤ 0;
4. the 0.75-only fill set (fills 1.0 did not take) contains zero flip-fill
   losses and is net ≥ $0;
5. out-of-fit sign flips on 0.75-armed windows ≤ the 1.0 arm's (currently 0).

If 0.5 passes its own standing bar it dominates (it nests 0.75). If neither
passes, 1.0 stands and the §2 paper bar keeps running.

## Days-to-validation and the kill line

At 0.75's era rate (0.44 fills/day) the ≥20-fill §2 bar needs ~45 more days;
at the current supply regime ~90. **Kill criterion (pre-registered):** if at
the re-decision date the chosen floor's projected bar-completion exceeds
120 days at the trailing-14-day fill rate, this leg cannot validate on any
reasonable horizon at $400 — escalate to the operator as a kill-market
decision rather than another floor adjustment. A second supply halving
(trailing-7-day deep-sell value < ~$450/day, i.e. half of 08-28..30) triggers
the same escalation early.

## How this is wrong

If the 08-24-style sweeps are a regime artifact of August volatility, the
0.75 record (whose dollars are 2 sweeps) evaporates in September and clause 2
or 4 fails — that is the desired behavior: the bar kills it. If supply
recovers (rewards-expiry shakeout reverses), the 1.0 clock shortens and the
whole question de-escalates.

## Scheduled alongside (same date, no new mandates)

- Weekly census + at-price queue re-read post rewards-expiry (09-01+): the
  135-sh constant and wall structure were measured in the subsidized regime.
- Regime-offensive pre-registration: corr(trailing-day gap p50, next-day
  deep-sell value) on ≥28 days — measurement only; alert-only stays.
- Margin-table re-fit falls due at ≥28 days per its own register row
  (the freeze-span reproduction chain in r1 is the convention).
