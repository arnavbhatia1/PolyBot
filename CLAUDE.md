# PolyBot

5-min BTC Up/Down trader for Polymarket. The **late-window sniper** (§2) is the
bot's **only strategy** — base entries are always suppressed (no toggle).
It passed its SIM kill bar and went live 2026-07-04, but the post-live read
failed its bar: the +9-10¢/sh SIM figure is a full-population REPLAY ceiling no
real bot can reach (the CONTROL — buy spot-side at ask — nets ~0, so G-M holds
and the whole apparent edge is stale-ask capture). The bot fires ~9-16×/day
(catches ~17-20% of the ~56 qualifying windows), and that caught subset is
ADVERSELY selected: realized is **measured NEGATIVE and degrading** — live
−4¢/sh (t −0.6, trailing-4d −7¢, days +7/+13/−1/−13/−28¢), paper-shadow
trailing-4d −5¢. The corrected-to-the-cent strike (2026-07-08) did NOT change
this — a 16-agent walk-forward re-analysis on the correct strike found no config
or selection filter beats current out-of-sample, and "only profitable/never-lose"
is mathematically impossible (irreducible terminal-flip floor 2.6-8%). Now
**re-validating in paper** (`mode: paper`, `sniper_enabled: true`) on the
post-fix code (no realized corrected-strike fills exist yet); the **binding
deployment gate is the paper-shadow's REALIZED fills, not the harness** (§2). The **base strategy**
(§3) has no proven edge, never touches real capital, and survives only as the
zero-capital ghost/counterfactual evidence stream the gate needs.

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

