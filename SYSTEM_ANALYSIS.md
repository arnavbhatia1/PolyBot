# SYSTEM_ANALYSIS.md — PolyBot as it actually exists

Standalone technical analysis of the PolyBot trading system, written for a
reader who has never seen the repository. Prepared 2026-08-27/28 from a
read-only audit of the deployed code (`main` @ 03349951 / 15471a9a, the
revision running on the VPS), the recorded data, the tests, and the live
configuration. Documentation was treated as claims and carries no weight
here; where the existing docs and the code disagreed, the code won.

Evidence tags: **[code path:line]** = the deployed code; **[data …]** = a
recorded artifact with its date range and N; **[cfg key]** =
`polybot/config/settings.yaml`; **[test file]** = an enforced property.
Every number carries one. Supporting traces live in `docs/audit/`
(`01a` infra/config/state, `01b` money path, `01c` data+nightly,
`01d` tests+dead code, `02` data facts, `03_verify_*` claim dispositions).

---

## 1. Thesis

**Venue.** Polymarket runs a binary market every five minutes on whether
Bitcoin's price at the window close is at or above its price at the open
(`btc-updown-5m-<epoch>` [code feeds/market_scanner.py:71-72]). Two ERC-1155
tokens, Up and Down, trade on a central limit order book at $0–$1; the winner
pays $1. Since 2026-08-14 00:00 UTC the price used for both boundaries is
Chainlink's **60-second time-weighted average** of BTC/USD, delivered over
Polymarket's real-time data socket as topic `crypto_prices_twap_sixty`
[code feeds/chainlink_feed.py:522-543]; the served strike and final equal that
stream's first report at or after each boundary, bit-exact: 1,565/1,565
trusted captures on 08-19/20/26 and 207/207 across five nightly checks
[data 03_verify_C001-C088; polybot.log NIGHTLY PING]. The switch from the
30 s stream is pinned in the tape — it matched served finals 275/276 on
08-13 and 0/274 on 08-14 — and the bot's own hook moved 4 d 16 h later
(mid-day 08-18) [data 03_verify_C001-C088]. Ties resolve Up [code main.py:1692].

**Edge mechanism.** A 60-second average is mostly written before the window
ends. At *k* seconds before the close, the bot reconstructs the already-observed
part of the average from the raw Chainlink stream and carries the current spot
over the unobserved tail:

```
proj = w · avg(close−60 … now) + (1 − w) · spot,   w = (now − (close−60)) / 60
```
[code feeds/chainlink_feed.py:219-259]. The order book, by contrast, prices
off spot. When `|proj − strike|` exceeds a calibrated error margin for that
*k*, the outcome is treated as written while the book still shows the winner
below $1. The system's only capital deployment today is a **maker ladder**
resting five GTC bids (0.80/0.65/0.50/0.35/0.20) on the projected winner's
token for the final 25 seconds, filled by sellers panicking through the book,
held to resolution [code execution/maker_bid.py; main.py:1050-1096].

**What the market's own data says about the seat.** Over the full 60s era
(08-14..21, 3.32M prints), makers as a class lost $7.8k/day on $6.62M/day
notional; every day-stable profit pocket is bid-side [data
scripts/research/data/vps-0821/h1_report.md, 8 days]. The winner-side
0.65–0.80 deep-bid seat the bot occupies is small — ~123 shares/window across
all occupants, 78% win rate, +$1,063 on a 159-window sample — near break-even
at the 0.80 end and positive at 0.65–0.75 [data r5_report.md, 08-21..27,
159 sampled windows]. Fill-channel adverse selection is the measured
mechanism: on the era replay, win rate at fills pins to the rung price
(0.95→92.3%, 0.80→80.0%) because the windows that hit a bid are
disproportionately the ones that flip [data h1b_extended_rungs.md, 2,066
windows]. Four alternative edges tested this month against pre-registered bars
were all refuted (cross-window strike knowledge, complement arbitrage,
sell-at-certainty inventory, both-sides dip-buying) [data h2/h3/h4/r4
reports]. The system is therefore a **rare, high-confidence deep-bid strategy
with a small expected edge whose existence is not yet established** (§3, §10).

**Mode and scale.** `mode: paper` [cfg mode]. Paper bankroll $396.37 (set to
$400 by the operator 08-25; the $400 figure is the planned go-live wallet)
[data polybot_paper_audit.db bankroll, 08-27]. The live wallet holds $123.40
and has not placed an order since 2026-08-15 [data polybot_live_audit.db;
fill_stats.json last_updated 2026-08-13].

---

## 2. Strategy mechanics

### 2.1 Decision clock and strike

The loop wakes on any CLOB book update, on market-resolved events, and — while
`late_window.trading_enabled` is true — on every raw Chainlink report, with a
100 ms housekeeping timeout [code main.py:2212-2245]. Evaluations are throttled
to 4 Hz inside the 58 s zone and 1 Hz outside unless the projection is already
within 90% of its margin ("hot"), in which case every wake evaluates
[code main.py:389-426, 2128-2133].

