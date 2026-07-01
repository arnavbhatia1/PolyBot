# PolyBot

Lean 5-min BTC Up/Down trader for Polymarket. Entry is **inventory sourcing**
(L1 fair-value anchor + execution-quality gates — entry forecasting has no edge
over the CLOB price; evidence in `tasks/todo.md`). The edge is the **exit
engine**: re-evaluate every tick, sell overpriced hope to momentum chasers or
ride to $1. A 1 Hz window-path recorder feeds the nightly exit-value model and
wallet-fingerprint tables (`tasks/todo.md` is the build plan and kill-bar
status).

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
.\scripts\run_polybot.ps1                 # daily cycle: trade -> nightly jobs -> commit -> restart; also supervises the calibration monitor
```

### Secrets

| Key | When |
|---|---|
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_PRIVATE_KEY` | Live mode (EIP-712 signing) |
| `POLYMARKET_FUNDER` | Live mode (USDC funding address) |

---

# Part A — Trading Logic

## 1. What it bets on

Every 5 min, Polymarket runs a market: will BTC close higher or lower than at
the window's start? Up/Down ERC-1155 tokens trade $0-$1; the winning side pays
$1/share. Chainlink (via Polymarket RTDS) is the resolution source; Gamma
mirrors it for the slug feed. Two modes, one engine: **paper** (realism shim:
real CLOB books, FOK semantics, convex slippage, network-fail/latency jitter,
$1 min, tick snapping) and **live** (`py-clob-client-v2` FOK against the real
CLOB; USDC balance + allowance verified at boot).

## 2. The model (L1 only)

```
ac         = clamp(lag1_autocorr(closes, regime_lookback), ±0.5)
vol_scaled = (max(atr, atr_floor) / atr_sigma_ratio) * sqrt(minutes_remaining) * sqrt((1+ac)/(1-ac))
z          = (btc_price - strike) / vol_scaled
prob_up    = StudentT_CDF(df, z * sqrt(df/(df-2)))        # df clamped ≥3
```

