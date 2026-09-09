# PolyBot engine-restart: complete data inventory

Scout slice: exhaustive machine-readable inventory of every locally-available
dataset for engine research. Companion machine-readable file:
`INVENTORY.json` in this same directory. Per-dataset raw scan outputs
(`out_*.json`) sit alongside it with full per-window/per-day detail that
didn't fit in the curated summary.

All numbers below are **measured** by opening the actual files (row counts,
column lists, null counts) unless marked otherwise. Scan scripts are in this
directory (`scan_*.py`, `assemble.py`, `condense_recordings.py`) and are
re-runnable.

**IMPORTANT -- path portability**: all paths below are real Windows paths
(`C:/Users/...`). This machine's `python.exe`/`python3.exe` are native
Windows Python -- they do NOT understand Git-Bash `/c/...` style paths. Any
script here must be invoked with a `C:/...` path even when run from Bash.

---

## 1. Top-level inventory of `scripts/research/data/`

```
boundaries.json          695 KB   JSON dict, 8,816 boundary captures
polybot_live.db          2.0 MB   SQLite (stale copy, superseded by polybot/db/)
polybot_paper.db         13.3 MB  SQLite -- the CANONICAL, most up-to-date paper DB (era 7,089 labeled windows)
polybot_paper_0827.bak.db 12.4 MB SQLite snapshot, frozen 08-27
win_streams.jsonl.gz     35.7 MB  gzip JSONL, 8,726 windows (7,018 era)
winner_books.jsonl.gz    90.8 MB  gzip JSONL, 4,692 windows (all era)
ws1_errors60.csv         13.4 MB  CSV, 105,170 rows / 5,378 windows
vps-0821/                6.5 GB   snapshot pulled 08-21/08-27 (partial recordings + h*/r* research artifacts)
vps-0831/                3.0 GB   snapshot pulled 08-31/09-01/09-08 (window_paths.db=2.9GB is the WIDEST source; binance/, binance_agg/, r6-r27 outputs)
```

Plus `polybot/memory/recordings/` (outside `scripts/research/data/`, the
live/canonical tape+micro store, 32 days 08-07..09-07) and
`docs/audit/data/` (07-29 audit snapshot: two more SQLite DBs +
point-in-time runtime-state JSONs, not a time series).

---

## 2. `win_streams.jsonl.gz` -- the per-window raw-stream corpus

- **8,726 rows** (one JSON object per labeled window), **7,018 in the 60s
  era** (ep >= 1786665600), 1,708 pre-era (30s rule, retargeted-synthetic
  only).
- Top-level keys per record: `ep, strike, final, up, token_up, token_down,
  l, bz, cb, t`.
- Stream presence (of 8,726 windows): `l` (raw Chainlink) in **8,726**
  (100%, by construction), `t` (official twap_sixty) in **8,726** (100%),
  `bz` (Binance relay) in **6,811** (78%, stream didn't exist before
  ~08-15), `cb` (Coinbase) in only **421** (4.8%, existed only ~08-11..12).
- Stream length quantiles: `l` p50=92 reports/window (min 6, max 290), `t`
  p50=135 (min 9, max 460), `bz` p50=97 (0 when absent), `cb` p50=0.
- `up` balance: 4,342 Up / 4,384 Down. `strike`/`final` never null.
- Coverage by ET day (era) ranges 158-288 windows/day; low days are
  **08-14 (283 raw-stream rows, but see section 13 -- only 183 have a
  non-null window_labels.final_price)** and **08-17 (158)**.