The strike is Gamma's served `price_to_beat` when present (trusted outright,
sticky per window); otherwise the bot's own boundary capture — the sixty
stream's first report at or after the window epoch — trusted only if that
report's own timestamp is within 0.5 s of the boundary (a later first report
means the true boundary report was missed) [code main.py:1395-1455;
feeds/chainlink_feed.py:299-316, 417-436]. An untrusted strike blocks **both**
legs [code main.py:1009-1014]. Boundary trust does not survive a restart, so
the first windows after every boot cannot deploy capital [code
feeds/chainlink_feed.py:292-297; data polybot.log — exactly two `SOURCE CHECK
SKIPPED` lines after each of three boots on 08-27].

### 2.2 Entry gates, in execution order

Twenty-six ordered checks sit between a wake and an order [code
main.py:895-1392; full table in docs/audit/01b_money_path.md §3]. The ones
that matter for capital:

| gate | input → threshold | provenance of threshold |
|---|---|---|
| Stale Chainlink | `age_seconds > 60` → skip | literal [code main.py:969] |
| Official TWAP frozen | value unchanged 20 s while raw moved ≥ $2 → skip | constants [code feeds/chainlink_feed.py:52-57]; a 35 s stall observed 08-10 (pre-era) |
| Leg block | `trading_enabled` and `seconds_remaining ≤ 58` | [cfg late_window.twap_zone_s 58.0] = last fitted margin knot |
| Strike trust | `_strike_trusted[window]` | §2.1 |
| Taker signal | `|disp| ≥ MAX-tier margin`, ask ≤ prob − 0.04 | tables §2.4; `require_max_tier: true` |
| **Taker dormant** | every taker fire rewritten to SKIP | [cfg late_window.taker_enabled false]; if the key is deleted the default is **True** [code main.py:1034] |
| **Ladder placement** | `6 ≤ k ≤ 25` and `|bridged disp| ≥ 1.0 × p99.5(k)` and no position in this window | [cfg maker.maker_k_place_min/max, maker_ladder need 1.0] |
| Ladder budget | `round(bankroll × 0.15 × breaker_mult, 2)`, split 20%/rung, ≥5 shares/rung or the rung is skipped | [cfg maker_bankroll_frac 0.15; code execution/maker_bid.py:43-45, 154-162] |

The taker sizing chain (edge cap 0.50, Kelly × breaker, correlation
multiplier, 0.80 deployment cap, book-depth gates, net-edge-after-slippage,
$1 minimum, FOK limit pad 0.01, pre-submit VWAP drift) exists and is tested
but is **dormant** in the checked-in configuration [code main.py:1133-1300;
docs/audit/01d §3.3].

### 2.3 Exit engine

**None is wired.** `BaseTrader.close_trade` and the whole sell chain beneath
it (`_execute_sell`, `_sellable_shares`, `_scalp_residual_credit`, sell
audits, sell warm-ups) exist in code with zero production callers; every position is held
to resolution and booked by `resolve_position` at $1 or $0
[code execution/base.py:442-556; grep evidence in docs/audit/01b §6.4,
01d §3.1]. The only exits in the live ledger tagged `scalp` (19 rows) date
from before 2026-07-08 [data polybot_live_audit.db trade_history]. The maker
ladder cancels its *resting bids* on a projection flip, a cold projection, or
a post-close winner mismatch — that is order management, not an exit.

### 2.4 Margin tables (the calibrated core)

`TWAP_MARGIN_P995` / `TWAP_MARGIN_MAX` are piecewise-linear knots in *k*
[code core/signal_engine.py:34-45, 53-60]. **Re-fit 2026-08-27** on 3,695
real-final 60s-era windows (15 ET days, 08-13..27): p99.5 = fitted 99.5th
percentile of `|final − proj|` rounded up to $0.5 per knot; MAX = per-tick
interval maxima unioned with 1,651 pre-rule windows re-targeted to a synthetic
60s average (widen-only; widened nothing this fit), rounded up to $1,
monotone-enforced [data r1_report.md, r1_tables.json]. Ladder-relevant
p99.5: $4.0 at k=6, $7.5 at 10, $12.5 at 15, $20.0 at 20, **$28.5 at 25**.

Why this matters: the previous tables (frozen 08-18 on 970 windows from four
calm days, 08-14..17) were exceeded on **11.1% of k=25 samples** against a 0.5% design
once volatility rose (median |final−strike| $5–28/day on the freeze week vs
$43–106/day after) [data r1_report.md]. The chain reproduces the 08-18 knots
16/16 on their own span, so the widening is the market, not the estimator.
Leave-one-day-out at k=25 spans $27.5–29.9 across all 15 folds.

### 2.5 Kelly as implemented vs textbook

```
b     = (1 − ask) / ask
net_b = b × (1 − 0.07)
f     = max(0, (p·net_b − (1 − p)) / net_b) × 0.08,   p = ask + 0.04
```
[code core/signal_engine.py:151-160]. Textbook Kelly for a binary bet at
net odds *b* is `f* = (b·p − q)/b`. The implementation is that form with two
substitutions: the fee is applied as a **flat 7% haircut on the win payoff**
(`b·(1−0.07)`) — a third fee form distinct from both the `rate·p·(1−p)` curve
used in booking and the flat 0.0175 used in the spread gate — and *p* is the
**market ask plus the 0.04 edge floor**, not the tier probability (0.995 /
0.999), so sizing never trusts the tail bound. The result is then scaled by
`kelly_fraction` 0.08 [cfg math.kelly_fraction]. This chain only sizes the
dormant taker; the ladder budget is the flat 15% fraction above.

