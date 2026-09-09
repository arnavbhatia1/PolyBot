# Wallet-identity flow: feasibility of a new data class, and the first measurement

Scout slice `wallets` of the engine-restart program. 2026-09-08. All numbers below are measured on
local copies unless tagged **inferred** or **unverified**. Scripts and outputs live next to this
file (see "Artifacts").

## 0. Verdict in five lines

1. Wallet identity is a **genuinely new data class**: the market's fast public feeds (CLOB WS
   `last_trade_price`, `best_bid_ask`, RTDS) carry no counterparty identity; only the data-api
   `/trades` endpoint does, stamped at Polygon **block time = exchange match + 2.28 s (p50; p10 1.51,
   p90 3.08; N = 244,444 uniquely matched prints)**, plus an indexing lag that cannot be measured
   offline.
2. A walk-forward class of **"top-decile prior-P&L, one-sided" wallets carries information the book has
   not priced**: their BUYs win **+2.4 to +6.7 pp more than the book mid** at k > 25 s (t = 2.8 to 5.0,
   day-clustered, 13 test days excluding the selected h1 windows), their SELLs lose the same way, and
   wallet skill persists out of sample (Spearman(prior P&L, later excess) = +0.37, N = 3,056 wallets,
   p ~ 1e-99).
3. The information is **partly absorbed by the book within 2 s**; a follower who buys at the ask 2.5 s
   after the match keeps **+0.75 / +0.9 / +2.7 / +5.4 pp** at k 25-60 / 60-120 / 120-240 / >240
   (t 0.9 / 1.2 / 3.1 / 4.4); net of the 7 % taker fee only the k > 120 s buckets stay positive
   (+1.7 and +4.0 pp). At k <= 25 s (our current lane) there is nothing (+0.9 pp, t 1.1; negative at
   0-6 s).
4. The **six-pseudonym cluster's deep sells are fair**: sold-token win rate 28.5 % vs book mid 32.1 %
   (N = 2,134 rows, 72 windows, 18,242 sh); per horizon bucket the excess is -0.1 to -0.5 pp with
   |t| <= 1.0. They carry no information; the "sells the winner at 0.55-0.67" census read was the
   winner-conditioned half of a symmetric flow.
5. The corpus is **small and biased** (362 windows, 20 ET days, 68 of them selected for reversal-heavy
   wall capture; the earliest ~100 s of busy windows censored by a script-side 3,500-row cap that does
   **not** exist upstream). Everything in section 3 is a first measurement, not a validated edge; the
   fix is a full re-pull (section 6).

## 1. Data: what exists, exactly

### 1.1 Record schema (data-api `/trades?market=<conditionId>&takerOnly=false`)

Fields kept by every pull script (`KEEP` tuple: pm_trades_download.py:23-24, r5_census_pull.py:28-29,
r6_census_pull.py:28-29, r12_sept1_pull.py:19-20, h1_wallets_pull.py:23-24):

| field | meaning (measured on 1,230,658 rows + live check 09-08) |
|---|---|
| `proxyWallet` | the counterparty's proxy wallet (lower-case 0x...). ONE wallet per row. |
| `side` | that row's own side, `BUY`/`SELL` of `asset` |
| `asset` | CLOB token id (Up or Down token; joins to `window_labels.token_up/token_down`) |
| `size`, `price` | shares, price of `asset` |
| `timestamp` | integer seconds, **Polygon block time** (section 4) |
| `transactionHash` | settlement tx; groups all rows of one fill |
| `outcome` | "Up"/"Down" |
| `name` | display name ("" for 3,527 of 19,665 wallets) |

Full live response (14 read-only GETs total, 09-08, market `btc-updown-5m-1788873300`, cid
`0xe05bb168...`): `asset, bio, conditionId, eventSlug, icon, name, outcome, outcomeIndex, price,
profileImage, profileImageOptimized, proxyWallet, pseudonym, side, size, slug, timestamp, title,
transactionHash`. **There is no maker/taker flag** (checked both `takerOnly=false` and `=true`).

