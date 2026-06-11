# Goal: Find the One Real Edge — or Prove It Isn't in the Data

## What we already know (do not re-litigate)

A prior session established, with day-clustered significance, that the entry model has no edge over `prob = market_price`:
- L1-only beats the full 7-layer stack (layers 2-6 net-hurt).
- L1-only loses to `market_price` at t = 2.23 (day-clustered).

The market already prices distance-to-strike, vol-scaling, autocorrelation, CLOB flow, and CVD. No entry-side probability built from the current features beats it. Tuning weights or adding gates cannot fix this. Stop trying to predict BTC better than the market with these inputs.

This goal is NOT "tune the model." It is: decompose where the money actually comes from, isolate the single real edge if one exists, build the simplest bot that exploits only that edge — or conclude no edge exists in the current data and specify exactly what new signal would create one.

"Goal complete, zero changes" is not an acceptable terminal state. The deliverable is either (a) a measurably better bot, or (b) a concrete, falsifiable research plan for a new edge with experiment designs. One of those two. Not a green scorecard.

## Sharpe is defined once, here, and not reinterpreted

Sharpe = the per-trade Sharpe exactly as `weight_optimizer.py` computes it in fold replay (the same number the adoption gate uses), on the trailing 200 trades. No annualization substitution. No daily-P&L Sharpe cited as a pass when the per-trade number fails. Report all conventions for context, but pass/fail is the per-trade fold-replay number. If it cannot be computed, the criterion is unmet — say so plainly.

Every P&L claim needs a day-clustered bootstrap CI. +$122 over 100 windows means nothing without the variance. Block-bootstrap whole days, 1000 iterations, report the 10th percentile. If the lower bound straddles zero, the bot is break-even with sizing variance, not proven profitable.

## Phase 1 — Decompose the P&L. Where does the money come from?

Before any new logic, attribute every dollar across all closed trades:
- Split P&L by exit type: hold-to-resolution vs scalp vs loss-cut. Which is net-positive?
- Split by whether the model agreed with the eventual winner. If the bot makes money on trades where the model was directionally wrong, the edge is not in the model.
- Bucket by entry edge (model_prob - entry_price). Do high-edge entries realize higher win rates, or is "monotonic calibration" the calibrator fitting noise? Test the slope's significance day-clustered.
- Total fees paid vs total gross P&L. A large ratio means the bot is churning.

Output one table: source of P&L, dollar contribution, day-clustered CI. This dictates what to keep and what to delete.

## Phase 2 — Conditional edge search

The model loses to market on average. Averages hide pockets. Re-run `l1_vs_market` segmented by:
- Regime (trend / mean-revert / quiet)
- Time-of-window (0-60s, 60-180s, 180-300s)
- ATR bucket (vol regime)
- Book depth and spread
- Distance-from-strike at entry

For each segment with >=30 trades, test model-vs-market day-clustered. If ANY segment shows the model beating market at t > 2 with gain spread across >=3 days (not one lucky day), that segment is your edge: gate all entries on it, delete the rest. If NO segment survives, the prediction approach is dead — proceed to Phase 3 without sentiment.

## Phase 3 — Microstructure mispricing (the likeliest real edge)

A small bot's edge on Polymarket is rarely "predict BTC." It is "the market price is mechanically wrong right now." The spec already names the raw material: negRisk cross-match returns phantom prices near expiry, books go stale, price_up + price_down drifts off 1.00. Test whether entering on price inconsistency rather than model edge is profitable:
- When price_up + price_down deviates from 1.00 beyond fees, is there a capturable cross-book trade?
- Near expiry, when one side is phantom-priced, does fading it pay?
- When one side's book is thin and stale (the freshness gate normally skips), is there a liquidity-provision premium?

None of this needs L1-L6. It needs the executable BBO, the spread, and the freshness signals — all already captured. Build a minimal `MispricingEngine` that ignores the BTC model entirely and enters only on internal price inconsistency. Backtest it against the same trade pool. If it beats the current bot, that is the architecture.

## Phase 4 — Verify the exit edge is real, not assumed

The prior session claimed "exits + Kelly are the profit engine," but Kelly cannot create edge — it only sizes one that already exists. Pull the counterfactual tracker: for every scalp, compare realized scalp P&L to what hold-to-resolution would have paid. Sum both. If scalps net-lose to holding, the exit logic is destroying value and the claim is false. If scalps net-win day-clustered, exits are a genuine edge — quantify it and protect it.