### 2.6 Fee model vs the current Polymarket schedule

Code: `DEFAULT_FEE_RATE = 0.07`; taker fee `= rate · shares · p · (1−p)`,
collected in shares on buys and USDC on sells; `EFFECTIVE_FEE_PEAK = 0.0175`
(the p=0.5 peak) used only as a flat additive cost in the spread gate; maker
fills booked with zero fee; `maker_rebate` written as 0.0 on every history row
[code execution/base.py:140-173; db/models.py:91-94, 273]. `fetch_fee_rate`
returns the constant without a network call [code feeds/market_scanner.py:210-217].

Venue, as read 2026-08-27: crypto up/down `feeSchedule {rate 0.07,
exponent 1, takerOnly, rebateRate 0.2}`, `/fee-rate` base 1000 bps, tick 0.01
(0.001 after the late flip), minimum 5 shares — **unchanged**; the 20% maker
rebate is real but not modeled [data r5_report.md Part A, sourced to
docs.polymarket.com]. Every one of 3,321,809 era tape prints carries
`fee_bps = 0` — fees are off-tape [data h1_report.md]. The one schedule
change found: the crypto taker delay was cut from 250 ms to 50 ms on
2026-08-17 11:00 UTC [data r5_report.md, Polymarket changelog]; it affects the
dormant taker's latency table (§3), not the maker leg.

---

## 3. Post-TWAP status of every calibration

The resolution source changed on 2026-08-14 00:00 UTC. Anything measured on
the 30 s stream is a claim about a market that no longer exists.

| calibration | value in code/config | status | evidence | data available today to recalibrate |
|---|---|---|---|---|
| Margin tables p99.5/MAX | 16 knots each | **VALID** — re-fit 08-27 | 3,695 real-final windows, 15 ET days; LODO k=25 $27.5–29.9 [data r1_report.md] | `data/win_streams.jsonl.gz` 5,495 windows through 08-27; `ws1_errors60.csv` 105,170 rows |
| Ladder floor `need` | 1.0 | **VALID** — re-decided 08-27 | ws1_oos LODO: 0.5 fails clause i (0.80 rung 84.2% vs 90%) and iv (a 0.5-only arm swept 4 rungs, −$18); 1.0 out-of-fit 4/4 wins, 0 flips in 3,210 arms [data r1_report.md] | same corpus |
| `maker_k_place_max` | 25 | **VALID (stands)** | k15/k20 lose the second OOS half at need 1.0 and 0.5 [data r23_tables.md] | same |
| Rung set / weights | 0.80/0.65/0.50/0.35/0.20 × 20% | **UNDECIDABLE** at honest tables | 4/1/1/1/1 fills per rung in 14 days; the frozen-table row shows 0.65 and 0.50 rungs ran 53%/36% win against 65%/50% break-evens [data r23_tables.md] | accrues at ~0.3 fills/day; ~74 days to 20 fills at current rates |
| Extended 0.85–0.95 rungs | not in config | **REFUTED** | each rung wins less than its price on ≥17 fills; one 95¢ loss erases 19 wins [data h1b_extended_rungs.md] | — |
| `AT_PRICE_QUEUE_SH` (paper at-price credit) | 135 sh | **CONSERVATIVE, unchanged** | sweep-consumed depth med 29 / p75 77 on 56,523 sweeps, 08-14..21; nightly watch alarms if p75 > 135 [data 08-21 re-measurement; code main.py ops watch] | live fill pairs: **none** (0 live ladder fills post-era) |
| Paper GTC round-trip table | p50 56 ms | **UNVALIDATED against live** — the binding gap | 12-sample smoke-test measurement 08-07 (not persisted) [code paper_trader.py:293-299]; live in-anger `gtc` samples = 0 [data latency_stats.json]; the ~500 ms reconstruction is prose only | run `scripts/smoke_gtc_test.py --confirm --samples 12` on the box (persists; nightly KS/p50 watch lights at n ≥ 10) |
| Paper POST-RTT table + `paper_latency_scale` 0.95 | quantiles p50 436 ms | **EXPIRED** by route change | the table is a literal equal to the 07-08 live ledger (n=20); the 19 later nightly ledgers read p50 312–432 ms and the last (08-13, n=2) 302.9 ms [data git history of memory/state/latency_stats.json]; the 250 ms taker hold embedded in it became 50 ms on 08-17 [data r5_report.md] | no re-derivation code exists (`smoke_order_test.py` bypasses `_record_submit_latency` and writes nothing [code scripts/smoke_order_test.py:115]); only live taker POSTs add samples; taker is dormant so no live exposure |
| `twap_k_min_s` | 6.0 | **CARRIED** from a 30 s-era realized breach (k=1.1 fire bought a $0 token, 08-12) | pre-era scar; 60s-era k∈[2,6) knots now $2.5–4.0 p99.5 / $18–19 MAX [data r1_tables.json] | same corpus; not re-decided |
| `RAW_GAP_MAX_S` | 10 s | **VALID** — re-derived 08-18 on 60s-era data | conditional p99.5 reconstruction error $0.79 at gap ≤ 10 s [RESEARCH.md register — artifact not located in the data dir; treat the number as **unverified**, the constant as code-verified] | micro-tape holes |
| HOSTILE regime thresholds | gap p50 < $6, photo < $1 > 15% | **PORTED** (percentile-ported 08-18 from 30 s-era positions) — alert-only | [code main.py:2868-2882]; no 60s-era validation of the thresholds as predictors | window_labels gaps (3,720 era windows) |
| Kill rule | any lock_dip loss; trailing-4-calendar-day mean $ < 0 with ≥4 days and ≥5 fills, per leg | **VALID as code**; sparsity guard measured 08-18 | [code scripts/analyze_late_window.py:146-177; test test_live_health_read.py] | — |
| Chainlink delivery lag ~1.6–1.8 s | narrative | **VALID** — re-measured on era data | sixty-topic delivery lag p50 1.70–1.77 s; payload ts integer-second on 158,676/158,676 records [data 03_verify_C001-C088, micro-tape 08-19/20/26] | — |
| Book reprices 0.33 s after Binance / 2.5 s before our receipt | narrative | **EXPIRED** — 08-10 pre-era race analysis (464 races), no artifact | era raw inter-report gaps p50 0.938 s / p99 2.16 s are the only adjacent current measurement [data feed_staleness.json 08-27, n=2,000] | micro-tape `l`/`t`/`s` rows carry payload and receipt ts for every report |
| Reconstruction accuracy (`running_avg` vs served final) | narrative "median $0.028 / p90 $0.22" | **EXPIRED** — the calm 08-14..17 span | today median $0.11–0.18 / p90 $0.51–0.67 (08-19/20/26, n≈273/day) — regime shift, not a method change; margins are what absorb it [data 03_verify_C001-C088] | `win_streams.jsonl.gz` |
| Binance relay lag p50 0.421 s | narrative | measured 08-18 on 74,184 records (60s era) | artifact not in the data set — **unverified here** | micro `s` rows |
| Fee model 0.07 / maker zero-fee | constants | **VALID** vs venue | [data r5_report.md 08-27] | — |

