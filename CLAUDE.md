# PolyBot

Evidence tags: **[code path:line]** deployed code (main @ 03349951, the VPS
revision) · **[data source, range, N]** recorded artifact · **[cfg key]**
`polybot/config/settings.yaml` · **[test file]** enforced property.
Full traces and every claim's disposition: `docs/audit/`. Deep analysis:
`SYSTEM_ANALYSIS.md`. Research register: `RESEARCH.md`; counterparty census:
`WALLETS.md`. Prior version of this file: `CLAUDE.md.archive-2026-08-27`.

## 1. WHAT THIS IS

A maker bot for Polymarket's 5-minute BTC Up/Down markets. Each window
resolves on Chainlink's 60-second BTC/USD TWAP (final ≥ strike → Up, tie →
Up) [code main.py:1692; feeds/chainlink_feed.py:522-543]. In the final 25 s
the bot reconstructs the mostly-written average from the raw Chainlink stream,
and when its projection clears a calibrated error margin it rests seven GTC
bids (0.80/0.65/0.50/0.35/0.20/0.15/0.10) on the projected winner, filled by sellers
panicking through the book, held to resolution [code execution/maker_bid.py;
main.py:1050-1096]. No sell path is wired (the sell code exists with zero production callers) and there is no entry-side model. Mode:
**paper** [cfg mode]; paper bankroll $405.57 (set to $400 on 08-25, the
planned go-live size; first epoch fill +$9.20 on 08-31) [data paper DB 08-31];
live wallet $123, idle since 08-15 [data polybot_live_audit.db]. The edge is
small and not yet established: 1 fill since the current epoch (0.26/day, on
the replay rate), and makers as a class lose in this market
[data r1_report.md; h1_report.md] — see §5.

## 2. ARCHITECTURE AS-BUILT

**Process.** One Python process on one Oracle VM (954 MB RAM), supervised by
systemd → `scripts/run_polybot.sh` → `python -m polybot.main --mode <mode>
--auto-restart` [code scripts/polybot.service:17; run_polybot.sh:35]. Trading
hours 00:01–23:30 ET [cfg schedule.*]; nightly jobs at 23:45 ET, then exit 0,
git commit+push of `settings.yaml`/`memory/`/`db/`, relaunch at 00:01 ET
[code run_polybot.sh:41-79; agents/scheduler.py:83-106]. Any exit before
23:30 ET relaunches (nonzero after 60 s, zero after 10 s) [code
run_polybot.sh:56-79]. Single-instance lock on `127.0.0.1:49653` [code
main.py:3092-3112].

**Feeds** [code feeds/]. `ChainlinkFeed`: one RTDS socket
(`wss://ws-live-data.polymarket.com`) carrying four topics — raw
`crypto_prices_chainlink` (~1 Hz; the decision clock and the reconstruction
input), `crypto_prices_twap_sixty` (THE strike/final source; first report at
or after each boundary is captured, first write wins), `crypto_prices_twap_thirty`
(recorded only, as A/B evidence), and Binance `crypto_prices btcusdt`
(feeds only the projection's spot bridge) [chainlink_feed.py:357-436,
522-543]. `ClobWebSocket`: L2 books, BBO, prints for the two subscribed
tokens; every reconnect wipes books and print buffers and stamps
`last_print_gap_ts` [clob_ws.py:159-215]. `BTCMarketScanner`: Gamma
`/events?slug=` (fallback `/events/slug/{slug}`) for discovery,
`price_to_beat`, `final_price`; CLOB REST `/book`, `/tick-size`, `/spread`;
`fetch_fee_rate` returns the constant 0.07 [market_scanner.py:74-99, 189-259].

**Decision path** (identical in paper and live) [code main.py:895-1392;
full 26-gate table in docs/audit/01b §3]. Wake → throttle (4 Hz in the 58 s
zone, 1 Hz outside, unthrottled when |disp| ≥ 0.9 × margin) → discovery →
book gates (freshness ≤ 10 s, price sum ∈ [0.98, 1.02], depth ≥ $50, spread)
→ strike (Gamma wins; else our capture, trusted only if its payload ts is
within 0.5 s of the boundary; untrusted → no capital) → feed guards (raw
> 60 s stale; official value frozen 20 s while raw moved ≥ $2; spot > 3 s;
raw hole > 10 s) → taker signal (dormant, §3) → **ladder placement**: at the
first tick with 6 ≤ k ≤ 58 where `|proj_bridged − strike| ≥ 0.6 × p99.5(k)`
and no position in the window, rest `budget = bankroll × 0.40 × breaker_mult`
split ~1/7 per rung, each rung ≥ 5 shares or skipped. `maintain()` every tick
cancels all rungs when the signed displacement drops below the floor
("flipped"/"inside noise") or the projection goes cold; after the close it
keeps resting ≤ 60 s only while both boundary captures are trusted and name
our side, failing closed after 5 s [code execution/maker_bid.py:138-295].

