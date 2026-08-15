# PolyBot

5-min BTC Up/Down trader for Polymarket, rebuilt lean for the TWAP era. The
only feeds the STRATEGY reads are Chainlink (RTDS) + the Polymarket CLOB +
Gamma; every position holds to resolution. There is no other model and no exit
path.

**The edge is the projection — the mostly-written 30s average the book cannot
price because it prices off spot.** We are demonstrably not the fast
participant: the book reprices 0.33s after Binance and 2.5s BEFORE our own
oracle receipt, winning 97-100% of sharp-move races. The 08-13 live probe
(12h, real orders) refuted every queue-camping geometry (§2 refutation
record); what survived is the projection's information, harvested at prices
that match its confidence.

**Two legs, one signal (the projection), risk priced two ways:**
1. **Deep-projection maker ladder (§2, `signal_leg="deep_proj"`) — the
   business.** Reverse-engineered 08-14 from the market's best late maker
   (+$12.9k/4.5d, 99% maker): our projection's SIGN matches its side on 89% of
   its deep fills, and its edge is ZERO where the projection disagrees. Rungs
   0.80/0.65/0.50/0.35/0.20 — 1723's OWN fill distribution (it avoids >0.87,
   losing 5.9¢/sh at 0.95) — rest on the projection-favored side (k place
   [6,25]s), hold through the close gated on the boundary-verified winner,
   and are filled by panic crossing the spread. The margin of safety is the
   PRICE: break-even = price paid, against a measured 65-97% window win.
2. **Lock-dip taker (§2)** — fires on the **max tier ONLY**
   (`require_max_tier`) at **k ≥ 6s ONLY** (`twap_k_min_s`): below ~6s the
   margin knots collapse to $0.70-$4 — bounds 564 windows cannot pin, and both
   realized low-k fires ran errors ABOVE the claimed max (one a breach that
   bought a $0 token, §2). At k ≥ 6 the knots are $14+ and 889 locked windows
   over 7 TWAP-era days show zero breaches.

**LIVE PROBE (validation_epoch 2026-08-14T18:30Z)**: deep_proj runs live at
the current wallet — max ~$22.50/window exposure (`maker_bankroll_frac` 0.15
of ~$150), bounded and pre-registered: expect ~5-9 filled windows/day at 65%+
window win; after ~2 days, under 2 fills/day or under 50% win on ≥10 windows
means STOP and investigate. Live is the only queue oracle and the only place
a real maker fill can be proven (the exchange has never filled one of ours).
Halts: any lock_dip loss; trailing-4-day dollars < 0; `sniper_enabled` false
is the brake. The nightly health job reads the realized ledger daily.
Gate-vetoed fires persist as leg-stamped ghosts.

**Resolution mechanism (since 2026-08-07 00:00 UTC)**: Polymarket resolves on
the official **30-second TWAP stream** (strike = the stream's value at the
open, final = its value at the close — both verified bit-exact against served
price_to_beat/final_price, and each close chains into the next strike to the
cent). That switch killed the burst sniper (−17.5¢/sh under TWAP scoring).

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

