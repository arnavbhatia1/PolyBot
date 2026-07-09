# PolyBot — Independent Audit & Optimization Charter (for a fresh elite model)

You are a stronger model brought in to **independently determine the truth** about this
bot and make it **as good as it provably can be**. You have full access to the repo, the
collected data, the web, and (via the operator) the ability to propose fixes.

## Prime directive: independence + rigor (read this twice)

1. **Re-derive everything from primary evidence** — the raw data (`polybot/db/*.db`,
   `polybot/memory/**`), the code, and authoritative web sources. **Do NOT accept any
   conclusion in `CLAUDE.md`, `MEMORY.md`, git history, or code comments as true.** Treat
   every prior claim as a *hypothesis to independently confirm or refute*. Prior work
   concluded "no deployable edge, infrastructure-bound, only-profitable is impossible" — you
   must verify or overturn that yourself, from the data, not inherit it. If you can find a
   real edge that prior work missed, that is the most valuable thing you can do. If you
   independently confirm there is none, that is also valuable — but it must be *your*
   finding.

2. **But independence ≠ gullibility.** This market has specific ways a backtest *lies*. The
   go-live in this project's history failed because a SIM number that looked like +10¢/sh
   was a full-population replay artifact that realized break-even-to-negative live. The
   guardrails in the next section are not "bias" — they are how you avoid shipping a
   curve-fit that bleeds real money. Satisfy them, or your "edge" is fake. You may challenge
   a guardrail only with a rigorous, data-backed argument for why it doesn't apply.

3. **The goal is TRUTH, not a nice answer.** The operator wants "god tier / only profitable."
   Your job is to determine whether that is achievable and, if so, how — and to say plainly
   if it is not. Do not manufacture optimism. Do not manufacture pessimism. Follow the data.

## Non-negotiable methodology guardrails (how not to fool yourself on THIS market)

- **Metric = EQUAL-WEIGHT per-fill net ¢/share**, net of fee, day-clustered by ET. Never
  share/notional-weight (it just reflects Kelly upsizing losers). `gain = pnl/size`.
- **Walk-forward / out-of-sample ONLY.** Fit on early days, report on held-out later days;
  ideally leave-one-day-out across all 14+ days. A config that wins only in-sample is fake.
  State your exact train/test split.
- **Anchor on REALIZED fills, not the replay harness.** `scripts/analyze_late_window.py` is a
  FULL-POPULATION REPLAY CEILING — it fires on every qualifying window and fills perfectly.
  It is NECESSARY-NOT-SUFFICIENT. The binding truth is realized fills (`polybot_live.db`
  trade_history; paper-shadow outcomes). A harness "pass" that isn't reproduced on realized
  fills is not an edge.
- **Always run the G-M CONTROL.** For any proposed signal, also run "buy the spot-side at ask,
  no filter." If the control is ≈0 and your signal is only positive on the harness, the
  "edge" is stale-ask capture no real bot harvests. If your signal can't beat the control
  out-of-sample on realized-fidelity terms, it's not real.
- **Never score fills against the 1 Hz BBO** — it manufactures edges that spuriously pass
  t/LOO/bootstrap. Model the actual FOK fill at decision + measured RTT.
- **Counterfactuals: score `actual − cf`, re-decide BOTH directions branch-faithfully.** Never
  a naive signed sum of stored `delta_pnl`.
- **`None` vs `0.0` is load-bearing** — cold feeds record `None`. A `0.0` you treat as a real
  reading will poison the analysis.
- **Default to REFUTED / "no edge"** unless the evidence is concrete, out-of-sample, and
  realized-fidelity. The cost of a false "there's an edge" is real money lost.
- **Tests passing ≠ the bot works.** Verify runtime wiring end-to-end (trace creation→use).
- **Web-verify market facts** — do not trust code/memory for how the market actually works.

## Operating rules (safety)

- **DB and files are READ-ONLY for analysis** (open sqlite `mode=ro`). Do not corrupt state or
  the money ledgers (`polybot/db/polybot_*.db`).
- **You do not launch or stop the trading bot** unless the operator explicitly authorizes it;
  hand the operator the command. Analysis on a stopped or running system is fine (reads only).
