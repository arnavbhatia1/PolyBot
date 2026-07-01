# POLYBOT ROADMAP — LEAN KILLING MACHINE (BTC ONLY)

**Open work only.** Completed phases and dated history are deleted, not checked
off — they live in git + memory. This file is the forward roadmap and kill-bar
status from here.

**Scope:** BTC 5-min markets only. Multi-asset expansion is the single TODO after
this goal completes — do not start it early.
**Constraint:** Recorder-first. No phase ships to live capital before its kill bar
passes. Zero exceptions.
**Authority:** the counterfactual replay harness (`scripts/sweep_exit_policy.py`)
is ground truth for any exit-policy change. Score
via `actual − cf` / `scalp_was_optimal`, never a naive signed sum of the stored
`delta_pnl` (sign-inconsistent per arm — it inverts the edge).

---

## ESTABLISHED FACTS (constrain all future work)

1. **No entry-side information advantage EARLY/MID-window.** At entry the CLOB price
   beats the model (Brier t≈−5.6; k=0, 44/44 segments, day-clustered t≈3.7–4.4
   against) and `0x565ca5`'s own early trades win only 42% — G-M holds. Entry is
   inventory sourcing; never rebuild early/mid entry prediction. **Exception under
   validation:** the **final ~30–60s** is a separate regime where winning wallets
   demonstrably beat the CLOB (Late-window sniper, OPEN ROADMAP) — gated by its own
   kill bar, not a reopening of this fact.
2. **The base (entry + exit-engine) strategy has NO proven edge — measured, not
   pending.** The binding ≥10-clean-day post-fix read landed 2026-07-01: scalp-vs-hold
   act−cf +$32/day at day-clustered t **+1.07** (strict 9-day cut t +1.48), per-$
   ~zero (t −0.20), the whole dollar lean two adjacent days (drop-best t +0.51).
   The bot is a well-built ~zero-EV G-M machine; realized P&L swings are BTC-vol
   variance. The base strategy never deploys live — `sniper_only` suppresses it in
   the live recipe while keeping its evidence stream alive as ghosts.
3. **Depth ceiling is real.** Top-5 book is $50–2k/side; per-scalp size caps
   ~$100–300; bankroll stops mattering past ~$5–10k. The only multiplier is
   parallel markets (post-goal).

---

## THE GO-LIVE GATE — READ 2026-07-01: **FAILED.** The binding milestone is now the sniper kill bar.

The base exit-edge gate (positive scalp-vs-hold CF EV, ≥10 clean ET days,
day-clustered t ≥ 2) was read at 10 clean days on 07-01 and failed on every
defensible cut — t_day +1.07 (10d, +$32.29/day) / +1.48 (strict 9d), per-$ −0.0046
(t −0.20), bootstrap p10 −$4.14, lean concentrated in 06-25+06-26 (drop-best
t +0.51). Two independent agents reproduced each other to the digit. **Go-live for
the base strategy is off — permanently unless something structural changes**
(~35 clean days at the current SNR just to reach t≥2, and the clean series is
closed: 06-30 is mixed-regime — 3.1h recorder outage + restarts bracketing the
paper_trader edit — and 07-01+ runs the retuned fill-realism, so any future
base-edge clock restarts at 07-01 under the new regime).

Real capital now has exactly one candidate path: the **late-window sniper**
(below), through its own kill bar, deployed as `mode: live` + `sniper_enabled:
true` + `sniper_only: true`. `tasks/live_readiness.md` Phases 1–6 (realism-shim
audit, live-integrity dry-run, staged ramp) still apply to that deploy.

---

## OPEN ROADMAP