**The live recipe** (only after the §2 bar passes AND `smoke_gtc_test.py --confirm`
passes — the live ledger holds 332 taker fills and ZERO maker fills; the 08-13
probe proved the GTC path places cleanly but never filled a resting bid):
`settings.yaml` → `mode: live`
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
0.95 — re-measured after Polymarket's async-commit rollout;
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
  ≥ `twap_k_min_s` (**6.0s**) left, displacement ≥ the k-interpolated margin,
  ask ≤ `tier_prob − sniper_min_edge`. Two frozen tiers
  (`signal_engine.TWAP_MARGIN_P995`/`_MAX`, measured on 564 rx-clock tape
  windows): beyond the max-ever error → prob 0.999 (ask ≤ ~0.96); beyond p99.5
  → prob 0.995 (ask ≤ ~0.955). The cap DERIVES from the edge floor — one knob,
  no separate ask-cap to drift. Tuning the margin tables to make a window fire
  is relaxing a bar.
  **`twap_k_min_s: 6.0` is a safety bound, not a tunable.** Below ~6s the max
  knots collapse to $0.70-$4 — tail bounds 564 windows cannot pin, and the
  window itself can be a coin flip finer than any projection: on 08-12 a
  "max-tier" fire at k=1.1s / disp $0.73 bought a $0 token at 0.83 (**the
  first realized max-tier breach**; the window resolved on a $0.0007
  final-strike gap, and the true error $0.7307 beat the $0.70 knot), and the
  08-13 live win at k=2.6s / disp $3.31 also ran a true error ($2.00) above
  its knot ($1.69) — it paid only because the $1.31 gap broke our way. At
  k ≥ 6 the knots are $14+; 889 locked windows over 7 TWAP-era days (a 26x
  larger corpus than the freeze) show zero breaches.
  **`require_max_tier: true` (default) refuses the p99.5 tier outright** —
  p99.5 has breached THREE times, and the 08-11 13:49 breach (disp $21.90 at
  k=19s, verified real projection error $24.83) still sat inside the max-tier
  margin of $26.40: the max bound held through the very event that broke
  p99.5, and max tier would not have taken the trade. Both boundary captures
  for that window were verified bit-exact against the recorded TWAP stream —
  the loss was a projection tail, not a data fault.
  **The tables are correctly sized, NOT conservative — measured 08-10 on 739
  windows**: making them vol-conditional (looser on calm windows, the obvious
  volume lever) introduces 6 windows where the LOSING side clears P≥0.995
  against 0 under the frozen tables. Vol is not persistent at this timescale —
  both breach windows were dead-calm and then moved $28/$18 inside the
  averaging window, and 10% of windows double their vol inside the zone. Calm
  is not safe. Widening the rule below the frozen tail is also unprofitable,
  not merely unsafe: a fully calibrated continuous P(win) over the 0.85-0.99
  body loses to the CLOB ask on log score in every split, and its claimed-edge
  gradient inverts (see the standing monotonicity bar below).
  The main loop wakes on raw Chainlink reports (`report_event`) and CLOB book
  events; the µs pre-gate (`_twap_hot`) fast-paths any wake at ≥90% of the
  p99.5 margin so a 1s dip is never throttled past.
- **Hold to resolution — structurally, not by policy.** There is NO sell path
  in the codebase: no exit evaluation, no scalp, no `close_trade` caller. Both
  legs' edges were MEASURED hold-to-resolution, and a short-horizon lens
  re-deciding a resolution bet sells every noise bottom — night one it scalped
  a fill −64% forty seconds in and dumped a WINNING maker fill at 0.05 seconds
  before it paid $1.00 (that scalp was the leg's entire loss). Any smarter exit
  needs its own measured evidence AND new code; none exists to re-enable.
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
  floor, $1 min size, and the
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
  `live_health_read`, scoped to `validation_epoch`; earlier fills ran different
  code). **The bar is PER LEG** — restated 2026-08-14 for the deep_proj deploy:

  | | lock-dip TAKER | deep_proj LADDER |
  |---|---|---|
  | shape | rare fills at +10¢/sh | ~5-9 filled windows/day, 65-97% win |
  | clean ET days | ≥ 6 | ≥ 6 |
  | fills | ≥ 10 | ≥ 20 windows |
  | profit | EW net ≥ +2¢/sh | EW net ≥ +5¢/sh, `usd_per_day > 0` |
  | expectation | — | backtest says +5..+26¢/sh; falling under +5¢ means paper broke from the backtest — investigate before extending |
  | halt-on-sight | **any lock_dip loss** (every fire is max-tier, so a loss IS a breach) | none (rung losses are priced-in; dollars rule judges) |

  `live_health_read.kill_rule_tripped` computes: any lock_dip loss trips
  immediately; otherwise trailing-4-day mean DOLLARS < 0 trips once ≥ 4 ET
  days exist. Never deploy real capital on the harness print alone. The
  nightly health job (§6) re-reads both legs in production. The paper fill
  rule (strictly-below) is EXACTLY the backtest's fill rule — paper and
  backtest must agree or something is wrong.
