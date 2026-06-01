# Model Improvements — making the base model trade better

---

# ⭐ STRATEGIC STATUS & REFRAME — 2026-05-31 (READ THIS FIRST)

> **The headline below ("edge = better P(up) model") was tested with data and is wrong.**
> Everything under "## Model Improvements" still holds as *incremental* model work, but it is
> **not the priority** until a base edge is proven to exist. This section is the live source of
> truth for what we're actually trying to achieve and why.

## What we set out to do
Build a *great* bot. For months the working theory (and the premise of the roadmap below) was:
the edge comes from forecasting P(BTC up this window) more accurately than the market price.
We finally **measured** that theory instead of assuming it.

## What the data actually says (the evidence — measured, not guessed)
- **Model vs market horse race** (1,310 resolved trades + ghosts): model log-loss **1.0520** vs
  market price **0.6700** vs constant base-rate **0.6774**. *Our model is worse than the market by
  a mile, and worse than guessing the base rate.* (Brier: model 0.35 vs market 0.24.)
- **The trades we most want are the ones we're most wrong on:** the "edge ≥ 4pp" cohort (479 trades)
  won **56.4%** while the market priced them at **63.5%**. When the model shouts "edge," the market
  was right and we were wrong — the textbook *optimizer's curse*.
- **Track record (paper):** 763→918+ trades, per-trade Sharpe ≈ **0.002** (statistical zero).
  Bankroll **$132 peak → ~$83** now; resolution-holds (+) and early scalps (−) roughly cancel.
  Counterfactuals show the scalp logic is ~neutral, not the culprit — the missing piece is **edge**.
- **Conclusion:** a better P(up) model is NOT where the edge is. The market already forecasts 5-min
  BTC direction better than we can, and 1.8% taker fees + ~300 trades/day is a hostile fee structure
  for a near-zero forecasting edge. *Confidence: high.*

## What we did about it (2026-05-30/31)
1. **Froze the learning pipeline** (sentinel `memory/state/PIPELINE_FROZEN`; gates `save_config` +
   calibrator save). This is a **one-time measurement freeze**, not the production cadence — it makes
   the next ~2 weeks **one stationary strategy** so its Sharpe/log-loss are interpretable. Reversible:
   delete the file to resume daily adoption.
2. **Built passive microstructure telemetry** (`feeds/microstructure_recorder.py`, two observation-only
   hooks in `main.py`, analyzer `analyze_microstructure.py`). Logs CLOB-book-vs-spot snapshots to
   `memory/microstructure/` (gitignored, local-only). Zero effect on trading.

## The edge hunt — hypotheses & verdicts
Each is a falsifiable test with a **pre-registered** PURSUE/KILL threshold (don't move them after
seeing data). "KILL" = *this specific edge hypothesis is disproven — stop pursuing it*, NOT "kill the
bot." Eliminating dead ends fast is the point.

| # | Hypothesis | Type | Verdict (as of 2026-05-31, ~1 day) |
|---|---|---|---|
| H1 | **Forecasting edge** — out-predict the market price | model | **KILL** — disproven by horse race above |
| H2 | **Latency / stale-quote arb** — pick off a slow CLOB book when spot moves | taker | **KILL (early)** — book staleness p90 = 18ms; reprices ~instantly. No lag to exploit at our speed |
| H3 | **Resolution-lag** — buy the Chainlink-near-certain side at a discount in the last 60s | taker | **KILL (early)** — winning side already priced right; only 9.4% offer a fillable ≥5pp discount |
| H4 | **Fill toxicity** — are we the adversely-selected taker? | risk | **Borderline** — post-fill drift mildly negative; informs the maker case |

*Verdicts H2–H4 are ~1 day of data — directional, not final. The 2-week baseline confirms them
across regimes (esp. a high-volatility stretch, the one thing that could re-open H2/H3).*

## Where this is heading: the MAKER pivot (likely primary direction)
Every KILL above is a **taker** hypothesis, and we *pay* 1.8% to take liquidity. The structural
inversion:
- **Post resting limit orders** at/near the best instead of crossing the spread with FOK.
- **Earn the spread** and flip the fee from a cost toward a maker rebate/zero — the single biggest
  swing in the unit economics.
- Get filled by exactly the impatient taker flow that currently picks *us* off (H4).
- **The real work is risk, not forecasting:** inventory management, adverse-selection on fills,
  quote placement/skew, and binary-resolution exposure on unhedged inventory near expiry.
- **Why it's promising:** there's a sound economic reason a non-HFT participant can earn spread in a
  thin market like Polymarket 5-min BTC, where forecasting and latency edges are closed.