**Fills and booking.** Paper: a print strictly below a rung fills it in
full; at-price prints credit only volume beyond 135 shares [maker_bid.py:191-218].
Live: `size_matched` polled at 1 Hz plus a final poll at retire
[maker_bid.py:283-325; live_trader.py:674-683]. Accrued fills book as ONE
blended position with zero maker fee; a booking that fails the deployed cap
logs CRITICAL and leaves the shares unbooked [code base.py:379-438].
Resolution: Gamma `final ≥ strike` → $1/$0; else a coherent closed book at an
extreme; never Binance; orphans resolve only from two trusted captures
[main.py:1673-1716, 1823-1956]. Live bankroll = wallet balance, wins wait
for on-chain redeem [live_trader.py:685-758]; paper = arithmetic.

**Persistence.** Per-mode SQLite (`positions`, `trade_history`, `bankroll`,
`peak_bankroll`, `window_labels`) [code db/models.py]; sidecar
`window_paths.db` (1 Hz, 5 Hz final 45 s, 90-day retention); recordings
`tape_*.jsonl` (every print) and `micro_*.jsonl` (BBO changes final 90 s +
every report of all four topics), gzipped nightly, 30-day retention
[recording.py]. State survives restarts via the DB; an in-flight ladder and
boundary trust do not (first windows after any boot cannot deploy) [docs/audit/01a §5.3].

**Nightly** (23:45 ET, 600 s per job, over-budget jobs are abandoned not
stopped): compress → retention ×3 → `maker_ladder` (report-only) →
`sniper_health` → one Discord ping to `#polybot-daily`: realized per-leg
ledger since `validation_epoch`, kill-rule verdict, regime line, chain and
SOURCE watches, ops watch (POST/GTC latency drift, at-price queue drift,
owned-latency breaches) [code main.py:2707-2964]. Nothing retrains; no nightly
output is read by the decision path [docs/audit/01c §4.6].

**Parity.** `polybot/tests/test_decision_parity.py` replays real recorded
windows through both traders (live wire mocked) and asserts identical gates,
signals, sizing, intents, cancels, bookings, and wire == intents; CI runs the
481-test suite on every push [test tests.yml]. It proves identical decisions
*given* paper's fill rule; the one decision-level divergence it cannot see is
a live GTC rejection (paper never rejects) [docs/audit/01b §7-8].

**Discord.** `!status !history [n] !pause !resume !clear … confirm !session
!pipeline !commands`; `!pause` blocks new entries only, in memory [code
discord_bot/bot.py:57-265].

## 3. LIVE CONFIGURATION

