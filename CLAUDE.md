# PolyBot

5-min BTC Up/Down trader for Polymarket, rebuilt lean for the TWAP era: the
only feeds the STRATEGY reads are Chainlink (RTDS) + the Polymarket CLOB +
Gamma, the only strategy is the two-leg TWAP lock system (§2), and every
position holds to resolution. There is no other model or exit path. One feed
records without deciding: **Coinbase spot (recording-only, §6)** — the oracle
relay hands us each Chainlink report ~1.63s after the report's own timestamp
(p10 1.26 / p50 1.63 / p90 2.07, both topics — it is the relay, not the
topic), so the last ~1.6s of every resolving average is already fixed but
unseen. Spot ticks stamped on OUR clock are the only way to measure that gap
against the same tape's book events; a cross-venue REST pull cannot, because
its clock is not ours. **No gate, signal, or size reads it** — promoting it to
a decision input requires its own measured bar.
**PAPER SHADOW since 2026-08-07**: at 00:00 UTC that day Polymarket switched
resolution from the terminal Chainlink snapshot to the official **30-second
TWAP stream** (strike = the stream's value at the open, final = its value at
the close — both verified bit-exact against served price_to_beat/final_price,
17/17 windows, and each close chains into the next strike to the cent). The
switch killed the burst sniper that had been live since 07-19 (its own fills
recompute to −17.5¢/sh under TWAP scoring, t −5.0) and created the current
edge: in the final 30s the resolving average is mostly already observed, so
the outcome is often mathematically decided while spot-reflexive traders still
quote it — night-one books priced locked windows at 0.99-1.00 (the naive
buy-the-lock is dead on arrival) but a late spot whipsaw dips the WINNER's
ask to 0.84-0.93 for 1-4s in ~1 in 6 windows, well inside the ~0.4s FOK reach.
The lock call itself has never been wrong (583/583 windows, margins frozen in
`signal_engine.TWAP_MARGIN_*`). **No real capital until the pre-registered
paper bar passes** (§2); the nightly health job re-reads the realized shadow
daily. Gate-vetoed fires persist as leg-stamped ghosts — the zero-capital
evidence stream the gate machinery reads.

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
scripts/run_polybot.sh                    # daily cycle: trade -> nightly jobs -> commit -> restart (VPS only)
```

**The live recipe** (only after the §2 bar passes): `settings.yaml` → `mode: live`
+ `late_window.sniper_enabled: true` + a fresh `validation_epoch`. That is the
complete switch; paper and live share every decision path, and the sniper is the
only strategy either can run.

### Secrets

| Key | When |
|---|---|
| `DISCORD_BOT_TOKEN` | Always (monitoring) |
| `POLYMARKET_PRIVATE_KEY` | Live mode (EIP-712 signing) |
| `POLYMARKET_FUNDER` | Live mode (USDC funding address) |

---

# Part A — Trading Logic

## 1. The market + the two modes

Every 5 min, Polymarket runs a market: will BTC's **30-second TWAP** at the
window close be ≥ its value at the open (tie → Up)? Up/Down ERC-1155 tokens
trade $0-$1; the winning side pays $1/share. The resolution source is
Chainlink's official BTC/USD 30s-TWAP stream (via Polymarket RTDS topic
`crypto_prices_twap_thirty`, ~1Hz on integer seconds, delivered ~1.6-1.8s
behind observation); Gamma mirrors it for discovery. The per-window **decision
strike** is the TWAP stream's **first report at/after the window-boundary
timestamp** (`chainlink_feed.get_strike`; `_compute_strike`) — the
exact `price_to_beat` rule, verified bit-exact against Gamma's served value
(17/17), and each window's final equals the next window's strike to the cent
(the resolution-watch invariant, §6). The RAW ~1Hz stream (`crypto_prices_chainlink`)
is NOT the strike source — its boundary value differs from the served strike by
$10+ — it feeds the running TWAP reconstruction (`running_avg`/`twap_30`,
rx-clock ZOH: the official aggregator weights by arrival spacing, which fits
4× tighter than payload spacing) and the sniper's projection
(`projected_final_twap`). Gamma's `event_metadata.price_to_beat` is the
RESOLVED truth, but served late/unreliably in-window: when present it WINS;
otherwise the boundary capture carries. A capture landing > 2s past the
boundary (`strike_reliable`: the topic ticks on integer seconds, so the true
boundary report carries ts == boundary; a later first capture = delivery hole;
pre-boundary gaps don't veto) still serves logging/telemetry but is UNTRUSTED —
no leg deploys capital on it (`_strike_trusted`). Two modes, one
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

## 2. The TWAP lock sniper — the candidate edge (paper shadow)

In the final 30s the resolving average is w-observed: `proj = w·A + (1−w)·spot`
(A = running rx-clock average of the raw stream over the elapsed part of the
averaging window, spot = latest raw report). When `|proj − strike|` exceeds the
frozen projection-error margin for the time remaining, the outcome is decided.
Night-one books price that lock at 0.99-1.00 — the edge is the **whipsaw dip**:
a late spot move against the locked side scares spot-reflexive traders into
selling the WINNER at 0.84-0.93 for 1-4s (~1 in 6 windows on night one). The
sniper buys those dips and holds ≤30s to resolution.

- **Fire condition** (`SignalEngine.evaluate_twap_lock`): inside
  `twap_zone_s` (30s, hard ceiling — the projection is undefined earlier) with
  ≥ `twap_k_min_s` (0.8s) left, displacement ≥ the k-interpolated margin, ask ≤
  `tier_prob − sniper_min_edge`. Two frozen tiers
  (`signal_engine.TWAP_MARGIN_P995`/`_MAX`, measured on 564 rx-clock tape
  windows, zero disagreements in 583 incl. night one): beyond the max-ever
  error → prob 0.999 (ask ≤ ~0.96); beyond p99.5 → prob 0.995 (ask ≤ ~0.955).
  The cap DERIVES from the edge floor — one knob, no separate ask-cap to
  drift. Tuning the margin tables to make a window fire is relaxing a bar.
  **The tables are correctly sized, NOT conservative — measured 08-10 on 739
  windows**: making them vol-conditional (looser on calm windows, the obvious
  volume lever) introduces 6 windows where the LOSING side clears P≥0.995
  against 0 under the frozen tables. Vol is not persistent at this timescale —
  both breach windows were dead-calm and then moved $28/$18 inside the
  averaging window, and 10% of windows double their vol inside the zone. Calm
  is not safe. Widening the rule below the frozen tail is also unprofitable,
  not merely unsafe: a fully calibrated continuous P(win) over the 0.85-0.99
  body loses to the CLOB ask on log score in every split, and its claimed-edge
  gradient inverts (see the monotonicity bar under the open leg).
  The main loop wakes on raw Chainlink reports (`report_event`) and CLOB book
  events; the µs pre-gate (`_twap_hot`) fast-paths any wake at ≥90% of the
  p99.5 margin so a 1s dip is never throttled past.
- **Hold to resolution — every sniper leg, unconditionally**
  (`_evaluate_and_exit_position` skips all `signal_leg`-stamped positions):
  all three legs' edges were MEASURED hold-to-resolution, and L1's
  short-horizon spot lens re-deciding a resolution bet sells every noise
  bottom — night one it scalped an open_edge fill −64% forty seconds in and
  dumped a WINNING maker fill at 0.05 seconds before it paid $1.00 (the
  scalp was that leg's entire loss). The lock-hold projection guard remains
  for unstamped/legacy positions only. Any smarter exit needs its own
  measured evidence first.
- **Fill**: the sniper FOK limit pads the decision ask by only `sniper_fok_slip`
  (0.01, ~one tick) then dies — the pad absorbs benign jitter, but a dip that
  snapped back to 0.99+ before the order lands KILLS it and the bot sits that
  window out (never chase a vanished dip upward; burst-era fills proved wide
  pads buy exactly the repriced books). Capped at `model_prob − min_edge` so a
  true reprice can never fill below the edge floor. All gates run at the decision ask (harness-faithful);
  the pre-submit VWAP re-check still vetoes books that lost the edge. The booked
  entry is the CLOB's TRUE fill VWAP — resolved WS-tape → balance-delta →
  `associate_trades` REST → loudly-logged limit fallback; in production the
  lookups lose the indexer race on nearly every fill, so the **+8s audit is
  the de-facto booking authority**: it syncs entry + shares_held to the
  wallet's chain truth (avgPrice when served, else notional/wallet-shares —
  the wallet holds exactly notional/VWAP shares, NO share-denominated fee
  on-chain; scalps book the modeled exit fee into pnl deliberately — while
  the modeled buffer lives in the gates and get_day_stats) for any position
  whose trade_history row isn't booked yet, so last-seconds fills that close
  the window before the audit still book chain-true. Logging follows the audit
  (log-only; engine/DB unchanged): live prints a short FILLED line at fill time
  and the full OPEN banner ONCE, from the audit's `on_entry_settled` callback,
  with the settled entry (flagged provisional if the chain lookup failed); the
  Discord OPEN ping rides the same callback so it always agrees with the
  RESOLVED ping and the books; paper banners/pings stay instant. The OPEN
  banner/ping print the share-denominated fee (notional − settled shares ×
  entry; "~$ (est)" when the chain lookup failed).
  **⚠️THAT READS $0.00 AND IT IS NOT THE FEE. Takers ARE charged.** Polymarket
  ADDS the taker fee to the buyer's USDC debit instead of shaving shares, so
  `notional − shares × entry` is structurally blind to it. Verified 08-10 on
  1,751 live fills via `data-api/activity` `usdcSize`: 903 BUYs match the
  documented model at ratio −0.9996, 327 SELLs at +0.9989, and the 317
  zero-fee rows are the MAKERS (docs: "Makers are never charged fees"). In the
  sniper's own 0.84-0.99 dip band, 175 of 202 BUY fills match the model to
  0.998-1.000 — **0.46-0.89¢/share of real, unbooked taker fee against a
  +2¢/sh bar.** The fee MODEL (`DEFAULT_FEE_RATE` 0.07, `EFFECTIVE_FEE_PEAK`
  0.0175) matches the documented curve exactly and correctly runs the gates;
  it is the REALIZED-fee measurement that is wrong, so every taker ¢/sh in the
  ledger is overstated by that much and the `fees` column must be read as
  "share-denominated fee", not "fee charged". Measuring it truly needs the USDC
  delta (or `data-api/activity`), not the share derivation. **The maker legs are
  genuinely fee-free**, which is why a resting bid nets more at 0.95 than a
  taker does at its own 0.959 cap. SELL exits get the same chain-truth treatment: a FOK
  SELL fills at limit-or-BETTER, so when the trade indexer loses the race the
  close books the padded limit (worst case) — the post-close sell audit
  (`_audit_sell_fill`, ~8-32s) re-reads the order's trade record and syncs
  trade_history + bankroll to the true VWAP (EXIT CORRECTED log + Discord
  note; a real +$0.15 scalp once pinged as a −$0.08 loss without it). CAVEAT on the pre-07-08 live
  ledger: those 46 fills booked the padded limit (silent fallback + a defeated
  audit) — chain-truth reconstruction puts them ≈ breakeven, ~4.4¢/sh better
  than the ledger's −4.3¢/sh; read that era's kill-rule prints accordingly.
- **The gates** (all of them): trusted strike, Chainlink freshness (≤60s),
  **official-TWAP stall veto** (`chainlink_feed.twap_frozen`: the resolution
  source itself can freeze while raw spot moves — measured 08-10 04:15 UTC, the
  official 30s value repeated for 35s while raw climbed $18, leaving our
  reconstruction $5.59 off the served final, i.e. breach-capable at low k.
  Invisible to both existing guards: the freshness gate reads the RAW stream
  (healthy throughout) and the reconnect watchdog reads TWAP RECEIPT time
  (advancing on the repeated value). Fires only on BOTH an exactly-unchanged
  official value ≥20s AND ≥$2 of raw travel in that span — ~11% of consecutive
  reports legitimately repeat, because the relay appears to poll rather than
  stream, and a truly flat market freezes the average honestly),
  edge cap (`sniper_max_edge` 0.50 — wider = stale phantom price), chosen-side
  depth ≥ $50 with a 50% book-fill cap, net-edge after modeled slippage ≥ the
  floor, $1 min size, scar vetoes (graduated + operator-enabled only), and the
  pre-submit VWAP re-check against the live book. **Sizing is market-anchored**:
  leg Kelly is
  computed on `ask + sniper_min_edge` (the defended edge at market odds),
  never on the tier prob — the tier floors are empirical tail bounds, and
  Kelly on a tail bound upsizes exactly the fires a regime shift breaks
  first. The FOK-limit cap rides `signal.prob − min_edge` (the tier/calib
  prob) so a true reprice can never fill below the edge floor.
- **Kill bar — two gates; the harness is only the first.** (1) The
  `analyze_twap_lock.py` replay (lock-dip fires over the micro-tape with FOK
  reachability modeled, plus the bit-exact mechanism check) is a CEILING —
  fills book the decision ask, queue depth is invisible. (2) The BINDING gate
  is the **paper-shadow's realized fills** (`sniper_shadow_status.py` /
  `live_health_read`, scoped to `validation_epoch` 2026-08-07T21:45Z — the
  ladder deploy; earlier fills ran different code):
  **pre-registered 08-07** as the deadline-shortened leg-identical form of
  the 8-day bar (07-15 precedent) — ≥ 6 clean ET days, ≥ 40 fills,
  equal-weight net ≥ +2¢/sh, `t_day ≥ 2`, ≥ 5/6 days positive, day-bootstrap
  `p10 > 0`, AND zero realized lock-breaches (a locked side losing =
  mechanism failure → investigate before any deploy). Never deploy real
  capital on the harness print alone. The nightly health job (§6) re-reads
  both in production.
- **Open head-start leg** (`SignalEngine.evaluate_open_edge`, §3c in
  settings): the strike is the average of the 30s BEFORE the open — known at
  t=0 — so a spot that opens displaced from it is a calibrated favorite
  (843-window curve, lower-bounded, frozen in `signal_engine.OPEN_CALIB`:
  0.65 @$10 → 0.82 @$50; below $5 = noise, never fires). Fires in the first
  `open_zone_s` (20s) when calibrated prob − ask ≥ `open_min_edge` (0.06) —
  night-one books sold $10+ favorites at 0.62 median, and the edge floor
  silences the leg by itself the day they adapt (watch the ping's head-start
  gauge). Trusted strike + fresh raw spot required; every execution gate
  runs; Kelly anchors to ask + floor. **DISABLED 08-07, then REFUTED AND
  CLOSED 08-10 — do not re-enable, and do not re-enable behind a recalibration
  either.** Night one: 22/51 wins vs the calibrated 65% (z ≈ −3.3). The 749-window
  re-test found the curve is approximately RIGHT and the rule is still
  anti-predictive: bucketed by claimed edge (calib − ask), the most profitable
  cell is where the model says DON'T buy (+16.6¢/sh, n=34) and tightening the
  edge floor cuts 45-65% of fires without raising ¢/sh. Under TWAP the open ask
  is at least as good a forecaster and strictly better where they disagree, so
  `calib − ask ≥ floor` selects for windows where the book knows more. The
  early-seconds pocket is not the answer either (paired early-vs-late t_day
  +0.57; Gamma never serves the strike first in any TWAP window, 0/757, so the
  head start is over Polymarket's REST layer, not over the market makers). The
  ping's head-start gauge keeps measuring unconditionally. **The transferable
  bar this produced, now standing for ANY new leg: net ¢/sh must rise
  monotonically across model-edge buckets, scored against an `edge < 0` control
  bucket — a candidate whose best cell is the control is anti-predictive no
  matter how good its aggregate looks.** Fills stamp
  `signal_leg="open_edge"` (+ `open_disp`) — per-leg ledgers in the nightly
  ping; the leg's own bar mirrors the lock bar and its kill is the operator
  flipping `open_edge_enabled: false`.
- **POST-CLOSE CERTAINTY PHASE** (`maker_bid.certain_winner` +
  `_place_post_close_ladder`, `maker.post_close_*` in settings): the market keeps
  ACCEPTING ORDERS FOR MINUTES after the close — verified live at close+143s,
  `acceptingOrders=True` on both Gamma and the CLOB, winner showing 101 bid
  levels and ZERO asks (so nothing can be lifted; only a resting bid works).
  Measured over 814 windows / 4.29M trades: post-close is the ONLY part of the
  window where makers win on EVERY day (takers lose it 5/5), because the outcome
  is settled fact while sellers who haven't read it yet dump the winner. So the
  ladder no longer dies at `maker_k_cancel_s` — it holds through the close for
  `post_close_s` (90s) and arms `post_close_ladder` on the settled side.
  **It also arms with NO pre-close ladder at all** (`arm_post_close` /
  `_promote_pending`): the outcome is settled fact in EVERY window, but the
  ladder only rests on the few that lock at max tier inside k [3,25]s, so tying
  the two together threw away all but a handful of windows a day. The fire path
  arms an intent on every window (safe on any strike — `certain_winner`
  re-verifies both boundary captures at promotion and fails closed), sized by
  `post_close_bankroll_frac` since a settled outcome is not a Kelly bet. Both
  sides stay WS-subscribed while pending, because the winner is unknown until
  the closing boundary lands ~2s later and going deaf there would break
  paper/live parity silently (live polls its fills over REST). Standalone fills
  stamp `signal_leg="post_close"`; a ladder-promoted post-close stays
  `maker_bid` because those fills blend with pre-close rungs into one position.
  **Geometry measured 08-11 over 150 windows / 1,364 post-close sales of the
  winner** (`data-api/trades`, winner from Gamma `outcomePrices`, 0
  disagreements with `finalPrice >= priceToBeat`): ~$475/window of supply,
  149/150 windows had some, print price 0.990 from p05 through p50, 1,115 of
  1,364 inside the first 60s, and trades stop dead at close+300s. The window is
  90s not 300s because the tail is dollar-rich but PROFIT-poor — $560 of the
  $589 profit-if-all sits at ≤0.99 while the 60-300s flow prints at median 0.999
  (0.1¢/share) — and a 300s rest would hold the single ladder slot straight
  through the next window's k [3,25]s placement point, suppressing the pre-close
  leg outright. Supply far
  exceeds what the bankroll can absorb, so the top rung takes MARGIN over queue
  position — 0.995 wins the race and halves the 1¢. Rungs 0.99/0.97/0.95/0.90
  split 70/10/10/10: the deep ones are the fat tail (8 of 1,364 sales printed
  ≤0.95, returning 22% against 1.01%) and a resting bid that never fills costs
  nothing. **This leg is capital-VELOCITY bound, not supply bound** — the
  ceiling is bankroll × turns/day, which is why manual redemption is the
  binding constraint rather than any edge.
  **This phase does NOT use the projection.** It uses the two official TWAP
  boundary captures: `final >= strike` (tie → Up) is the exact rule Polymarket
  resolves on, and both must be captured AND `strike_reliable` or every rung is
  pulled (5-14 boundaries/day never arrive). The settled winner is re-checked
  EVERY tick, not just at the transition — a bid resting on a $0 token is this
  leg's only unbounded loss. The 4¢ edge floor does not apply here: the tier
  probability was a tail bound on an unfinished average, this is a finished one,
  so 0.5¢ is certain rather than expected — and makers pay no fee, so it is
  gross. Fills book through the same blended `book_maker_fill` path and hold to
  resolution. Its bar is its own; `sniper_enabled: false` still halts it.
- **Lock-informed maker LADDER** (`execution/maker_bid.py`, §3d in settings):
  when a window locks but no dip is trading, a LADDER of GTC bids rests on
  the locked side (0.90/0.60/0.35/0.20, budget split 15/20/30/35) — the
  measured dip CDF (233 locked windows) says panic goes DEEP when it comes
  (touch rates: 0.96 → 5.6%, 0.93 → 4.7%, 0.86 → 3.9%), so rungs across the
  depth beat any single bid (a static 0.935 filled 0/45). Panic fills resting
  orders with ZERO latency and queue priority instead of a 0.4s FOK race, no
  250ms taker hold. Rung prices are set by BREAK-EVEN economics in
  settings.yaml — a resting buy held to resolution breaks even at exactly the
  price paid, so a 0.20 rung needs 20% against a measured 77-96%. The nightly
  `ladder_recalibrate` only REPORTS the trailing dip CDF and never writes the
  geometry: a dip-frequency estimator measures how deep panic happened to reach
  in one day, so it drags the deep rungs shallow — the direction already
  measured wrong. An operator-supplied `state/maker_ladder.json` still
  overrides prices, clamped [0.15, 0.95] and only when its rung count matches
  the config. One ladder at a
  time; placed from the fire path when the taker SKIPs on a locked window
  (k within [3, 25]s) — placement demands the NEVER-BREACHED max tier, and
  the deepest rung arms only at ≥1.5× that margin (deep fills concentrate in
  violent windows where breach risk is conditionally elevated). All rungs
  cancelled the instant the lock weakens below the p99.5 margin, the
  projection goes cold, or k < 1s; accumulated fills book as ONE position at
  the blended price; the taker leg is suppressed while a bid rests
  (one entry path per window — a dip must never fill both). Fills book
  through `BaseTrader.book_maker_fill` (open_trade's tail with the same
  preflight; a rejected booking is a LOUD reconcile-manually error). LIVE
  fills poll at 1Hz off-path; PAPER fills are print-through conservative —
  only tape prints STRICTLY BELOW the bid count, because queue position is
  unknowable and at-price fills would flatter the shadow. Fills stamp
  `signal_leg="maker_bid"` — its own ledger line and bar.
- **Capital deploys ONLY through these legs** — there is no other entry
  path in the codebase. `sniper_enabled` is the shared kill-bar SAFETY across
  all legs — the emergency brake (set `false` to halt every leg), not a
  strategy choice. Recipe: `mode: live` + `sniper_enabled: true`.
- **Post-live kill rule** (armed at any future go-live): trailing-4-day
  lenient mean < +2¢/sh or trailing-8-day t < 2 → set `sniper_enabled: false`.
  A single realized lock-breach at max tier trips it immediately.

## 3. Sizing (every leg)

```
size  = bankroll * kelly * circuit_breaker_mult
size *= concurrent_multiplier(side, market, opens)     # correlation-aware
size  = min(size, bankroll * max_bankroll_deployed)    # 0.80
size  = min(size, side_depth * max_book_fill_pct)      # 0.50
if size < 1.0: skip                                    # CLOB $1 floor
```

`kelly` is the fee-aware Kelly on the market-anchored defended edge
(ask + sniper_min_edge), already scaled by `math.kelly_fraction` (0.08) —
fractional Kelly, not full.

- **Circuit breaker**: tier-locked floor at $100/150/200/... milestones
  (floor = tier × 0.85; sqrt Kelly interpolation down to 0.40×; tier never
  resets down; persists via `peak_bankroll`).

## 5. Orders

FOK via `py-clob-client-v2`, up to 3 attempts with jittered backoff — only
provably-unposted failures retry (exchange-confirmed rejects + pre-POST local
errors); ambiguous outcomes never resubmit (double-fill guard). **Order-POST
RTT from the box: p50 356ms (latency_stats.json). ~250ms of that is
Polymarket's DELIBERATE taker delay on crypto up/down markets (`itode: true`
in the market's `/clob-markets` config, verified live 07-22): a marketable
order is validated, HELD 250ms, then re-validated and matched-or-killed — the
post-hold re-validation is mechanically what kills our FOK when the book
reprices (the adverse-selection filter working as designed). It is a policy
floor every taker on these markets pays; no host placement or code beats it.
Client side, the EIP-712 sign runs pure-python without coincurve (p50 17.5ms
on the box) — requirements installs coincurve on Linux (~10× faster sign; no
Windows wheel, dev boxes skip it) — and the rest of the software path is at
the floor (~2-6ms).** py-clob is pinned <1.1.0: 1.1.0 wraps post_order in a
blocking transaction-hash poll (0.25s×30s) that goes live with Polymarket's
2026-07-24 async-commit rollout; the bot reads none of the fields it resolves.
SELL signatures pre-armed on prior HOLD ticks; BUY pre-signs concurrently with
the submit and the submit AWAITS a param-matching in-flight sign (never
double-signs against it — two concurrent pure-python signs contend on the
GIL). WS-only book pre-check, BUY fill VWAP from WS
trade events (SELL fill price via REST after the fill, off the latency path),
tick-size/neg-risk/fee + contract-version caches prewarmed per window,
warm pooled HTTP/2 singleton (keepalive_expiry 60s > 5s ping, connect timeout
5s, TCP_NODELAY), gc.freeze() post-boot (full-GC pauses off the fire path).
`cl_report_to_submit_ms` in trade_context measures the sniper's true race
(latest raw Chainlink receipt → submit) per fill; `cb_tick_to_submit_ms`
stays for cross-era comparisons; `lat_cb_feed_ms`/`lat_clob_feed_ms` stamp the
upstream feed-transit leg (receipt − the message's own exchange timestamp) so
the race is measured from the exchanges' clocks, not just ours. Live boot: key+funder required,
balance/allowance preflight, allowance recheck every 10 fills. Per-trade DB
writes are atomic. `fill.fill_size` is always USDC notional.

## 5. Hold to resolution + resolution

There is NO exit engine: every position rides to resolution, exactly how each
leg's edge was measured (night one proved the alternative — a spot-lens exit
scalped a winning maker fill at 0.05 seconds before it paid $1.00).

**Resolution**: the TWAP oracle decides; winner $1/loser $0 credited
atomically. Exit price is oracle-first (`event_metadata` final_price vs
price_to_beat; a coherent resolved CLOB book as fallback; never Binance);
the orphan fallback (~30 min of Gamma silence) resolves ONLY from genuine
TWAP boundary captures — it waits and pages rather than fabricate. Our own
tape prints a TAPE VERDICT ~85s before Gamma serves, and a per-window
RESOLUTION DRIFT warning fires if Gamma ever disagrees with a reliable
capture by more than a cent. Winner payouts book via Polymarket auto-redeem
(the bankroll sync waits for the winning tokens to clear).

Resolved shares are not swept on-chain — winners are claimed manually at
polymarket.com/portfolio (or via Polymarket's Auto-Redeem), and losing $0 stubs
sit inert on the wallet, locking nothing (the loss is already booked in the
ledger at resolution — deliberately NOT automated: CLOB orders are the only
on-chain thing the bot's wallet ever signs). The startup wallet-check reports
any unclaimed winners honestly; the redeemable-aware orphan gate lets resolved
dust through and fail-closes only on genuinely unresolved positions.

## 6. Recorders + evidence stream

- **Window-path recorder** (`recording.py`, in-process; 1 Hz, 5 Hz in the final
  45s): both tokens' BBO + touch sizes + top-3 depth + book ages, Chainlink
  live price + age, and the strike, for EVERY window (~288/day,
  self-discovering). Columns from the removed feeds (Coinbase/Binance/L1)
  stay in the schema and record NULL — None-not-0.0 is load-bearing. Tables
  `window_paths` (gitignored sidecar DB) / `window_labels`; 90-day retention
  nightly. **This feeds the head-start gauge, the label flow (labels are the
  kill-bar ground truth), and the pivot-research corpus — everything already
  flowing through the process gets persisted.**
- **Tape recorder**: every CLOB print (incl. the exchange's own timestamp +
  fee_rate_bps) → `memory/recordings/*.jsonl` (gitignored).
- **Coinbase spot (recording-only)**: `feeds/coinbase_feed.py` streams the
  BTC-USD ticker; `MicroTape.on_cb_tick` writes `"s"` records (our receipt ts,
  price, and the tick's exchange-transit delay). Wired to the tape and to
  nothing else — the callback swallows its own errors so a tape fault can never
  reach the socket or the trading loop. It exists to measure the relay gap in
  the header; treat any use of it in a decision path as a new leg needing a
  bar.
- **Micro-tape** (`MicroTape`): event-true streams the 5Hz sampler can't see —
  every CLOB best-bid/ask CHANGE (final 90s of each window) and every
  Chainlink RTDS report (always; payload + receipt ts, so delivery holes are
  measurable) → `memory/recordings/micro_*.jsonl`
  (gitignored). It also records the official 30s-TWAP stream (the resolution
  source) as `"t"` records with payload + receipt ts;
  `chainlink_feed.running_avg`/`twap_30()` reconstruct the average from raw
  reports on the rx clock (None until covered — a partial average must never
  masquerade), and `twap_official`/`twap_official_ts`/`twap_official_rx` hold
  the latest official value — the topic delivers ~1.6-1.8s behind observation,
  so our own reconstruction is the faster read while the official value
  remains the resolver and the strike source. This tape is what the
  `analyze_twap_lock.py` harness replays (fires + FOK reachability against the
  true book trajectory). Finished days are **gzipped nightly**
  (`compress_recordings_job`, ~39× at ~40 MB/s: 1.9 GB → ~50 MB; today's file
  is skipped while it's still being appended), which is what lets the corpus
  keep 30 days on a 45 GB host instead of 7 — readers take `.jsonl` and
  `.jsonl.gz` interchangeably (`_open_tape`).
- **Per-decision records**: `trade_context` stamped into outcomes + ghosts
  (entry facts, CLOB book aux, `cl_report_to_submit_ms` decision latency +
  the per-segment `lat_*` breakdown, the TWAP fire facts
  (`twap_proj`/`twap_disp`/`twap_k_s`/`twap_tier` — the lock each fire stood
  on), `open_disp`, `maker_bid`, and `signal_leg` — the per-leg ledger key on
  every fill AND every ghost). **None-vs-0.0 is load-bearing**: cold inputs
  record `None`, never 0.0.
- **NightlyScheduler** (23:45 ET): record rollups + retention sweep + the
  **sniper-edge health report** (`_sniper_health_job`, skipped when
  `sniper_enabled` is false — reports BOTH the SIM read (`analyze_twap_lock.
  health_read`, the lock-dip replay over the trailing micro-tape days — a
  decision-ask ceiling, context only) and the REALIZED fills for
  the current mode (`live_health_read`: live → polybot_live.db; paper →
  polybot_paper.db scoped to `late_window.validation_epoch`, the BINDING
  paper-shadow gate) side by side with their ¢/sh gap — plus a PER-LEG line
  (`signal_leg` ledgers: lock_dip / open_edge, never collapsed) and the
  **head-start gauge** (`analyze_twap_lock.open_gap_read`: trailing-day
  favorite win rate vs the median ask it traded at — the open leg's oxygen
  meter) — and drives the kill-rule verdict off the realized ledger once
  fills exist; alert-only, never flips config). The same ping carries the
  **scar scan** (`polybot/core/scar_scan.py` via `scar_scan_read`) — the
  nightly learning loop: every realized fill for the current mode is sliced
  along a FROZEN dimension library (ask/edge/prob buckets, time-remaining,
  signal_leg, side, DoW, session, refire class, strike displacement,
  chosen-side depth, vig, in-window kill count, CLOB book age at fire, and
  Chainlink-confirms-cross; dims fed by the removed feeds record None and
  never populate cells; booked slip + submit latency are observational-only —
  you can't veto on information the fill created); any cell passing the pre-registered flag rule (n ≥ 8, ≥ 3 ET days,
  EW ≤ −5¢/sh, day-clustered t ≤ −1.5, ≤ 50% ledger coverage, fire-time dims
  only, ≤ 2 new/night, ≤ 6 active) auto-registers as a zero-capital SHADOW
  gate in `memory/state/scar_gates.json`, then each gate runs its own
  frozen-σ SPRT (μ₁ +6¢/sh, σ from its first 4 qualifying days, which never
  score) STRICTLY on post-discovery fills — the OOS test IS the
  multiple-comparisons control, backed by one-active-gate-per-dimension and a
  sibling-overlap veto (a noise cluster gets ONE SPRT, not K correlated
  shots), and gates are mode-stamped (a mode flip PAUSES foreign-mode gates
  rather than splicing ledgers into one frozen-σ test; a VOID restarts the
  gate's test on fresh days per the SPRT doctrine). Accept-H1 ⇒ the ping says
  add the gate name to `late_window.scar_enforce` (enforcement is always a
  manual config flip, and the fire path independently requires status
  `graduated` — a shadow gate in the list is ignored); accept-H0 ⇒
  auto-retired, can never re-register. Enforced vetoes ghost the attempt,
  journal ONCE per (window, gate) to `state/scar_vetoes.jsonl`, and resolve
  nightly per-gate against `window_labels` — an UPPER-BOUND read (assumes a
  clean fill at the decision ask) that keeps a live veto proving its keep.
  The fire path stamps `scar_refire_class`/`scar_kill_min_ask` (from the
  per-window killed-FOK tracker, decision-ask basis — never the padded FOK
  limit) + `scar_cb_move` into `trade_context`; with `scar_enforce` empty
  (default) the fire path only stamps, never vetoes, and the whole enforce
  hook fails OPEN (a scar-machinery error can never block trading). Flag-rule
  and SPRT constants are design-frozen — tuning them to make a pocket flag
  (or stop flagging) is relaxing a bar. The ping also carries the
  **resolution-mechanism watch** (`resolution_snapshot_read`): every window's
  official final_price must equal the NEXT window's price_to_beat bit-exact
  (both are the TWAP stream's value at the same boundary instant; 17/17 on
  night one) — systematic divergence means Polymarket changed the resolution
  rule again and the lock premise needs re-verification; the ping then says
  set `sniper_enabled: false` (alert-only, like everything here). Pings
  Discord `#polybot-daily` (✅/⚠️/⏳ sniper).

## 7. Hard rules

- No ML/feature-stack entry-side prediction — the CLOB price wins. The two
  sanctioned exceptions, each through its own bar: the final-30s TWAP lock (a
  projection of an already-observed average) and the open head-start leg (a
  frozen calibration of a known strike's displacement). Anything else fires
  zero capital.
- No deployment before a kill bar passes; never relax a bar to pass it.
- No symmetric market-making, no oracle-cadence trading, no expansion past BTC
  until the goal completes.
- No mid-price edge math (executable CLOB BBO only). Never skip the fee: `rate*shares*p*(1-p)`, rate 0.07
  (`DEFAULT_FEE_RATE`); flat-additive gates use `EFFECTIVE_FEE_PEAK` 0.0175 —
  never mix them.
- `gain_pct = pnl/size`, never log_return. Don't bypass the circuit breaker.
  Don't delete `polybot/db/polybot_*.db`.

---

# Part B — Operations

## 8. Project layout

```
polybot/
  main.py                Trading loop; entry/exit/sizing orchestration; sniper hook
  config/                settings.yaml (THE single config source), loader.py (loads + range-validates it)
  core/                  signal_engine (the TWAP legs — margins, calibration,
                         Kelly), scar_scan (nightly learning loop), sprt
  feeds/                 chainlink_feed (strike + projection + resolution),
                         clob_ws (books/tape), market_scanner (discovery + gamma fallback),
                         coinbase_feed (spot, recording-only — §6),
                         _socket, _staleness, _json
  recording.py           WindowPathRecorder (all windows) + TapeRecorder +
                         MicroTape + retention
  execution/             base (BaseTrader, fee math), paper_trader, live_trader,
                         maker_bid (lock-informed resting bid), circuit_breaker,
                         correlation
  agents/                scheduler, outcome_reviewer, ghost_tracker,
                         pipeline_analytics
  memory/                outcomes/, ghost_outcomes/ (+ rollups);
                         recordings/ (gitignored); state/. Layout: polybot/paths.py
  discord_bot/           monitoring + control commands (§11)
  db/models.py           SQLite per mode (positions, trade_history, bankroll,
                         peak_bankroll; window_labels lives here too; window_paths
                         sits in a gitignored sidecar DB — window_paths.db)
scripts/
  run_polybot.sh         THE daily supervisor (systemd unit: polybot.service)
  analyze_twap_lock.py   TWAP lock kill-bar harness (micro-tape replay + bit-exact
                         mechanism check; health_read feeds the nightly ping)
  analyze_late_window.py realized-ledger readers for the nightly job
                         (live_health_read / SPRT / scar / resolution watch)
  sniper_shadow_status.py  paper-shadow fills vs the harness
  verify_keys.py         live preflight: GET-auth + balance/allowance
  smoke_order_test.py    live preflight: one unfillable FOK proves order POSTs
                         clear Cloudflare (verify_keys covers GETs only)
  reset_paper_clean.py   clean-slate the paper ledger (operator-run, bot STOPPED)
```

## 9. Data sources

| Source | Feed | What |
|---|---|---|
| Polymarket CLOB | WS + `GET /price`, `/book`, `/spread`, `/tick-size` | Books, tape, executable prices |
| Polymarket Gamma | `GET /events?slug=` (deprecated upstream; auto-fallback `GET /events/slug/{slug}` — `gamma_events_by_slug`) | Discovery + resolution + labels |
| Chainlink (RTDS WS) | `wss://ws-live-data.polymarket.com` (`crypto_prices_twap_thirty` + raw `crypto_prices_chainlink`) | Strike + resolution (TWAP topic); raw stream feeds the projection. Arrives ~1.63s behind its own payload ts |
| Coinbase (WS ticker) | `wss://ws-feed.exchange.coinbase.com` BTC-USD | **RECORDING ONLY** — measures the relay gap; no decision path reads it |

## 10. Running + invariants

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

## 11. Discord

`!status` `!history [n]` `!pause` `!resume` `!clear [trades|control|all] confirm`
`!session` `!pipeline` `!commands` — `!pause` halts new entries only; `!clear`
purges Discord messages only.