Realized record on the current configuration: **0 fills since the
2026-08-27 19:28Z epoch** (the tables deployed 19:04Z) [data
polybot_paper_audit.db]. Preceding epochs on the thin tables: 16 fills,
12W/4L, +$21.12, −0.9¢/share equal-weight (08-24 15:40Z → 08-27); 36 fills
since 08-19 13:00Z, +$3.17 [data polybot_paper_audit.db positions ×
trade_history]. The deployment bar the operator applies — ≥6 clean ET days,
≥20 filled windows, EW ≥ +5¢/share, dollars/day > 0 — is **not computed
anywhere in code**; three code-side texts state three different bars
(`sniper_shadow_status.py` ≥40 fills / t ≥ 2 / p10 > 0; the nightly
"Shut-off line" +2.0¢ / t ≥ 2.0; `settings.yaml` comments ≥20 / +5¢)
[code scripts/sniper_shadow_status.py:11-14, 93-95; main.py:2838-2846;
settings.yaml:5-6, 137-138].

---

## 4. Execution and latency

**Critical path for a taker fill** (dormant today) from the raw report the
decision stands on to the order on the wire, measured from every stamped fill
in existence — 36 live fills, 08-05..13, of which 27 ran the current code
[data latency_report.md]:

| hop | measured | owner |
|---|---|---|
| raw-report age at decision | ~1,000 ms p50 (the stream ticks ~1 Hz; wakes land on book events between reports; `sig_woke=False` on 5/5 stamped fills) | Chainlink cadence |
| wake → eval + positions + tick + context (owned compute) | **~3 ms p50 / ~5 ms p99** (n=27); the only segment above 0.1 ms is the positions read at 2.1 ms | ours |
| EIP-712 sign | 4.3 ms p50 (n=1, coincurve on the box); 1.5–5.6 ms across ledgers since coincurve arrived 07-24 vs 17.5 ms pure-python before (07-21 ledger, n=17) | ours |
| POST round trip | ~303 ms p50 (n=2, 08-13) — includes the venue's deliberate taker hold, 250 ms at the time, **50 ms since 08-17** | venue |
| end-to-end report-rx → submit | 1,052 ms p50 (n=5) | — |

Every owned millisecond is already spent; a 25 ms owned budget with a
WARNING at 1.5× guards it (0 breaches logged; no stamped fill since it
shipped) [code main.py:528, 1340-1355; data polybot.log]. The venue-side
floors moved under the table: the paper latency table still embeds the
250 ms hold (§3).

**Maker leg timing** (the live leg): five sequential GTC POSTs per ladder, each
paying a real round trip that is **unmeasured in anger** — paper pays 56 ms/rung
from a 12-sample idle smoke test (08-07); the live recorder that would answer it has zero samples
[code execution/paper_trader.py:297-299; data latency_stats.json]. Cancel
speed is the ladder's risk control (a flip-cancel racing an avalanche) and is
equally unmeasured. Both stamp into every booked fill since 08-22
(`gtc_place_ms`, `gtc_cancel_ms`) [code execution/maker_bid.py:163-172,
304-312, 345-347]; the 16 paper fills since carry only the simulated values.