- **Known defects**: `bz` empty is not "no Binance activity" before 08-15
  (the relay stream itself didn't exist yet); `cb` is a near-dead column;
  pre-era rows carry the OLD 30s-rule final/strike and must not be mixed
  with era rows without the `ws1_errors60.csv` `src=syn` retargeting.
- Produced by `scripts/research/ws1_reduce.py`.

## 3. `boundaries.json`

- **8,816 entries**, JSON object keyed by boundary epoch `B` (string) ->
  `[ts, rx, price, prev_t_ts]`.
- `B = floor(first "t"-record's payload_ts / 300) * 300` -- one entry per
  300s bucket in which the official `crypto_prices_twap_sixty` stream
  reported at least once, over the ENTIRE micro-tape span (08-07..09-07),
  not restricted to labeled/traded windows -- a **superset** of
  `win_streams`'s window set (8,816 vs 8,726).
- `rx` null in 0 entries; `prev_ts` null only for the earliest entries.
- Range: ep 1786060500 -> 1788825300 (2026-08-06 20:35 UTC -> 2026-09-07
  23:55 UTC).
- Produced by `scripts/research/ws1_reduce.py`.

## 4. `ws1_errors60.csv`

- **105,170 rows**, **5,378 distinct windows**, fixed 20-value k-grid per
  window (`1.1,2,3,4,5,6,7,8,10,12,15,20,25,29,35,40,45,50,55,58`).
- Columns: `ep,k,final,strike,src,served,up,w,veto,gap,nrep,plain,bz,cb,kl`.
  - `src`: `real` (served twap_sixty final, era) vs `syn` (retargeted
    synthetic final, pre-era 08-07..13).
  - `plain/bz/cb/kl` are **projected prices** per bridge estimator, not
    error deltas despite the filename -- error = `plain - final` etc. is
    computed downstream by `ws1_measure60.py`/`ws1_oos.py`, not stored
    here.
  - `bz` blank before ~08-15; `cb` blank outside ~08-11..12; `kl` sparse
    throughout.
  - `veto`: thirty-stream-stall proxy flag; `gap`: max raw inter-report
    gap (s) in the averaging span; `nrep`: raw report count used.
- Exact era/pre-era split, `veto`/`src` counts, gap-quantile table, and
  per-ET-day window counts are in `INVENTORY.json` and `out_misc1.json`.
- Produced by `scripts/research/ws1_measure60.py`.

## 5. `window_paths*.db` (1 Hz book/feed sampler)

Two copies exist, **not** duplicates of each other -- different windows:

| file | size | rows | distinct windows | span | notes |
|---|---|---|---|---|---|
| `vps-0821/window_paths_60s.db` | 198.6 MB | 983,434 | 2,088 (all era) | 08-14 00:00 -> 08-21 16:36 UTC | narrow first cut |
| `vps-0831/window_paths.db` | **2.9 GB** | 9,704,304 | 21,830 (5,281 era) | **2026-06-11 14:44 -> 2026-09-01 18:43 UTC** | **wide/canonical copy**, has extra `strike_trusted` column |

Shared core schema: `window_id, ts, elapsed_s, bid_up, ask_up, bid_down,
ask_down, depth3_bid_up, depth3_ask_up, depth3_bid_down, depth3_ask_down,
coinbase_price, strike, traded, binance_price, binance_cvd_10s,
binance_cvd_30s, atr, model_prob_up, chainlink_price, chainlink_age_s,
book_age_up_s, book_age_down_s, coinbase_bid, coinbase_ask,
coinbase_cvd_10s, coinbase_cvd_30s, bid_sz_up, ask_sz_up, bid_sz_down,
ask_sz_down, depth20_bid_usd, depth20_ask_usd` (+`strike_trusted` in the
wide DB only).

**NULL-by-design columns (both DBs, confirmed 100% null)**: `coinbase_price,
coinbase_bid, coinbase_ask, coinbase_cvd_10s, coinbase_cvd_30s,
binance_cvd_10s, binance_cvd_30s, atr, model_prob_up`. `binance_price` is
NOT fully null in the narrow DB but is null in a large fraction of the wide
DB's era rows (4.3M/9.7M) when the relay stream was down -- check
`INVENTORY.json`'s exact per-column null counts before assuming either way.

**Known defects**:
- Bid-size columns (`bid_sz_up/down`, `ask_sz_up/down`) are worst-level
  only before 2026-08-21 00:00 UTC (per CLAUDE.md).
- 27-29 windows per DB have <200 rows (reconnect gaps / short windows).
- Rows/window quantile in the wide DB: min 1, p10 296, p50 475, p90 478,
  max 598 -- a small tail of windows is severely under-sampled.
- `strike_trusted` is null in 8,253,281/9,704,304 rows (column added
  partway through the DB's life).
- Missing-hours-of-coverage list is in `INVENTORY.json` -> `missing_hours_utc`.

Produced by the bot's own `WindowPathRecorder` (VPS-resident, not a
`scripts/research/*.py` product) -- the two files are pulled snapshots.

## 6. SQLite DBs -- full census (15 files scanned)

All share schema `bankroll, peak_bankroll, positions, sqlite_sequence,
trade_history, [wallet_stats], window_labels`. `window_labels` columns:
`window_id, resolved_up, final_price, price_to_beat, labeled_at, token_up,
token_down`.

| db | window_labels rows (btc5m) | span end | era rows |
|---|---|---|---|
| `scripts/research/data/polybot_paper.db` (**canonical, live-updating**) | 17,218 | 09-08 13:25 | 7,089 |
| `scripts/research/data/polybot_live.db` | 4,997 | 08-15 00:45 | 111 |
| `scripts/research/data/polybot_paper_0827.bak.db` | 13,824 | 08-27 18:10 | 3,695 |
| `.../vps-0821/paper_0824.db` | 12,919 | 08-24 14:40 | 2,790 |
| `.../vps-0821/paper_0825.db` | 13,202 | 08-25 14:15 | 3,073 |
| `.../vps-0821/paper_0827.db` | 13,824 | 08-27 18:10 | 3,695 |
| `.../vps-0821/polybot_live_0821.db` | 4,997 | 08-15 00:45 | 111 |
| `.../vps-0821/polybot_paper_0821.db` | 12,101 | 08-21 16:20 | 1,972 |
| `.../vps-0831/paper_0831.db` | 14,986 | 08-31 19:05 | 4,857 |
| `.../vps-0831/paper_0901.db` | 15,222 | 09-01 14:45 | 5,093 |
| `.../vps-0831/paper_now.db` | 17,218 | 09-08 13:25 | 7,089 |
| `docs/audit/data/polybot_live_audit.db` | 4,997 | 08-15 00:45 | 111 |
| `docs/audit/data/polybot_paper_audit.db` | 13,849 | 08-27 20:15 | 3,720 |
| `polybot/db/polybot_live.db` (**live runtime copy**) | 4,997 | 08-15 00:45 | 111 |
| `polybot/db/polybot_paper.db` (**live runtime copy**) | 17,102 | 09-08 03:45 | 6,973 |

`polybot_paper.db` also carries `wallet_stats` (71,035 rows: `wallet,
n_trades, n_won, stake_usd, pnl_usd, classification, updated_at`).

**Note**: `scripts/research/data/polybot_paper.db`,
`scripts/research/data/vps-0831/paper_now.db`, and
`polybot/db/polybot_paper.db` are near-identical (all "now") -- pick ONE
(the first, 09-08 13:25, is the most recent) rather than triangulating
across all three.

## 7. `info_dataset.parquet`

- **17,269 rows**, 39 columns, 4,729 distinct windows, ~08-13 -> ~09-01
  span (`et_day` column as `"MM-DD"` strings, `k` = seconds-remaining
  grid, `ts` epoch, `label` = resolved outcome).
- 27 model-relevant feature columns (per `r17_out.txt`): `spread_up,
  spread_dn, cvd10, cvd30, cl_minus_strike_z, mid_dev, tod_sin, tod_cos,
  size_imb_up, depth_imb_up, ret_10/30/60/120/300, rv60, rv300, rv_ratio,
  z_ret60, imb_30/60/120, perp_ret_5m, basis, oi_d30m, clob_flow_60/120`.
- **`cvd10`/`cvd30` are 100% NULL (17,269/17,269)** -- never populated,
  same planned-but-dead defect as `window_paths.db`. `size_imb_up`/
  `depth_imb_up` null in 10,438/17,269 rows (~60%). `ask_sz_up`/`ask_sz_dn`
  null in 5/3 rows (negligible).
- This is the feature table behind the 09-01 "book perfectly calibrated"
  finding (0/135 feature x horizon tests significant -- `r17_out.txt`,
  `r18_out.txt`).
- Produced by `scripts/research/r16_dataset.py`.

## 8. Binance public dumps

| kind | dir | files | day range | cadence | columns | timestamp unit |
|---|---|---|---|---|---|---|
| `spot1s_*.zip` | `vps-0831/binance/` | 19 | 08-13->08-31 | 1s (86,400 bars/day) | `open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore` (no header) | **MICROSECOND** epoch (16-digit) |
| `perp1m_*.zip` | `vps-0831/binance/` | 19 | 08-13->08-31 | 1m (1,440 bars/day) | same fields, header present | **MILLISECOND** epoch (13-digit) |
| `metrics_*.zip` | `vps-0831/binance/` | 19 | 08-13->08-31 | 5m (288 rows/day) | `create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio`, header present | `create_time` = human string, not epoch |
| `agg_*.zip` (aggTrades) | `vps-0831/binance_agg/` | **5 only** (08-20, 24, 25, 27, 29 -- NOT contiguous) | selected days | tick-level (~1.6M trades/day) | `agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker,is_best_match` (no header) | **MICROSECOND** epoch |

**Defect to flag hard**: spot1s klines and aggTrades use microsecond epoch
timestamps while perp1m klines use millisecond epoch; `metrics` uses a
formatted datetime string. Mixing these without unit-normalizing silently
produces timestamps off by 1000x or 1e6x. Never published same-day, so
09-01 onward is unavailable via this route without a fresh pull (last
available day is 08-31).

Produced by `scripts/research/r15_binance_dl.py` (klines/metrics) and
`scripts/research/r20_agg_dl.py` (aggTrades).

## 9. `pm_trades` directories (Polymarket data-api `/trades`, both counterparties)

| dir | files | rows | wallets | ep span (UTC) |
|---|---|---|---|---|
| `vps-0821/h1_pm_trades` | 68 | 237,472 | 9,002 | 08-14 00:10 -> 08-21 13:50 |
| `vps-0821/r5_pm_trades` | 160 | 537,548 | 12,199 | 08-21 00:00 -> 08-27 17:15 |
| `vps-0831/r6_pm_trades` | 91 | 292,042 | 8,178 | 08-28 00:00 -> 08-31 18:05 |
| `vps-0831/r12_pm_trades` | 102 | 163,596 | 5,139 | 09-01 00:00 -> 09-01 16:40 |
| `vps-0831/r10_pm_15m` | 564 (incl. `.meta.json`) | 715,500 | 5,985 | 08-23 00:00 -> 08-31 18:45 |

Union distinct wallets across all 5 dirs: **22,787**. No default
`data/pm_trades/` directory exists.

Record fields (btc-updown-5m dirs): `proxyWallet, side, asset, size, price,
timestamp, transactionHash, outcome, name`. `r10_pm_15m` additionally has
`<ep>.meta.json` sidecars: `conditionId, outcomePrices, outcomes,
clobTokenIds, volume, closed, orderPriceMinTickSize, orderMinSize`.

**Known defect -- the data-api 3,500-row cap**: a majority of files in
every 5m dir hit exactly 3,500 rows (upstream page-offset hard cap),
meaning true trade counts for busy windows are undercounted -- e.g. h1:
66/68 files capped; r5: 142/160 capped. Full per-window capped-epoch lists
are in `out_pmtrades.json`.

Produced by `h1_wallets_pull.py`, `r5_census_pull.py`, `r6_census_pull.py`,
`r12_sept1_pull.py`, `r10_15m_pull.py` respectively.

## 10. `winner_books.jsonl.gz` / `token_map.json`

- `winner_books.jsonl.gz`: **4,692 rows**, all era, keys `ep, asks` where
  `asks` = list of `[ts, price]` snapshots of the resolved WINNER token's
  ask side (post-hoc labeled). List length quantiles: min 2, p50 8,979,
  max 60,681 entries/window. Coverage 08-13->08-30 (17 ET days). Feeds
  the `ws3_dips.py` dip-event census (58 plain max-tier dip events / 17
  days = 3.41/day -- underlies the "taker DORMANT" finding).
- `token_map.json` (`vps-0821/`): `{map: {ep: {up: token_id, down:
  token_id}}, missing: []}`, 1,972 windows, 08-14->08-21 -- older/narrower
  than `window_labels.token_up/token_down`; treat as a historical fixture.

## 11. `micro_*` / `tape_*` recordings

**Canonical store**: `polybot/memory/recordings/` -- **32 days, 08-07
through 09-07, unbroken** (every day in range has both a `micro_` and a
`tape_` file). Partial older snapshots also exist under
`scripts/research/data/vps-0821/` (15 days, 08-14..08-27) and
`scripts/research/data/vps-0831/` (3 days, 08-28..08-30) -- strict subsets
of the canonical dir, don't use as a second source of truth.

- **micro record kinds** (`k` field): `b` (CLOB BBO change, only in the
  final 90s of each window, elapsed>=210s -- `ts,token,bid,ask`), `l` (raw
  Chainlink report, always ~1 Hz -- `ts,rx,p,pub`), `t` (official
  twap_sixty stream, always -- `ts,rx,p,pub`), `t3` (retired twap_thirty
  stream, present from ~08-19 on -- `ts,rx,p`), `s` (Binance relay tick,
  `src:"bz"` -- `ts,rx,p`).
- **tape record fields**: `ts, token, price, size, side, ets, fee_bps` --
  one row per CLOB print (both counterparties, not just the bot's own
  fills).
- Sample day (2026-08-07): micro 15,173,035 lines (`b`=14,934,961,
  `l`=79,618, `t`=79,676, `other`=78,780); tape 472,002 lines, 285/289
  windows with prints, **131 windows with post-close prints present**.
  Post-close prints exist on every sampled day (roughly 130-257
  windows/day) -- real signal for post-close/redeem-lag research.
- **Known defects**: 08-18 has a duplicate plain+gz pair for both
  micro/tape (pre-gzip timing edge -- use the `.gz`); `t3` essentially
  absent before ~08-19; tape's `unknown_token_prints` (unresolvable asset
  ids) reaches tens of thousands on some days (46,931 on 08-21) -- a real
  chunk of tape is unattributable to a window without deeper
  reconstruction; `ets` field's epoch/unit is inconsistent with `ts`
  (diffing gives nonsense, order -1.78e12) -- don't trust `ts - ets`
  without first determining `ets`'s actual unit.