### Late-window directional sniper — BUILT (gated OFF), validating
**The one bot-formable edge found. Entry path is built + reviewed + unit-tested, gated OFF
behind `late_window.sniper_enabled` (default false) pending its kill bar.** Reverse-engineering
the profitable wallets found a real, day-cluster-robust edge: winners buy a directional side
in the final ~30–60s at prices the CLOB hasn't repriced, win ~74% (`0x565ca5`: 845 late buys,
74.3% at avg 0.459, t_day +2.6→+4.5). The winners *lead* the feed (sub-second prediction the
bot can't buy); the **bot-formable version *follows*** — when a Coinbase move over ~2s pushes
price past strike and the chosen-side ask is still cheap (stale book), buy that side before
the CLOB reprices. This is L1's own favored side (so it doesn't fight the model); the only
reason the normal path rejects it is `max_edge=0.20` (tuned for the dead entry game).

**Built + adversarially reviewed this session:**
- `scripts/analyze_late_window.py` is now **RTT-parametric** (`--rtt-sweep`, `--max-slip`):
  the fill is the ask **interpolated at decision+RTT** along its repricing path → an
  **edge-vs-latency curve**. The momentum repricing half-life is ~350ms, so **order latency
  IS the lever** — this CORRECTS the old "colo doesn't help" note: feed-leadership can't be
  bought, but the fill is a race against the CLOB's repricing, which lower RTT wins.
- Gated entry path: `coinbase_feed.cb_move` (interpolated 2s lookback, + a `price_event`) +
  `SignalEngine.evaluate_late_sniper` + `main.py` hook (bypasses ONLY `max_edge`→`sniper_max_edge`
  at BOTH the edge-cap and pre-submit gates, and the time penalty; every other safety gate
  stays) + a gated main-loop Coinbase-tick wake. 5-lens adversarial review (2 real bugs found
  + fixed: pre-submit re-imposing 0.20; cb_move 1s-bucket overstatement) + 12 unit tests; 491 green.

**Latest read (07-01 ~20:30 UTC; 7 ET days 06-25→07-01):** momentum still passes every
statistical condition at the measured ~135ms RTT — t_day **+3.56, p10 +0.0449**, net
+0.0745/sh (lenient 5¢ FOK limit) AND **+2.20, p10 +0.0218** (strict 0.5¢); 6/7 days
positive (06-28 the one negative); CONTROL ~0 both limits; survives the look-ahead audit
(one bet/window, no fill-skip conditioning; collapsing-loser fires still fill and count
as losses). Paper-shadow (ON since the 06-30 restart): 3 fills, 3/3 wins +$82.47, sides
agree 3/3 with the harness and fill prices track to ≤2¢ — no adverse discrepancy; live
fires ~1.5/day vs harness ~59/day (the documented conservative-subset effect of
`sniper_min_edge` + the base gates). **Watch: per-day edge has drifted down three days
running (+0.072 → +0.064 → +0.038 lenient)** — if the slide continues the bar can fail
honestly on the new days. Blocked ONLY by n_days=7<8 and the shadow span (2 days).
**Earliest n_days pass: 07-02 data, clean full-day read morning 07-03. Shadow-span leg
under the same-span reading: through 07-07, read 07-08** (~10–15 expected shadow fills —
fidelity tracking, not an independent t-test). Re-run
`analyze_late_window.py --rtt-sweep 0.135 --max-slip 0.05` (and `--max-slip 0.005`) then.