**Where latency is lost in practice:** not in our process. The feeds are.
The 30 h 55 m log copy shows 43 Chainlink watchdog reconnects (60 s idle) and
73 CLOB drops (`1013 slow consumer: send buffer full`) [data polybot.log
08-26 13:31 → 08-27 20:26]; during the 08-27 nightly the CLOB socket went
172 s without a PONG while tape threads ran [data polybot.log 03:45–04:04Z].
Every CLOB reconnect wipes books and print buffers, so paper fills during a
gap are unobservable (`print_gap` is stamped on the fill, read by nothing)
[code feeds/clob_ws.py:159-165, 183, 211; execution/maker_bid.py:338-343].

---

## 5. Risk stack as implemented

| control | trigger / rule (verified in code) | current state |
|---|---|---|
| Circuit breaker | Milestone tiers `[100,150,200,300,400,600,…]`; floor = locked tier × 0.85; `kelly_multiplier` = 1.0 at/above tier, 0.40 at/below floor, concave (√) between; tier ratchets up on `peak_bankroll`, never down; persisted across restarts [code execution/circuit_breaker.py:17-98; cfg circuit_breaker.floor_pct 0.85, min_multiplier 0.4] | `Tier locked $400 -> floor $340.00` at every boot since 08-25 [data polybot.log]. Scales the ladder budget as well as taker size [code main.py:1076-1078, 1145-1147] |
| Ladder budget | 15% of bankroll × breaker multiplier, ≤ 5 rungs, each ≥ 5 shares else skipped | at $396 → $59.40/ladder, $11.88/rung. Below ~$180 bankroll the 0.80 rung starves (1,355 `MAKER RUNG SKIPPED` lines, concentrated 08-22..24) [data polybot.log.1] |
| Position caps | `max_concurrent_positions` 2 (open only; pending does not count); one ladder at a time; deployed + size ≤ 0.80 × (bankroll + deployed) enforced at booking for maker fills too [cfg execution.*; code execution/base.py:316-326, 399-410] | a maker fill that fails this preflight is **logged CRITICAL and left unbooked** — the shares are already on the exchange. Happened twice in the 08-11/12 live probe [data polybot.log.1 `MAKER UNBOOKED`] |
| Emergency brake | `late_window.trading_enabled` — removes the signal block and the Chainlink wake [code main.py:1003, 2212-2213] | `true` |
| Resolution-source hard gate | per labeled window, served strike/final vs our trusted capture; `|Δ| > 0.005` flips `trading_enabled` False **in-process**, logs CRITICAL, pages Discord; latches once per process; disk config untouched [code recording.py:325-360; main.py:2679-2698]. The nightly source line is a second, narrower check over the last ~2 h of captures only (41–45 windows/night vs ~288 labels/day) [code chainlink_feed.py:429-436] | **0 fires ever** [data logs]. Blind spots: windows without a trusted capture (32 `SOURCE CHECK SKIPPED`, two per boot) and windows Gamma never labels (not counted) |
| Kill rule (nightly, alert-only) | per §3 | `⏳ STILL ACCRUING`; last trip 07-31..08-04 on the pre-era live strategy [data polybot.log.1] |
| Post-close hold | keep resting ≤ 60 s after close **only** while both boundary captures are trusted and name our side; fails closed after a 5 s grace [code execution/maker_bid.py:230-267; cfg post_close_hold_s 60] | |
| Adverse-selection monitoring | **none as a runtime control.** The subsystem was deleted; its state files (`adverse_state.json` 08-03, `scar_gates.json`, `sprt_burst.json`, `cf_watchlist.json`) remain committed with no reader or writer [code paths.py:35; docs/audit/01d §3.1]. What exists instead: the nightly at-price queue watch (p75 vs 135) and the kill rule | |
| Live boot safety | `verify_auth` (balance/allowance ≥ bankroll × kelly × positions × 10), `cancel_all` sweep of resting orders, orphan-token detection (fails closed unless `--allow-orphans`), missed-close reconciliation, dust sweep [code main.py:2462-2577; execution/live_trader.py:316-349, 1325-1657] | the sweep is what protects live GTC rungs across an ungraceful restart |
| Feed guards | stale raw > 60 s; official stall; spot > 3 s; raw hole > 10 s in the averaging span; projection None → ladder cancels [code main.py:969-985; feeds/chainlink_feed.py:37-44, 237-253; execution/maker_bid.py:269-273] | 488 stale skips, 68 stall guards, 638 untrusted-strike skips over 07-13..08-27 [data logs] |

---

## 6. The learning loop, end to end

**Nothing retrains.** The scheduler's own docstring says "Tunes NOTHING"
[code agents/scheduler.py:1-4] and the code agrees: no nightly job writes any
file the decision path reads. Margin tables are module constants; the ladder,
floor and *k* bounds come from `settings.yaml`; `maker_ladder.json` — the one
override the ladder would read — has a reader and **no writer anywhere**
(`ladder_recalibrate` is report-only, `applied: False` on every path)
[code execution/maker_bid.py:111-134; scripts/analyze_twap_lock.py:372-436].

