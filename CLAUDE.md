# PolyBot

5-min BTC Up/Down trader for Polymarket. **The deployable strategy is the
late-window sniper** (§2) — the one edge that survived testing, gated by a
pre-registered kill bar (`tasks/todo.md` is the live status + go-live runbook).
The **base strategy** (§3) runs in paper only as the evidence engine; its
go-live gate failed final on 2026-07-01 and it is barred from real money —
`sniper_only: true` suppresses it on the live flip.

**This file is the single source of truth — update it in the same commit as any
behavioral change.**

## Quick Start

```bash
pip install -r requirements.txt

cp polybot/config/.env.example polybot/config/.env
# Required: DISCORD_BOT_TOKEN (monitoring)
# Live mode also needs: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

python -m polybot.main --mode paper       # paper trading
python -m polybot.main --mode live        # real USDC (needs allowance)
python -m polybot.main --run-pipeline     # one nightly cycle, no trading
python -m pytest polybot/tests/           # full suite
.\scripts\run_polybot.ps1                 # daily cycle: trade -> nightly jobs -> commit -> restart
```

**Go-live flip** (only after the sniper kill bar passes — `tasks/todo.md`):
`settings.yaml` → `mode: live` + `late_window.sniper_only: true`. That is the
complete switch; paper and live share every decision path.

### Secrets