**Maker vs taker is inferential.** With `takerOnly=false` every fill appears once per counterparty
under the same `transactionHash`. Shapes on 200 live rows: `{BUY Down + SELL Down}` (same-token
maker/taker pair) 19 tx; `{BUY Down + BUY Up}` (mint-matched pair, counterparties on different tokens)
17 tx; multi-maker fills such as `{BUY Up x43 + BUY Down + SELL Down}`. Taker rule used here
(s3_join.py:47-52): in a tx with >= 3 rows, the unique row whose size equals the sum of the others is
the taker. Result on the joined table: 143,994 taker rows, 537,388 maker rows, 549,276 undetermined
(2-row txs). The prior loose match validated the rule against the tape's taker side at 99.6 % where
it fires (93,262 vs 327; latency_match.parquet).

**Timestamp semantics** (s2b_latency_strict.py; only (token, price, size) keys unique among the
window's tape prints and <= 2 data-api rows): `timestamp - floor(exchange_ts)` is 2, 3 or 4 s for
96 % of rows (2 s: 76,921; 3 s: 133,522; 4 s: 24,243; N = 244,444). Stable by day (p50 2.23-2.42 s on
all 20 days) and by horizon (p50 2.23-2.34 s in every k bucket). So `timestamp` is the block in which
the fill settled, ~2.3 s after the exchange match, at 1-s resolution. Our tape's receipt is
`ets + 0.16 s` (d_rx p50 2.12 s vs d_ets 2.28 s).

### 1.2 The "3,500-row cap" is script-side, not upstream

Every pull loops `while offset <= 3000` with `limit=500` (pm_trades_download.py:64,
r5_census_pull.py:69, r6_census_pull.py:69, r12_sept1_pull.py:62, h1_wallets_pull.py:88), so at most
3,500 rows. The docstring's "offset hard-caps at 3000 upstream" (pm_trades_download.py:4) is **false
today**: on the busiest capped r12 window (1788280800, cid `0xb966b49e...`) `offset=5000` returned
500 rows (elapsed 85..115 s) and `offset=8000` returned 158 rows (elapsed -1485..-141 s, pre-open
trades). Whether it was true in August is unverified. Because the API returns newest-first, the cap
drops the **earliest** rows: in capped windows the earliest surviving row sits at elapsed
p10/p50/p90 = 9 / 102 / 219 s (k = 291 / 198 / 81). Capped files: h1 66/68, r5 142/160, r6 75/91,
r12 43/51, i.e. 324 of the 355 joined windows.

### 1.3 Inventory (pull_inventory.json; s1_inventory.py re-run and verified)

| pull | files | rows | capped | empty | wallets | ep range | ET days |
|---|---|---|---|---|---|---|---|
| h1 (vps-0821/h1_pm_trades) | 68 | 237,472 | 66 | 0 | 9,002 | 1786666200..1787320200 | 9 (08-13..08-21) |
| r5 (vps-0821/r5_pm_trades) | 160 | 537,548 | 142 | 1 (1787721000) | 12,199 | 1787270400..1787850900 | 8 (08-20..08-27) |
| r6 (vps-0831/r6_pm_trades) | 91 | 292,042 | 75 | 4 (1788159900, 1788163500, 1788167100, 1788170700) | 8,178 | 1787875200..1788199500 | 5 (08-27..08-31) |
| r12 (vps-0831/r12_pm_trades) | 51 (+51 .meta.json) | 163,596 | 43 | 3 (1788220800, 1788222000, 1788223200) | 5,139 | 1788220800..1788280800 | 2 (08-31..09-01) |
| r10_15m (vps-0831/r10_pm_15m) | 282 (+282 .meta.json) | 715,500 | 71 | 3 | 5,985 | 1787443200..1788201900 | 10 (08-22..08-31) |

Empty files = Gamma returned no market for that slug (the scripts write "" as a done marker).
**Union of btc-5m windows with wallet rows: 362** (h1 68, r5 159, r6 87, r12 48; the four pulls are
disjoint), ep 1786666200..1788280800 (08-14 01:30Z .. 09-01 16:40Z), **20 ET days (08-13..09-01)**,
1,230,658 rows, 19,665 distinct wallets. The 15m family is not in `window_labels` (only btc-5m is
labeled) but each r10 window has a `.meta.json` with `outcomePrices`, so it is joinable; not
measured here.

**Selection bias to carry:** h1's 68 windows were chosen as the top-35 by mid-window bid-wall capture
and top-35 by terminal lottery notional (h1_wallets_pull.py:30-52). They are reversal-heavy: across
all 5,121 labeled 60 s-era windows the displacement-side token (sign of chainlink - strike) wins
+1.0 to +2.2 pp more than its mid at k <= 240 (t 1.7-3.2), but inside the 362 sample windows it
**loses** 4.1 / 5.1 / 5.4 pp at k 60-120 / 120-240 / >240 (t about -2) (s7_uncond_disp.py). r5/r6/r12
are stride samples (unbiased).

## 2. Joinability and the joined sample

Built by s3_join.py into `joined_trades.parquet` (1,230,658 rows) and, with classes,
`joined_trades_classed.parquet`; observation level in `observations.parquet`.

(a) **Outcome**: 362/362 windows join to `window_labels` in `scripts/research/data/polybot_paper.db`
(17,220 btc-5m labels through ep 1788873900, 0 null `resolved_up`; `vps-0831/paper_now.db` is
identical).

(b) **Book at the trade instant**: `vps-0831/window_paths.db` (2.9 GB, rowid range
1781189082..1788288162 = 06-07..09-01 00:02Z, 1 Hz, elapsed 1..300; indexes on `window_id` and `ts`)
holds **all 362 windows** (171,616 rows). Quote joined at `t_match = ets` where a unique tape print
exists (235,438 rows) else `timestamp - 2.3 s`; the last sample <= t_match is 0.47 s old (p50) /
0.63 s (p90). Book quote for 1,206,980 rows (98.1 %). Sub-second BBO from the micro tapes (`k = "b"`
records, final 90 s) extracted for 360/362 windows (16.9 M records, `micro_bbo.parquet`); 1 Hz mid
vs micro pre-trade mid differ by 0.013 mean / 0.035 p90.

(c) **Projection**: `win_streams.jsonl.gz` covers 361/362 windows; the plain projection
proj = w*A + (1-w)*spot re-implemented on the receipt clock with the same anchor / 3 s spot-stale /
10 s hole rules as `polybot/feeds/chainlink_feed.py:151-250` (s3_join.py:73-100). Computed for
311,317 of the 318,856 rows with 0 < k <= 60 (97.6 %). Outside the zone
`disp = chainlink_price - price_to_beat` from window_paths. Rows inside the lock (6 <= k <= 25 and
|disp| >= 0.6 * p99.5(k)): 11,417 of 60,098.

**Joined sample per horizon bucket (k = ep + 300 - t_match):**

| bucket | rows | windows | wallets | exact match time | rows from capped windows |
|---|---|---|---|---|---|
| post-close (k < 0) | 7,025 | 346 | 2,043 | 2,101 | 6,715 |
| 0-6 | 9,301 | 218 | 1,683 | 1,767 | 9,190 |
| 6-25 | 60,098 | 322 | 5,371 | 13,342 | 59,268 |
| 25-60 | 249,457 | 346 | 9,813 | 49,749 | 246,920 |
| 60-120 | 347,550 | 340 | 12,093 | 70,305 | 338,882 |
| 120-240 | 426,044 | 271 | 12,529 | 79,029 | 386,174 |
| >240 | 107,505 | 141 | 6,246 | 18,859 | 80,341 |

The >240 bucket is populated by the 31 uncapped (quiet) windows plus capped windows whose earliest
surviving row happened to be early; treat it as a quiet-window sample.

## 3. First measurement

### 3.1 Design

- Observation = one (wallet, window, side, token, bucket): share-weighted book mid / ask / bid / price
  of the traded token at the match instant, and the token's outcome. `exc_mid = win - mid`. For BUYs,
  positive = informed buyer; for SELLs, negative = informed seller. SE clustered by ET day (13-20
  clusters); window-clustered SE also computed (class_bucket_table.csv, column `se_win`).
- **Walk-forward classes** (s4_measure.py:38-63): on each ET day D a wallet's class is computed from
  its trades in windows on days < D only, using those windows' (already resolved) outcomes:
  `top_directional` = >= 5 prior windows, one-directional in >= 70 % of them, prior P&L in the top
  decile of eligible wallets that day; `bottom_directional` = same, bottom decile (control);
  `two_sided_mm` = >= 5 prior windows, one-directional < 50 %, inferred maker share >= 70 %;
  `wall_camper` = >= 3 prior windows, >= 50 % of bought shares at >= 0.97; `other_known`; `new`.
  P&L = cash flow + terminal payout of net inventory from the (truncated) rows.
  `six_cluster` = the six addresses resolved by display name (seabears 0xbfa0ed0c..., pinkypanda
  0xd6d2b81f..., porkypie12 0x809f2752..., grumbong 0x32c4922d..., wundawally 0xa3338de3..., spork30
  0x1dd2a69e...) - **identity-based, not walk-forward**. Class rules were fixed before any result was
  read but were not pre-registered outside this session.
- top_directional: 459 distinct wallets over 1,880 wallet-days; prior windows p50 31, prior P&L p50
  $130; the class grows from 5 wallets (08-15) to 203 (09-01) as history accumulates.

### 3.2 Results: excess of the traded token's win rate over the book mid (pp), all test windows

Full table: class_bucket_table.csv (also an "excl. h1" set).

**top_directional, BUY**

| bucket | N obs | wallets | windows | days | win | mid | exc vs mid | t | exc vs ask | t |
|---|---|---|---|---|---|---|---|---|---|---|
| 0-6 | 130 | 48 | 33 | 13 | 0.108 | 0.124 | -1.6 | -3.4 | -2.6 | -3.4 |
| 6-25 | 659 | 131 | 99 | 18 | 0.319 | 0.309 | +0.9 | +1.1 | +0.05 | +0.05 |
| 25-60 | 3,003 | 264 | 231 | 18 | 0.453 | 0.422 | **+3.1** | **+3.5** | +2.2 | +2.7 |
| 60-120 | 4,797 | 360 | 311 | 18 | 0.524 | 0.497 | **+2.7** | **+3.1** | +2.0 | +2.3 |
| 120-240 | 5,433 | 356 | 258 | 18 | 0.623 | 0.584 | **+3.9** | **+4.6** | +3.3 | +3.8 |
| >240 | 1,132 | 188 | 127 | 13 | 0.637 | 0.571 | **+6.6** | **+4.9** | +6.1 | +4.5 |

Excluding the 68 selected h1 windows (13 test days): 25-60 +2.4 (t 2.8), 60-120 +1.7 (t 2.45),
120-240 +3.7 (t 4.3), >240 +6.7 (t 4.95), 6-25 +1.5 (t 1.65).

**top_directional, SELL** (sold token win - mid; negative = they sell what loses): 25-60 -2.6
(t -1.9, N 369); 60-120 -2.9 (t -2.3, N 744); 120-240 -3.3 (t -3.1, N 835); >240 -14.9 (t -3.3,
N 98).

**bottom_directional, BUY** (control): 120-240 -1.1 (t -2.3, N 3,984); >240 -6.0 (t -5.0,
N 1,365); other buckets within +/-1.2 pp, |t| < 1. Losers keep losing.

**two_sided_mm, BUY**: vs mid -0.3 to +0.2 pp except 60-120 -0.7 (t -3.4, N 15,034); vs ask -0.7 to
-1.4 pp (t -2.6 to -6.4): makers' buys are adversely selected by about half a spread, no information.
SELL 25-60 +1.7 (t 1.9, N 2,186).

**wall_camper, BUY**: post +0.7 (t 3.4, N 748); 0-6 +1.4 (t 4.9, N 852); 6-25 +0.2 (t 0.15); 25-60
-0.8 (t -0.7); 60-120 -0.2; 120-240 **+3.2 (t 9.5, N 3,113, mid 0.856 -> win 0.888)**; >240 +3.3
(t 2.1, N 289). The 120-240 cell is the strongest t in the table; it says buying the 0.86 favorite
mid-window won 88.8 % in this sample. Unclear whether wallet skill or a favorite underpricing at that
horizon in this sample; re-test on the unbiased re-pull before believing it.

**six_cluster** (identity-based): SELL 0-6 +0.03 (t 0.01, N 93); 6-25 -0.1 (t -0.1, N 545, 78
windows); 25-60 -0.04 (t -0.07, N 1,807); 60-120 -0.45 (t -1.0, N 2,907); 120-240 -0.25 (t -0.8,
N 2,774); >240 +0.5 (t 0.8, N 1,323). BUYs (N 68-199 per bucket, 5 wallets) all within noise.
**Deep sells k in [6, 25], px <= 0.80: 2,134 rows, 72 windows, 18,242 sh; sold-token win rate 28.5 %
vs book mid 32.1 % vs their price 32.2 %; 73 % are resting asks lifted (maker side), 0 % identified
taker; the sold token is on the projection side only 30 % of the time.** Per wallet the sold-token
win rate is 25.5-31.3 % against mids 30.1-35.3 %. Their sells are fair-to-slightly-correct inventory
flattening, not panic and not information. They are 34 % of all SELL rows at k in [6, 25] in the
sample and present on all 20 days.

**All wallets (book-join sanity)**: BUY excess vs mid within +/-0.7 pp for every bucket k <= 120
(se 0.3-0.9 pp), +1.8 pp at 120-240 (t 6.4) and +0.5 at >240; SELL within +/-0.8 pp everywhere. The
120-240 offset is a trade-time (not uniform-time) sampling effect in this reversal-biased sample;
class differences within the bucket are what carry meaning.

### 3.3 Robustness

- **Sub-second book (micro tape, k <= 90)**: top_directional BUY excess vs the pre-trade mid
  (t - 0.3 s) 25-60 +3.0 (t 3.5), 60-120 +3.4 (t 3.1); vs the mid 2 s after the trade +1.4 (t 1.6)
  and +1.9 (t 2.0). About half of the information is in the book within 2 s of the match
  (class_bucket_micro_k90.csv).
- **Per test week** (classes trained on strictly earlier days): r5 08-20..27 (8 days): 25-60 +2.2
  (t 1.4), 60-120 +2.0 (t 2.0), 120-240 +2.3 (t 2.6), >240 +7.5 (t 8.1); r6 08-27..31 (5 days):
  +1.2 (2.5), +2.6 (3.0), +4.4 (2.3), +2.8 (1.3); r12 08-31..09-01 (2 days): +5.3, -0.3, +5.9, +10.7;
  h1 (selected windows): +6.3 (6.3), +10.9 (5.5), +8.0 (1.5). Same sign in every week at 120-240 and
  >240; 3 of 4 weeks at 25-60 and 60-120.
- **Wallet-level persistence**: among 3,056 wallets with >= 5 prior windows and >= 10 later BUY
  observations at k > 25, Spearman(prior P&L, later excess vs mid) = **+0.37 (p ~ 1e-99)**; monotone
  by decile: -3.0, -3.8, -1.7, -2.1, -0.3, +3.4, +4.6, +4.8, +4.9, +3.9 pp (unweighted); share-weighted
  -1.3 -> +1.4 pp (the largest wallets in the top decile dilute it).
- **Is it just displacement-following?** Splitting BUYs by agreement with sign(chainlink - strike)
  at the trade instant, top_directional exceeds all-buyers inside **both** halves at every horizon:
  25-60 agree +0.9 vs -1.6, disagree +4.8 vs +2.7; 60-120 +2.1 vs -1.3, +3.2 vs +2.2; 120-240 +8.2
  vs +6.3, -2.9 vs -4.2; >240 +15.8 vs +13.8, -9.7 vs -13.8. The wallet increment is ~+1 to +3 pp
  beyond the displacement sign we already have.
- **Multiple comparisons**: ~98 class x side x bucket cells; a 5 % Bonferroni bar is |t| >= 3.3.
  Passing: top_directional BUY 25-60 (3.5), 120-240 (4.6), >240 (4.9); top SELL >240 (-3.3); bottom
  BUY >240 (-5.0); wall_camper BUY 0-6 (4.9), 120-240 (9.5); two_sided_mm BUY 60-120 (-3.4).

### 3.4 What a follower could keep (s8_follower.py, follower_edge.csv)

Buy the same token at the **ask observed d seconds after the exchange match** (d = 2.5 s is the
earliest the data-api can show the row; indexing lag comes on top):

| top_directional BUY | N obs | edge vs ask(+2.5 s) | t | net of 0.07*p*(1-p) | edge at +5 s | at +10 s |
|---|---|---|---|---|---|---|
| 6-25 | 659 | -0.9 | -1.0 | -1.2 | -0.7 | -0.7 |
| 25-60 | 3,003 | +0.75 | +0.9 | +0.2 | +0.55 | +0.44 |
| 60-120 | 4,797 | +0.9 | +1.2 | +0.2 | +1.1 | +0.7 |
| 120-240 | 5,433 | **+2.7** | **+3.1** | **+1.7** | +2.6 | +2.7 |
| >240 | 1,132 | **+5.4** | **+4.4** | **+4.0** | +5.4 | +5.4 |

The ask of the bought token moved up >= 1 tick within 2.5 s after 37 % of these buys (unchanged 21 %;
median move 0, p90 +0.07). Controls at 120-240: other_known +1.5 (t 3.8), new +1.2, two_sided_mm 0.0,
bottom_directional -1.6, so ~1.5 pp of the 120-240 follower edge is a general buy-flow effect in this
sample and ~1.2 pp is wallet-specific.

**Per-window availability**: 339 of 355 windows have >= 1 top_directional BUY at k > 25; 35 such
wallets per window (p50; p90 49), 2,861 shares/window (p50). The share-weighted net direction of
their buying predicts the outcome in 65.5 % of the 339 windows against a mean bought-side mid of
0.578; by consensus strength: [0, 0.2) 59.2 % (120 windows), [0.2, 0.5) 70.7 % (147), [0.5, 0.8)
71.2 % (59), [0.8, 1] 38.5 % (13).

## 4. Latency reality

Established offline (N = 244,444 uniquely matched prints, 354 windows, 20 days):

- data-api `timestamp` = Polygon block time; `timestamp - exchange match ts` p10/p50/p90 =
  1.51 / 2.28 / 3.08 s; integer-second granularity. Our tape receipt is 0.16 s after the exchange
  clock, so the data-api row is stamped >= 2.1 s after we saw the print.
- What cannot be measured offline: the **indexing lag** between the block and the row becoming
  queryable, and whether it varies with load. Match -> observable >= 2.3 s + indexing lag.
- The CLOB WS `last_trade_price` payload is documented to carry `transactionHash`
  (docs.polymarket.com/developers/CLOB/websocket/market-channel: `payload: { market, tokenId, price,
  size?, feeRateBps?, side, timestamp?, transactionHash? }`). The bot drops it
  (`polybot/feeds/clob_ws.py:352-361` keeps price/size/side/timestamp/fee_rate_bps only). Recording it
  would make the tape -> wallet join exact instead of (token, price, size)-matched. Whether the field
  is actually populated on the live stream is **unverified** (no WS subscription was run).

**Live measurement the operator would run (read-only GETs, <= 1 per 1.5 s, one window; not run
here):**

```
python - <<'EOF'
# Measures data-api /trades visibility lag for ONE live btc-updown-5m window.
# Records, per new row, (first poll time it appeared) - (its block timestamp).
# Total match->visible latency = this + ~2.3 s (block time vs match clock, measured offline).
import json, time, urllib.request, statistics
ep = (int(time.time()) // 300) * 300 + 300          # next window
slug = f"btc-updown-5m-{ep}"
def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "polybot-latency-probe"}), timeout=20))
while time.time() < ep: time.sleep(1)
cid = get(f"https://gamma-api.polymarket.com/events?slug={slug}")[0]["markets"][0]["conditionId"]
seen = {}
while time.time() < ep + 360:
    t0 = time.time()
    for r in get(f"https://data-api.polymarket.com/trades?market={cid}&limit=200&takerOnly=false"):
        key = (r["transactionHash"], r["proxyWallet"], r["asset"], r["side"], r["size"], r["price"])
        seen.setdefault(key, (t0, r["timestamp"]))
    time.sleep(max(0, 1.5 - (time.time() - t0)))
lags = sorted(obs - ts for obs, ts in seen.values())
print(f"{slug} rows={len(lags)} visibility lag after block ts: p10={lags[len(lags)//10]:.1f}s "
      f"p50={statistics.median(lags):.1f}s p90={lags[9*len(lags)//10]:.1f}s (poll interval 1.5s adds ~0.75s)")
json.dump({str(k): v for k, v in seen.items()}, open(f"dapi_visibility_{ep}.json", "w"))
EOF
```

Cross-check afterwards against the bot's own tape (`tape_YYYY-MM-DD.jsonl`, fields ts/ets/token/
price/size) by (token, price, size) to get match -> visible directly.