- **The standing bar every new leg owes** (born from the open head-start leg,
  refuted and deleted 08-11): net ¢/sh must rise monotonically across
  model-edge buckets, scored against an `edge < 0` control bucket — a
  candidate whose best cell is the control is anti-predictive no matter how
  good its aggregate looks.
- **POST-CLOSE: REFUTED LIVE 2026-08-13 — never rebuild it on paper evidence.**
  The premise was real (the market accepts orders for minutes after the close;
  the winner has bids and zero asks; ~$400/window still sells at 0.990 — 40,620
  shares across the probe's 141 windows), but every price level is owned:
  - **0.99 (pre-flip cap)**: ~1,400-26k shares rest at 0.99 from ~43s BEFORE
    the close (median first-touch k=42.6s over 142 windows), and a ~290k-share
    wall lands at close+0-2s. Post-close drain is ~290 sh/window. Our probe:
    **102 placements, 13 sh each, ZERO fills** while 21,691 shares printed at
    exactly 0.990 during our own 90s rests. Time priority at a shared price is
    unwinnable at any join time we can reach.
  - **0.999 (post-flip)**: the tick flips 0.01→0.001 at a variable close+6s to
    +86s (per-token `tick_size_change` WS event; flip requires price > 0.96 per
    Polymarket docs, applied late server-side). After the flip the 0.999 level
    carries **61k-237k shares**. The whole sub-penny layer printed $12.62 of
    margin in a day — and nothing printed at 0.991-0.998 (one 2-share
    walk-down all day).
  - **Deep tail (≤0.95)**: 123 shares in a day, one window. The 08-11 "8 of
    1,364" was representative; there is no deep business.
  - Total post-close pie ≈ **$431/day of margin across ALL participants**,
    owned by six-figure resting capital. Not a compounding business at any
    bankroll we will ever deploy, and we are structurally last in its queues
    (our certainty needs the closing boundary report, p50 +1.71s; the
    spot-synced crowd is ~2.5s earlier).
  Paper "validated" this leg at 77 fills/day because a snapshot queue model
  cannot see competitors who bid before the snapshot. That failure mode is now
  structural doctrine: **paper maker fills require a print STRICTLY below the
  rung** (see the ladder bullet) — conservative by construction.
- **HAIRLINE-UNDERDOG: REFUTED 2026-08-13, 14,897 windows — do not re-hunt.**
  In label space, windows finishing within $1 of the strike show the cheap
  side (avg ask 0.18) winning 27.6% → +9.2¢/sh. But conditioned on the only
  tradable trigger — OUR displacement small at fire time (k=2 or k=5) — the
  cheap side wins 6.4% against an 8.1¢ ask: **−2.2¢/sh, negative in every
  disp band**, no monotonic gradient vs the disp≥$10 control. The book's
  final-seconds conviction is calibrated even on windows our TWAP arithmetic
  calls a coin flip. Same verdict as the continuous-P refutation: the book
  wins; the label-space richness was hindsight conditioning.
- **DEEP-PROJECTION LADDER** (`execution/maker_bid.py`, §3d in settings,
  `signal_leg="deep_proj"`) — the 1723 mimic, the leg that carries the
  strategy. Discovery 08-14: the market's best late maker (wix 1723,
  0x0a2c53bd…, +$12,922/4.5d, 99% maker, 124/814 windows, fills 0.02-0.87 at
  +12..+61¢/sh) has NO information we lack — our projection's sign matches its
  side on 89.2% of its 706 scored deep fills (92.9% win when agreeing) and its
  edge is a coin flip (52.6%) where the projection disagrees. Its filter is
  contested-ness (its windows' median final gap $2.80 vs market $12.30; it
  touches only 8% of deep-print windows, never the dying-loser flow), it is
  one-sided (107/124 windows), holds to resolution (22 sells in 2,073 rows),
  and 23% of its pnl lands just after the close.
  **The mechanism**: in the final seconds spot is mostly irrelevant to the
  resolution (the average is written) but it is all of how the book prices —
  so panic dumps the projection-favored side into deep resting bids.
  **The ladder decides on the BRIDGED projection** (`bridged=True`):
  spot_est = latest raw Chainlink + Binance's movement since that report's
  payload ts (`spot_bridge_delta`, RTDS `crypto_prices` topic — the crowd's
  feed, ~2.2s fresher than our oracle receipt; lead-lag fit on 4.65M recorded
  pairs bottoms at 2.0-2.5s). The basis cancels in the delta, and every
  failure mode collapses to the PLAIN projection. The TAKER never uses the
  bridge — its frozen margin tables were measured on the plain projection.
  Each placement stamps `twap_proj` (bridged) and `twap_proj_plain` for the
  nightly A/B; micro-tape records the Binance stream as `"s"/src:"bz"`.
  **Rungs are [price, budget_frac, need]** where `need` = the fraction of the
  max-tier margin the displacement must clear at placement (the sign-quality
  floor). The geometry [0.80/0.65/0.50/0.35/0.20], all need 0.18, IS 1723's
  own fill distribution — it avoids >0.87 (−5.9¢/sh at 0.95), so no shallow
  rung exists; every rung's PRICE is its margin of safety. Placement
  k ∈ [6,25]s while the taker SKIPs; rungs keep resting to the close and
  beyond. Cancel-all when the projection goes cold, flips, or drops under the
  noise floor (min_need × max-margin); rungs under `DEEP_HOLD_MAX_PX` 0.85
  survive a p99.5 weakening (the wick that fills a deep rung IS the move that
  dips the projection). **Post-close hold** (`post_close_hold_s` 60): rungs keep
  resting after the close ONLY while the boundary-verified winner
  (`certain_winner`, fails closed, re-checked every tick) equals our side —
  this is NOT the refuted 0.99-cap camp: deep levels carry ~55-100 sh/level
  (measured live 08-14), not 290k walls.
  **Backtest, strictly-below fills, feed lag embedded (receipt-clock
  trajectories vs exchange-clock prints), stale-feed windows vetoed**:
  in-sample 08-06..10 **+26.2¢/sh EW, 9.2 win/day, 97% win, t_day 4.46,
  4/4 days**; out-of-sample 08-11..14 **+5.1¢/sh, 5.0/day, 65% win** — while
  the ANTI-side control loses 46-49¢/sh at t −42/−21 on identical rules in
  BOTH samples. Sim fills are capped at print size (under-counts winners) —
  these are the pessimistic reads. The sign
  carries everything. Sizing: `maker_bankroll_frac` (0.15) of bankroll split
  by the frozen fractions — deep bids are not a Kelly bet on a certainty
  claim. Fills book through `BaseTrader.book_maker_fill` as ONE blended
  position; the taker is suppressed while a bid rests (one entry path per
  window). LIVE fills poll at 1Hz off-path; **PAPER fills: strictly-below prints fill
  a rung in FULL** (the book walked through our level); **AT-price prints
  credit only the volume beyond `AT_PRICE_QUEUE_SH` (55 sh — the live-measured
  median resting size per deep level, 10-window book watch 08-14)**,
  accumulated across the window and tracked separately (`filled_at_px`) so
  live fills recalibrate the constant; above-price prints never fill.
  Snapshot-based `queue_ahead` modeling remains BANNED (102 live placements,
  zero fills against snapshot queues the old model thought beatable) — the
  at-price constant is a fixed live measurement, not a book snapshot. **Paper pays the
  MEASURED GTC round trip on every place AND cancel** (`_simulate_gtc_latency`,
  place p50 0.056 / cancel p50 0.054, box-measured; NEVER the taker table —
  a resting bid never pays the 250ms `itode` hold). Rung prices pass
  `legal_price` (round DOWN to the served tick, clamp [tick, 1−tick]) and the
  5-share minimum (`MIN_SHARES`) — both learned from real rejections. A
  rejection from any cause logs `MAKER BID REJECTED` at ERROR. The nightly
  `ladder_recalibrate` only REPORTS the trailing dip CDF; an operator
  `state/maker_ladder.json` may override prices, clamped [0.15, 0.95], only
  when its rung count matches.