| item | value | provenance |
|---|---|---|
| mode / brake / taker | `paper` / `trading_enabled: true` / `taker_enabled: false` | [cfg]; if `taker_enabled` is absent the code default is **True** [code main.py:1034] |
| validation_epoch | 2026-09-04T15:50:00Z | [cfg late_window.validation_epoch] |
| zone / k floor / placement | 58 s / 6 s / k ∈ [6, 58] | [cfg twap_zone_s, twap_k_min_s, maker_k_place_min/max]; k_max 58 operator-directed 09-04 on r24 (21 fills 100%, 0 flip-fills at every k_max on the re-fit tables) |
| ladder | 0.80/0.65/0.50/0.35/0.20/0.15/0.10 × ~1/7, need 0.6 each; budget 40% of bankroll × breaker | [cfg maker.maker_ladder, maker_bankroll_frac]; operator-directed 09-01/09-04 on the r19/r22/r24/r25 frontier [data docs/research/exploit_pass_2026-09-01.md; RESEARCH.md 09-04] |
| rule tripwire | Gamma `resolutionSource` per market vs `market.expected_resolution_source`; mismatch → `trading_enabled=False` in-process + CRITICAL + Discord, latched per process | [code feeds/market_scanner.py `_check_rule_surface`; main.py `_on_rule_surface_change`; test test_rule_tripwire.py] |
| post-close hold | 60 s, `certain_winner` gated | [cfg maker.post_close_hold_s] |
| margin tables | p99.5: $4.0@6 · 7.5@10 · 12.5@15 · 20.0@20 · 28.5@25 · 107.5@58; MAX: $19@6 · 100@25 · 371@58 | [code core/signal_engine.py:34-45]; re-fit 08-27, 3,695 real-final windows, 15 ET days [data r1_report.md] |
| taker (dormant) | max tier only (0.999), ask ≤ prob − 0.04, edge cap 0.50, FOK pad 0.01, Kelly `(b'p−q)/b'`, `b' = b(1−0.07)`, `p = ask+0.04`, × 0.08 | [cfg late_window.*, math.kelly_fraction; code signal_engine.py:84-160] |
| circuit breaker | tiers 100/150/200/300/400/600…; floor = tier × 0.85; multiplier 1.0 → 0.40 (√) between tier and floor; ratchets up only | [code circuit_breaker.py:17-98; cfg circuit_breaker.*]; locked $400 → floor $340 [data polybot.log 08-27] |
| position caps | 2 concurrent (open only); one ladder; deployed ≤ 0.80 × equity; ≤ 50% of side depth (taker); $1 min | [cfg execution.*; code base.py:316-326, 399-410] |
| paper realism | POST RTT quantiles p50 436 ms × 0.95, floor 0.32 s; fail rate 0.5–3%; GTC 56 ms/rung; at-price queue 135 sh | [code paper_trader.py:284-299; cfg execution.paper_*]; POST table = the 07-08 live ledger (n=20; later ledgers p50 312–432 ms, last 302.9 ms n=2 on 08-13) and **expired** by the 08-17 taker-hold cut 250→50 ms [data git history of latency_stats.json; r5_report.md]; GTC table **validated idle** 08-28: live place p50 57 / p90 64 / max 174 ms, cancel p50 55 ms, n=12 (paper 56/60/170) [data latency_stats.json gtc]; in-anger stamps pending the first live ladder |
| fees | taker `0.07·sh·p·(1−p)`; spread gate flat 0.0175; maker 0; rebate not modeled | [code base.py:140-173]; venue schedule unchanged 08-27 [data r5_report.md] |
| feed constants | trust gap 0.5 s; spot stale 3 s; raw hole 10 s; horizon 60 s; bridge anchor 2 s / 1%; stall 20 s / $2 / $0.005 | [code chainlink_feed.py:33-57] |
| kill rule (alert-only) | any `lock_dip` loss; or trailing-4-calendar-day mean $ < 0 with ≥ 4 days and ≥ 5 fills, per leg | [code scripts/analyze_late_window.py:146-177; test test_live_health_read.py] |
| SOURCE hard gate | per labeled window: served strike/final vs trusted capture, `|Δ| > 0.005` → `trading_enabled=False` in-process + CRITICAL + Discord; latches per process. The nightly source line re-checks only the last ~2 h of captures (41–45 windows) | [code recording.py:325-360; main.py:2679-2698; chainlink_feed.py:429-436]; 0 fires ever [data logs 07-13..08-27] |
| secrets | `DISCORD_BOT_TOKEN` (monitoring — without it the bot still trades after a 15 s wait, Discord retries forever); `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER` (live) in `polybot/config/.env` | [code main.py:2966-2984; loader.py:176-180; live_trader.py:261-270] |
| host | Python 3.12.3, coincurve 21.0.0, orjson; 954 MB RAM / 4 GB swap; service peak 711 MB RSS / 1.6 GB swap | [data latency_report.md; journalctl 08-27] |

Config drift worth knowing:
in-code defaults differ from yaml for `post_close_hold_s` (0 vs 60), the
ladder seed `need` (2.0 vs 1.0), one `twap_zone_s` site (60 vs 58) and one
`max_concurrent_positions` site (1 vs 2) [docs/audit/01a §3.4].

## 4. INVARIANTS

Properties of the running system, each verified in code or data.

- Every position is held to resolution: `close_trade` and the sell chain
  have zero production callers; `resolve_position` is the only close
  [code base.py:442-556; docs/audit/01b §6.4].