- **What it needs:** a quoting/limit-order execution path (today's `LiveTrader` is FOK-only), a
  maker-fee check on Polymarket, and its own falsifiable paper experiment before any real money.
- *Confidence: moderate — unvalidated, but the logic and the KILLs both point here.*

## Hypothesized edges — running list (add freely; each gets a falsifiable test)
- **Maker / spread capture** (H5) — see above. Front-runner.
- **Cross-venue lead-lag** (H6) — does Binance lead Coinbase/Chainlink on 1–5s horizons? Likely
  overfished by HFT, but cheap to check from data we already log.
- **Session / time-of-day conditioning** (H7) — not a standalone edge, but a real *calibration/sizing*
  improvement (see #1a below) once a base edge exists.
- **Selective participation** (H8) — if we can't predict winners, can we predict *toxic* windows and
  simply not trade them? Filtering the bad half could flip break-even to positive even with no
  forecasting edge. Testable from ghost + outcome data.
- **Weekend / day-of-week effect** (H9) — *early signal, 2026-06-01:* weekday win rate 60–64% vs
  weekend 50–54% (mean gain +5.2% vs −4.1%) in the first ~5 days. Plausible mechanism: BTC weekend
  seasonality (low vol/volume, thin books, chop) makes the vol-scaled L1 overconfident and fills
  worse. **NOT confirmed — N=1 weekend; can't separate "weekend" from "one low-vol regime."** Re-check
  at the end of the frozen run (~2 weekends). If it holds, fix = a weekday/weekend calibration bucket
  (#1a) or simply skip weekend trading. Do NOT act mid-experiment.
- *(add new hypotheses here as we think of them)*

## The asset we already have (genuinely good news)
The **infrastructure is alpha-agnostic and strong** (~82/100 engineering): leak-controlled backtests,
crash-coherent state, calibration gating, ghost tracking, shared live/replay math, full telemetry.
The months of work built **the factory**; the model is just one product line that didn't pan out. A
maker strategy (or any future edge) plugs straight into machinery that already works — we are not
starting over. And we learned the forecasting edge is dead in **paper, at $0 real loss** — the truth
most retail bots only buy with a blown-up live account.

## The plan / definition of done
1. **Now → ~2026-06-13:** let the frozen baseline run (paper). Don't touch params; don't act on daily
   noise. Re-run `python analyze_microstructure.py` ~day 7 (≈2026-06-06) and ~day 14.
2. **If H2/H3 stay KILL through a volatile stretch:** build the **maker** execution path + a falsifiable
   paper experiment (H5).
3. **Only after a real edge is measured:** turn daily learning back on, **tiered** — fast adaptation
   on high-signal inputs (calibration, risk/sizing), slow/evidence-gated adoption on low-signal core
   params (raise the z-floor; adopt on accumulated significance, not one day of noise).

---

## Model Improvements — making the base model trade better

> **[Superseded 2026-05-31]** The premise below — that the edge is a better P(up) model — was
> falsified by the horse race (see Strategic Status above). Keep this roadmap: calibration (#1) still
> improves *sizing* regardless of edge source, and these items remain valid *incremental* work. But
> none of it is the priority until a base edge (likely maker, H5) exists. Do not read the line below
> as the current strategy.

The bot's edge comes from one thing: **how accurately it estimates P(BTC closes up this window).**
The plumbing (feeds, execution, learning pipeline) is sound. This file is the focused roadmap
for the only thing that actually grows edge — a better probability model.

**How the base model works today:** it stacks evidence in logit space — L1 (how far price is from
the strike vs. expected move, using fat-tailed Student-t volatility), L2 (trend/mean-revert regime),
L3+L3b (order-book flow + Coinbase buy/sell pressure), L4 (RSI/MACD/etc. committee), L5 (carry from
last window) — then squashes to a probability and applies an isotonic calibration curve. L6 (extra
derived features) exists but is switched off.

**Ground rule:** everything below lands as a *mechanism* the bot's own backtest/adoption pipeline
turns on **only with evidence**. Nothing here changes live trading until it proves out. Ranked by
payoff-per-effort.

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

## Do first — uses data we already have (cheap, no new feeds)

### 1. Smarter calibration (highest ROI)
Today **one** correction curve fixes the model's overconfidence everywhere. But the model is almost
certainly mis-calibrated *differently* in different conditions — early vs. late in the window, calm
vs. volatile, small-edge vs. big-edge, **and at different times of day / days of week.** Fit a
**separate calibration curve per condition.**
- **Why it helps:** calibration directly drives bet *size*. Sizing each bet on a probability that's
  right *for that condition* improves compounding more than almost anything else.
- **Cost/risk:** each condition needs enough trades to fit, or it overfits — so it ships gated
  (falls back to the single curve until a bucket has the samples + passes the same confidence test).

#### 1a. Time-of-day / session conditioning (do this first — cheapest high-value win)
BTC has strong, persistent **intraday and weekly seasonality** in both volatility and confidence —
the Asia / EU / US sessions, the CME-hours liquidity vacuum, weekend thinness, top-of-hour and
funding-clustering effects. The current model is purely *window-relative* (seconds-remaining), so it
is blind to "this is the 3am ET dead zone" vs. "this is the 9:30am ET equity-open vol spike," and a
single calibration curve averages those regimes together — systematically over- or under-sizing in
each. Add **time-of-day / session as a calibration condition** (the first condition to wire into the
#1 machinery above).
- **Why this one first:** it's built entirely from data **already recorded** (the UTC `timestamp` on
  every trade), so it backtests over your **entire existing history with no warm-up and no new
  feed** — the cheapest possible high-value win, and calibration is the highest-leverage lever.
- **Why calibration (#1) and not an L6 feature:** the effect is primarily one of *confidence / vol
  regime* — exactly what a calibration curve corrects and what drives bet size — rather than a clean
  directional signal. It also reuses #1's existing per-bucket sample-count + bootstrap-CI gate, so
  there's almost no new machinery. (It *can* instead enter as an L6 feature per #2 — e.g. a session
  one-hot or a `sin/cos(hour-of-day)` pair at weight 0 — but #1 is preferred for the reasons above.)
- **How (to avoid overfitting):** bucket by a **small, fixed** session set (e.g. 4–6 buckets: Asia /
  EU / US / overnight, or trading-day quartiles), NOT a 24-way hour split, and **never cross it
  combinatorially** with the other conditions above (condition × hour explodes the bucket count).
  Each session bucket fits its own curve under the same gate, falling back to the global curve until
  it earns the samples.
- **Default-neutral:** with no session buckets fitted, behavior is byte-identical to today's single
  curve — it only ever turns on per-bucket, with evidence.

### 2. Wake up and grow the L6 layer
L6 (derived features) is fully built but every weight is 0 — it's dormant. Add a handful more
features computed from data the model *already sees* (e.g. richer combinations of flow, vol, and
momentum) and let the pipeline discover which earn their weight.
- **Why it helps:** cheapest way to let the model find new *combinations* of existing information.
- **Cost/risk:** low — features start at weight 0 and only turn on if they prove out.

### 3. Expose the model's hard-coded constants as tunable knobs
Several internal constants are still a human's first guess: the flow-agreement discount (0.5), the
L3b flow saturation scale, the L4 mean-revert/trend split. Turn them into knobs.
- **Why it helps:** lets the optimizer improve them with evidence instead of leaving them frozen.
- **Cost/risk:** low — each defaults to today's value, so nothing changes until the pipeline tunes it.

### 4. Let layer weights become regime-aware
The model already changes L4/L5 *behavior* by regime. Go one step further and let the pipeline learn
**different layer weights** in trending vs. choppy markets.
- **Why it helps:** the same signal often means different things in different regimes; weighting it
  accordingly captures that.
- **Cost/risk:** more knobs = bigger search space; gated and pipeline-validated.

---

## Bigger bets — need a new data feed (higher ceiling, more work)

### 5. A sharper volatility estimate in L1 (highest-leverage layer)
L1 is the dominant layer, and it relies on ATR — which is **backward-looking**. Feed it a
**forward-looking** vol input (options-implied vol, or sub-minute realized vol) as a better estimate
or cross-check of the expected move.
- **Why it helps:** L1 sets the core probability; even a small accuracy gain there compounds across
  every trade.
- **Cost/risk:** needs an implied-vol source (or reuse fast realized vol); must prove it beats ATR.

### 6. Give the model information it's currently blind to
The model only sees price, the order book, Coinbase flow, and indicators. The biggest *new* edge
usually comes from new information:
- **Perp funding rate** — leveraged positioning / directional bias.
- **Options-implied vol** — the market's own forward view of volatility.
- **Order-book depth *dynamics*** — how liquidity is building or pulling, not just the snapshot.
- **Combined multi-venue flow** — Binance + Coinbase pressure together, not one venue alone.
- **Why it helps:** this is where genuinely new edge lives.
- **Cost/risk:** each needs a new feed, and most new signals are noise — so each enters as an L6
  feature at weight 0 and survives only if the backtest says it adds real edge. Capture the data
  first, then let the pipeline judge.

---

## The order I'd go in
1–4 use data already in hand and are low-risk — do them first (calibration #1 is the biggest single
win). 5–6 need new feeds and have the highest ceiling but more work. Every one of them goes through
the backtest gate, so the model only ever adopts what actually trades better.