## Phase 5 — Build or specify (pick exactly one)

**(a) An edge was found.** Implement the simplest bot that exploits only it. Delete every layer, gate, and feature that Phases 1-4 showed doesn't contribute. A bot that does one thing with a proven edge beats a bot that does ten things with none. Prove the new bot clears the criteria below.

**(b) No edge in current data.** Specify concretely what new signal could create one, and why it is not already in the market price. Candidates worth designing experiments for: cross-venue lead-lag (does Coinbase or Binance lead the Chainlink resolution by enough ms to matter?), perp funding/basis as a directional tilt, options-implied vol vs your ATR estimate, order-book resilience and queue position. For each: a falsifiable experiment — what data, what test, what result confirms or kills it. Design the test first; do not implement speculatively.

## Hard rules

- Do not touch infra: feeds, execution, circuit breaker, DB, resolution logic, fee math. They work.
- Every improvement claim needs a day-clustered significance test. One-day gains are noise.
- One change per backtest. No batches until each passes alone.
- If backtest and paper disagree, stop and reconcile before trusting either.
- Calibration is downstream of edge. Do not fit a calibrator to a model with no edge.

## Done when

- A bot exists whose per-trade fold-replay Sharpe > 0.5 on the trailing >=200 trades, with the gain surviving a day-clustered bootstrap (10th-percentile CI above zero), OR
- A written verdict that no edge exists in the current feature set, backed by Phase 1-4 evidence, plus >=2 new-signal experiment designs ready to run.

The wrong outcome is a clean scorecard with zero changes and no new understanding. The right outcome is knowing exactly where your money comes from — or knowing, with proof, that it does not come from anywhere yet.

---

# FINDINGS (2026-06-10, fable_dev) — goal complete, terminal state (b)

Method: five independent analyses, each re-derived from scratch by an adversarial verifier
(scripts + verifiers in `scripts/goal_eval/`, raw results in `workflow_results.json`). Pool:
2,858 closed trades, 952 resolved ghosts, 2,503 deduped counterfactuals over 14 ET days
(2026-05-28..06-10). All pnl at the correct 0.07 fee; day = ET date; bootstrap = 1,000
day-resamples, seed 42; t = day-clustered throughout. Every number below survived verification;
where a first-pass number was corrected by the verifier, the corrected value is cited.

## Phase 1 — Where the money comes from

| source | n | $ | boot p10 | boot p90 | days positive |
|---|---|---|---|---|---|
| exit: resolution | 1,383 | **+3,772** | +2,846 | +4,800 | 14/14 |
| exit: scalp | 1,475 | **−2,911** | −3,731 | −2,129 | 0/14 |
| side won window | 1,743 | +4,964 | +3,785 | +6,299 | 14/14 |
| side lost window | 1,091 | −4,098 | −5,108 | −3,194 | 0/14 |
| TOTAL | 2,858 | **+862** | +229 | +1,460 | 9/14 |

- **Fees eat 59.7% of gross** (+$2,135 gross, $1,273 fees): the bot churns.
- Directionally-wrong trades cost −$4,098 — 4.8× the total profit.
- **Claimed entry edge does not realize**: per-day OLS slope of (won − price) on edge ≈ 0 under
  either edge definition (cal t_day −0.15/−0.38, raw −0.34/−0.52, 14 days); realized WR *falls*
  0.71→0.57 as claimed edge rises. The calibrator compresses; it does not rank new information.
- Read with Phase 4: scalp exits lose $ in aggregate because they fire on bad inventory — but vs
  holding that same inventory they ADD ~+$900. Both are true; the exit engine is loss mitigation.

## Phase 2 — Conditional edge search: NO SEGMENT SURVIVES

44 segment tests (regime × time-of-window × ATR tercile × depth/spread × distance-from-strike,
× {full-stack raw, re-derived L1-only}, n≥30, day-clustered): **zero survivors** of the bar
(t>2, ≥3 positive days). Overall, the market price beats the model *harder* than the prior
estimate: pool-A raw t=−3.66, L1 t=−3.10 (trades), pool-B −4.35/−3.77 (with ghosts; negative =
market better). Best pockets (trending_up n=29 t=0.81; volatile n=242 t=0.62) are noise at 44
tests, and all flip negative on per-trade weighting. **The prediction approach is dead in every
pocket large enough to measure.**