- **Capital deploys ONLY through these legs** — there is no other entry
  path in the codebase. `sniper_enabled` is the shared kill-bar SAFETY across
  all legs — the emergency brake (set `false` to halt every leg), not a
  strategy choice. Recipe: `mode: live` + `sniper_enabled: true`.
- **Post-live kill rule** (armed at any future go-live, and what
  `live_health_read.kill_rule_tripped` computes): **any lock_dip loss** trips
  on ONE occurrence (every fire is max-tier, so a loss IS a breach of the
  never-breach bound — it happened once, 08-12 in paper at k=1.1s, and is why
  the k≥6 floor exists); otherwise **trailing-4-day mean DOLLARS < 0** → set
  `sniper_enabled: false`. Dollars, not ¢/sh — a ¢/sh floor condemns any
  high-price maker fill regardless of profit.

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
  nightly. **This feeds the label flow (labels are the
  kill-bar ground truth), and the pivot-research corpus — everything already
  flowing through the process gets persisted.**
- **Tape recorder**: every CLOB print (incl. the exchange's own timestamp +
  fee_rate_bps) → `memory/recordings/*.jsonl` (gitignored).
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
  (`signal_leg` ledgers: lock_dip / deep_proj, never collapsed)
  — and drives the kill-rule verdict off the realized ledger once
  fills exist; alert-only, never flips config). The ping carries a **regime
  line** (trailing-day |final−strike| p25/p50/p75 + photo-finish share from
  `resolution_snapshot_read`; p50 < $8 or >15% photo-finishes = HOSTILE for
  deep_proj — measured 08-14..15: the massacre regime ran p50 $6.3 / 24%
  against a market-normal $12.3) and the
  **resolution-mechanism watch** (`resolution_snapshot_read`): every window's
  official final_price must equal the NEXT window's price_to_beat bit-exact
  (both are the TWAP stream's value at the same boundary instant; 17/17 on
  night one) — systematic divergence means Polymarket changed the resolution
  rule again and the lock premise needs re-verification; the ping then says
  set `sniper_enabled: false` (alert-only, like everything here). Pings
  Discord `#polybot-daily` (✅/⚠️/⏳ sniper).