## 5. Public read-only check (14 GETs, 09-08; s5_public_check.json)

- Gamma `GET https://gamma-api.polymarket.com/events?slug=btc-updown-5m-1788873300`: market keys
  include `conditionId`, `clobTokenIds`, `outcomes`, `outcomePrices`,
  `resolutionSource = https://data.chain.link/streams/btc-usd-twap-60s-streams`, `makerBaseFee`,
  `takerBaseFee`, `makerRebatesFeeShareBps`, `holdingRewardsEnabled`, `rewardsMinSize`,
  `rewardsMaxSpread`.
- `GET https://data-api.polymarket.com/trades?market=<cid>&limit=...&offset=...&takerOnly=false`:
  fields as in 1.1; both counterparties present (same `transactionHash`); no maker/taker field;
  `timestamp` integer seconds; newest row at elapsed 363 s (post-close trades exist); `offset=3500`,
  `5000`, `8000` all return rows on busy markets, so no upstream offset ceiling was found.
- WS market channel docs (`https://docs.polymarket.com/developers/CLOB/websocket/market-channel`):
  event types `book, price_change, best_bid_ask, last_trade_price, tick_size_change,
  market_resolved, new_market`; `last_trade_price` carries `transactionHash` but **no wallet**. The
  user channel (`https://docs.polymarket.com/developers/CLOB/websocket/user-channel`) carries
  `owner`, `maker_address`, `taker_order_id` but only for the **authenticated account's own**
  orders/trades (`auth: {apiKey, secret, passphrase}`). The comments topic exposes `proxyWallet` of
  commenters (irrelevant). **No public real-time feed carries counterparty identity.**
  `https://docs.polymarket.com/api-reference/trades/get-trades` returned 404 (page moved; not chased).