**What the nightly actually is** (23:45 ET, jobs in order, each under a 600 s
`wait_for`): compress recordings (internal 540 s deadline) → window-path
retention (90 d) → price-sum retention (90 d) → `maker_ladder` dip-depth
report (internal 480 s deadline) → recordings retention (30 d) →
`sniper_health` (realized ledger since `validation_epoch`, kill rule, chain
watch, source watch, queue-depth watch, latency watches → one Discord message)
[code main.py:2707-2964; agents/scheduler.py:41-76]. Then the process exits
0 and the supervisor commits `settings.yaml`, `memory/`, and the DBs to git
[code scripts/run_polybot.sh:41-52].

**Promotion gate.** The operator, by hand: `mode`, `trading_enabled`,
`validation_epoch` in `settings.yaml` [code settings.yaml:1, 68, 79]. No code
gates `mode: live` on any bar; live preflight checks only auth, balance and
allowance [code main.py:2462-2475]. The live DB shows 5 deep_proj fills on
08-14/15 (−$34.18) while every paper validation epoch is ≥ 08-19 — the
"bar before live" rule is policy, and the record shows it was not applied to
that probe [data polybot_live_audit.db].

**Recalibration** (the tables, floor, placement window) is offline research
run on a workstation against the pulled corpus and shipped as a commit; the
08-27 re-fit is the worked example [data r1/r23 reports; commit 03349951].

**Failure modes.**
- A job over budget is *abandoned*, not stopped: the thread keeps running.
  `maker_ladder` overran on 08-20/21 and 08-23..26; `compress_recordings` was
  killed mid-write at the midnight restart on 08-20 (leaving a `.gz.part`)
  until it gained an internal deadline on 08-21; the SIM read inside
  `sniper_health` still has no internal deadline [data polybot.log.1;
  code agents/scheduler.py:62-67; scripts/analyze_twap_lock.py:333-336].
  A 30 s daemon timer at shutdown force-exits any straggler (`EXIT FORCED`
  ×3: 08-20, 08-21, 08-25) [code main.py:3047-3058].
- Empty-ledger nights used to send one contentless Discord line and drop every
  watch; fixed 08-24 [code main.py:2812-2815 removed].
- Gamma down at label time: the window is retried for 40 min then silently
  never labeled — invisible to the source gate, the chain/regime watches and
  the research corpus (the kill rule itself reads `trade_history`, not labels)
  [code recording.py:294-323; scripts/analyze_late_window.py:91-105].
- Discord down: the ping is retried 3× and always written to the log first;
  the source-gate halt does not depend on Discord [code discord_bot/alerts.py:118-137; main.py:2685-2697].
- A nightly producing garbage can only act through the operator; its outputs
  have no runtime consumer (§6 first paragraph).

---

## 7. Paper = live parity

Both modes run the same loop, gates, signal, sizing, ladder logic, booking
arithmetic and DB layer; the trader object is the only polymorphic part
[code docs/audit/01b §7, 21 divergence points]. Enforced contract:
`test_decision_parity.py` replays four real recorded 60s-era windows through
the production feed ingestion, strike derivation, fire path and ladder with
`PaperTrader` and with a `LiveTrader` whose CLOB client is mocked, asserting
element-wise identical gate skips, signals, sizing, GTC/FOK intents, cancels,
retire reasons, bookings, end bankroll, and that the live wire (`post_order`
args) equals the traced intents [test polybot/tests/test_decision_parity.py].
It runs in CI on every push.

Materiality of what differs:

| divergence | type | materiality |
|---|---|---|
| Live `place_gtc_bid` can return None (exchange rejection → `MAKER BID REJECTED`); paper never rejects | **decision** — the resting rung set can differ | Real but unobserved: 0 rejections in the logs; the parity mock always returns an id, so the suite cannot see it |
| GTC fill observation: live polls `size_matched` at 1 Hz (+ a final poll at retire); paper credits prints strictly below a rung in full and at-price volume beyond 135 shares | fill semantics | The whole paper record rests on this rule. Live pairs to check it against: **none**. Complement-cross fills that print on the other token are invisible to paper — bounded at ≤14–17% of deep flow, conservative direction, unconfirmed [data h3_report.md] |
| GTC latency: paper 56 ms/rung sim vs real (unmeasured) | timing → fill semantics | Rungs become matchable ~9× sooner in paper if live is ~500 ms; direction of the P&L bias unknown (flatters wins, punishes losses) |
| FOK latency/fail sims (`_LATENCY_QUANTILES` × 0.95, fail rate 0.5–3%) | timing/fill | dormant leg; table expired (§3) |
| Fill price: live WS VWAP → balance delta → `associate_trades` → limit; paper book walk | fill/booking | dormant leg |
| `_resolve_bankroll`: live reads the wallet and waits for on-chain redeem (PAYOUT STUCK after 600 s); paper adds arithmetic | booking/timing | Live wins settle p50 ~100 s after close [data h4_report.md, 5,736 redeems]; one `WINNING REDEEM STUCK` 07-30 |
| Live-only boot reconciliation, fill audits, banners, allowance rechecks | booking/logging | not decisions |
| Parity config ≠ production config | test scope | the suite uses zone 30 s, need 0.25, min_edge 0.05 (so the recorded windows arm under the wider tables); it proves identical *decisions given paper's fill rule*, not live fill realism |

