# LIVE_READINESS_GOAL.md

**Purpose:** Decide go/no-go on deploying PolyBot with real USDC, and size the
initial live tranche. This is **not** a "confirm it works" task. Your job is to
**falsify** the thesis below. Greenlight only if you cannot.

**Reconcile with `tasks/todo.md`** (the kill-bar ledger) before and after every
phase. If a claim here conflicts with current kill-bar status there, `todo.md`
wins on *status*; this file wins on *what to test*.

---

## The thesis you must try to falsify

> "PolyBot's measured edge survives contact with live execution and is net
> positive after real fees, real slippage, and real adverse selection."

**Prior going in (treat as the null to disprove):** the live edge is materially
smaller than the recorded paper edge, and possibly ≤ 0. Reasons:

1. The headline ROI is paper. Confirm provenance in Phase 0 — do not inherit it.
2. The only kill-bar-passed edge component (passive maker exit, +$0.056/fill,
   83-87% ITM, `scripts/shadow_passive_exit.py`) is **paper-only**. LiveTrader
   is FOK. Live forfeits the maker rebate *and* pays the taker fee
   (`rate*shares*p*(1-p)`, rate 0.07 → ~1.75c/share at p=0.5, one way, each exit).
3. The *current* exit edge is the hand-built `ExitBoundary` curve + the −0.10
   threshold blend, **not** the Phase 3 exit-value model (which has not passed
   its CF-replay kill bar). Unvalidated curve = possible in-sample artifact.
4. 3 months is one regime sample. 10k trades are heavily autocorrelated
   (shared windows, repeat wallets) → effective N is far smaller than 10k.

Your output is a single number with a confidence interval — **expected live
ROI/month under FOK mechanics** — plus a go/no-go and a capital cap.

---

## Ground rules (do not violate)

- **Never deploy live on a paper number.** Every EV claim that gates a live
  decision must be recomputed under live FOK mechanics (no maker rebate, full
  taker fee, modeled-vs-realized slippage).
- **Do not conflate paper passive-exit EV with live FOK EV.** They are different
  strategies. Strip passive fills out when computing the live baseline.
- **Do not rebuild entry-side prediction.** Entry has no edge (k=0, 44/44,
  day-clustered t≈3.7-4.4 against). This audit is about the *exit* edge and
  *execution*, not resurrecting L2-L6.
- **Do not relax a kill bar to pass it.** If a bar fails, the answer is "not
  yet," not "lower the bar."
- You **may** recalibrate the realism shim, resize Kelly, and fix integrity
  bugs. You **may not** flip any `deployed` flag or move real capital — that is
  a human decision gated on Phase 6.
- Honor existing invariants: UTC storage / ET bucketing, fee math
  (`DEFAULT_FEE_RATE` 0.07 multiplicative vs `EFFECTIVE_FEE_PEAK` 0.0175
  flat-additive — never mix), `gain_pct = pnl/size`, don't touch
  `db/polybot_*.db`, executable CLOB BBO only (no mid-price edge math).

---

## Definition of done

A written verdict (`LIVE_READINESS_VERDICT.md`) containing:
1. Live-FOK expected ROI/month, with CI and the method behind it.
2. Pass/fail on each phase kill bar, with the evidence.
3. Go / no-go.
4. If go: the staged capital ramp and the pre-committed abort condition.
5. The single largest unresolved risk, named.

---

# Phase 0 — Establish the true live baseline

**Objective:** Replace the inherited ROI with a number that reflects how the
strategy actually pays under live mechanics.

**Tasks**
- Confirm provenance of the 39% / 3mo / 10k figure. Which mode DB? Paper or
  live? State it explicitly.
- Decompose realized P&L by exit type: passive maker fill vs FOK-taker fill.
  Use `memory/recordings/` (tape) + outcome records. What share of total P&L
  came from maker fills that **cannot occur** in the current LiveTrader?
- Recompute ROI as **FOK-only**: every exit pays the taker fee, no maker rebate,
  filled at the FOK VWAP from the ask/bid ladder (not the resting mid). This is
  the live-path number.
- Per-trade EV distribution under FOK-only: mean, median, P25, fee drag,
  slippage drag. Use `scripts/diagnose_edge.py` as the loader; extend if needed.