| Key | When |
|---|---|
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_PRIVATE_KEY` | Live mode (EIP-712 signing) |
| `POLYMARKET_FUNDER` | Live mode (USDC funding address) |

---

# Part A — Trading Logic

## 1. The market + the two modes

Every 5 min, Polymarket runs a market: will BTC close higher or lower than at
the window's start? Up/Down ERC-1155 tokens trade $0-$1; the winning side pays
$1/share. Chainlink (via Polymarket RTDS, tracking Coinbase) is the resolution
source; Gamma mirrors it for discovery. Two modes, one engine: **paper**
(realism shim: real CLOB books, FOK semantics, convex slippage,
network-fail/latency jitter calibrated to the measured warm POST RTT
~0.118-0.138s, $1 min, tick snapping) and **live** (`py-clob-client-v2` FOK
against the real CLOB; USDC balance + allowance verified at boot).

## 2. The late-window sniper — the deployable edge

In the final `sniper_late_start_s` (45s) of a window, a sharp Coinbase move
(the resolution venue) can push price past the strike while the CLOB ask lags
~350ms behind. The sniper buys that side at the stale price before the book
reprices.

- **Fire condition** (`SignalEngine.evaluate_late_sniper`): Coinbase move over
  `sniper_move_window_s` (2s) ≥ `sniper_cb_move` ($8) pushed price past strike
  AND the chosen side's ask ≤ `sniper_ask_cap` (0.92). The main loop wakes on
  Coinbase ticks when enabled.
- **What it bypasses**: `max_edge` (→ `sniper_max_edge` 0.50, at both the
  edge-cap and pre-submit gates), the late-window time penalty, and the base
  signal-level gates `min_model_probability`, `min_kelly`, and the ATR
  percentile (deliberate and harness-faithful — the signal is move-driven, not
  prob-driven; `sniper_min_edge` = `min_edge` is the floor and the $1 min-size
  backstops tiny-Kelly fires). Every execution-quality gate stays — adverse
  selection, edge-decay, depth, net-edge, min-size, pre-submit VWAP re-check,
  feed freshness, flip hurdle.
- **Kill bar** (deployment authority, at the host's measured RTT):
  `analyze_late_window.py` momentum `t_day ≥ 2.0` AND block-bootstrap `p10 > 0`
  over ≥ 8 clean ET days, ≥ 6 positive, ≥ 40 fills, net of fee (the PASS print
  enforces all of these; the control-~0 leg is read by eye from its row) —
  PLUS the paper-shadow tracking the harness (`sniper_shadow_status.py`).
  Current status, decision table, and runbook: `tasks/todo.md`.
- **Sniper-only mode** (`late_window.sniper_only`) — the go-live switch:
  base-entry BUYs are suppressed and recorded as `sniper_only` ghosts (free
  evidence), capital deploys only on sniper fires. ON in the paper shadow too,
  so the shadow trades the same fire population live would (with it off, the
  base path consumed the high-conviction sniper moments first). Live recipe:
  `mode: live` + `sniper_enabled: true` + `sniper_only: true`.
- **Post-live kill rule**: re-run the harness every 2-3 days; trailing-4-day
  lenient mean < +2¢/sh or trailing-8-day t < 2 → set `sniper_enabled: false`.

## 3. The base strategy (paper-only evidence engine)

Entry = inventory sourcing off a single fair-value model; **no proven edge**
(binding 10-day read failed 2026-07-01: t_day +1.07; paper P&L swings are
BTC-vol variance). It keeps running in paper because it exercises the exit
engine and generates the outcome/ghost/counterfactual evidence stream.

**The L1 model** (the only model — never rebuild entry-side prediction):

```
ac         = clamp(lag1_autocorr(closes, regime_lookback), ±0.5)
vol_scaled = (max(atr, atr_floor) / atr_sigma_ratio) * sqrt(minutes_remaining) * sqrt((1+ac)/(1-ac))
z          = (btc_price - strike) / vol_scaled
prob_up    = StudentT_CDF(df, z * sqrt(df/(df-2)))        # df clamped ≥3
```

- `student_t_cdf` + df clamp + `autocorr_vol_scale` in `core/aux_layers.py`.
- ATR floor is dynamic: `max(min_atr, 0.30*rolling_20)`, widened when
  `rolling_20/long_term_200 < atr_regime_shift_threshold`. ATR buffers keep one
  slot per 1-min candle (entry + exit ticks share the dedup).
- `btc_price` from Coinbase WS only (<2s stale → decision skipped, never
  zeroed) — the venue Chainlink resolves against.
- L1 is uncalibrated (`model_probability` == `model_probability_raw`).

**Entry gates** — edge = `model_prob - market_price`; all must pass:

| Gate | Threshold |
|---|---|
| Chosen-side prob | ≥ `min_model_probability` (0.56) |
| Edge | ≥ `min_edge` (0.04; flip premium scales it — §6) |
| Kelly (fee-aware) | ≥ `min_kelly` (0.01); `b_eff = b*(1-fee_rate)` |
| Spread | `spread/2 + EFFECTIVE_FEE_PEAK ≤ max_spread` (0.10); both sides unavailable = skip |
| Book depth | ≥ `min_book_depth_usd` ($50) on the chosen side |
| Price sum | `price_up + price_down ∈ [0.98, 1.02]` |
| Book freshness | both sides' WS books ≤ 10s old |
| Edge cap | edge ≤ `max_edge` (0.20) — wider = stale phantom price |
| ATR | ≥ 5th percentile (lower bound only) |
| Adverse-selection hard skip | `adverse_rate_at_30s ≥ 0.80` |
| Edge-decay | mean 15s post-fill drift ≥ −0.05 (needs ≥15 resolved fills/30min) |
| Net edge after slippage | `edge − price*est_slip ≥ min_edge` |
| Pre-submit re-check | FOK VWAP keeps net edge in `[min_edge, max_edge]` |
| Min order | ≥ $1 (CLOB floor; paper mirrors live) |
| Feed staleness | Coinbase ≤30s, Chainlink ≤60s, Binance aggTrade ≤30s, kline ≤45s |

Adverse selection also scales size:
`kelly_mult = max(0.30, 1 − 1.5*max(0, adverse_rate − 0.45))`, Bayesian-shrunk.

**Ghosting**: every gate veto records a ghost (`GhostTracker`) that resolves at
window close — the evidence stream for gate evaluation.

## 4. Sizing (applies to both strategies)

```
size  = bankroll * kelly * circuit_breaker_mult * time_mult * adverse_kelly_mult
size *= concurrent_multiplier(side, market, opens)     # correlation-aware
size  = min(size, bankroll * max_bankroll_deployed)    # 0.80
size  = min(size, side_depth * max_book_fill_pct)      # 0.50
if size < 1.0: skip
```

`kelly` is the fee-aware Kelly already scaled by `math.kelly_fraction` (0.08) —
fractional Kelly, not full.

- **Circuit breaker**: tier-locked floor at $100/150/200/... milestones
  (floor = tier × 0.85; sqrt Kelly interpolation down to 0.40×; tier never
  resets down; persists via `peak_bankroll`).
- **Time multiplier**: full Kelly for the first 60% of the window; after,
  penalty scales by (1 − conviction) up to 0.30. (Sniper bypasses this.)

## 5. Orders

FOK via `py-clob-client-v2`, up to 3 attempts with jittered backoff — only
provably-unposted failures retry (exchange-confirmed rejects + pre-POST local
errors); ambiguous outcomes never resubmit (double-fill guard). **Latency is at
the floor** (~120ms warm POST RTT, measured): SELL signatures pre-armed on
prior HOLD ticks (BUY pre-signs concurrently with the submit — a best-effort
race; inline sign is ~3-5ms), WS-only book pre-check, BUY fill VWAP from WS
trade events (SELL fill price via REST after the fill, off the latency path),
tick-size/neg-risk/fee + contract-version caches prewarmed per window,
warm pooled HTTP/2 singleton (keepalive_expiry 60s > 5s ping, connect timeout
5s, TCP_NODELAY). The only remaining lever is geographic
(docs/DEPLOY_ORACLE_VPS.md, ~40ms EU VPS). Live boot: key+funder required,
balance/allowance preflight, allowance recheck every 10 fills. Per-trade DB
writes are atomic. `fill.fill_size` is always USDC notional.

## 6. The exit engine + flips (manages every position, sniper included)

Every tick while holding, re-run L1 and decide HOLD vs EXIT
(`SignalEngine.evaluate_hold`). `holding_edge = model_prob - bid`; the exit
threshold blends `exit_edge_threshold` (−0.10) with the binary-payoff
`ExitBoundary` curve (deep-ITM patience, OTM urgency, ATM fee-aware time
value); the blend is stamped so the phantom-bid SELL re-verify gates against
the same number.

Branches in order: **loss-cut** (market < entry×0.65, <90s left, BTC wrong
side of strike by >0.5×ATR; locks the loss only when holding_edge ≤ 0, else
HOLDs the residual), **deep-loss hold** (holding_edge < −0.10 and
market < entry → binary residual beats locking the loss), **scalp**
(holding_edge ≤ effective threshold, outside the whipsaw cushion), else
**hold**. No confidence overrides.

Re-entry after a scalp (one position per window; `max_concurrent_positions`
caps across windows) requires clearing a flip hurdle:
`min_edge + max(flip_premium, spread + 2*fee_rate*p*(1-p))`, premium
`0.015 + 0.005*max(0, flips-2)`.

**Resolution**: Chainlink decides; winner $1/loser $0 credited atomically.
Exit price is oracle-first (`event_metadata` final_price vs price_to_beat;
Gamma resolved prices as fallback; never Binance); Chainlink orphan fallback
after ~30 min of Gamma silence. Live redeems settle non-blockingly.

**Passive exits** (`passive_exit_enabled`): OFF — measured −2.1¢/sh ≈ −$62/day
(t_day −2.03) over 8 clean days. The code path (paper tape-sim, live GTD rest +
FOK fallback, maker-rebate accounting kept OUT of pnl) remains implemented and
tested but stays off; never a reason to rest new quotes.

## 7. Recorders + evidence stream

- **Window-path recorder** (`recording.py`, in-process; 1 Hz, 5 Hz in the final
  45s): both tokens' BBO + touch sizes + top-3 depth + book ages, Coinbase
  price/BBO/CVD, Chainlink live price + age (the resolution venue), strike,
  Binance price/CVD/depth20-sides, live-L1 ATR + prob stamp (NULL on cold
  feeds, never 0.0) for EVERY window (~288/day, self-discovering). Tables
  `window_paths` (gitignored sidecar DB) / `window_labels`; 90-day retention
  nightly. **This is the sniper kill-bar feed, the post-live kill-rule input,
  and the pivot-research corpus — everything already flowing through the
  process gets persisted.**
- **Tape recorder**: every CLOB print (incl. the exchange's own timestamp +
  fee_rate_bps) → `memory/recordings/*.jsonl` (gitignored).
- **Wallet fingerprints** (`wallets.py`): nightly data-api ingestion →
  per-wallet markout → donor/noise/sharp (`wallet_stats`; write-only, no
  decision-time reader).
- **Per-decision records**: `trade_context` stamped into outcomes + ghosts
  (entry facts, model prob, flow/CVD telemetry, book aux, adverse audit
  fields). **None-vs-0.0 is load-bearing**: cold feeds record `None`, never
  0.0. `CounterfactualTracker` records both arms of every scalp/hold — the
  ground truth for exit-policy changes (score via `actual − cf`, never a naive
  signed sum of `delta_pnl`).
- **NightlyScheduler** (23:45 ET): record rollups + retention sweep + wallet
  tables.

## 8. Hard rules

- No early/mid-window entry-side prediction (ML or rules) — the CLOB price
  wins; the final-45s sniper is the one sanctioned exception, through its bar.
- The base strategy never deploys live. No deployment before a kill bar passes;
  never relax a bar to pass it.
- No symmetric market-making, no oracle-cadence trading, no expansion past BTC
  until the goal completes (`tasks/todo.md`).
- No Gaussian, no Binance resolution, no mid-price edge math (executable CLOB
  BBO only). Never skip the fee: `rate*shares*p*(1-p)`, rate 0.07
  (`DEFAULT_FEE_RATE`); flat-additive gates use `EFFECTIVE_FEE_PEAK` 0.0175 —
  never mix them.
- `gain_pct = pnl/size`, never log_return. Don't bypass the circuit breaker.
  Don't delete `polybot/db/polybot_*.db`.

---

# Part B — Operations

## 9. Project layout

```
polybot/
  main.py                Trading loop; entry/exit/sizing orchestration; sniper hook
  config/                settings.yaml, loader.py, param_registry.py (defaults + ranges)
  core/                  signal_engine (L1 + exit engine + sniper), exit_boundary,
                         returns, adverse_selection, order_flow, aux_layers
  feeds/                 coinbase_feed (primary BTC + CVD), binance_feed (candles/ATR),
                         binance_depth, binance_trades, chainlink_feed (strike/resolution),
                         clob_ws (books/tape), market_scanner (discovery + gamma fallback),
                         _socket, _staleness, _json
  indicators/            ATR engine
  recording.py           WindowPathRecorder (all windows) + TapeRecorder + retention
  wallets.py             wallet fingerprinting (nightly)
  execution/             base (BaseTrader, fee math), paper_trader, live_trader,
                         circuit_breaker, correlation
  agents/                scheduler, outcome_reviewer, counterfactual_tracker,
                         ghost_tracker, pipeline_analytics
  memory/                outcomes/, ghost_outcomes/, counterfactuals/ (+ rollups);
                         recordings/ (gitignored); state/. Layout: polybot/paths.py
  discord_bot/           monitoring + control commands (§12)
  db/models.py           SQLite per mode (positions, trade_history, bankroll,
                         peak_bankroll; window_labels + wallet_stats live here
                         too; window_paths + wallet_trades sit in gitignored
                         sidecar DBs — window_paths.db, wallet_tape.db)
scripts/
  run_polybot.ps1        daily supervisor (Linux port: run_polybot.sh + polybot.service;
                         VPS runbook: docs/DEPLOY_ORACLE_VPS.md)
  analyze_late_window.py sniper kill-bar harness (RTT-parametric; --rtt-sweep --max-slip)
  sniper_shadow_status.py  paper-shadow fills vs the harness
  verify_keys.py         live preflight: GET-auth + balance/allowance
  smoke_order_test.py    live preflight: one unfillable FOK proves order POSTs
                         clear Cloudflare (verify_keys covers GETs only)
  reset_paper_clean.py   clean-slate the paper ledger (operator-run, bot STOPPED)
```

## 10. Data sources

| Source | Feed | What |
|---|---|---|
| Coinbase | `ticker` WS (BTC-USD) | Primary BTC price + CVD + 1s history |
| Binance.com | `kline_1m` / `depth20@100ms` / `aggTrade` WS | Candles, ATR, depth, cross-venue gap |
| Polymarket CLOB | WS + `GET /price`, `/book`, `/spread`, `/tick-size` | Books, tape, executable prices |
| Polymarket Gamma | `GET /events?slug=` (deprecated upstream; auto-fallback `GET /events/slug/{slug}` — `gamma_events_by_slug`) | Discovery + resolution + labels |
| Polymarket data-api | `GET /trades?market=` | Wallet-tagged taker tape |
| Chainlink (RTDS WS) | `wss://ws-live-data.polymarket.com` | Strike + resolution price |

## 11. Running + invariants

`run_polybot.ps1`: starts 12:01 AM ET, stops trading 11:30 PM ET, nightly jobs
11:45 PM ET, commits + pushes `origin main` on a clean exit, restarts at
midnight. **Single-instance guarded**: the wrapper refuses to start if another
is running, and `polybot.main` holds an OS single-instance lock
(localhost-port bind). Live preflight: `verify_keys.py` then
`smoke_order_test.py --confirm`.

- UTC for storage; ET (`America/New_York`) only for date-bucketing + trading
  windows. Daily rollups bundle per-trade JSON; readers glob both.
- Recordings (`memory/recordings/`) are gitignored — never in the nightly
  commit. `memory/` records + per-mode DB + settings.yaml are committed
  nightly.
- Kill bars are the deployment authority (`tasks/todo.md` = status + runbook).

## 12. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all] confirm`
`!session` `!pipeline` `!commands` — `!pause` halts new entries only; `!clear`
purges Discord messages only.
