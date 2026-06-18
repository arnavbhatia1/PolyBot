# POLYBOT ROADMAP — LEAN KILLING MACHINE (BTC ONLY)

**Open work only.** Completed phases and dated history are deleted, not checked
off — they live in git + memory. This file is the forward roadmap and kill-bar
status from here.

**Scope:** BTC 5-min markets only. Multi-asset expansion is the single TODO after
this goal completes — do not start it early.
**Constraint:** Recorder-first. No phase ships to live capital before its kill bar
passes. Zero exceptions.
**Authority:** the counterfactual replay harness (`scripts/sweep_exit_policy.py`,
`scripts/shadow_exit_model.py`) is ground truth for any exit-policy change. Score
via `actual − cf` / `scalp_was_optimal`, never a naive signed sum of the stored
`delta_pnl` (sign-inconsistent per arm — it inverts the edge).

---

## ESTABLISHED FACTS (constrain all future work)

1. **No entry-side information advantage.** At entry the CLOB price beats the
   model (Brier t≈−5.6; k=0, 44/44 segments, day-clustered t≈3.7–4.4 against).
   Entry is inventory sourcing. Never rebuild entry prediction.
2. **The exit edge is UNPROVEN on the live (post-gut) machine.** The historical
   +$690/14d exit edge was measured on the pre-gut L2-L6 codebase; the same CF
   harness on the post-gut L1-only machine shows the same exit policy at
   −$182/7d vs always-hold (not significant) — the edge was an artifact of
   inventory the deleted entry stack sourced. Two suppressor bugs were fixed
   06-17 (loss-cut HOLDs when holding_edge>0; ATR keeps one slot per candle);
   the post-fix edge has not yet been measured — see THE GO-LIVE GATE.
3. **Depth ceiling is real.** Top-5 book is $50–2k/side; per-scalp size caps
   ~$100–300; bankroll stops mattering past ~$5–10k. The only multiplier is
   parallel markets (post-goal).

---

## THE GO-LIVE GATE (binding — the current milestone)

Real capital deploys **only** on a demonstrated, significantly-positive post-gut
exit edge measured on the FIXED code: a positive scalp-vs-hold CF EV over
**≥10 clean ET days with a day-clustered t ≥ 2**.

- Clean post-fix data began **06-17 13:19 ET** (bot restarted onto the fixed
  code). Earliest valid read **~06-27 eve / 06-28** (10 clean days from 06-18).
- **Cannot be compressed.** The replay harness reuses the stamped (buggy-period)
  probability, so the ATR fix can't be back-applied to already-recorded data —
  the gate needs new fixed-code data; and ≤5 day-clusters can't carry t≥2 (df
  too small: t=2 @ df=4 is only p≈0.058, not 95% sig). A read before ~10 days is
  a non-binding directional preview only — it can justify abandoning early, never
  greenlight early.
- Full falsification plan: `tasks/live_readiness.md` (Phases 0–6 — FOK-only
  baseline, realism-shim audit, exit markout vs counterparties, regime
  robustness, live-integrity dry-run, staged ramp). Phase 0 is the gate above;
  a STOP there is terminal.

All clean-data measurement (the shadow comparison *and* this gate) excludes
pre-fix/pre-gut days via the `CLEAN_EPOCH` cutoff (06-17 13:19 ET) in
`shadow_exit_model.py` — decisions made before the fixed code went live are never
scored.

If the post-fix edge does not come back significantly positive, **go-live is off**
until the strategy improves — the −$182 stands. **If it clears:** deploy
ExitBoundary live (it is already the live exit policy), keep paper running side by
side to feed counterfactuals, and iterate the floored overlay (below) in the
background through the shadow harness — never flipping an untested change onto
live capital.

---

## OPEN ROADMAP

### Exit-value model → ExitBoundary overlay (build AFTER the gate clears)
**Sequencing first:** this is a refinement of the exit curve, so it is **moot
until the go-live gate confirms ExitBoundary itself has a positive edge** on the
fixed code. Do not prioritize it over the gate — optimizing a policy you haven't
confirmed wins is backwards.