- `student_t_cdf` + df clamp + `autocorr_vol_scale` live in `core/aux_layers.py`.
- **ATR floor**, dynamic: `max(min_atr, 0.30*rolling_20)`; widens when
  `rolling_20/long_term_200 < atr_regime_shift_threshold` (anti-overconfidence
  on vol collapse). Buffers keep one ATR slot per 1-min candle — replaced within
  the forming candle, not appended per `compute_probability` call (entry + every
  exit tick share the dedup, keyed on the candle's `candle_ts`).
- `btc_price` from `_fastest_btc_price`: **Coinbase WS only (<2s)** — the venue
  Chainlink resolves against. Coinbase stale → decision skipped, never zeroed.
- L1 is the entire model — there is no L2-L6 stack, SPRT, or isotonic entry
  calibration. The market price beats every feature stack at entry (k=0, 44/44
  segments, day-clustered t≈3.7-4.4 against). Never rebuild entry-side
  prediction — `tasks/todo.md` "WHAT YOU ARE NOT ALLOWED TO DO".

## 3. Entry gates

Edge = `model_prob - market_price`. All must pass; any failure skips the tick.

| Gate | Threshold | Source |
|---|---|---|
| Chosen-side `prob` | >= `min_model_probability` (0.56) | `SignalEngine.evaluate` |
| `edge` | >= `min_edge` (0.04, scaled by flip premium — §6) | `SignalEngine.evaluate` |
| `Kelly` (fee-aware) | >= `min_kelly` (0.01); `b_eff = b*(1-fee_rate)` | `SignalEngine._kelly` |
| Spread either side | `spread/2 + EFFECTIVE_FEE_PEAK <= max_spread` (0.10); unavailable = skip | `_fetch_market_prices` |
| Book depth | both-sides-thin first (>= `min_book_depth_usd` $50 on one side); chosen side must clear it too | `_evaluate_signal_and_enter` |
| Price sum | `price_up + price_down in [0.98, 1.02]`; out-of-band moments logged to `state/price_sum_outliers.jsonl` | `_fetch_market_prices` |
| Book freshness | both sides' WS books <= `_WS_STALE_S` (10s) old | `clob_ws.both_books_fresh` |
| `edge <= max_edge` | 0.20 — wider = stale phantom price | inline |
| ATR gate | ATR >= 5th percentile (lower-bound only) | `IndicatorEngine` |
| Adverse-selection hard skip | `adverse_rate_at_30s >= 0.80` | `AdverseSelectionMonitor` |
| Edge-decay | mean 15s post-fill drift (30-min lookback) >= -0.05; inactive < 15 resolved fills | `AdverseSelectionMonitor` |
| Net-edge after slippage | `edge - price*est_slip >= min_edge` | `slippage_pct` |
| Pre-submit re-check | FOK VWAP from the ask ladder must keep net edge in `[min_edge, max_edge]` | `compute_buy_vwap` |
| Min order size | >= $1 (CLOB floor; paper mirrors live) | inline |
| Feed staleness | Coinbase <= 30s, Chainlink <= 60s, Binance aggTrade <= 30s, kline <= 45s | inline |

Adverse selection is sizing-side above the hard skip:
`kelly_mult = max(0.30, 1 - 1.5*max(0, adverse_rate - 0.45))`, lookback
Bayesian-shrunk to a neutral prior (n=10, 0.5).

**Ghosting:** below-min-prob skips and downstream gate vetoes (adverse,
edge-decay, edge cap, flip hurdle, net-edge, pre-submit drift) record a ghost
(`GhostTracker`) that resolves at window close — the evidence stream for any
future gate evaluation.

## 4. Sizing

```
size  = bankroll * kelly * circuit_breaker_mult * time_mult * adverse_kelly_mult
size *= concurrent_multiplier(side, market, opens)     # correlation-aware
size  = min(size, bankroll * max_bankroll_deployed)    # 0.80, enforced again in open_trade
size  = min(size, side_depth * max_book_fill_pct)      # 0.50
if size < 1.0: skip
```

- **Circuit breaker** — tier-locked floor at $100/150/200/... milestones; floor =
  tier × 0.85; concave (sqrt) Kelly interpolation down to 0.40×; tier never
  resets down; persists via the `peak_bankroll` DB row.
- **Time multiplier** — full Kelly for the first 60% of the window; after,
  penalty scales by (1 − conviction) up to 0.30.
- **Late-window sniper** (gated; `late_window.sniper_enabled`, **default OFF** —
  the one bot-formable late-window edge, `tasks/todo.md`). In the final
  `sniper_late_start_s` (45s), if a Coinbase move over `sniper_move_window_s` ≥
  `sniper_cb_move` pushed price past strike and the chosen-side ask ≤ `sniper_ask_cap`,
  `SignalEngine.evaluate_late_sniper` fires a BUY that bypasses ONLY `max_edge`
  (→ `sniper_max_edge`, at both the edge-cap and pre-submit gates) and the time penalty
  — every other gate stays; the main loop also wakes on Coinbase ticks when enabled.
  NOT DEPLOYED: stays OFF until its kill bar passes at the host's measured RTT
  (`analyze_late_window.py --rtt-sweep`) AND a paper-shadow run confirms it.
- **Sniper-only mode** (`late_window.sniper_only`, default OFF) — the go-live
  switch: base-entry BUYs are suppressed and recorded as `sniper_only` ghosts
  (the base strategy keeps accruing resolution evidence at zero cost); capital
  deploys only on sniper fires. The live recipe once the kill bar passes is
  `mode: live` + `sniper_enabled: true` + `sniper_only: true` — the base
  strategy has no proven edge and never deploys live (`tasks/todo.md`).

## 5. Orders

FOK via `py-clob-client-v2`, up to 3 attempts with jittered backoff — only
exchange-confirmed rejections retry; ambiguous outcomes never resubmit
(double-fill guard). **Latency floor** (no colo / no WS orders): the hot path is
one POST RTT (~135ms warm vs ~300ms cold) — signing is presigned off-path (both
sides), book pre-check + fill VWAP come from the WS feed, and
tick-size/neg-risk/fee + contract-version caches are prewarmed per window. Orders
ride a warm pooled HTTP/2 singleton (`keepalive_expiry` 60s > the 5s keepalive
ping so no cold handshake between orders; `connect` timeout 5s bounds a
dead-connection reconnect; TCP_NODELAY on by default). Live boot: key+funder
required, balance/allowance preflight (`max_single × max_concurrent × 10`),
mid-session allowance recheck every 10 fills (warn only). Per-trade DB writes
are atomic. `fill.fill_size` is always USDC notional.

## 6. The exit engine (the edge) + flips

Every tick while holding, re-run L1 and decide HOLD vs EXIT
(`SignalEngine.evaluate_hold`). `holding_edge = model_prob - bid`. The exit
threshold blends `exit_edge_threshold` (-0.10) with the binary-payoff
`ExitBoundary` curve (`core/exit_boundary.py` — deep-ITM patience, OTM urgency,
ATM fee-aware time value); blend stamped to `last_effective_exit_threshold` so
the phantom-bid SELL re-verify gates against the same number.

Branches in order: **loss-cut** (market < entry×0.65, <90s left, BTC wrong side
of strike by >0.5×ATR — the whipsaw cushion), **deep-loss hold**
(holding_edge < -0.10 and market < entry → binary residual beats locking the
loss), **scalp** (holding_edge <= effective threshold, unless within the
whipsaw cushion), else **hold**. No confidence overrides.

After a scalp the bot may re-enter the same window (one position per window;
`max_concurrent_positions` caps across windows) over a flip hurdle:
`min_edge + max(flip_premium, spread + 2*fee_rate*p*(1-p))`, premium
`0.015 + 0.005*max(0, flips-2)`.

**Resolution:** Chainlink decides; winner $1/loser $0 credited atomically.
Exit price oracle-first (`event_metadata` final_price vs price_to_beat; Gamma's
coherent resolved prices as fallback; never Binance). Chainlink orphan fallback
after ~30 min of Gamma silence. Live redeems settle non-blockingly.

**Passive exits** (`execution.passive_exit_enabled`, **OFF — measured negative**:
rest-vs-immediate-FOK including miss cost is −2.1c/sh ≈ −$62/day, t_day −2.03 over
8 clean days; maker fills at mid are markout-fair, so the earned half-spread comes
back as adverse selection on the misses): on a non-loss-cut scalp the
bot rests a SELL at `_resting_level` (mid, capped at the ask, floored at bid+1
tick) for `passive_exit_timeout_s` (10s), takes the maker fill (zero taker fee)
if a BUY prints strictly through, else FOK-falls-back; loss-cuts skip resting, a
HOLD-flip cancels it. PaperTrader simulates this from the tape; LiveTrader mirrors
it with a real GTD resting SELL (`create_order`/`post_order` GTD, poll `get_order`,
`cancel_orders` + FOK fallback, cancel/fill-race double-sell guard, 120s GTD
self-expiry safety net). Maker fills also book the **maker rebate** (Polymarket
redistributes 20% of crypto taker fees to makers daily, `DEFAULT_MAKER_REBATE_RATE`;
`maker_rebate` = `rebate_rate × taker_fee`, the sole-maker ceiling): paper credits
it to the bankroll (`_maker_rebate_credit`), live returns 0 there and reconciles
against the actual daily pUSD payout (per-fill crediting would double-count); stored
in `trade_history.maker_rebate`, kept OUT of `pnl` so paper/live records stay
comparable and the CF/go-live-gate pnl is untouched. It is a small cost-offset
(dwarfed by adverse selection on the same fills), never an edge — never a reason to
rest NEW quotes (that is the §9 symmetric-MM ban).

## 7. Recorders + learning loop

- **Window-path recorder** (`polybot/recording.py`, in-process; 1 Hz, **5 Hz in
  the final 45s**): both tokens' BBO + top-3 depth, Coinbase mid, strike, elapsed,
  traded flag, **+ Binance aggTrade `binance_price`/`binance_cvd_10s`/`binance_cvd_30s`**,
  **+ the live-L1 stamp `atr`/`model_prob_up`** (dedicated engine instances, same
  config as live — lets offline harnesses replicate the live `sniper_min_edge`
  floor exactly; NULL on cold feeds, never 0.0), for EVERY window (self-discovers
  contracts via Gamma; labels itself from `event_metadata`). Tables
  `window_paths` (gitignored sidecar DB) / `window_labels`;
  90-day retention sweep nightly. ~288 labeled windows/day — the exit-research
  corpus + the **late-window-sniper kill-bar** feed (`tasks/todo.md`).
- **Tape recorder**: every CLOB trade print →
  `memory/recordings/tape_YYYY-MM-DD.jsonl` (gitignored). Input to the
  passive-exit shadow sim.
- **Wallet fingerprints** (`polybot/wallets.py`): nightly data-api ingestion of
  each labeled window's taker tape → per-wallet resolution markout →
  donor/noise/sharp classification (`wallet_stats`). Counterparty information —
  the one learning surface that compounds. `wallet_stats` is write-only; no
  decision-time routing consumes it (pre-post identity is infeasible on the
  anonymous CLOB book — `tasks/todo.md`).
- **NightlyScheduler** (`agents/scheduler.py`): 23:45 ET — record rollups
  (outcomes/ghosts/counterfactuals → daily bundles) + registered jobs
  (retention sweep, wallet tables).

## 8. Evidence stream

Per-decision `trade_context` stamped into outcomes + ghosts: entry facts
(btc/strike/seconds/prices/closes_tail/ATR fields), `model_probability`
(= `model_probability_raw`; L1 is uncalibrated), flow/CVD telemetry
(`flow_score`, `spot_flow_signal`, `coinbase_cvd_60s`, `coinbase_taker_*`,
`cross_venue_gap`, `fast_realized_vol_60s` — recorded for offline exit research,
no logit consumes them), CLOB book aux (`clob_depth_top5_*`, `clob_book_age_s`),
`depth_usd_top20` (Binance BTC depth), adverse-selection audit fields, flip
fields. **None-vs-0.0 is load-bearing:** signal fields record `None` when their
feed is cold/stale (never 0.0). `edge_decay.deltas` (post-fill drift at
5/10/15/30/60s) merged at close.

**CounterfactualTracker**: every scalp records both arms (actual vs
hold-to-resolution); every held position records its worst moment (hypothetical
scalp arm), keyed to the resolving position. This is the ground-truth data for
any exit-policy change — score via `actual − cf` / `scalp_was_optimal`, never a
naive signed sum of the stored `delta_pnl`.

Gate-skip stats: `state/gate_stats_current.json` (today) folds nightly into
`state/gate_stats.json` (lifetime). Feed staleness P50/P95/P99 →
`state/feed_staleness.json`.

## 9. What it deliberately won't do

- No entry-side prediction (ML or rules) — the bar is beating the CLOB price
  and nothing clears it. No symmetric market-making (1-2c overround vs ~5c/$1
  toxicity). No oracle-cadence trading (resolution uses pull-based Data
  Streams — no locked-print window). No expansion past BTC until the
  `tasks/todo.md` goal completes.
- No deployment of any phase before its kill bar passes. No Gaussian, no
  Binance resolution, no mid-price edge math (executable CLOB BBO only), never
  skip the fee (`rate*shares*p*(1-p)`, rate 0.07 = `DEFAULT_FEE_RATE`;
  flat-additive gates use `EFFECTIVE_FEE_PEAK` 0.0175 — never mix them).
- `gain_pct = pnl/size`, never log_return. Don't bypass the circuit breaker.
  Don't delete `polybot/db/polybot_*.db`.

---

# Part B — Operations

## 10. Project layout

```
polybot/
  main.py                      Trading loop, entry/exit/sizing orchestration
  config/                      settings.yaml, loader.py, param_registry.py (defaults + validation ranges)
  core/                        signal_engine (L1 + exit engine), exit_boundary, returns,
                               adverse_selection, order_flow,
                               aux_layers (student_t_cdf, autocorr_vol_scale, compute_spot_flow_signal)
  feeds/                       coinbase_feed (primary BTC + CVD + 1s price history),
                               binance_feed (1m candles, ATR), binance_depth, binance_trades,
                               chainlink_feed (strike + resolution), clob_ws (books/tape/on_trade hook),
                               market_scanner, _socket, _staleness, _json
  indicators/                  atr + engine (ATR-only)
  recording.py                 WindowPathRecorder (1 Hz, all windows) + TapeRecorder (JSONL) + window_paths retention
  wallets.py                   wallet fingerprinting (data-api ingestion + classification)
  calibration/                 long-horizon edge harness (measurement-only, own gitignored
                               db/calibration.db): discovery + clob + deribit (one-touch/
                               digital pricing) + store + analysis (event-clustered kill bar)
  execution/                   base (BaseTrader, fee math), paper_trader, live_trader,
                               circuit_breaker, correlation
  agents/                      scheduler (NightlyScheduler), outcome_reviewer,
                               counterfactual_tracker, ghost_tracker,
                               pipeline_analytics (ET date helper for rollups)
  memory/                      outcomes/, ghost_outcomes/, counterfactuals/ (+ rollups);
                               recordings/ (gitignored JSONL);
                               state/ (gate stats, adverse, staleness, prev margin, ...)
                               Layout in polybot/paths.py (override: POLYBOT_MEMORY_DIR)
  discord_bot/                 monitoring + control commands (§13)
  db/models.py                 SQLite per mode (positions, trade_history, bankroll, peak_bankroll,
                               window_paths, window_labels, wallet_trades, wallet_stats)
scripts/                       run_polybot.ps1 (daily loop; Linux port run_polybot.sh
                               + polybot.service systemd unit for a VPS — see
                               docs/DEPLOY_ORACLE_VPS.md),
                               calibration_harness.py (long-horizon calibration CLI:
                               ivcheck|snapshot|label|analyze|monitor; `monitor` runs supervised
                               by run_polybot.ps1, log-only; check anytime with analyze/ivcheck),
                               verify_keys.py (GET-auth + balance preflight),
                               smoke_order_test.py (go-live preflight: one unfillable
                               FOK proves order POSTs clear Cloudflare — verify_keys
                               covers GETs only),
                               analyze_late_window.py (RTT-parametric late-window sniper kill-bar),
                               sniper_shadow_status.py (paper-shadow fills vs the harness),
                               reset_paper_clean.py (clean-slate the paper experiment
                               — backs up, then resets the ledger to a clean baseline; operator-run
                               with the bot STOPPED)
```
(Paper realism — `execution.paper_latency_*` / `paper_network_fail_rate` in settings.yaml —
is calibrated to the **measured Ireland-VPN warm POST RTT** (~0.118–0.138s) to the London
order origin; `paper_network_fail_rate` 0.03 remains an estimate until calibrated at live launch.)

## 11. Data sources

| Source | Feed | What |
|---|---|---|
| Coinbase | `ticker` WS (BTC-USD) | Primary BTC price + per-trade CVD + 1s price history |
| Binance.com | `kline_1m` / `depth20@100ms` / `aggTrade` WS | Candles, ATR, depth, cross-venue gap |
| Polymarket CLOB | WS + `GET /price`, `/book`, `/spread`, `/tick-size` | Books, tape, executable prices |
| Polymarket Gamma | `GET /events?slug=...` (deprecated upstream; auto-falls-back to `GET /events/slug/{slug}` — `gamma_events_by_slug`) | Discovery + resolution + window labels |
| Polymarket data-api | `GET /trades?market=...` | Wallet-tagged taker tape |
| Chainlink (RTDS WS) | `wss://ws-live-data.polymarket.com` | Strike capture + resolution price |

## 12. Running + invariants

`run_polybot.ps1`: starts 12:01 AM ET, stops trading 11:30 PM ET, nightly jobs
11:45 PM ET, commits + pushes `origin main` on exit, restarts at midnight (or
immediately if the exit slipped past it). Each cycle it also (re)launches one
supervised log-only child on freshly-pulled code (kills any prior instance
first): the calibration monitor (`calibration_harness.py monitor` —
measurement-only, own gitignored `db/calibration.db`), so one launch starts
everything. **Single-instance guarded**: the
wrapper refuses to start if another is already running, and `polybot.main` holds an
OS single-instance lock (localhost-port bind) — so a double-launch (which silently
doubled every record 06-21/06-22) cannot recur. Live pre-flight:
`python scripts/verify_keys.py`.

- **UTC for storage; ET (`America/New_York`) only for date-bucketing + trading
  windows. Daily rollups bundle per-trade JSON; readers glob both.**
- `model_probability` == `model_probability_raw` (L1 uncalibrated) — both keys
  stamped for record-schema continuity.
- Recordings (`memory/recordings/`) are gitignored — never in the nightly
  commit. `memory/` records + per-mode DB + settings.yaml are committed nightly.
- Kill bars are the deployment authority — no phase ships to live capital
  before its bar passes (`tasks/todo.md` is the open roadmap + kill-bar status).

## 13. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all] confirm`
`!session` `!pipeline` `!commands` — `!pause` halts new entries only; `!clear`
purges Discord messages only.