Produced/recorded by `polybot/recording.py` (`MicroTape`, `TapeRecorder`).

## 12. Research report/output files (30 found under `scripts/research/data/`)

One-line summary per file is in `INVENTORY.json` ->
`report_and_output_files`. Highlights not already in CLAUDE.md section 6:
`r16_out.txt`/`r17_out.txt`/`r18_out.txt` are the info-program pipeline
logs behind `info_dataset.parquet`; `r21_out.txt`/`r21_race.json` is the
latency-race check against `binance_agg`; `r24`-`r27` are the k_max sweep
sequence behind the current `k_place_max=25` config (commit `d2777840`,
research doc `4499c4ab`).

## 13. Era-complete windows (win_streams AND window_labels.final AND tape-day AND micro-day)

Definition used (day-granularity for tape/micro): window counted complete
iff (1) present in `win_streams.jsonl.gz`, (2)
`window_labels.final_price IS NOT NULL` in the canonical `polybot_paper.db`,
(3) its ET calendar day has a `tape_<day>` file in
`polybot/memory/recordings/`, (4) same day has a `micro_<day>` file there.

**Total: 6,918 era windows** (of 7,018 in win_streams / 7,089 with a
non-null label). Tape/micro coverage is unbroken for every day 08-07..09-07
(section 11), so conditions (3)/(4) are non-binding here -- the 6,918
figure is driven entirely by the win_streams AND label intersection.

