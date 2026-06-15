# POLYBOT GOAL — LEAN KILLING MACHINE (BTC ONLY)

**Scope:** BTC markets only. Do not touch multi-asset expansion. That is the single remaining TODO when this goal is complete.  
**Constraint:** Recorder-first. Every phase with live capital exposure requires its kill-bar to pass before deployment. Zero exceptions.  
**Authority:** You have full read/write over the codebase. Audit the current state of every module before touching it. Trust the counterfactual replay harness — it is the ground truth for whether a deletion is safe.

---

## GROUND TRUTH ESTABLISHED BY THE DATA

Before any code changes, internalize these facts. They are not hypotheses:

1. **You have no entry-side information advantage.** At the moment of entry, the CLOB price is a better predictor of resolution than your model (Brier t=−5.6). Entry is inventory sourcing, nothing more.
2. **Your exit-side edge is real and measured.** The leading side's bid is overpriced ~7.9¢ mid-window vs. true continuation value (t=+4.7). You are selling overpriced hope to FOMO chasers.
3. **Your entries mark out against you.** Entry fills favor the maker by +2.9¢ at 60s (t=5.2). Entry = dumb money. Exit = smart money. P&L = the gap.
4. **The entire entry-side ML stack beats nothing.** k=0, 44/44 segments, t≈3.7–4.4 against. Every entry-forecasting model, gate, and feature is dead weight.
5. **The depth ceiling is real.** Top-5 book is $50–2k/side. Per-scalp size caps ~$100–300. Bankroll stops mattering past ~$5–10k. The multiplier is parallel markets — addressed post-goal.

---

## PHASE 0 — CODEBASE GUTTING

**Do this before anything else. It is a prerequisite, not optional.**

Audit the codebase. Identify and delete every component that falls into the dead categories below. For each deletion, run replay before committing. If naive deletion causes replay failure on the current regime, find the minimal safe removal path — but still remove it. Do not preserve dead code "just in case."

### DELETE (proven dead, stop spending cycles here)

- **L2–L6 gates** — all entry-side feature gates above L1. Gone.
- **SPRT (Sequential Probability Ratio Test)** — dead entry confidence mechanism. Gone.
- **Isotonic calibration stack on entry** — calibrating a dead signal. Gone.
- **Taker burst-chasing entry logic** — edge_cap ghost pool lost −0.70/$1, 24.7% win rate, t=−11.6. Gone.
- **Symmetric market-maker logic** — total gross overround 1–2 ticks vs. ~5¢/$1 mid-window toxicity. You'd be selling umbrellas to yourself. Gone.
- **Oracle cadence trading logic** — Chainlink Data Streams are pull-based, sub-second, no deviation threshold, no heartbeat, no locked print window. Dead at mechanism level. Gone.
- **Entry-side forecast models (ML or rule-based)** — same verdict as SPRT. The bar is beating the CLOB; none of them do. Gone.
- **Knob-tuning recommender / parameter optimizer for entry** — tunes a dead edge. Gone.
- **Any component whose sole purpose is improving entry prediction** — dead category. Gone.

### KEEP (do not touch)

