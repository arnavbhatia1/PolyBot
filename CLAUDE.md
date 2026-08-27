# PolyBot

5-min BTC Up/Down trader for Polymarket. The only feeds the STRATEGY reads are
Chainlink (RTDS) + the Polymarket CLOB + Gamma; every position holds to
resolution. There is no other model and no exit path.

**The edge is the projection — the mostly-written 60s average the book cannot
price because it prices off spot.** We are demonstrably not the fast
participant (the book reprices 0.33s after Binance, 2.5s before our oracle
receipt); the projection's information is harvested at prices that match its
confidence. The sign record and its out-of-fit bounds live in RESEARCH.md —
quote those, not an in-sample count.

**Resolution mechanism (since 2026-08-14 00:00 UTC)**: Polymarket resolves on
the official **60-second TWAP stream** (RTDS topic `crypto_prices_twap_sixty`;
strike = the stream's value at the open, final = its value at the close, both
verified bit-exact against served price_to_beat/final_price incl. a live probe
08-18). The switch from the 30s stream was SILENT and the bot traded the wrong
stream for 4 days — the full incident, and why both watchers missed it, is in
RESEARCH.md. The nightly ping now carries a SOURCE watch (`mechanism_read`:
served values vs our own captured boundaries) that turns red the same night
this ever happens again. **On any mechanism alarm: set `trading_enabled:
false`, then run `scripts/research/ws1_boundary_autopsy.py` before anything
else.**

**Sibling docs — this file stays lean:**
- `REFUTATIONS.md` — the graveyard (binding; killed lanes + methodology bans)
- `RESEARCH.md` — ranked open problems, frozen-measurement register with
  reopening conditions, the 08-14 incident record
- `WALLETS.md` — the counterparty census (who is extracting, how, era-split)

**This file is the single source of truth for how the bot works — update it in
the same commit as any behavioral change.**

## Quick Start

```bash
pip install -r requirements.txt

cp polybot/config/.env.example polybot/config/.env
# Required: DISCORD_BOT_TOKEN (monitoring)
# Live mode also needs: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER

python -m polybot.main --mode paper       # paper trading
python -m polybot.main --mode live        # real USDC (needs allowance)
python -m polybot.main --run-pipeline     # one nightly cycle, no trading
python -m pytest polybot/tests/           # full suite (also CI on every push)
scripts/run_polybot.sh                    # daily cycle: trade -> nightly jobs -> commit -> restart (VPS only)
```

**The live recipe** (only after the per-leg bar passes AND
`smoke_gtc_test.py --confirm` passes): `settings.yaml` → `mode: live` +
`late_window.trading_enabled: true` + a fresh `validation_epoch`. That is the
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

Every 5 min, Polymarket runs a market: will BTC's **60-second TWAP** at the
window close be ≥ its value at the open (tie → Up)? Up/Down ERC-1155 tokens
trade $0-$1; the winner pays $1/share. The resolution source is Chainlink's
official BTC/USD 60s-TWAP stream (`crypto_prices_twap_sixty`, ~1Hz on integer
seconds, delivered ~1.6-1.8s behind observation); Gamma mirrors it for
discovery. The **decision strike** is that stream's first report at/after the
window boundary (`chainlink_feed._record_boundary`); Gamma's served
`price_to_beat` WINS when present. Boundary trust runs on the **payload
clock**: a capture is trusted iff its report's OWN timestamp is within 0.5s
of the boundary (the topic ticks on integer seconds, so the true boundary
report carries ts == boundary exactly) — delivery lag (rx − ts, ~1.6-1.8s)
never enters the comparison, so normal delivery cannot veto a capture; only
a genuine hole can. The RAW ~1Hz stream
(`crypto_prices_chainlink`) is NOT the strike source — it feeds the running
reconstruction (`running_avg`, rx-clock ZOH: matches the served 60s final at
median $0.028 / p90 $0.22) and the projection (`projected_final_twap`,
horizon 60s). A boundary capture landing > 0.5s past the boundary is
UNTRUSTED — no leg deploys capital on OUR capture (`_strike_trusted`); a
served Gamma `price_to_beat` is the resolution source itself, so it restores
trust when it arrives. The projection
additionally refuses: spot older than 3s, and any raw delivery hole > 10s
inside the averaging span (`RAW_GAP_MAX_S` — a 68s hole once projected a $24
error onto a $0.14 photo-finish behind a perfectly fresh spot).

Two modes, one engine: **paper** (realism shim: real CLOB books, FOK
semantics, latency sampled from the live ledger's measured POST-RTT
distribution, network-fail sim, tick snapping; maker fills are print-through
conservative — see §2) and **live** (`py-clob-client-v2` against the real
CLOB; balance + allowance verified at boot). Decision parity is a CI
invariant: `test_decision_parity.py` replays real recorded windows through
both traders and asserts bit-identical gates, signals, sizing, and order
intents (fixture regenerates via `scripts/research/parity_fixture_gen.py`).