## Phase 3 — Microstructure mispricing: not supported; partly untestable (censored)

- **Cross-book sum**: across all 3,810 stamped decisions, ask_up+ask_down is hard-truncated to
  {1.01: 3,371, 1.02: 439} — zero below 0.98, zero buy-both arbs after fee, $0 locked. The
  [0.98,1.02] gate censors out-of-band moments *without recording them* (473,859 lifetime
  `stale_prices` tick-skips carry no magnitude/side) → out-of-band region untestable today (→ E1).
- **Near-expiry phantom fade: decisively dead.** Sides ≥0.85 with <90s left (n=407) win 95.1% vs
  price 0.955; fading nets **−0.49/$1, t=−4.35** (−0.67/$1, t=−4.51 at ≥0.90/<60s). Extreme late
  prices are calibrated to within 0.7pp — consistent with the deleted 226k-row tick study.
- **Thin/stale book premium: untestable as specified** — `depth_usd_top20` is *Binance BTC* depth
  (main.py → BinanceDepthFeed), not CLOB depth; no record carries CLOB depth (→ E2). On the
  Binance proxy: nothing (thinnest-quartile signed edge −0.0002, t=0.40).
- **MispricingEngine verdict: NO** on available data.

## Phase 4 — The exit edge is real; passes the bootstrap bar; weaker than previously claimed

Exact comparison (CF records carry both arms; always-hold arm is binary resolution, no replay
approximation): actual policy vs always-hold **delta +$897** (fee-corrected +$812 — pre-06-05 CF
arms still carry the 0.018 fee basis, exit-leg correction −$85.63 on 859 scalps):

- **t_day = 2.08** (1.91 fee-corrected), 11/14 days positive, best day 31.7% of delta,
  top-3 days = 88%. **Bootstrap p10 = +$376 (+$294 fee-corrected) > 0 → the goal's bar PASSES.**
- Composition: **ITM scalps (mp≥0.5) are the entire edge** (+$980, t 2.12); OTM scalps add
  nothing (−$83, t −0.50); profit-scalps +$760 (t 2.60), loss-scalps +$137 (t 0.53).
- Corrections to the prior session's claims: the "+$1,238 vs +$337 (~4-5 SE)" levels reproduce
  only WITHOUT dedupe — they double-counted +$1,204 of pnl from 298 duplicate CF records, and the
  honest day-clustered significance is **t≈2, not 4-5 SE**. The delta itself was robust
  (+$902 undeduped vs +$897 deduped) because the duplicates are hold-type (zero delta).
- **Root cause found and FIXED this session**: hold-CF records were keyed to the *previous*
  position on flip re-entries (`_hold_worst` keyed by market_id with no position guard) —
  explains the 298 duplicates and 355 outcomes with no CF. Fix in `counterfactual_tracker.py` +
  `main.py` call site, 4 regression tests, 609 pass. Verified non-bugs: entry-fee basis is
  consistent across arms (fee comes out of shares at entry — checked empirically both eras).

## Done-when criterion — computed, said plainly

- **Per-trade fold-replay Sharpe** (exact adoption-gate convention: `_kelly_bankroll_returns` on
  the trailing 200 trades, current live config incl. the L6 flow_disagreement 0.005 weight,
  production isotonic, `weighted_sharpe_from_returns`): **+0.247** (44/200 re-enter; identity
  calibrator: +0.062 on 197). Realized trailing-200: +0.023. **Bar >0.5: FAIL.**
- Realized-$ day-clustered bootstrap p10: **+$288 over the full 14-day pool (PASS >0)**;
  trailing-200 +$151 but spans only 3 ET days (too thin to lean on).
- Criterion (a) is unmet → the deliverable is **(b)**, below.

## Phase 5 — pick one: (b), with the simplification for (a) tested and rejected

**Simplification tested** (delete L2-L6 = L1-only, full adoption-replay, 3 pools × 2 calibrators):
NOT supported. z=+0.39 on real+ghosts/production-cal, but **z=−0.30 with day-t=−3.0 on the
trailing 200** (the current selective regime), and the sign flips with calibrator choice. (The
first-pass "deleting L2-L6 never hurts" was an artifact of a baseline missing the live L6 weight —
caught in verification.) The stack stays.