The freestanding two-head model (`polybot/exit_model.py`) is an orthogonal
nightly experiment, **NOT a go-live dependency** and **gates nothing**: it
competes with ExitBoundary head-to-head on price-derived features (no information
the curve lacks → expected to tie-or-lose, and no downside floor). `deployed`
stays False; its shadow verdict is not a milestone. **Operator decision: kill it
early** — abandon at day 3–4 if it's negative every day rather than waiting the
full 5-day shadow; killing it costs nothing, and clean post-fix data confirms it
loses to the curve on every day. Treat it as deprecated in favor of the overlay
below.

**The design to actually build — a floored overlay** (cannot underperform the
curve by construction): one pure-numpy L2 logistic that outputs a bounded
additive NUDGE to `effective_exit_threshold`, **fed ExitBoundary's own output as
a feature so β→0 reproduces the curve EXACTLY** — a provable downside floor,
verifiable analytically (no data wait). Single new separating feature — the
vol-normalized **Coinbase-vs-strike lead residual** =
`StudentT_CDF(z_lead) − clob_mid_for_side`, with
`z_lead = (coinbase_price − strike)/(recon_rv·√seconds_remaining)` and `recon_rv`
the trailing-60s realized vol of the 1Hz `coinbase_price` series (computed
causally): "is the bid rich vs where the resolution venue already points, before
the slower CLOB repriced?" Nudge applies only in the ITM-patience regime
(mp≥0.55), Bayesian-shrunk to 0 on cold rows (never impute 0). Reuse the nightly
refit + `shadow_exit_model.py` (add as a 3rd policy).
**Kill bar (all four):** positive ITM $ delta over ≥5 frozen-OOS days;
day-clustered t clearly positive; window-level block-bootstrap p10>0 (~1.7k
independent windows, NOT per-row); lead_residual coefficient materially non-zero
(cheap pre-gate — if ~0, ship nothing).
**Honest ceiling:** the floor guarantees it **can't hurt** (β→0 = the curve), but
a real positive edge is **NOT guaranteed** — beating the curve out-of-sample needs
a genuine signal, and the no-entry-edge theorem says that's probably thin or a
clean NULL (a persistent observable lead would itself be a disproven entry edge).
Best realistic outcome = ties, no harm. Build it only because it's free (data +
harness exist) and floored — never on the expectation of a guaranteed win. Do NOT
use raw `cross_venue_gap` (Binance) — it's a ~57¢ venue basis, not signal.

### Wallet-aware exit routing — BLOCKED as specced; realizable alternative open
The fingerprinting pipeline runs nightly and classifies (donors −3..−11¢/$1,
sharps +12..+37¢/$1 → `wallet_stats`). The original spec ("check the book for
sharp counterparties before resting") is **infeasible**: the live CLOB book/tape
is anonymous L2 — no maker/taker/owner identity pre-post. Live identity exists
only post-match (authenticated user channel for own fills; RTDS proxyWallet
~100ms; mempool ~1.5s); none screen the resting book before you post.
`wallet_stats` is write-only — no decision-time reader.
**Realizable alternative (operator decision; needs its own 7-day kill bar):** a
post-fill learning loop (identify who lifts our rests in ~real-time, fold their
markout into `wallet_stats`) feeding a STATISTICAL regime gate — rest only in
window / time-of-day / quote / flow contexts where sharps don't dominate. Not
built.

### Box-arb execution
The monitor runs continuously (15m/5m shared-expiry pairs, ~96 overlaps/day,
log-only → `memory/recordings/box_arb.jsonl`). Build execution (size + FOK both
legs or neither) only if the logged data shows real, repeatable riskless boxes;
else document the abort. Expected yield $2–15/day — not the main event.

---

## ONE REMAINING TODO (post-goal)

Expand passive-exit → exit-value model → wallet fingerprinting to ETH, SOL, XRP
5-min markets. Architecture is parameterized; execution is a symbol loop. Do not
start until the BTC goal is complete and all kill bars have held in production
≥7 days.

---

## WHAT YOU ARE NOT ALLOWED TO DO

- Add any entry-side prediction logic (ML or rules).
- Expand to non-BTC markets before this goal is fully complete.
- Deploy any phase to live capital before its kill bar passes.
- Relax a kill bar to pass it — the answer to a failed bar is "not yet," never
  "lower the bar."
- Preserve deleted code in comments or dead branches.
- Rebuild symmetric market-making, or the wide-quote maker sleeve (its
  precondition — a stale touch through a material Coinbase move — never occurs in
  this regime), under any name.
- Treat the oracle cadence or Chainlink heartbeat as a tradeable signal.