## 2. The two legs (one signal, risk priced two ways)

Margin tables (`signal_engine.TWAP_MARGIN_P995/_MAX`): frozen 2026-08-18 on
970 real-final 60s-rule windows + 1,651 synthetic (max-union only — synthetic
finals are our own a60 reconstruction re-targeted onto pre-rule tape, which
makes low-k errors self-referentially SMALL, so synthetic windows may only
ever WIDEN the max knots, never tighten anything); estimator = rx-clock ZOH +
coverage guard, MAX from per-tick interval maxima. p99.5 at k=6 is $1.5;
knots run to k=58. Re-fit at ≥14 real-final days is re-measurement, not
bar-relaxing (RESEARCH.md). Tuning them to make a window fire IS bar-relaxing.

**Deep-projection maker ladder (`signal_leg="deep_proj"`) — the business.**
Rungs 0.80/0.65/0.50/0.35/0.20 × 20% of `maker_bankroll_frac` (0.15) rest on
the projection-favored side while the BRIDGED projection's displacement clears
`need` × p99.5(k) — need 1.0, the interim floor from the 08-18 walk-forward
audit (the in-sample 0.5 grid could not be validated out-of-fit; the ≥14-day
re-fit re-decides — RESEARCH.md #1). Placement k ∈ [6,25]: the k>25 flow is
REFUTED as harvestable (sweeps traverse the whole ladder inside ~1s and
outrun any cancel; flip-race loss probability exceeds every rung's price
margin — REFUTATIONS.md). The same floor cancels resting rungs when it
breaks. Post-close hold 60s gated on the boundary-verified winner
(`certain_winner`, fails closed). The bridge: spot_est = latest raw report +
Binance movement since that report's payload ts (`spot_bridge_delta`); every
failure mode collapses to the plain projection. Fills book through
`book_maker_fill` as ONE blended position. **Paper fill rule
(live-calibrated, conservative)**: strictly-below prints fill a rung in FULL;
at-price prints credit only volume beyond `AT_PRICE_QUEUE_SH` (135 sh);
snapshot queue models are BANNED (REFUTATIONS.md). Paper pays a GTC round
trip on place and cancel that is **not measured** — 56ms/rung against ~500ms
reconstructed from the one live ladder, so paper's rungs become matchable
about twice as fast as the real ones (RESEARCH.md). Maker fills are fee-free
(re-verified on post-rule fills 08-18: 274/274 USDC deltas exact).
**Bar (unchanged)**: ≥6 clean ET days, ≥20 filled windows, EW ≥ +5¢/sh,
`usd_per_day > 0`, on realized paper fills since `validation_epoch`.

**Lock-dip taker (`lock_dip`) — DORMANT (`taker_enabled: false`, 08-18).**
Its whipsaw supply died with the 60s rule: 4 winner-side max-lock dips in
1,184 windows, one FOK-reachable, vs a ≥1-per-3-days bar — a 60s average
moves too slowly to produce panicked max-lock asks. Not refuted: the code
stays, the signal still evaluates and logs would-be fires, and the re-arm
condition is in RESEARCH.md. Its mechanics when armed: max tier ONLY
(`require_max_tier` — p99.5 tiers realize breaches; the 60s sim's one loss
was a p99.5 fire), k ≥ 6s, PLAIN projection, ask ≤ tier_prob −
`sniper_min_edge`, one-tick FOK pad, market-anchored Kelly, all §1 gates.
Booking: chain-truth via the +8s audit; the `fees` column is
share-denominated, NOT the charged taker fee — takers pay the documented
curve via the USDC debit (re-verified post-rule 08-18: 326 rows at fee/model
median 1.000).

**Hold to resolution — structurally.** No sell path exists in the codebase;
both legs' edges were measured hold-to-resolution (REFUTATIONS.md: exits).

**Kill rules** (`live_health_read.kill_rule_tripped`, armed at any go-live):
any `lock_dip` loss trips on ONE occurrence (every fire is max-tier — a loss
IS a breach); otherwise trailing-4-day mean DOLLARS < 0, judged only once the
trailing window holds ≥4 ET days AND ≥5 fills — sparse fills keep accruing
(one −$4.50 rung loss after three quiet days must not halt a leg that is up
on the week; measured 08-18). `trading_enabled: false` is the shared
emergency brake for every leg; the per-window SOURCE gate (§6) flips it
in-process on a resolution-source mismatch. Never deploy on a harness print
alone — the paper shadow's realized fills are the binding gate. Capital
deploys ONLY through these two legs.

## 3. Sizing (every leg)

```
size  = bankroll * kelly * circuit_breaker_mult
size *= concurrent_multiplier(side, market, opens)     # correlation-aware
size  = min(size, bankroll * max_bankroll_deployed)    # 0.80
size  = min(size, side_depth * max_book_fill_pct)      # 0.50
if size < 1.0: skip                                    # CLOB $1 floor
```

`kelly` = fee-aware Kelly on the market-anchored defended edge, scaled by
`kelly_fraction` (0.08). Circuit breaker: tier-locked floor at $100/150/200…
milestones (floor = tier × 0.85, sqrt interpolation to 0.40×, never resets
down, persists via `peak_bankroll`). The ladder budget is a flat fraction,
not Kelly — deep bids are not a certainty claim.

## 4. Orders

FOK via `py-clob-client-v2` (pinned <1.1.0 — 1.1.0 wraps post_order in a
blocking 30s hash-poll), 3 attempts, only provably-unposted failures retry.
Order-POST RTT p50 ~410-436ms as last measured (pre-08-13); that table embeds
Polymarket's DELIBERATE taker hold on crypto up/down markets (`itode: true`),
which the changelog cut from 250ms to 50ms on 08-17 11:00 UTC — the paper
RTT table is stale by route change and re-derives from the next measured
POST samples (`smoke_order_test.py --confirm`), never by hand. EIP-712
sign 17.5ms pure-python on the box (coincurve on Linux ~10× faster; dev boxes
skip it). SELL signatures pre-armed; BUY pre-signs concurrently. WS-only book
pre-check; warm pooled HTTP/2; gc.freeze() post-boot. GTC rungs pass
`legal_price` (round DOWN to tick, clamp [tick, 1−tick]) and the 5-share
exchange minimum. A rung that cannot be rested logs `MAKER BID REJECTED` at
ERROR, refusal and POST failure alike; a rung the budget cannot afford is
`MAKER RUNG SKIPPED` at INFO — routine, not a rejection.
`cl_report_to_submit_ms` + `lat_*` stamps measure the race per fill; GTC
place/cancel RTTs stamp per rung (`gtc_place_ms`/`gtc_cancel_ms`, plus the
`latency_stats.json` gtc section — `smoke_gtc_test.py --samples` feeds it
too), and a fill whose owned segments exceed 1.5× the 25ms budget logs
LATENCY BUDGET at WARNING. Live boot: key+funder, balance/allowance
preflight, allowance recheck every 10 fills. `fill.fill_size` is always USDC
notional.

## 5. Resolution

The TWAP oracle decides; winner $1/loser $0 credited atomically. Exit price is
oracle-first (Gamma `event_metadata`; coherent resolved CLOB book fallback;
never Binance); the orphan fallback resolves ONLY from genuine boundary
captures — it waits and pages rather than fabricate. Our tape prints a TAPE
VERDICT before Gamma serves; per-window RESOLUTION DRIFT warns when Gamma
disagrees with a reliable capture (log-level; the nightly SOURCE watch is the
systematic net). Winner payouts book via Polymarket auto-redeem; losing $0
stubs sit inert on the wallet (deliberately not automated — CLOB orders are
the only on-chain thing the bot signs).

## 6. Recorders + nightly

- **Window-path recorder** (1 Hz, 5 Hz final 45s): both tokens' BBO/depth +
  Chainlink price + strike (with `strike_trusted`, since `get_strike` also
  serves untrusted captures) for EVERY window → `window_paths` (gitignored
  sidecar DB) / `window_labels`; 90-day retention. Labels are the kill-bar
  ground truth.
- **Tape recorder**: every CLOB print (+ exchange ts, fee bps) →
  `memory/recordings/tape_*.jsonl` (gitignored).
- **Micro-tape**: every CLOB BBO change (final 90s) + every raw report ("l")
  + the official 60s stream ("t") + the RETIRED 30s stream ("t3", recorded
  only — A/B evidence for the next silent source swap; RTDS resumed serving
  it by 08-27, and the nightly SOURCE line states the count) + Binance relay
  ("s"/src "bz"), payload+receipt ts → `micro_*.jsonl`; nightly gzip (~39×);
  readers take .jsonl(.gz).
- **Per-decision records**: `trade_context` on fills AND ghosts (`signal_leg`
  is the per-leg ledger key). **None-vs-0.0 is load-bearing** — cold inputs
  record None, never 0.0. Ladder fills carry `print_gap`: 1 when the CLOB feed
  reconnected while the rungs rested, so paper's fill count is short there.
- **The per-window SOURCE hard gate** (`recording._check_resolution_source`):
  every labeled window's served strike/final is compared against our TRUSTED
  stream captures; a >$0.005 mismatch flips `trading_enabled` false
  in-process and pages Discord (settings on disk unchanged — the operator
  re-arms by restart after re-pointing the feed). The one wired exception to
  "watches never flip config": a source mismatch means every leg is
  computing fiction.
- **NightlyScheduler** (23:45 ET): rollups + retention + the sniper health
  ping (`_sniper_health_job` → Discord `#polybot-daily`): realized per-leg
  ledger + kill-rule verdict (realized-only authority), SIM ceiling read,
  regime line (trailing gaps p25/50/75 + photo-finish share <$1; HOSTILE =
  p50 < $6 or photo > 15%, percentile-ported to the 60s rule 08-18 — HOSTILE
  predicts zero fills, not losses), chain watch (final==next strike), the
  nightly SOURCE summary (`mechanism_read`), and the ops watch (POST RTT p50
  vs the 436ms table ±25%; trailing-7d sweep-consumed deep-queue p75 vs the
  135-sh at-price constant; measured GTC place p50 vs paper's 56ms table
  ±25%, dark until samples exist; owned-latency budget breaches). Alert-only.

## 7. Hard rules

- No ML/feature-stack entry-side prediction — the CLOB price wins everywhere
  our arithmetic doesn't. The ONE sanctioned exception is the TWAP-lock
  projection (an already-observed average). Measurement of observed
  quantities is always in scope; prediction of unobserved ones is not.
- No deployment before a kill bar passes; never relax a bar to pass it.
  Re-measurement on a bigger corpus/better estimator is not relaxing
  (RESEARCH.md register).
- No symmetric market-making, no oracle-cadence trading, no expansion past
  btc-5m. What is actually refuted is post-close camping on the siblings
  (30s era, REFUTATIONS.md); their in-window deep flow is unmeasured and low
  priority (RESEARCH.md). Scaling is SIZE on this one book.
- No mid-price edge math (executable CLOB BBO only). Never skip the fee:
  `rate*shares*p*(1-p)`, rate 0.07; flat-additive gates use 0.0175 — never
  mix them.
- `gain_pct = pnl/size`, never log_return. Don't bypass the circuit breaker.
  Don't delete `polybot/db/polybot_*.db`.

---

# Part B — Operations

## 8. Project layout

```
polybot/
  main.py                Trading loop; gates; ladder hook; nightly health job
  config/                settings.yaml (THE single config source), loader.py
  core/signal_engine.py  Margin tables (60s-rule freeze 08-18) + lock math
  feeds/                 chainlink_feed (sixty topic, strike, projection,
                         bridge, coverage guard), clob_ws, market_scanner
  recording.py           WindowPathRecorder + TapeRecorder + MicroTape
  execution/             base (fee math), paper_trader, live_trader,
                         maker_bid (deep_proj ladder), circuit_breaker
  agents/, memory/, discord_bot/, db/models.py (per-mode SQLite + labels)
scripts/
  run_polybot.sh         Daily supervisor (systemd unit: polybot)
  analyze_twap_lock.py   Lock replay harness (60s) + bit-exact mechanism check
  analyze_late_window.py Realized-ledger readers + resolution/SOURCE watches
  sniper_shadow_status.py, verify_keys.py, smoke_order_test.py,
  smoke_gtc_test.py, reset_paper_clean.py
  research/              Offline analysis tooling (see its README; data/ is
                         gitignored) — census, error tables, engine-true grid
REFUTATIONS.md  RESEARCH.md  WALLETS.md
```

## 9. Data sources

| Source | Feed | What |
|---|---|---|
| Polymarket CLOB | WS + `GET /price /book /spread /tick-size` | Books, tape, executable prices |
| Polymarket Gamma | `GET /events?slug=` (fallback `/events/slug/{slug}`) | Discovery + resolution + labels |
| Chainlink (RTDS WS) | `wss://ws-live-data.polymarket.com` (`crypto_prices_twap_sixty` + raw `crypto_prices_chainlink` + Binance `crypto_prices`) | Strike + resolution (sixty topic); raw feeds the projection; Binance feeds ONLY the bridge delta |

## 10. Running + invariants

The bot runs ONLY on the VPS (Oracle Stockholm, systemd `polybot`): starts
12:01 AM ET, stops 11:30 PM ET, nightly jobs 11:45 PM ET, commits + pushes
`origin main` on clean exit, pulls + restarts at midnight; mid-day crash
restarts after 60s. **Never run the bot on a workstation** (single-host lock
only). Live preflight: `verify_keys.py` then `smoke_order_test.py --confirm`.

- UTC for storage; ET only for date-bucketing + trading windows.
- Recordings are gitignored; `memory/` records + per-mode DBs + settings.yaml
  commit nightly. Heavy analysis never runs on the box — scp the tape local.
- Kill bars are the deployment authority.

## 11. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all]
confirm` `!session` `!pipeline` `!commands` — `!pause` halts new entries only.