By ET day (era, 08-13 partial -> 09-07; 09-08 excluded, today, no
win_streams/tape/micro coverage yet):

| day | win_streams | label-final | complete |
|---|---|---|---|
| 08-13 | 48 | 48 | 48 |
| **08-14** | 283 | **183** | **183** |
| 08-15 | 286 | 287 | 286 |
| 08-16 | 287 | 287 | 287 |
| 08-17 | 158 | 159 | 158 |
| 08-18 | 288 | 288 | 288 |
| 08-19 | 287 | 287 | 287 |
| 08-20 | 282 | 284 | 282 |
| 08-21 | 260 | 262 | 260 |
| 08-22 | 287 | 288 | 287 |
| 08-23 | 288 | 288 | 288 |
| 08-24 | 287 | 288 | 287 |
| 08-25 | 288 | 288 | 288 |
| 08-26 | 288 | 288 | 288 |
| 08-27 | 287 | 287 | 287 |
| 08-28 | 287 | 287 | 287 |
| 08-29 | 288 | 288 | 288 |
| 08-30 | 288 | 288 | 288 |
| 08-31 | 287 | 288 | 287 |
| 09-01 | 286 | 286 | 286 |
| 09-02 | 287 | 287 | 287 |
| 09-03 | 287 | 287 | 287 |
| 09-04 | 288 | 288 | 288 |
| 09-05 | 288 | 288 | 288 |
| 09-06 | 288 | 288 | 288 |
| 09-07 | 240 | 288 | 240 |