### VERDICT

**No new edge exists in the current feature set.** Entry-side forecasting has zero edge over the
market price in aggregate and in every segment (Phases 1-2). Microstructure mispricing is either
provably absent (phantom fade, in-band cross-book) or unrecorded (out-of-band, CLOB depth)
(Phase 3). The single real edge is the **exit engine** — ITM scalps fired off mid-window
repricing latency — worth ~+$900/14d over always-hold, day-clustered bootstrap p10 > 0, t≈2
(Phase 4). The current bot already exploits it; it fails the Sharpe>0.5 bar (+0.247). The money
comes from: −EV inventory acquisition at entry (fees eat 60% of gross), rescued by ITM scalp
exits and resolution holds on the winning side.

### New-signal experiment designs (ready to run)

**E1 — Out-of-band price-sum recorder (cross-book arb).** *Why it could exist unpriced:* not a
forecast — mechanically desynced books, proven to occur (473,859 gate skips) but never measured.
*Data:* at the price-sum gate fire (`main.py` `_fetch_market_prices`, the `stale_prices` skip),
append {ts, market, best bid/ask both sides, top-of-book sizes, sum} to a JSONL; no behavior
change; run 7 days. *Test:* distinct windows with ask_sum < 1 − 0.07·(p_u(1−p_u)+p_d(1−p_d)) and
both top sizes ≥$25; day-clustered frequency × margin. *Confirm:* ≥5 fillable opportunities/day
across ≥3 days at ≥0.5c/$1 net → build a buy-both sleeve. *Kill:* anything less → cross-book arb
closed permanently (in-band already proven $0).

**E2 — CLOB depth + book-age stamping (thin-book premium).** *Why unpriced:* tests whether the
CLOB's own liquidity state predicts its pricing error — market-quality, not BTC forecasting.
*Data:* stamp per-side CLOB top-5 USD depth + book age into `trade_context` for trades AND ghosts
(alongside the existing Binance depth stamp); run ≥10 days. *Test:* signed (won − price) and
|won − price| by CLOB-depth quartile × book-age bucket, day-clustered on the thinnest/stalest
cell. *Confirm:* t>2 across ≥3 days → liquidity-conditioned entry (maker sleeve is a separate
decision). *Kill:* |t|<2 → thin-book premium dead (Binance-proxy null stands).

**E3 — Latency features for exit refinement.** *Why ours:* the edge IS mid-window latency, and
the threshold family provably cannot separate right from wrong ITM scalps; a separating feature
might. *Data:* already recording — `cross_venue_gap`, `fast_realized_vol_60s` in CF aux context;
needs ~2-3 weeks of post-fix records (hold-CFs are trustworthy as of today's keying fix).
*Test:* split `scalp_was_optimal` by feature quartile; day-clustered delta_pnl spread between
extreme quartiles; then a feature-conditioned threshold through the symmetric replay + full
adoption gates. *Confirm:* separation t>2 across ≥3 days AND replay delta survives day-clustering
→ adopt via pipeline. *Kill:* no separation → exit boundary is final.

**E4 — Perp funding/basis tilt.** *Why possibly unpriced at 5 min:* funding cycles are 8h; a slow
basis tilt may not be arbed into 5-min binaries. *Data:* poll Binance perp mark/funding once per
window open; stamp into `trade_context`; run ≥14 days. *Test:* fit k in
sigmoid(logit(price) + k·(logit(model) − logit(price))) per basis-sign/magnitude bucket,
day-clustered. *Confirm:* k>0, t>2 in any bucket spread over ≥3 days → basis-gated entry tilt.
*Kill:* k=0 everywhere (the unconditional result) → entry forecasting stays dead.

### Hard-rule compliance

Infra untouched (feeds, execution, circuit breaker, DB, resolution, fee math). The one code
change (CF hold-record keying) is in the learning evidence stream, fixes a confirmed recording
bug, and protects the audit trail of the one validated edge; 609 tests pass. No parameter,
gate, or exit change shipped — every candidate tested failed its significance bar, and the
goal's own rules forbid shipping unproven changes.