---

## 8. Test coverage vs the risk surface

481 tests in 33 files; CI = one job, Python 3.12, full suite on every push;
no coverage tool [test .github/workflows/tests.yml; docs/audit/01d]. Where
the tests are: the feed (46), the breaker (39), the base trader (39), the
live trader (46), the ladder (28), the signal (22+6), the kill-rule reader
(22), the recorder (21), parity (9).

Where the money is and the tests are not (**zero coverage**):
- `trading_loop` / `_entry_pass` — the wake/throttle/discovery/order of
  operations that decides *when* the fire path runs.
- `_resolve_expired_position` — the booking tail: pending mark, breaker
  update, outcome record, drift warning.
- `_on_source_mismatch` — the in-process brake (its detector is tested; the
  handler that flips config is not).
- `LiveTrader._ws_vwap_since` — the real fill-price fast path; parity stubs it.
- `LiveTrader.reconcile_open` and its three recovery helpers; `verify_auth`.
- `base.slippage_pct` (dormant taker), `ChainlinkFeed.ingest_*` and live
  `cancel_gtc`/`poll_gtc_fill` outside the parity replay.

Four test files assert on source text/AST rather than behavior. The kill rule
and the source-mismatch *detector* are well tested; the live *response*
paths (config flip, boot sweep after a crash, reconciliation) are the least
covered code closest to real money.

---

## 9. Infrastructure hygiene

**Host.** One Oracle VM (`polybotvcn`), Ubuntu noble, **954 MB RAM, 4 GB swap**;
the service peaked at 711 MB RSS and **1.6 GB swap** in its last accounting
period [data journalctl -u polybot 08-27]. Python 3.12.3, coincurve 21.0.0,
orjson 3.11.9 in the venv [data latency_report.md]. Uptime 25 days at audit.

**Restart surface.** systemd `Restart=always` runs `run_polybot.sh`, which
relaunches *any* exit before 23:30 ET (nonzero after 60 s, zero after 10 s +
commit) [code scripts/run_polybot.sh:41-79]. Consequences verified in data:
(a) a live preflight failure (`main()` returns 0) becomes a 10 s relaunch loop
until 23:30 ET; (b) **unattended-upgrades restarts the trading service
mid-session** — 08-27 06:45:53Z, `libssl3`/`openssl` upgraded and
`needrestart` cycled the unit; the daily timer fires ~06:45 UTC (02:45 ET)
inside trading hours; a resting paper ladder was lost with the process and
the boot `git pull` failed on DNS [data journalctl, apt history,
unattended-upgrades.log 08-27]. In live mode the boot sweep would cancel the
orphaned GTC rungs; any fill between the kill and the sweep is caught only by
orphan detection. Boundary trust is lost on every restart (§2.1).

**Config drift** (14 items, docs/audit/01a §3.4). The material ones:
`late_window.taker_enabled` defaults **True** when absent and is not
validated [code main.py:1034]; `execution.fok_spread_cross_floor` and
`market.entry_window_seconds` are validator-*required* but read by nothing;
in-code defaults disagree with yaml for `post_close_hold_s` (0 vs 60), the
ladder seed `need` (2.0 vs 1.0), `twap_zone_s` at one site (60 vs 58) and
`max_concurrent_positions` at one site (1 vs 2) — unreachable while the
validator requires the keys, but they are what runs if validation is ever
relaxed; `!pipeline` hardcodes 23:45.

**Dormant by configuration.** With `taker_enabled: false` every ghost gate sits
behind the taker remap, so the ghost tracker records nothing (`ghost_outcomes/`
does not exist on disk) [docs/audit/01d §3.3; 03_verify_C198-C290 C263].

**Dead code and orphaned state.** The entire sell chain under
`close_trade` (docstrings still claim `main.py` calls `warm_sell_signature`
— it does not); six `_REGIME_*` shadow constants; `_AUX_FRESH_S_*`,
`_last_hold_log`, `_last_adverse_skip_log_window`; `fetch_market_price`;
the `scar_enforce` validator branch; `polybot/indicators/` (empty).
Committed nightly with no code reference: `adverse_state.json`,
`cf_watchlist.json`, `scar_gates.json`, `sprt_burst.json`; a 71,035-row
`wallet_stats` table in the paper DB that no code creates or reads;
`gate_stats.json` accumulating under deleted ML-era gate names
(`model:below min prob 56%` 5.5M). `window_paths` carries ten NULL-by-design
columns from deleted feeds [docs/audit/01d §3.1; 01a §4.1].

**Comment/docstring drift observed while tracing** (code is right, text is
wrong): `maker_bid.py` docstring "at-price prints never count" (code credits
beyond 135 sh); `_compute_strike` docstring "30s-TWAP stream"; `main.py:911`
"final 30s" (zone is 58 s); `settings.yaml` §3d says fills stamp
`signal_leg="maker_bid"` (code stamps `deep_proj`); `main.py:2342-2344`
says an auth exit waits until 12:01 AM (the script relaunches in 60 s);
`analyze_late_window.TWAP_SWITCH_TS` (08-07) ≠ `analyze_twap_lock`'s (08-14).