- Capital deploys only through `book_maker_fill` (ladder) and, if
  `taker_enabled`, `open_trade`; both apply the duplicate-market, position-count
  and 0.80 deployed-cap preflight [code base.py:293-438].
- No capital on an untrusted strike: `_strike_trusted[window]` gates both
  legs; trust needs a Gamma `price_to_beat` or a boundary capture within 0.5 s
  (payload clock) [code main.py:1009-1014; chainlink_feed.py:299-316].
- The ladder's placement floor and its cancel floor are the same number
  (`min_need × p99.5(k)`); post-close resting requires a boundary-verified
  winner and fails closed [code maker_bid.py:216-282].
- The projection returns None (and the ladder cancels) on spot > 3 s old or
  any raw-receipt hole > 10 s inside the averaging span [code chainlink_feed.py:237-253].
- `trading_enabled` is the single brake: false removes the signal block and
  the Chainlink wake; the SOURCE gate is the only code that flips it, and only
  in-process [code main.py:1003, 2212-2213, 2685].
- The circuit-breaker floor never moves down; it persists via `peak_bankroll`
  [code circuit_breaker.py:100-124; main.py:2509-2527].
- Maker fills book at zero fee; taker fees use `0.07·sh·p·(1−p)` in shares on
  entry and USDC on exit; `gain_pct = pnl/size` [code base.py:140-173, 415, 489, 540].
- Paper and live decisions are bit-identical on recorded windows given paper's
  fill rule, enforced in CI [test test_decision_parity.py].
- Fills, ghosts and resolutions record None for cold inputs, never 0.0 [code main.py:1223-1272].
- Live boot: auth/allowance preflight, `cancel_all` sweep of resting orders,
  orphan-token detection fails closed unless `--allow-orphans`
  [code main.py:2462-2564; live_trader.py:1325-1500].
- The bot process runs only on the VPS (single-instance lock + `pkill` in the
  supervisor); the workstation is for analysis on pulled copies [code main.py:3092-3112; run_polybot.sh:31].
- Recording never rides the money path: tape/micro writes go through
  single-thread executors, flushed every 200 rows or 10 s [code recording.py:32-33, 531-546].

## 5. OPEN CALIBRATION

What is unmeasured or expired, the N that exists, and what resolves it.

1. **The edge itself.** Realized on the current tables: 1 fill since
   2026-08-27 19:28Z (+$9.20, 08-31 10:51Z, 0.26/day). On the era replay the
   honest floor fills 0.22 windows/day (4 fills / 18 days, 4/4 wins, +$100.75
   at $60 budget); the ≥20-fill paper bar needs ~90 days at that rate — and
   the deep-sell supply fell ~2.9× in 08-28..30 vs the prior week, with the
   lock structurally excluding ~98% of the pie [data r8_replay_out.txt;
   docs/research/ceiling_2026-08-31.md]. Prior paper epochs (thin tables): 36 fills, +$3.17 since
   08-19; 16 fills, +$21.12, −0.9¢/sh since 08-24 [data polybot_paper_audit.db].
   The deployment bar (≥6 clean ET days, ≥20 filled windows, EW ≥ +5¢/sh,
   $/day > 0) is operator policy — not computed in code; three code texts
   state three different bars [code sniper_shadow_status.py:11-14; main.py:2838-2846; settings.yaml:5-6].