**Flag: 2026-08-14 is anomalous** -- 283 windows have a raw stream but only
**183** have a non-null `final_price` in `polybot_paper.db` (every other
day's label count is >= its win_streams count). This is the day of the
60s-rule switch (`sixty-rule-change.md` memory entry: "resolution moved to
crypto_prices_twap_sixty silently; bot traded the wrong rule 4 days") --
the ~100-window gap likely reflects windows whose labeling was disrupted by
that transition. **Not independently re-verified beyond this cross-check**
-- flagging for whoever builds a training corpus: 08-14 needs its own audit
before being trusted at face value. 08-17's mid-day dip (158/159 windows)
is a partial real-time service gap per CLAUDE.md, not a data-pull defect.

Full per-day table (including days outside 08-13..09-07) and the raw script
are in `out_era_complete.json` / `scan_era_complete.py`.

## 14. Not deeply inventoried (present but out of scope / low value)

- `scripts/research/data/vps-0821/h1_cellstats.pkl` (3.0 MB pickle,
  regenerable intermediate cache -- not opened).
- `docs/audit/data/*.json` + `polybot.log*` -- point-in-time runtime-state
  snapshots from the 07-29 audit, not a time series useful for engine
  research; schemas are small self-describing dicts, listed by name in
  `INVENTORY.json` for completeness.

---

## Files produced by this scout

- `INVENTORY.json` -- the requested structured inventory (curated, ~200 KB).
- `REPORT.md` -- this file.
- `out_winstreams.json, out_misc1.json (boundaries+ws1_errors60), out_wp.json,
  out_dbs.json, out_binance_parquet.json, out_binance_zips.json,
  out_pmtrades.json, out_recordings.json (full), out_recordings_condensed.json,
  out_winnerbooks.json, out_era_complete.json` -- raw per-dataset scan output
  backing every summary number above.
- `scan_misc1.py, scan_binance_zips.py, scan_era_complete.py,
  condense_recordings.py, assemble.py` -- this scout's new scan scripts. The
  other `scan_*.py`/`out_*.json` in this directory were inherited from the
  interrupted prior run, verified valid, and reused as-is.

All work was read-only against the repo (no tracked file edited); zero
network calls were made this session -- everything above came from files
already on disk.