**Single points of failure.** One host/process; one socket per feed (all
four Chainlink topics share one connection); Gamma for discovery, strike,
resolution and labels; Discord as the only alert channel; git push as the
only off-box copy of state (nothing pages on `PUSH FAILED`) [docs/audit/01a §6].

**Data caveat for research.** Bid-side size/depth columns in `window_paths`
recorded the *worst* bid level for the whole era until 08-21 (WS books arrive
price-ascending on both sides; `[0]` indexing) — 539,734 of 568,609 rows
[data h3_report.md; fix commit a0e0fb5b].

---

## 10. Findings register (ranked by expected P&L / risk impact)

1. **The margin floor was 2–4× too thin for 13 trading days.** Frozen from four
   calm days (08-14..17); exceeded on 11.1% of k=25 samples; every realized loss of the
   validation period ($24, $17.8, $13.7 reversals) sat inside the honest
   band. Fixed 08-27. The prior paper record (36 fills, +$3.17) is a record of
   the wrong floor [data r1_report.md; polybot_paper_audit.db].
2. **The paper fill clock is unvalidated.** Live GTC round trips: 0 samples;
   paper charges 56 ms/rung from a 12-sample idle smoke test. The binding gate is
   measured against a fill rule whose timing has never been checked against
   the exchange [data latency_stats.json; code paper_trader.py:297-299].
   One command resolves it (§3).
3. **Live GTC rejection is a decision-level paper/live divergence the parity
   suite cannot observe** (mock always returns an id) [code paper_trader.py:63-73;
   live_trader.py:643-667; test_decision_parity.py:215-222].
4. **`taker_enabled` arms itself if the key is deleted** (default True,
   unvalidated) — the dormant taker's whole sizing chain and the expired
   latency table would go live [code main.py:1034; loader.py].
5. **Automatic package upgrades restart the service inside trading hours**
   (08-27 06:45Z, needrestart after openssl); combined with restart-lost
   boundary trust and a 954 MB host that swapped 1.6 GB, the platform is
   fragile under load [data journalctl, apt logs].
6. **Zero-coverage money path**: `trading_loop`/`_entry_pass`,
   `_resolve_expired_position`, `_on_source_mismatch`, `_ws_vwap_since`,
   `reconcile_open` [docs/audit/01d §2.11].
7. **The deployment bar is not code and the code texts disagree** (≥20/+5¢
   vs ≥40/t≥2/p10>0 vs +2.0¢/t≥2.0); nothing gates `mode: live`; the 08-14
   live probe ran before any paper epoch [docs/audit/01c §4.6;
   polybot_live_audit.db].
8. **Source-gate blind spots**: restart-straddling windows (2 per boot) and
   never-labeled windows are unchecked and uncounted; the hard gate has never
   fired, so its response path (`_on_source_mismatch`) is untested by data or
   tests [code recording.py:325-360; data logs].
9. **A maker fill can be refused at booking after it executed** (deployed cap
   / slot preflight) — CRITICAL log, shares unbooked; occurred twice in the
   08-11/12 live probe [code base.py:394-410; data polybot.log.1].
10. **Nightly jobs starve the feeds** (CLOB 172 s no-PONG on 08-27) and
    abandoned threads run into the restart; `maker_ladder` overran 6 nights
    before its 08-27 deadline fix; the SIM read still lacks one [data
    polybot.log; code scripts/analyze_twap_lock.py:333-336].
11. **Paper POST-RTT table and the 436 ms ops-watch anchor are stale by route
    change** (taker hold 250→50 ms, 08-17) [data r5_report.md].
12. **Complement-cross fills may be invisible to paper** (≤14–17% of deep
    flow, conservative, unconfirmed) [data h3_report.md].
13. **Paper fills are under-counted during CLOB gaps** (73 drops in 31 h;
    `print_gap` stamped but never read) [data polybot.log; code maker_bid.py:338-343].
14. **Config drift items** (§9): required-but-unused keys, divergent in-code
    defaults, hardcoded display time.
15. **Orphaned state and dead code shipped nightly**: four state files, the
    `wallet_stats` table, ML-era gate counters, the sell chain, false
    docstrings [docs/audit/01d §3.1; 01a §3.5].
16. **Bid-side depth columns pre-08-21 are worst-level, not touch** — any
    research using `bid_sz_*`/`depth3_bid_*` before that date is wrong [data h3_report.md].
17. **Supervisor relaunch loop on live preflight failure** (10 s cycle until
    23:30 ET) and the comment that misdescribes it [code run_polybot.sh:69-79; main.py:2342-2344].
18. **Live redeem wait** can leave a win pending (PAYOUT STUCK after 600 s;
    one occurrence 07-30) — booking, not decision [code live_trader.py:745-758].

*Findings, not roadmap: no strategy proposals are made here. Items 1 and 11
were remediated on 08-27 and 08-24 respectively; the rest stand as of the
audited revision.*
