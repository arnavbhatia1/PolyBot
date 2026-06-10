# PolyBot — what the edge actually is (and isn't)

Verdict from 12 days / 3,786 resolved decisions (2,834 trades + 952 ghosts,
scalp resolutions recovered via counterfactuals). Scripts: `scripts/diagnose_edge.py`,
`scripts/fit_anchor.py`, `scripts/check_sizing_value.py`, `scripts/sweep_exit_policy.py`.

## Q1 — Does the model have signal?

Ranking yes, absolute no. Realized WR rises monotonically with raw prob
(0.42 -> 0.78 across bins) but every bin is overconfident by 5-14pp, and the
edge **over the market price** is ~0 (gap_mkt -0.03..+0.04, mixed sign).

## Q3 — Is our vol-scaling better than the market's?

No. Market price beats the model as a predictor (Brier 0.224 vs 0.235,
log-loss 0.637 vs 0.669, both halves of the period). The optimal blend
`sigmoid(logit(price) + k*(logit(model) - logit(price)))` fits **k = 0**:
conditional on the market price, the L1-L6 stack adds nothing at entry.
Tuning `atr_sigma_ratio` & co. is tuning noise — the directive's suspicion
confirmed. Claimed-edge buckets realize ~0 everywhere (best band +1.4pp,
~1 SE, pre-fee).

## Q2 — Are the gates eating weak signal?

At entry, no: every gate's rejected ghost pool is negative-EV (sub_threshold
-0.17/share, edge_cap -0.70, cvd_decel -0.39...). At **adoption**, yes — but
the cause was a blind replay, not gate density (see fix below).

## Where the money actually comes from

- Always-hold-to-resolution would have made **+$337**; the live exit policy
  made **+$1,238** (oracle bound +$6,870). The exit engine IS the edge
  (~4-5 SE on the paired scalp-vs-hold comparison).
- Coherent mechanism: at entry (window start) the CLOB is sharp; mid-window
  the model re-prices off Coinbase ticks seconds before the CLOB does.
  **The edge is latency at exit, not forecasting at entry.**
- Sizing stack is NOT noise: realized gain_pct rises with size quartile
  (-3.6% Q1 -> +6.2% Q3). Keep Kelly + multipliers.
- Entries are inventory acquisition for the exit engine: ~fair minus fees,
  rescued by exits (even the 0.12-0.20 claimed-edge band netted +$256 real).

## The bug that mattered (fixed on fable_dev)

The pipeline probed exit_edge_threshold {-0.08, -0.07, -0.05, -0.03} and
rejected all with `backtest_delta_sharpe: 0.0` — exactly zero, because:

1. **One-directional replay**: a less-patient candidate fires strictly more
   often, so the scalp-only re-pricing path never engages -> delta == 0 ->
   auto-reject. Hold-type counterfactuals (hypothetical-scalp arm) existed on
   disk but were never indexed. Fixed: `_kelly_bankroll_returns` now re-decides
   both directions, respecting the threshold-independent live branches
   (whipsaw cushion, deep-loss-hold).
2. **Rollup-blind index**: `_load_counterfactual_index` skipped rollup arrays,
   and the nightly rollup runs before the optimizer — the replay pool silently
   shrank to the current day (~24 scalps instead of 1,451 + 1,350 holds).

With the branch-faithful symmetric replay, the sweep answers honestly:
-0.10 (current) +$1,120, -0.08 +$1,149, -0.04 +$754, -0.03 +$780.
**Current threshold is near-optimal — keep -0.10.** (An asymmetric replay
falsely showed +$1,293 at -0.04; the symmetric one caught it. The pipeline
can now see both directions and will keep rejecting them — correctly.)

## Standing conclusions

1. Don't spend cycles tuning entry-side weights expecting edge — k=0 says the
   ceiling vs the market is zero with the current feature set. The calibrator
   keeps probabilities honest; that's its whole job.
2. Protect what generates the P&L: feed latency (Coinbase WS), the exit
   engine, and the counterfactual evidence stream that audits it.
3. Exit-engine headroom is real (+$5,600 oracle gap, concentrated in ITM
   scalps where model pessimism mid-window is wrong 63% of the time).
   Improvements must validate through the symmetric replay — the asymmetric
   near-miss above is the cautionary tale.
