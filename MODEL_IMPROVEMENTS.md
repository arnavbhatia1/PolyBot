# Model Improvements — making the bot trade better

**What actually makes money — read this first.** The bot's realized edge is its
**exit policy + Kelly sizing**: cut losers small, ride winners to $1, and size each bet
by conviction. Held to resolution the raw forecast *loses* — at a 5-minute horizon BTC
direction is close to a coin flip, and the model's directional hit-rate sits *below* the
price-implied breakeven. So the highest-value model work is whatever makes **bet-sizing
honest** (calibration) and **protects that exit alpha** — **not** forecasting direction
better, which has a near-zero empirical ceiling at this horizon. Rank everything below by
that lens. Judge every change on **dollar P&L (size-weighted)** — the metric the edge
actually lives in — never equal-weighted per-trade averages, and never at the cost of
degrading the exit policy.

**How the base model works today:** it stacks evidence in logit space — L1 (how far price
is from the strike vs. the expected move, using fat-tailed Student-t volatility), L2
(trend/mean-revert regime), L3+L3b (order-book flow + Coinbase buy/sell pressure), L4
(RSI/MACD/etc. committee), L5 (carry from the last window) — squashes to a probability,
then applies an isotonic calibration curve that corrects the model's overconfidence so bet
*size* is honest. L6 (extra derived features) exists but is switched off.

**Ground rule:** everything below lands as a *mechanism* the bot's own backtest/adoption
pipeline turns on **only with evidence**. Nothing here changes live trading until it proves
out on realized-fill dollar P&L.

## The rule for every addition: additive, never destructive

Your accumulated trade history is the bot's most valuable asset. Adding to the model must never put
it at risk or force a fresh start. **Every** addition — these and any future one — follows these rules:

1. **Default to neutral.** A new feature/term ships at weight 0 (a new knob at its current value), so
   the day it's added the bot behaves exactly as before. The pipeline raises it off neutral only with
   backtested evidence.
2. **Never remove what exists.** The current layers (L1–L6, calibration) and their logic stay. The
   pipeline may tune a weight up *or* down on evidence — that's the bot learning, not deletion. The
   mechanisms and your data are never removed.
3. **Only ADD record fields — never rename or delete them.** A new input adds a new key to each trade's
   record; old records are always read with a safe default for missing keys, so your full history stays
   valid and usable forever.
4. **Never delete the trade records.** `outcomes/` `ghost_outcomes/` `counterfactuals/` are append-only
   and git-tracked; no addition touches them.
5. **Weights persist.** `settings.yaml` is never reset by an addition; the pipeline keeps tuning from
   exactly where it is.

**In practice:** a feature built from data you *already record* can be backtested over your **entire
existing history the moment it's added — no warm-up.** A feature needing a brand-new input (funding,
implied vol) keeps all old data intact and just gathers the new input going forward until there's
enough to judge it. Neither case ever "retrains from scratch."

---

## Tier 1 — make sizing honest (the proven lever; data in hand)

Calibration leads the roadmap because it's the **sizing** lever, and sizing is where the edge is. It
doesn't try to predict direction better — it corrects how confident the model is allowed to be, so each
bet is sized on a probability that's actually true.

### 1. Smarter calibration (highest ROI)
The foundation — a single **global** isotonic curve recalibrating **P(up)** across the full [0,1] range
(fit and served in the same P(up) domain) — is live: it corrects overconfidence and drives honest bet
size, and is the single highest-value model change made to date. That global curve is the workhorse.
It carries two **tail-overconfidence guards** — a data-justified output clamp [0.15, 0.85] plus Beta-prior
smoothing of sparse extreme bins — so a few lucky high-confidence trades can't slam the curve to ~1.0 and
trick Kelly into max-sizing a false certainty (the failure mode that bought overpriced "certainties"); see CLAUDE.md §2.
The **next** layer is **per-condition** calibration: the model may be mis-calibrated *differently* in
different conditions — early vs. late in the window, calm vs. volatile, small- vs. big-edge, time of
day / day of week. Fit a **separate curve per condition**, gated per-bucket — but adopt it **only with
evidence the global curve's residuals genuinely differ by that condition**, measured on *post-calibration*
data. Otherwise it overfits and adds nothing over global.
- **Why it helps:** calibration directly drives bet *size* — the proven edge. Sizing each bet on a
  probability that's right *for that condition* compounds harder than almost anything else.
- **Cost/risk:** each condition needs enough trades to fit or it overfits — so it ships gated (falls
  back to the global curve until a bucket has the samples + clears the confidence test). **Premature
  until the global curve has traded selectively long enough to show condition-dependent residuals.**