**Kill bar:** FOK-only per-trade EV is **positive after fees and modeled
slippage, with day-clustered significance** (deflated Sharpe or clustered t).
If it isn't, **STOP — no live deployment.** Everything downstream is moot.

**Output:** `phase0_live_baseline.md` — the FOK-only EV number + CI + the
maker-vs-FOK P&L split.

---

# Phase 1 — Attack the execution-realism gap

**Objective:** Prove the paper shim is not lying. This is the highest-leverage,
lowest-confidence assumption in the system.

**Tasks**
- **Slippage calibration:** for a held-out set of real windows, compare paper's
  convex slippage model against actual CLOB prints at matched timestamps
  (tape recorder). Is modeled slippage ≥ realized? By how much, distributionally?
- **FOK fill-rate reality:** what fraction of intended exits fill at the modeled
  price vs fall back vs fail entirely? Live, a FOK against a thinned book may
  not fill — model what happens to a position you *wanted* out of but couldn't
  exit (it rides to resolution; price the tail).
- **Latency:** is paper's latency jitter calibrated to a real Coinbase WS →
  decision → CLOB submit round-trip? At tick-level exit re-eval, 1-2s moves the
  bid. Measure the actual loop latency; feed it back into the shim.
- **Exit-side depth:** entry gates require `min_book_depth_usd` ($50). Exit has
  no symmetric reality check. When you're dumping overpriced hope, is there
  depth to sell into? Quantify available bid depth at exit moments vs your size.

**Kill bar:** on the held-out real-window set, **modeled slippage ≥ realized
slippage** (paper is conservative, not optimistic) and modeled fill-rate ≤
realized. If paper is optimistic, recalibrate the shim and **re-run Phase 0**
before proceeding.

**Output:** `phase1_realism_audit.md` — modeled-vs-realized slippage and
fill-rate plots/tables; recalibration deltas if any.

---

# Phase 2 — Adverse selection: donor or shark?

**Objective:** Determine whether your exit counterparties make you money or pick
you off. This is the existential question for the exit edge.

**Tasks**
- Run the wallet-fingerprint pipeline (`polybot/wallets.py`, `wallet_stats`)
  over full history. Classify the wallets that **absorb your exit fills**:
  donor / noise / sharp. What fraction of your exit notional is bought by sharps?
- **Exit markout:** for every exit fill, compute price markout at 30s / 60s /
  resolution, segmented by counterparty class. The signal you want: after you
  sell, the buyer does *worse* than the price they paid (you sold them
  overpriced hope). The signal that kills you: the side you sold goes to $1
  (you sold to someone who knew).
- Net it: `extracted_from_donors − extracted_by_sharps_from_you`. The exit edge
  is only real if this is positive.
- Cross-check against the existing `AdverseSelectionMonitor` (entry-side) — is
  there a structural asymmetry where entry is clean but exit is toxic?

**Kill bar:** **net exit markout against the counterparty population is
positive.** If sharps are systematically on the buy side of your exits and net
markout is ≤ 0, the exit "edge" is adverse-selection bait — **STOP.**

**Output:** `phase2_adverse_selection.md` — exit markout by counterparty class;
the net number.

---

# Phase 3 — Is the edge real, or overfit / regime-bound?

**Objective:** Separate genuine exit alpha from a curve tuned to 3 months of one
regime.

**Tasks**
- On what data was `ExitBoundary` + the −0.10 threshold tuned? If in-sample to
  these 3 months, the recorded edge is optimistic. State it.
- **Regime split:** partition history by vol regime using the existing
  `atr_regime_shift_threshold` logic (rolling_20 / long_term_200). Does the exit
  edge hold in high-vol *and* low-vol? Trending *and* chop? Use the
  CounterfactualTracker arms (`memory/counterfactuals/`) — actual vs
  hold-to-resolution — as the ground-truth harness, per phase.
- **Effective sample size:** 10k trades are autocorrelated. Compute a
  day-clustered or block-bootstrap t-stat for the *exit* edge, mirroring the
  rigor used to kill entry. Run the CPCV / DSR / PBO apparatus on the exit
  policy specifically (`scripts/sweep_exit_policy.py`).