- **Inferred, unverified**: the CTF Exchange `OrderFilled` event on Polygon carries maker and taker
  addresses per fill; decoding it from a Polygon RPC/WS at block time would give identity ~2.3 s after
  the match without the data-api indexing lag, and `transactionHash` on the WS print would join to it.
  Not checked in this pass.

## 6. Recommendations for the design phase

1. **Treat wallet identity as a real, new data class**: the only one in this program that public
   real-time feeds do not carry and that shows out-of-sample information beyond the book
   (+2.4 to +6.7 pp vs mid at k > 25 s for a walk-forward top-decile class, rho = +0.37 persistence,
   ~+1 to +3 pp incremental to the displacement sign). It is entry-side outcome prediction (Hard Rule 1
   territory): it needs the operator's explicit override before anything is built.
2. **Where it could pay**: only as a k > 120 s feature/follower (+1.7 pp net at 120-240, +4.0 pp at
   >240 on a quiet-window-biased sample), never inside the current k <= 25 lane (nothing there). Frame
   it as one input to a window-outcome model tested against the book on log score (the 09-01
   standard), not as a stand-alone follower.
3. **Fix the corpus before any second measurement**: re-pull all ~5,100 60 s-era windows with full
   paging (drop the `offset <= 3000` bound; the API served offset 8000), which removes the
   early-window censoring and the h1 selection bias at once (about 16 pages/window at the scripts'
   0.12 s spacing, roughly 3 h). Pre-register the class rules and the test (top-decile prior P&L,
   one-sided; day-clustered; Bonferroni over cells) before running.