- Every data feed (Coinbase, Polymarket CLOB, Polymarket data-api, Chainlink if present)
- FOK execution path (entry continues to use it; exit will extend it with GTC)
- Counterfactual evidence stream and replay harness — this is your evaluation infrastructure
- Circuit breaker
- L1 entry gate (cheap sanity anchor + fee/spread gates — that's the entire legitimate entry logic)

### DONE WHEN

Replay passes on current regime with the gutted codebase. Codebase LOC for deleted components is gone, not commented out.

---

## PHASE 1 — PASSIVE EXIT CONVERSION

**This is the highest-certainty, highest-immediate-dollar play. Do it first after Phase 0.**

### What to build

Replace the current FOK-hit-the-bid exit with a two-stage exit engine:

1. **Stage 1 — Resting limit:** When exit fires, post a limit sell at mid-price. Hold for `N` seconds (tune `N` in shadow mode — start at 10–20s, measure fill rate vs. adverse fill rate, find the crossover).
2. **Stage 2 — FOK fallback:** If the resting order does not fill within `N` seconds, cancel and FOK-hit the bid as before.

The goal: late FOMO chasers lifting the book come to you. You collect the spread instead of paying it, pay zero taker fee, earn the maker rebate.

**Measured upside from your own DB:** Exit-leg taker fees ~$322/14d + half-spread cost ~$120–240/14d = ~$32–40/day recoverable on a book making ~$57/day. That is a 35–50% raise with zero new prediction.

### Kill bar (shadow sim, no live capital)

Run a 3-day tape-based shadow simulation. A resting order "fills" only if a trade prints strictly through your posted price (not at it — through it). Measure:

- Fill rate on ITM scalps (the ones carrying the edge)
- Kill threshold: **≥50% fill rate on ITM scalps.** Below this, the passive exit does not deploy.

If kill bar passes → deploy to live. If it fails → diagnose why (spread too wide? wrong `N`? timing?) and iterate, but do not deploy until it passes.

### DONE WHEN

Kill bar passes. Passive exit live in production on BTC. FOK fallback intact. No regression on circuit breaker logic.

---

## PHASE 2 — WINDOW-PATH RECORDER

**This is infrastructure for everything downstream (exit-value model, wallet fingerprinting). Build it in parallel with or immediately after Phase 1.**

### What to build

Record every window, not just every trade. Currently you learn from ~35 trades/day. The recorder gives you ~288 labeled observations/day — a 40x increase in learning signal.

For all 288 five-minute BTC windows per day, at 1Hz resolution, record:

- Both token (Up/Down) order book state: best bid, best ask, top-3 depth each side
- Coinbase BTC mid-price
- Strike price
- Current window time elapsed (0–300s)
- Whether your bot traded in this window (and if so, what)
- Window resolution label (free — windows resolve whether you trade or not)

**Labels are free.** You do not need to trade in a window to get a training example. This is the key insight. Every window is a labeled data point.

### Storage

Write to your existing DB. Schema: one row per (window_id, timestamp_unix, fields above). Index on window_id and timestamp. Retention: rolling 90 days minimum.

### DONE WHEN

Recorder running continuously. 288 windows/day being written. Labels populating on resolution. Verified: recorder does not interfere with live trading latency.

---

## PHASE 3 — EXIT-VALUE MODEL (REPLACE EXITBOUNDARY CURVE)

**Prerequisite: Phase 2 recorder running with at least 7 days of data.**

### What to build

A two-head neural model trained nightly on window-path data:

- **Head 1:** P(window resolves in my favor | current window state) — a calibrated probability, not a rank score.
- **Head 2:** E[my best available bid N seconds from now | current window state] — a regression target for exit timing.

This replaces the hand-drawn ExitBoundary curve with a learned value function. The measured headroom: $1,914/14d oracle ITM gap that the threshold family cannot reach because it lacks a new separating feature — the E3 fields (check what shipped) provide exactly that.

### Architecture constraints

- Small model. The effective sample is ~20–60k windows, not millions. Do not overfit. Two-layer MLP or gradient boosted trees are fine. Interpretability matters — you need to know when it breaks.
- Recency weighting. Weight recent windows more heavily. The regime drifts; the model must too.
- Input features: the 1Hz window-path fields from Phase 2, current inventory position, current fee state, time remaining in window.
- **No entry-side features.** The model is deployed at exit decision only.
- Calibration: Head 1 must be calibrated (Platt scaling or isotonic post-hoc). Brier score is your eval metric, not AUC.

### Nightly pipeline

- Runs after market close
- Retrains on rolling window of data (tune lookback — start 30 days)
- Writes model artifact to disk
- Bot loads new artifact at next session start
- Logs Brier score and Head 2 MAE for every nightly run — if either degrades >15% vs. 7-day trailing average, send alert and revert to previous artifact

### Kill bar

Shadow mode for 5 days post-training. Compare exit-value model's recommended exits vs. current ExitBoundary exits on the same windows using your counterfactual replay harness. Model must show positive expected-value improvement on ITM scalps vs. current threshold. If it does not beat ExitBoundary in replay → do not deploy, diagnose.

### DONE WHEN

Model live in production replacing ExitBoundary. Nightly refit pipeline running. Brier score logged nightly. Kill bar passed.

---

## PHASE 4 — WALLET FINGERPRINTING

**Prerequisite: Phase 2 recorder running. Can be built in parallel with Phase 3.**

### What to build

Polymarket's data-api tags trades by wallet address. The five-minute BTC casino has a recurring cast of wallets. Build per-wallet markout tables that update daily.

For each wallet that trades in BTC five-minute markets:

- Track: trade direction, entry price, resolution outcome, P&L per trade
- Compute: per-wallet win rate, per-wallet markout at 60s, 120s, 300s, per-wallet trade frequency
- Classify wallets into: **sharp** (positive markout, consistently correct), **noise** (random), **donor** (negative markout, consistently wrong)

**Trading rule:**
- When your exit engine is about to rest a limit order, check the current book: if the likely counterparty wallets are classified as **sharp**, do not rest — FOK fallback immediately. You do not want to be the resting counterparty to a sharp wallet.
- If counterparty flow is donor/noise → rest as normal.

This is counterparty information, not a public BTC feature. It does not fight the "price already knows" theorem. It compounds — the table gets more accurate every week forever.

### Why this matters

This is the only place the bot genuinely gets smarter over time without requiring new prediction on BTC price direction. It is a learning layer that improves purely from accumulated counterparty data.

### Kill bar

Paper-trade the wallet-aware exit routing for 7 days. Measure: fill quality on rested orders when counterparty is classified as donor vs. sharp. If donor fills are systematically better than sharp fills → deploy. If no measurable difference → deploy anyway (no downside, free information). If sharp counterparties fill and cause adverse outcomes → the classification is working; tighten the FOK threshold.

### DONE WHEN

Daily wallet classification pipeline running. Wallet-aware exit routing deployed. Tables persisting across sessions and updating each night.

---

## PHASE 5 — CROSS-HORIZON BOX ARB MONITOR

**This is small, riskless, and fast to build. Do it after Phase 1 is live.**

### What to build

The hourly BTC market and the :55 five-minute window resolve at the same instant from the same Chainlink oracle. Two digital contracts, same expiry, different strikes. Hard monotonicity constraint between their prices: if P(BTC > strike_high) > P(BTC > strike_low), a violation is riskless.

Write a monitor that:
1. Pulls current prices from both the hourly and five-minute BTC markets when they share an upcoming expiry
2. Checks monotonicity: if price_low_strike + (1 - price_high_strike) < 1.00 accounting for fees, a box exists
3. If a box exists, sizes and executes both legs simultaneously (FOK on both or neither)
4. Logs every detected opportunity and every execution

### Expected yield

$2–15/day when violations appear. Not the main event — but it's free money and a half-day build. The monitor runs passively; it does not interfere with the main bot.

### DONE WHEN

Monitor running continuously on BTC. Executes detected boxes. Logs all opportunities (executed and missed).

---

## PHASE 6 — WIDE-QUOTE MAKER SLEEVE

**Prerequisite: Phase 1 passive exit live and proven GTC orders fill in this venue.**

### What to build

Symmetric market-making is dead (you already deleted it in Phase 0). This is different: quote wide (sum of both sides ≥ 1.04) **only** when the touch is genuinely stale or empty — the gate-skip moments the Phase 2 recorder now measures.

Logic:
- Monitor book staleness using your Phase 2 1Hz recorder: if best bid/ask has not changed in `T` seconds (tune `T`, start 30s) AND Coinbase mid has moved materially (>0.5% threshold) → the book is stale
- Quote both sides with prices anchored to Coinbase-implied fair value ± your spread
- Cancel-replace on every Coinbase tick above threshold
- If a trade occurs in the market that updates the touch before your quote fills → cancel immediately

**This is not continuous quoting.** It is opportunistic quoting in the specific liquidity-desert moments the recorder identifies. Ceiling ~$30–80/day on BTC.

### Kill bar

Shadow mode for 3 days. Simulate fills only when a trade prints through your price. Measure: fill rate, realized spread captured, adverse fill rate (filled then price moved against you). Positive EV in shadow → deploy. Negative EV → abort.

**Only build if Phase 1 has proven GTC infrastructure works reliably in this venue.** If Phase 1 shadow sim showed GTC mechanics are unreliable, skip this phase entirely.

### DONE WHEN

Wide-quote sleeve live. Anchored to Coinbase. Cancel-replace on every material Coinbase tick. Kill bar passed.

---

## EXECUTION ORDER

```
Phase 0 (gutting)          → immediately, prerequisite for everything
Phase 1 (passive exits)    → immediately after Phase 0, 3-day shadow then live
Phase 2 (recorder)         → in parallel with Phase 1
Phase 5 (box arb)          → after Phase 1 live, small and fast
Phase 3 (exit model)       → after Phase 2 has 7 days of data
Phase 4 (wallet prints)    → after Phase 2 running, parallel with Phase 3
Phase 6 (maker sleeve)     → last, only after Phase 1 proves GTC fills
```

Do not skip the sequence. Phase 3 and 4 without Phase 2 data are building on nothing.

---

## DEFINITION OF DONE (FOR THIS GOAL)

**BUILD STATUS (updated 2026-06-14; 416 tests green). HARD DEADLINE: 2026-06-22.**
The kill-bar-phase builds are done — the two-stage passive exit (now live in
paper) and all shadow evaluators. Remaining boxes are a verdict (Phase 3 exit
model, 06-20/21) and genuine builds that surfaced this week: wallet-aware routing
is **infeasible as specced** (no live wallet identity in the CLOB feeds — Phase 4
box) and box-arb execution is unbuilt. Bot runs the lean code, **data epoch is
06-11**, recorders + nightly jobs live.

DEADLINE CALENDAR (data epoch 06-11; ~two days of slack vs 06-22):
- 06-12/13: data accrues (window paths ~288/day, tape, box-arb overlaps 96/day).
- 06-14 [DONE]: both kill bars ran 09:25 → shadow_eval_2026-06-14.txt.
  Phase 1 PASS on 4-day data (ITM fill 83-87%, uplift +$0.056/fill at mid/10s —
  the 2-day negative-uplift worry resolved on fuller data) →
  execution.passive_exit_enabled flipped true (paper; effective next restart).
  Phase 6 ABORTED (zero stale-touch episodes — strategy precondition never
  occurs). Wallet-aware routing NOT wired: infeasible as specced (no live wallet
  identity in CLOB feeds) — see Phase 4 box.
- 06-15 23:45: first exit-model refit (4-day label gate, amended for the deadline).
  REQUIRES the bot to have restarted after the 06-15 _fit_ridge OOM fix so the
  running process holds the fixed code — otherwise this refit silently writes no
  artifact and the first fit slips to 06-16 23:45.
- 06-16..20: Phase 3 five-day shadow — run `python scripts/shadow_exit_model.py`
  daily. The FIRST run (06-16 morning) freezes the latest artifact as the
  out-of-sample baseline; a late first run silently shortens the shadow
  (--refreeze only moves the cutoff later). 06-20/21: verdict; PASS → flip the
  artifact's deployed flag + wire evaluate_hold consumption.
- 06-15+: box-arb execution + Phase 6 sleeve only if their bars pass; else
  document abort (legitimate completion per plan).
- 06-21/22: buffer + final verification + multi-asset TODO handoff.

The goal is complete when all of the following are true:

- [x] Dead code deleted; replay passes on current regime with gutted codebase
      (L2-L6/SPRT/calibration/optimizer/recommenders gone; exit-policy sweep +
      L1 smoke verified on the gutted code; suite green)
- [x] Passive exit (resting limit + FOK fallback) live in PAPER; kill bar passed
      (kill bar 06-14: ITM fill 83-87% over 4 days, uplift +$0.056/fill at
      mid/10s — both axes clear; execution.passive_exit_enabled:true, effective
      next restart. Rest at _resting_level, conservative prints-through fill,
      timeout → FOK, loss-cut bypass + HOLD-flip cancel intact, 8 unit tests.
      LIVE-CAPITAL passive exit still UNBUILT — LiveTrader has no GTC subsystem,
      stays FOK; that remains a real build, not a flag.)
- [x] Window-path recorder running continuously; 288 windows/day persisted
      (LIVE since 06-11 10:44 — 1 Hz rows + self-labeling confirmed in DB)
- [ ] Exit-value model live, replacing ExitBoundary; nightly refit pipeline running; kill bar passed
      (trainer + nightly refit + degradation keep-back live; first fit 06-15;
      5-day shadow via scripts/shadow_exit_model.py; deploy flag flips at the bar)
- [ ] Wallet fingerprinting pipeline running + classifying nightly = DONE; routing live = BLOCKED
      (PIPELINE LIVE since 06-11: nightly job + 3-day backfill — tape rows in
      gitignored db/wallet_tape.db, wallet_stats aggregate in the per-mode DB;
      classifications real: donors −3..−11c/$1, sharps +12..+37c/$1.
      ROUTING BLOCKER (web-verified 06-14): the spec — "check the book for sharp
      counterparties BEFORE resting" — is infeasible. Polymarket's public/live
      order book is anonymous L2: book = {price,size} per level, tape =
      {price,size,side,ts}, NO maker/taker/owner identity (CLOB market-channel
      docs + py-clob-client OrderSummary). You cannot know who is resting, nor
      who will lift your rest, before you post.
      CORRECTION to the first pass: identity is NOT "only nightly". Live wallet
      identity DOES exist, just never pre-post — (a) the authenticated user
      channel returns the maker_address of YOUR OWN fills at match time; (b) the
      RTDS activity feed (wss://ws-live-data.polymarket.com, the service we
      already use for Chainlink) broadcasts proxyWallet for ALL trades at ~100ms,
      post-match; (c) the Polygon mempool exposes maker/taker ~1.5s pre-confirm.
      None screen the anonymous resting book pre-post. wallet_stats is still
      write-only — no decision-time reader exists.
      REALIZABLE ALTERNATIVE (operator decision; needs the Phase 4 7-day kill bar
      before any deploy): a post-fill learning loop (identify in ~real-time who
      lifts our rests via user-channel maker_address / RTDS, fold their markout
      into wallet_stats) feeding a STATISTICAL regime gate — rest only in
      window/time-of-day/quote/flow contexts where sharps don't dominate. NOT
      built today — surfaced, not implemented unasked.)
- [ ] Box arb monitor running on BTC; executing valid boxes
      (RUNNING since 06-11 — 15m/5m shared-expiry pairs, 96 overlaps/day,
      log-only to memory/recordings/box_arb.jsonl; execution after Phase 1)
- [x] Wide-quote maker sleeve ABORTED with documented reason
      (kill bar 06-14: shadow_wide_quote.py found ZERO stale-touch episodes — the
      book never sat still >=30s through a >=0.5% Coinbase move across 720 labeled
      windows, so the strategy's precondition never occurs in this regime. No EV
      to capture; legitimate abort per the plan. Re-evaluate only if the touch
      behaviour changes — the evaluator stays ready.)
- [x] Nightly pipeline: model refit + wallet table update + Brier/MAE logging
      (jobs registered: exit_model_refit, window_paths_retention, wallet_tables)
- [x] All components verified non-interfering with circuit breaker
      (recorders write-behind; passive exit reuses close_trade + breaker path
      unchanged; suite green)

**One remaining TODO:** Expand passive-exit → exit-value model → wallet fingerprinting to ETH, SOL, XRP five-minute markets. Architecture is parameterized; execution is a symbol loop. Do not start until this BTC goal is complete and all kill bars have passed in production for ≥7 days.

---

## WHAT YOU ARE NOT ALLOWED TO DO

- Add any new entry-side prediction logic
- Expand to non-BTC markets before this goal is fully complete
- Deploy any phase to live capital before its kill bar passes
- Preserve deleted code in comments or dead branches
- Rebuild symmetric market-making under any other name
- Treat the oracle cadence or Chainlink heartbeat as a tradeable signal