## 7. Hard rules

- No ML/feature-stack entry-side prediction — the CLOB price wins. The ONE
  sanctioned exception, through its own bar, is the final-30s TWAP lock (a
  projection of an already-observed average), and it fires only at max tier
  with k ≥ 6s. Anything else fires zero capital.
- No deployment before a kill bar passes; never relax a bar to pass it.
- No symmetric market-making, no oracle-cadence trading, no expansion past
  btc-5m. Expansion is not merely deferred, it is REFUTED: post-close supply
  measured 08-12 over 24 windows per family gives btc-15m 7.3 sh/window
  (20.8% of windows), eth-5m 1.8, xrp-5m 0.6, sol-5m 0.0 — against btc-5m's
  151.5 at 73.3%. All four siblings combined ceiling ~$16/day at an impossible
  100% capture. Scaling comes from SIZE on this one book, not more books.
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
  core/                  signal_engine (the TWAP legs — margins, Kelly)
  feeds/                 chainlink_feed (strike + projection + resolution),
                         clob_ws (books/tape), market_scanner (discovery + gamma fallback),
                         _socket, _staleness, _json
  recording.py           WindowPathRecorder (all windows) + TapeRecorder +
                         MicroTape + retention
  execution/             base (BaseTrader, fee math), paper_trader, live_trader,
                         maker_bid (deep-projection resting ladder), circuit_breaker,
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
                         (live_health_read / resolution watch)
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
| Chainlink (RTDS WS) | `wss://ws-live-data.polymarket.com` (`crypto_prices_twap_thirty` + raw `crypto_prices_chainlink` + Binance `crypto_prices`) | Strike + resolution (TWAP topic); raw stream feeds the projection (arrives ~1.63s behind its own payload ts); the Binance relay feeds ONLY the bridge delta |

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