**Kill bar (all, at the host's MEASURED RTT):** `t_day ≥ 2.0` AND block-bootstrap `p10 > 0`
over **≥ 8 clean ET days, ≥ 6 positive**, net of the 0.07 fee, executable asks, CONTROL
(spot-side@ask) ~0. PLUS — live applies an L1-edge floor (`sniper_min_edge`) the harness
`momentum()` does NOT (no ATR in `window_paths`), so live trades the higher-conviction SUBSET
and the harness is a CONSERVATIVE gate: **also paper-shadow** (`sniper_enabled` in PAPER) ≥ the
same span and compare realized fills/edge before deploy. The ATR/`model_prob_up` stamp in
`window_paths` (lets the harness replicate the live `sniper_min_edge` floor exactly) is built
and accrues once tonight's restart picks up the working tree. **No live capital before
this bar; flip `sniper_enabled` only after it passes at the achieved RTT.** Honest prior the
bot-formable version clears: raised from ~0.25–0.35 toward ~0.4–0.5 given the latency-monotonic
preview, but still gated on ≥8 days + the host RTT + the paper-shadow.

**Go-live runbook once the bar passes (all operator-run, in order):**
1. `python scripts/verify_keys.py` — GET-auth + balance/allowance preflight.
2. `python scripts/smoke_order_test.py --confirm` — proves the ORDER-POST path
   (EIP-712 sign + POST through Cloudflare) with one unfillable FOK; verify_keys
   only exercises GETs. Can be run any day before launch.
3. Calibrate `paper_network_fail_rate` expectations: note the smoke test + first
   live FOK success rate vs the 0.03 estimate.
4. Stop the bot; `python scripts/reset_paper_clean.py` (clean-slate ledger,
   operator-run with bot STOPPED) if a fresh live baseline is wanted.
5. settings.yaml: `mode: live` + `late_window.sniper_enabled: true` +
   `late_window.sniper_only: true` (base entries stay suppressed as ghosts).
6. Relaunch via `.\scripts\run_polybot.ps1`; watch the first fills vs the
   harness-predicted edge (`scripts/sniper_shadow_status.py`).

### Exit-value model → ExitBoundary overlay (build AFTER the gate clears)
**Sequencing first:** this is a refinement of the exit curve, so it is **moot
until the go-live gate confirms ExitBoundary itself has a positive edge** on the
fixed code. Do not prioritize it over the gate — optimizing a policy you haven't
confirmed wins is backwards.

The freestanding two-head model (`polybot/exit_model.py`) was an orthogonal
nightly experiment that gated nothing (`deployed` never flipped; it competed with
ExitBoundary on price-derived features with no information the curve lacks, and
lost to the curve on clean post-fix data every day). **REMOVED 2026-06-24** along
with its nightly refit + shadow harness (`scripts/shadow_exit_model.py`); the
window_paths retention sweep it carried moved to `polybot/recording.py`. The
floored overlay below supersedes it.

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
(mp≥0.55), Bayesian-shrunk to 0 on cold rows (never impute 0). Build a fresh
nightly refit + shadow harness when this is taken up (the freestanding exit_model
scaffolding was removed 06-24).
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

---

### Ops: Gamma `/events` offset pagination is formally deprecated
Live probe 07-01: Gamma returns `Deprecation: true; Sunset: Fri, 01 May 2026`
(date already passed) on `GET /events` — slug queries still 200, but the
scanner's discovery path rides a deprecated endpoint. Cheap insurance: add a
`/events/keyset` fallback in `market_scanner`/recorder discovery before Gamma
ever enforces it. Low priority; nothing broken today.

---

## ONE REMAINING TODO (post-goal)

Expand passive-exit → exit-value model → wallet fingerprinting to ETH, SOL, XRP
5-min markets. Architecture is parameterized; execution is a symbol loop. Do not
start until the BTC goal is complete and all kill bars have held in production
≥7 days.

---

## WHAT YOU ARE NOT ALLOWED TO DO

- Add any EARLY/MID-window entry-side prediction logic (ML or rules) — dead, G-M holds.
  (The final-30–60s late-window sniper is the one sanctioned exception, and only
  through its kill bar — see OPEN ROADMAP.)
- Expand to non-BTC markets before this goal is fully complete.
- Deploy any phase to live capital before its kill bar passes.
- Relax a kill bar to pass it — the answer to a failed bar is "not yet," never
  "lower the bar."
- Preserve deleted code in comments or dead branches.
- Rebuild symmetric market-making, or the wide-quote maker sleeve (its
  precondition — a stale touch through a material Coinbase move — never occurs in
  this regime), under any name.
- Treat the oracle cadence or Chainlink heartbeat as a tradeable signal.
