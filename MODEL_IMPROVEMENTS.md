# Model Improvements — making the base model trade better

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
vs. volatile, small-edge vs. big-edge. Fit a **separate calibration curve per condition.**
- **Why it helps:** calibration directly drives bet *size*. Sizing each bet on a probability that's
  right *for that condition* improves compounding more than almost anything else.
- **Cost/risk:** each condition needs enough trades to fit, or it overfits — so it ships gated
  (falls back to the single curve until a bucket has the samples + passes the same confidence test).

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