#### 1a. Time-of-day / session conditioning (first per-condition candidate, hard-gated)
BTC has persistent **intraday and weekly seasonality** in volatility and confidence — Asia / EU / US
sessions, the CME-hours liquidity vacuum, weekend thinness, top-of-hour and funding-clustering effects.
The model is purely *window-relative* (seconds-remaining), so it's blind to "3am ET dead zone" vs.
"9:30am ET equity-open vol spike," and a single calibration curve averages those regimes together —
systematically over- or under-sizing in each. Add **time-of-day / session as a calibration condition**
(the first condition to wire into #1's machinery).
- **Why this one first:** built entirely from data **already recorded** (the UTC `timestamp` on every
  trade), so it backtests over your entire history with no warm-up and no new feed — the moment there's
  evidence to justify it.
- **Why calibration, not an L6 feature:** the effect is one of *confidence / vol regime* — what a
  calibration curve corrects and what drives bet size — not a clean directional signal. It reuses #1's
  per-bucket sample-count + bootstrap-CI gate, so there's almost no new machinery.
- **How (to avoid overfitting):** bucket by a **small, fixed** session set (4–6 buckets: Asia / EU / US
  / overnight, or trading-day quartiles), NOT a 24-way hour split, and **never cross it combinatorially**
  with the other conditions (condition × hour explodes the bucket count). Each bucket fits its own curve
  under the same gate, falling back to global until it earns the samples.
- **Gated + default-neutral:** with no session buckets fitted, behavior is byte-identical to today's
  single curve. **Premature by its own gate until post-calibration data shows session-dependent
  residuals — and there is no such data yet.**

---

## Tier 2 — tune what already exists (cheap, low-risk, not new forecasting)

These add no forecasting surface — they let the optimizer improve mechanics that are currently a human's
first guess. Each defaults to today's value, so nothing changes until the pipeline tunes it on evidence.

### 2. Expose the model's hard-coded constants as tunable knobs
Several internal constants are still a first guess: the flow-agreement redundancy discount (0.5), the
L3b flow saturation scale, the L4 mean-revert/trend mix constants. Turn them into knobs the optimizer
can move with evidence.
- **Why it helps:** lets the pipeline improve existing combination logic instead of leaving it frozen —
  without widening the model's forecasting surface.
- **Cost/risk:** low — each defaults to today's value, so nothing changes until the pipeline tunes it.

### 3. Let layer weights become regime-aware
The model already changes L4/L5 *behavior* by regime. Go one step further: let the pipeline learn
**different layer weights** in trending vs. choppy markets.
- **Why it helps:** the same signal often means different things in different regimes; weighting it
  accordingly captures that — reweighting existing signals, not adding new ones.
- **Cost/risk:** more knobs = bigger search space; gated and pipeline-validated.

---

## Tier 3 — forecasting improvements (low ceiling at 5-min; hard-gated, do last)

**Honest caveat:** everything in this tier tries to predict direction better. Your own data says that's
the *weak* lever at this horizon — the forecast is ≈ noise and held-to-resolution loses. So each is
strictly evidence-gated, expected to move the needle little, and worth pursuing only after the sizing
levers (Tier 1–2) are exhausted. They're here for completeness, not because they're promising.

### 4. Wake up and grow the L6 layer
L6 (derived features) is fully built but every weight is 0 — dormant. Add a handful of features computed
from data the model *already sees* (richer combinations of flow, vol, and momentum) and let the pipeline
discover which earn their weight.
- **Why it might help:** cheapest way to let the model find new *combinations* of existing information.
- **Cost/risk:** low mechanically (weight 0 until proven) — but it's forecasting, so the expected payoff
  is low; don't mistake "cheap to try" for "likely to work."

### 5. A sharper volatility estimate in L1 (the best-justified forecasting item)
L1 is the dominant layer and relies on ATR, which is **backward-looking**. Feed it a **forward-looking**
vol input (options-implied vol, or sub-minute realized vol) as a better estimate or cross-check of the
expected move.
- **Why this ranks above the others here:** a better vol estimate mostly sharpens the model's *confidence*
  (the z-score magnitude), which feeds **sizing** through calibration — closer to the proven lever than
  picking direction. L1 sets the core probability, so even a small accuracy gain there compounds across
  every trade.
- **Cost/risk:** needs an implied-vol source (or fast realized vol), and must prove it beats ATR on
  dollar P&L.

### 6. Give the model information it's currently blind to
The model only sees price, the order book, Coinbase flow, and indicators. New information is where
genuinely new edge *could* live — but most new signals are noise:
- **Perp funding rate** — leveraged positioning / directional bias.
- **Options-implied vol** — the market's own forward view of volatility (also feeds #5).
- **Order-book depth *dynamics*** — how liquidity is building or pulling, not just the snapshot.
- **Combined multi-venue flow** — Binance + Coinbase pressure together, not one venue alone.
- **Cost/risk:** each needs a new feed, and most new signals are noise — so each enters as an L6 feature
  at weight 0 and survives only if the backtest says it adds real dollar edge. Capture the data first,
  then let the pipeline judge.

---

## The order I'd go in
**Tier 1 first** — calibration is the biggest win because it's the sizing lever. The global curve is
live; per-condition (1a) waits for evidence that residuals differ by condition on *post-calibration*
data, which doesn't exist yet. **Tier 2 next** — low-risk knob/weight tuning on what already exists.
**Tier 3 last** — forecasting bets with a low empirical ceiling at this horizon; hard-gated, and don't
expect much. Every one goes through the backtest gate on **dollar P&L**, and nothing is allowed to
degrade the exit policy that is the actual edge.