2. **GTC round trip (paper's fill clock).** Idle path measured 08-28 on the
   box: place p50 57.0 / p90 64.1 / max 173.8 ms, cancel p50 55.2 ms, n=12 —
   paper's 56 ms table holds [data latency_stats.json gtc]. Open: the in-anger
   case (five sequential POSTs during a sweep) — read from `gtc_place_ms` /
   `gtc_cancel_ms` on the first live ladder fills; the nightly watch (p50
   ±25%, KS D ≤ 0.30) now has a baseline [code main.py:502-576].
3. **Paper fill rule vs live.** Strictly-below full fill and the 135-share
   at-price credit have no live ladder pairs to check against (0 live fills
   post-era). At-price depth re-measured med 29 / p75 77 on 56,523 sweeps
   (08-14..21) — the constant is conservative [data 08-21 re-measurement].
   Complement-cross fills invisible to paper: ≤ 14–17% of deep flow, direction
   conservative, unconfirmed [data h3_report.md]. Paper fills during CLOB gaps
   (73 drops / 31 h) are unobservable [data polybot.log].
4. **Per-rung economics at honest tables**: undecidable (4/1/1/1/1 fills in
   14 days). Descriptive only: need 0.75 = 8 fills, 100%, +$201.50; the frozen
   tables' 0.65/0.50 rungs ran 53%/36% win against 65%/50% break-evens
   [data r23_tables.md]. Re-decision at ≥ 28 real-final days.
5. **Taker latency table** (`_LATENCY_QUANTILES`, p50 436 ms, scale 0.95):
   a hand-maintained literal equal to the 07-08 live ledger (n=20); later
   nightly ledgers read p50 312–432 ms; the embedded 250 ms venue hold is
   now 50 ms [data latency_stats.json git history; r5_report.md]. No code
   re-derives it (`smoke_order_test.py` bypasses the recorder and writes
   nothing); only live taker POSTs recorded by `_record_submit_latency`
   produce new samples. Matters only if the taker re-arms.
6. **`twap_k_min_s` 6.0** — carried from a 30 s-era realized breach; 60 s-era
   knots at k ∈ [2,6) are $2.5–4.0 p99.5 / $18–19 MAX [data r1_tables.json].
   Not re-decided.
7. **Regime thresholds** (HOSTILE if gap p50 < $6 or photo < $1 > 15%):
   percentile-ported from 30 s-era data; alert-only; no 60 s-era validation
   as a predictor [code main.py:2868-2882].
8. **Latency narrative.** The race numbers ("book reprices 0.33 s after
   Binance / 2.5 s before our receipt") are 08-10 pre-era measurements with
   no artifact retained. Re-established on era data: sixty-topic delivery lag
   p50 1.70–1.77 s (158,676 records, integer-second payload ts); raw
   inter-report gaps p50 0.938 s / p99 2.16 s [data 03_verify_C001-C088;
   feed_staleness.json 08-27, n=2,000]. Reconstruction error vs the served
   final: median $0.11–0.18 / p90 $0.51–0.67 (08-19/20/26, n≈273/day) — the
   $0.028/$0.22 figure was the calm 08-14..17 span [data 03_verify_C001-C088]. Owned compute is ~3 ms p50 /
   5 ms p99 (n=27, pre-era fills) — nothing left to buy [data latency_report.md].
9. **Lock-dip taker re-arm**: dormancy rested on 4 winner-side max-lock dips /
   1 FOK-reachable in 1,184 windows (08-14..18) — artifact not in the data
   set; re-run `scripts/research/ws3_dips.py` on the 14-day corpus before any
   re-arm.
10. **Census cadence**: weekly. Last run 08-28..31 (87 windows): deep-sell
    supply −2.9×, seller concentration top-5 76%, the six-pseudonym
    inventory-flattening cluster is ~40% of it and stationary. 09-01 check:
    rewards RENEWED into September ($10k/day live), all MMs present, 0.99
    wall builds on the 08-13 pattern (44k sh at close / 135k post-close),
    deep supply at ~$176/day pace — beneath the $450 trailing-7d kill-line
    pace [data WALLETS.md; RESEARCH.md 09-01 note].

## 6. APPENDIX: DEAD ENDS

Revisiting any entry requires new evidence exceeding the cited evidence.

- Extended 0.85/0.90/0.95 rungs — engine-true replay, 2,066 windows 08-14..27 → every rung wins less than its price (0.95: 92.3% on 39 fills; 0.90: 87.5%/24; 0.85: 82.4%/17), total < baseline → killed 08-21 [data h1b_extended_rungs.md].
- Cross-window strike knowledge (N's final = N+1's strike, traded on N+1's open) — 1,972 windows, held-out EW −2.1..−4.4¢/sh at every δ ∈ [2,30] s, monotonicity control anti-predictive → refuted 08-21 [data h2_report.md].
- Complement arbitrage (Up-ask + Down-ask < 1 − fees) — 973,302 synchronized pairs, event-true violating time 0.14 s in 8 days, $0.00/day → refuted 08-21 [data h3_report.md].
- Cheaper complement route into a position — median improvement $0.0000, ≥1 tick on 0.01% of arm-seconds → refuted 08-21 [data h3_report.md].
- Sell the boundary-certain winner post-close — auto-redeem lands p50 +100 s vs a +275 s redeploy deadline (5,736 on-chain redeems); haircut 1.07¢/sh costs 5.5× the freed-capital benefit → refuted 08-21 [data h4_report.md].
- Both-sides deep dip-buying, no sign filter (Candidate A) — 3,787 windows, every rung wins less than its own price (0.35: 22% vs 43% needed), −$1,852 at k[6,25]; the only profitable slice is the projection side → refuted 08-27 [data r4_report.md].
- Ladder floor need 0.5 — ws1_oos LODO on the re-fit tables: 0.80 rung 84.2% vs 90% bar; a 0.5-only arm swept 4 rungs −$18 → 1.0 stands 08-27 [data r1_report.md].
- k_place_max 15 / 20 — lose the second OOS half at need 1.0 and 0.5 → 25 stands 08-27 [data r23_tables.md].
- Mid-window touch-bid wall ($11.9k/day) — shared-price queues of 2.3–8.3k shares; 78% held by five wallets → not occupiable 08-21 [data h1_report.md]; live probe of the same mechanism post-close: 0/102 fills at 0.99 behind a ~290k-share wall, 08-13 [REFUTATIONS.md entry; artifact not retained].
- Terminal lottery counterflow ($854/day, final 6 s) — 98.9% is the 0.99 wall matched cross-book via minting → refuted 08-21 [data h1_report.md].
- Underdog-ask longshot tax (+$14.1k/day mean) — sign-flips daily (−$32k on 08-17) → not systematic 08-21 [data h1_report.md].
- k > 25 sign-gated placement — 60 s-era kinematics 08-18: sweeps traverse the ladder inside ~1 s; flip-race fill rates exceed every rung's margin (0.80: 90% vs 80% allowed); [6,58] never beats [6,25] → refuted 08-18 [REFUTATIONS.md entry; artifact not retained].
- deep_proj regime gate — engine-true replay of 991 windows 08-14..17: zero chop-regime full-sweep losses at any floor with k ≤ 25 → alert-only 08-18 [REFUTATIONS.md entry; artifact not retained].
- Hairline-underdog inverse — 14,897 windows: −2.2¢/sh conditioned on the tradable trigger → refuted 08-13 [REFUTATIONS.md entry; 30 s era].
- Continuous P(win) body trading — calibration excellent OOS yet the book wins log score in 100% of splits → refuted 08-10 [REFUTATIONS.md entry; 30 s era].
- Vol-conditional margins — 739 windows: 6 losing-side breaches vs 0 under frozen tables → refuted 08-10 [REFUTATIONS.md entry; 30 s era].
- Latency race / oracle-lag head start — 464 sharp-move races, book wins 97–100% → refuted 08-10 [REFUTATIONS.md entry; 30 s era].
- Open head-start leg — 749 windows, decision rule anti-predictive, Gamma never serves the strike first (0/757) → refuted 08-10 [REFUTATIONS.md entry; 30 s era].
- Burst sniper — −17.5¢/sh under TWAP scoring → ripped out 08-07 [REFUTATIONS.md entry; 30 s era].
- Multi-market expansion (btc-15m, eth/xrp/sol-5m) — post-close volume 0.0–7.3 sh/window vs btc-5m 151.5; combined ceiling ~$16/day → refuted 08-12 [REFUTATIONS.md entry; 30 s era].
- Symmetric market-making — 1-tick spread, 4,416-share touch = 5× bankroll → refuted 08-11 [REFUTATIONS.md entry].
- Strike-ladder / negRisk basket arbitrage — zero fee-clearing arbs → refuted 07-02 [REFUTATIONS.md entry].
- Entry-side feature/ML prediction — six lenses, all dead; filled-outcome records poisoned for entry research → removed 06-09..07-16 (code deleted 08-07, commit ca5ed7ed) [REFUTATIONS.md entry].
- Exit engines (passive exit −2.1¢/sh; night-one scalp sold a winner at 0.05) → sell path removed 07-01/07-08 [REFUTATIONS.md entry]; no production caller exists today [code docs/audit/01b §6.4].
- Post-close 0.99/0.999 camping — 102 live placements, 0 fills, 08-13 → refuted [REFUTATIONS.md entry; 30 s era].
- 30 s-TWAP-era calibrations (564-window tables, "2× floor" doctrine, 08-17 grid) — superseded by the 08-14 rule change; the breach-mechanism lessons carry, the numbers do not [REFUTATIONS.md "Superseded eras"].