**Kill bar:** exit edge **positive and significant across ≥ 2 of 3 vol
regimes** (mirrors your regime-stratified veto rule). If single-regime, either
gate deployment to that regime or size down hard.

**Output:** `phase3_robustness.md` — per-regime exit EV, deflated Sharpe / PBO.

---

# Phase 4 — Sizing and ruin under live conditions

**Objective:** Ensure Kelly and the circuit breaker are sized on the live
number, not the paper one.

**Tasks**
- Recompute Kelly using the **Phase 0 live-FOK EV**. If live EV < paper EV (it
  will), current `min_kelly` / Kelly multipliers are oversized → faster ruin.
  Resize.
- **Circuit breaker stress:** simulate the 20% drawdown breaker against the
  worst historical regime. Does it trip before or after a normal drawdown would
  recover? A breaker that locks you out at the bottom of a survivable drawdown
  is a defect — characterize it.
- **Depth-bounded deployment:** `max_bankroll_deployed` 0.80 and
  `max_book_fill_pct` 0.50 — at live exit-side depth (Phase 1), does deploying
  0.80 even fill? If not, the effective bankroll cap is depth, not 0.80.

**Kill bar:** simulated max drawdown under live mechanics stays within tolerance
**and** Kelly is sized on live EV. Document the resized parameters.

**Output:** `phase4_sizing.md` — resized Kelly, drawdown sim, breaker behavior.

---

# Phase 5 — Live-execution integrity

**Objective:** Verify the live path doesn't lose money to bugs, independent of
edge.

**Tasks**
- Live boot preflight: `scripts/verify_keys.py` — key + funder + allowance
  (`max_single × max_concurrent × 10`) + mid-session recheck.
- **Double-fill guard:** force ambiguous-rejection scenarios; confirm no
  resubmit. A double-fill on a thin market is real money gone.
- **Resolution correctness:** Chainlink-first oracle, atomic winner/loser
  credit, non-blocking redeem settlement, Chainlink-orphan fallback.
- **Feed failure while holding:** Coinbase WS stalls mid-position — README says
  "decision skipped, never zeroed," but if you can't price, you can't exit.
  Stress this: what is the realized outcome of an un-exitable held position?
- **Monitoring:** does Discord (`!status`) alert on the money-losing failure
  modes — stuck position, failed exit, drawdown breach — not just info?

**Kill bar:** a live dry-run at the $1 order floor with a trivial bankroll for a
pre-committed number of days, **zero integrity failures**, before any scale-up.

**Output:** `phase5_integrity.md` — dry-run log, failure-mode coverage table.

---

# Phase 6 — Go/No-Go + staged capital ramp

**Objective:** Single decision, then a ramp that re-validates the prediction
against live reality.

**Tasks**
- Synthesize Phases 0-5 into go / no-go. Any STOP kill-bar failure → no-go.
- If go: **stage capital.** Start at minimum (e.g. $1-order floor, smallest
  viable bankroll). Run live for a pre-committed number of trades. Compare
  **realized live EV vs the Phase 0 predicted EV.**
- **Abort condition (commit before starting):** if realized live EV
  underperforms the Phase 0 prediction by more than [define: e.g. 1 SE] over
  [N] trades, **halt and return to Phase 1** — the realism shim was wrong.
- Scale to the next capital tier **only** when realized live EV matches the
  prediction within CI at the current tier.

**Kill bar:** live realized EV matches Phase 0 prediction within CI at each tier
before the next tier increase. No tier-jumping.

**Output:** `LIVE_READINESS_VERDICT.md` — the Definition-of-Done artifact.

---

## Failure-mode summary (what would make this whole exercise return "no-go")

- FOK-only per-trade EV ≤ 0 after fees/slippage (Phase 0) → **terminal no-go.**
- Paper slippage optimistic vs tape (Phase 1) → recalibrate, EV likely collapses.
- Net exit markout ≤ 0 against counterparties (Phase 2) → **terminal no-go.**
- Edge single-regime only (Phase 3) → conditional deploy or no-go.
- Any integrity failure in dry-run (Phase 5) → fix before any capital.

The most likely killer, ranked: **Phase 0 (maker-rebate dependence) → Phase 2
(exit toxicity) → Phase 1 (shim optimism).** Spend effort there first.