4. **Instrument the join**: record the WS `transactionHash` on the tape (one field in
   `clob_ws.py:352-361` / `recording.py:521-530`), and run the section 4 visibility probe on one live
   window to close the only latency unknown. Both are operator actions.
5. **Do not build around the six-pseudonym cluster's sells**: they are fair (28.5 % vs 32.1 %,
   |t| <= 1 in every bucket). Their winner-side sells are the winner-conditioned half of a symmetric
   inventory flow; the supply they provide to our ladder is not informative either way.
6. **Look once at wall_camper 120-240** (+3.2 pp, t 9.5): if it survives the unbiased re-pull it says
   the 0.85 favorite is underpriced mid-window in this market, a book fact rather than a wallet fact.

## 7. Blockers and gaps

- 20 ET days, 362 windows, 68 of them selected for reversal-heavy wall capture; only 13-18 day
  clusters behind every SE; the >240 bucket is quiet windows only.
- The 3,500-row script cap censors elapsed < ~100 s in 324/355 windows; class P&L is computed on
  truncated inventories (noise in the classifier, not leakage).
- Class rules fixed a priori in-session but not pre-registered; six_cluster is identity-based.
- The walk-forward is by ET day; the first days have tiny classes (5 wallets on 08-15).
- Book mid at the "match instant" is an estimate (block ts - 2.3 s) for 81 % of rows; the micro
  sensitivity (k <= 90) agrees with the 1 Hz result.