**The live recipe**: `settings.yaml` → `mode: live` + `late_window.sniper_enabled:
true`. That is the complete switch; paper and live share every decision path, and
the sniper is the only strategy either can run.

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
source; Gamma mirrors it for discovery. The per-window **decision strike** is
Chainlink's **first btc/usd report at/after the window-boundary timestamp**
(`chainlink_feed.get_strike`, via RTDS; `_compute_strike_and_btc`) — the exact rule
Polymarket's `price_to_beat` uses (the same data stream it resolves on, matched at
+0ms; verified bit-exact against Gamma's served value), captured live and available
~ms into the window. Recording the last tick *before* the boundary instead missed
the official round by >$8 in a fast open (~1% of windows flipped side). Gamma's
`event_metadata.price_to_beat` is the RESOLVED truth, but served late/unreliably
in-window (whole windows never get it): when present it WINS (it covers RTDS
delivery holes, where our first-received at/after-boundary report is not
Polymarket's — measured ~1-2% of windows, up to $35+ off); otherwise the Chainlink
capture carries. A capture landing > 2s past the boundary (`strike_reliable`,
own-report basis: the ~1Hz RTDS heartbeat puts the true report inside
[boundary, boundary+1s], so a later capture means it was missed; pre-boundary
gaps don't veto) still serves the base path but is UNTRUSTED — the sniper never
deploys capital on it (`_strike_trusted`). Two modes, one
engine: **paper**
(realism shim: real CLOB books, FOK semantics, convex slippage, latency
SAMPLED inverse-CDF from the LIVE ledger's measured order-path POST-RTT
distribution (latency_stats.json → `_LATENCY_QUANTILES`) × `paper_latency_scale`
0.70 — the VPS's MEASURED warm signed-order RTT, p50 304ms from six smoke FOKs;
network-fail sim; $1 min, tick
snapping; FOK kill/fill stats recorded to `fill_stats_paper.json` in live's
schema so paper-vs-live kill rates are directly comparable) and **live**
(`py-clob-client-v2` FOK
against the real CLOB; USDC balance + allowance verified at boot).

## 2. The late-window sniper — the deployable edge

In the final `sniper_late_start_s` (45s) of a window, a sharp Coinbase move
(the resolution venue) can push price past the strike while the CLOB ask lags
~350ms behind. The sniper buys that side at the stale price before the book
reprices.

- **Fire condition** (`SignalEngine.evaluate_late_sniper`): Coinbase move over
  `sniper_move_window_s` (2s) ≥ `sniper_cb_move` ($8) pushed price past strike
  AND the chosen side's ask ≤ `sniper_ask_cap` (0.92). Both are the realized-
  profitable 07-03..07-05 values; the sim grid favored $12 / 0.80 but the sim is
  a full-population ceiling that did not translate (see the Fill bullet). The
  real adverse-selection defense is `sniper_fok_slip`, not these two knobs. The
  main loop wakes on Coinbase ticks when enabled.
- **Fill**: the sniper FOK limit pads the decision ask by only `sniper_fok_slip`
  (0.01, ~one tick) then dies — the pad absorbs benign jitter, but a genuine
  reprice KILLS the order, and that kill IS the adverse-selection filter (a book
  repricing away = the move is reverting → sit out). Capped at `model_prob −
  min_edge` so a true reprice can never fill below the edge floor. Realized paper
  fixed the pad size (96 fills / 9 ET days): clean fills (slip ≤ one tick) net
  +9¢/sh at 70% win; a wider pad admits reverting-move fills measured −16¢/sh —
  it turned 07-07's −15¢/sh into an all-day chase where one tick sits it out. Base entries keep the tight at-ask limit (a reject on adverse movement
  is a feature there). All gates run at the decision ask (harness-faithful);
  the pre-submit VWAP re-check still vetoes books that lost the edge. The booked
  entry is the CLOB's TRUE fill VWAP — resolved WS-tape → balance-delta →
  `associate_trades` REST (retry budget covers the ~100-300ms indexer lag) →
  loudly-logged limit fallback, corrected by the +8s audit, which recovers the
  gross VWAP from the wallet's chain-true net shares when the data-API serves
  `avgPrice 0.0` (it did for 5/7 fresh positions). CAVEAT on the pre-07-08 live
  ledger: those 46 fills booked the padded limit (silent fallback + a defeated
  audit) — chain-truth reconstruction puts them ≈ breakeven, ~4.4¢/sh better
  than the ledger's −4.3¢/sh; read that era's kill-rule prints accordingly.
- **What it bypasses**: `max_edge` (→ `sniper_max_edge` 0.50, at both the
  edge-cap and pre-submit gates), the late-window time penalty, and the base
  signal-level gates `min_model_probability`, `min_kelly`, and the ATR
  percentile (deliberate and harness-faithful — the signal is move-driven, not
  prob-driven; `sniper_min_edge` = `min_edge` is the floor and the $1 min-size
  backstops tiny-Kelly fires). Every execution-quality gate stays — adverse
  selection, edge-decay, depth, net-edge, min-size, pre-submit VWAP re-check,
  feed freshness, flip hurdle. **Sizing is market-anchored**: sniper Kelly is
  computed on `ask + sniper_min_edge` (the defended edge at market odds), never
  on raw L1 prob — L1 is ~+17pp overconfident conditional on firing (calm-vol
  ATR during the burst + winner's-curse selection) and Kelly on the phantom
  edge upsized exactly the losing fires. Entry floor and exit engine still use
  L1; the FOK-limit cap stays on L1 (anchoring it would zero the pad).
- **Kill bar — two gates; the harness is only the first.** (1) The
  `analyze_late_window.py` momentum read (`t_day ≥ 2.0` AND block-bootstrap
  `p10 > 0` over ≥ 8 clean ET days, ≥ 6 positive, ≥ 40 fills, net of fee,
  control ~0 by eye) is a full-population REPLAY CEILING — it fires on all
  ~58 windows/day and is BLIND to the latency-driven adverse selection that
  leaked ~16¢ live, so it is NECESSARY BUT NOT SUFFICIENT (the go-live that
  failed trusted it alone). (2) The BINDING gate is the **paper-shadow's
  realized fills** (`sniper_shadow_status.py` / `live_health_read`): ≥ 8 clean
  ET days, equal-weight net ≥ +2¢/sh, `t_day ≥ 2`, `p10 > 0`, AND a
  shadow-vs-harness gap < 3¢. Never deploy real capital on the harness print
  alone. The nightly health job (§7) re-reads both in production.
- **The sniper is the ONLY strategy** — not a toggle. Base-entry BUYs are
  ALWAYS suppressed (unconditional in `main.py`; recorded as `sniper_only`
  ghosts — free zero-capital evidence for the gate). There is no `sniper_only`
  lever any more; capital only ever deploys on sniper fires, in paper and live
  alike. `sniper_enabled` (default `false`) is the separate kill-bar SAFETY —
  the emergency brake, not a strategy choice. Recipe: `mode: live` +
  `sniper_enabled: true`.
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
errors); ambiguous outcomes never resubmit (double-fill guard). **The software
path is at the floor (~20-35ms incl. sign). Order-POST RTT measured p50 0.436s
/ p75 0.679s (latency_stats.json, 80 samples, zero ≤0.25s) — but probed warm
GETs AND unauth POSTs to /order run ~125-130ms from this host, so ~310ms of a
real order's RTT is Polymarket's SERVER-SIDE pipeline (auth/risk/FOK matching),
irreducible client-side; an EU VPS shaves only the ~130ms network leg to ~40ms.**
SELL signatures pre-armed on
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
after ~30 min of Gamma silence. Winner payouts book via Polymarket auto-redeem
(the bankroll sync waits for the winning tokens to clear).

Resolved shares are not swept on-chain — winners are claimed manually at
polymarket.com/portfolio (or via Polymarket's Auto-Redeem), and losing $0 stubs
have no UI redeem so they sit inert on the wallet, locking nothing. The startup
wallet-check reports any unclaimed winners honestly (never "worthless leftovers
ignored"); the redeemable-aware orphan gate lets resolved dust through and
fail-closes only on genuinely unresolved positions.

Exits are always taker FOK — no passive/maker/resting path exists (it was
measured −2.1¢/sh ≈ −$62/day over 8 clean days and moot under the <45s sniper, so
it was removed). Never re-add resting quotes.

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
- **Micro-tape** (`MicroTape`): event-true streams the 5Hz sampler can't see —
  every CLOB best-bid/ask CHANGE + every Coinbase tick (final 90s of each
  window) and every Chainlink RTDS report (always; payload + receipt ts, so
  delivery holes are measurable) → `memory/recordings/micro_*.jsonl`
  (gitignored). This is what lets future replays model FOK reachability
  against the true book trajectory instead of a sampled ceiling.
- **Per-decision records**: `trade_context` stamped into outcomes + ghosts
  (entry facts, model prob, flow/CVD telemetry, book aux, adverse audit
  fields). **None-vs-0.0 is load-bearing**: cold feeds record `None`, never
  0.0. `CounterfactualTracker` records both arms of every scalp/hold — the
  ground truth for exit-policy changes (score via `actual − cf`, never a naive
  signed sum of `delta_pnl`).
- **NightlyScheduler** (23:45 ET): record rollups + retention sweep + the
  **sniper-edge health report** (`_sniper_health_job`, skipped when
  `sniper_enabled` is false — reports BOTH the SIM corpus (`health_read`,
  window_paths, modeled at the measured 0.44s RTT) and the REALIZED fills for
  the current mode (`live_health_read`: live → polybot_live.db; paper →
  polybot_paper.db scoped to `late_window.validation_epoch`, the BINDING
  paper-shadow gate) side by side with their ¢/sh gap, and drives the kill-rule
  verdict off the realized ledger once fills exist; alert-only, never flips
  config). Pings
  Discord `#polybot-daily` (✅/⚠️/⏳ sniper).

## 8. Hard rules

- No early/mid-window entry-side prediction (ML or rules) — the CLOB price
  wins; the final-45s sniper is the one sanctioned exception, through its bar.
- The base strategy never deploys live. No deployment before a kill bar passes;
  never relax a bar to pass it.
- No symmetric market-making, no oracle-cadence trading, no expansion past BTC
  until the goal completes.
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
  config/                settings.yaml (THE single config source), loader.py (loads + range-validates it)
  core/                  signal_engine (L1 + exit engine + sniper), exit_boundary,
                         returns, adverse_selection, order_flow, aux_layers
  feeds/                 coinbase_feed (primary BTC + CVD), binance_feed (candles/ATR),
                         binance_depth, binance_trades, chainlink_feed (strike/resolution),
                         clob_ws (books/tape), market_scanner (discovery + gamma fallback),
                         _socket, _staleness, _json
  indicators/            ATR engine
  recording.py           WindowPathRecorder (all windows) + TapeRecorder + retention
  execution/             base (BaseTrader, fee math), paper_trader, live_trader,
                         circuit_breaker, correlation
  agents/                scheduler, outcome_reviewer, counterfactual_tracker,
                         ghost_tracker, pipeline_analytics
  memory/                outcomes/, ghost_outcomes/, counterfactuals/ (+ rollups);
                         recordings/ (gitignored); state/. Layout: polybot/paths.py
  discord_bot/           monitoring + control commands (§12)
  db/models.py           SQLite per mode (positions, trade_history, bankroll,
                         peak_bankroll; window_labels lives here too; window_paths
                         sits in a gitignored sidecar DB — window_paths.db)
scripts/
  run_polybot.sh         THE daily supervisor (systemd unit: polybot.service;
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
| Chainlink (RTDS WS) | `wss://ws-live-data.polymarket.com` | Strike + resolution price |

## 11. Running + invariants

The bot runs ONLY on the VPS (Oracle Stockholm, systemd unit `polybot` →
`run_polybot.sh`): starts 12:01 AM ET, stops trading 11:30 PM ET, nightly jobs
11:45 PM ET, commits + pushes `origin main` on a clean exit, pulls + restarts
at midnight; a mid-day crash restarts after 60s. **Never run the bot on a
workstation** — there is no cross-host lock (`polybot.main`'s localhost-port
lock guards one host only). Live preflight: `verify_keys.py` then
`smoke_order_test.py --confirm`.

- UTC for storage; ET (`America/New_York`) only for date-bucketing + trading
  windows. Daily rollups bundle per-trade JSON; readers glob both.
- Recordings (`memory/recordings/`) are gitignored — never in the nightly
  commit. `memory/` records + per-mode DB + settings.yaml are committed
  nightly.
- Kill bars are the deployment authority.

## 12. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all] confirm`
`!session` `!pipeline` `!commands` — `!pause` halts new entries only; `!clear`
purges Discord messages only.