- **Fixes:** minimal, root-cause, senior-level; add tests; run the full suite
  (`python -m pytest polybot/tests/`); update `CLAUDE.md` in the same change as any behavior
  change; no unrequested features (surface, don't build). **Never relax a kill bar to pass it,
  never deploy real capital before a bar passes.**

## Corpus you are auditing

14+ clean ET days (starts 2026-06-25) of tick-level `window_paths` (~3,200+ windows, 5 Hz in
the final 45 s), `window_labels` (resolved `price_to_beat` + outcome, ~6,500 windows), plus
`outcomes/`, `ghost_outcomes/`, `counterfactuals/`. The deployed strategy is the late-window
sniper only (final 45 s, Coinbase move ≥ $8 past strike, ask ≤ 0.92, `fok_slip` 0.01).

---

## The audit areas — each needs: findings with HARD NUMBERS, a confirm/refute verdict, and (if broken) a fix

### A. Market mechanics ground truth (WEB + data)
Independently establish, from Polymarket + Chainlink docs and the live API, then cross-check
the code:
- The exact **strike / "price to beat"** rule (the code now uses the *first Chainlink btc/usd
  report at/after the window-boundary timestamp*; verify this is what Polymarket resolves on,
  and whether the `GET /api/equity/price-to-beat/{slug}` endpoint agrees to the cent).
- Resolution rule (≥ vs >), fee schedule/rate, tick size, min order size, negRisk, auto-redeem,
  settlement timing, and the Chainlink data-stream report cadence/timestamp semantics.
- Cross-check EVERY market-mechanics assumption in `core/` and `execution/` against the spec.
Deliver: a table of {assumption in code → authoritative source → match/mismatch}.

### B. Data-collection integrity (does the corpus lie?)
Audit `recording.py` and the DBs: Are all ~288 windows/day captured? Any gaps, clock/timestamp
errors, sampling-rate issues, or `None`-vs-`0.0` corruption? Spot-check recorded values
(Coinbase/Binance/Chainlink price, BBO, depth) against an independent source for sampled
timestamps. Is any recorded field silently poisoning downstream analysis (e.g. `window_paths.strike`
was the recorder's own Chainlink capture, historically off — confirm nothing depends on it)?
Deliver: a data-quality report; fixes for any corruption; confidence that the corpus is
analysis-grade.

### C. Strike correctness — live verification
The strike capture was just changed to "first report at/after boundary." Independently verify
it equals Polymarket's `price_to_beat` **to the cent across many windows** (compare
`window_labels.price_to_beat` and the live API), confirm the sniper's fire condition uses the
*locked* value at t ≥ 255 s (not the cold-start fallback), and hunt for edge cases (feed gap at
the boundary, DST, window-slug math, ms/s timestamp normalization).

### D. Execution latency — CLOB round-trip flooring (operator priority)
Measure the TRUE hot-path order round-trip time and prove whether it is at the floor. Trace
every step from signal→signed order→POST→fill-confirm in `execution/` and `main.py`; find any
avoidable delay (serialization, awaits, retries, DNS, TLS, pool warmup, GIL stalls). Compare
measured warm POST RTT to the theoretical network floor and to the geographic lever (EU VPS).
Quantify how many ¢/sh a given RTT reduction is worth *on realized fills* (not the replay
ceiling). Verify the paper realism shim's latency model matches the measured live RTT.
Deliver: a latency budget (each step's ms), the floor, the achievable improvement, and its ¢/sh
value.

### E. Paper vs live fidelity (operator priority — "paper should match live")
Compare the paper realism shim (slippage curve, latency jitter, network-fail rate, FOK
semantics, fill-VWAP booking) against REALIZED live fills on matched days. Where do paper and
live diverge? The claim "paper ≈ live" must be proven fill-by-fill, not asserted. Fix the shim
so paper is a faithful predictor of live (this is what makes the paper-shadow gate trustworthy).
Deliver: paper-vs-live fill distribution comparison (fill price, slippage, win%, ¢/sh) on
matched days; a calibrated shim.

### F. Strategy edge — the central question (unbiased re-derivation)
From ALL collected data, independently determine whether ANY strategy/configuration is
**realized-profitable out-of-sample, clearing the +2¢/sh bar** — the operator's explicit target.
This includes but is not limited to:
- The full sniper parameter space (cb_move, ask_cap, fok_slip, late-window start, sizing),
  walk-forward, realized-fidelity, with the G-M control.
- Alternative at-decision signals/filters (cross-venue, order-flow, book-imbalance, oracle
  cadence, anything you can form from the recorded columns) — WITHIN price buckets so you're not
  just re-selecting favorites.
- Alternative entry/exit/timing structures; alternative sizing.
- The prior claim that this is a full-population replay ceiling with no formable edge (G-M
  holds): independently verify or refute it on the corrected-strike corpus.
Reach your OWN verdict: the most profitable robust configuration, its honest realized edge
(with t_day, p10, OOS, on realized-fidelity fills), and whether it confidently clears +2¢/sh.
If the honest answer is "no edge," prove it definitively (the control, the calibration, the
walk-forward). Also address the impossibility claim: is "never lose / only profitable" ruled
out (irreducible terminal-flip floor), or is there a filter that survives?

### G. Risk / sizing / tail / ruin
Verify Kelly fraction, the circuit breaker, `max_bankroll_deployed`, and the fat left tail.
Is sizing optimal for the *actual* (possibly thin or negative) edge? Quantify ruin risk under
realistic sizing. Is any sizing lever dead code that becomes dangerous if another knob changes?

### H. Resolution & booking accuracy
Does the bot resolve each window's Up/Down exactly as Polymarket does (oracle-first, orphan
fallbacks)? Does it book the TRUE fill VWAP (not the padded FOK limit)? Audit the ledger for
any systematic bias (the "booking bug" area). Any discrepancy directly distorts the kill-bar
read.

### I. Behavior / runtime wiring (does the bot do what the code says?)
Trace the actual running behavior vs intent: is `settings.yaml` applied, do the gates fire in
order, does the sniper fire only in the final 45 s on the right condition and side, is the
single-instance lock real, do the nightly jobs run and compute the right thing? Find any place
where the live behavior diverges from the code's stated design.

### J. Logging / observability (nitty-gritty)
Is the logging sufficient to diagnose everything live without guessing? What's missing that
would have caught the strike bug, the fill-selection, the latency, faster? Propose (and add)
targeted, low-noise logging where blind spots exist. Verify existing logs aren't misleading
(e.g. a logged value that isn't the value actually used).

### K. Feed reliability
Coinbase/Binance/Chainlink: freshness gating, reconnect/backoff, staleness handling, cross-venue
gap, clock skew. Any feed dropout that silently degrades decisions or the corpus?

### L. Mandate / alternative markets (challenge the scope)
The current mandate is BTC-5m only. As a fresh model: is that the right scope? Independently
assess whether a different cadence (hourly/daily crypto) or structure has a *better per-unit-time
edge* under the same rigor. Treat "expansion is forbidden" as a policy to question, not a law of
nature — but hold any alternative to the same realized-fidelity, walk-forward, G-M-control bar.

### M. The honest ceiling
Synthesize: what is the realistic best-case for this bot? If a profitable edge exists, the exact
config + expected ¢/sh + the deployment gate it must pass. If it does not, the definitive proof,
and precisely what would have to change (infrastructure, market, structure) to make it
profitable — or the honest recommendation to stop risking capital.

---

## Final deliverable

1. A **per-area verdict** with hard numbers (not adjectives).
2. A **confirm/refute** table for every material prior claim you tested.
3. A **prioritized fix list** — each with root cause, the fix, its test, and expected impact
   (separate "makes the bot correct/robust" from "creates profit" — do not conflate them).
4. A **single honest bottom line**: is there a configuration that is confidently, realized,
   out-of-sample profitable ≥ +2¢/sh? If yes, exactly what and how sure. If no, the proof and
   the recommendation.
5. Anything you **could not verify** and what data/access would be needed.

Take as long as you need. Verify from the nitty-gritty (a single log line, a timestamp
normalization) to the largest question (does this bot have any reason to exist). Make no
claim you have not checked against primary evidence.