- Follower fee model = taker 0.07*p*(1-p); a maker route (resting at the bid after the signal) was not
  modelled; queue position and fill probability unknown.
- data-api indexing lag unmeasured; WS `transactionHash` presence unverified; Polygon `OrderFilled`
  decoding path unverified.
- No combined model vs book log-score test was run (the standard the 09-01 program set).
- The 15m family (r10, 715,500 rows) is joinable via its meta files but was not measured.

## Artifacts (all under this directory)

- `REPORT.md` (this file), `pull_inventory.json` (per-pull inventory incl. capped window lists),
  `union_windows.json`, `wallet_names.json`
- `joined_trades.parquet` (1,230,658 rows: pull, ep, wallet, name, side, asset_up, price, size,
  dapi_ts, tx, t_match, t_exact, k, kb, up, strike, final, tok_win, is_taker, bid/ask up/down at
  t_match and offsets, mid_tok, ask_tok, bid_tok, proj, disp, disp_src, margin, locked, day,
  direction_up), `joined_trades_classed.parquet` (+ cls, prior_nwin, prior_pnl, named)
- `observations.parquet` (one row per wallet-window-side-token-bucket), `class_bucket_table.csv`,
  `named_wallets_table.csv`, `joined_per_bucket.csv`, `class_bucket_micro_k90.csv`,
  `observations_micro_k90.parquet`, `follower_edge.csv`, `uncond_disp_samples.parquet`
- `latency_match_strict.parquet` (244,444 uniquely matched prints), `latency_match.parquet` (prior
  loose match), `micro_bbo.parquet` (16.9 M BBO records)
- `s5_public_check.json` + `doc_*.txt` (fetched docs, text-stripped)
- scripts: `s1_inventory.py`, `s2b_latency_strict.py`, `s3_join.py`, `s3b_micro_bbo.py`,
  `s4_measure.py`, `s4b_micro.py`, `s5_public_check.py`, `s5b_offsets_docs.py`, `s6_checks.py`,
  `s7_uncond_disp.py`, `s8_follower.py`; logs `s3_join.log`, `s4_measure.log`
