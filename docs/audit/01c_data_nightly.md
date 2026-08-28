# 01c — Data ingestion, resolution, recorders, nightly pipeline (Phase 1, track C)

Surveyed at `main` HEAD `15471a9a` (2026-08-27; the working tree had advanced past the
`4fc8f847` snapshot the audit was briefed on — the R1/R2/R3 re-fit commits landed the same
day). Ground truth is code with line numbers, then data files in `docs/audit/data/` and
`scripts/research/data/`. Docs were not used as evidence. Description only.

Citation form: `[path:line]` or `[path:a-b]`. `main.py` = `polybot/main.py`,
`cl` = `polybot/feeds/chainlink_feed.py`, `ws` = `polybot/feeds/clob_ws.py`,
`ms` = `polybot/feeds/market_scanner.py`, `rec` = `polybot/recording.py`,
`sched` = `polybot/agents/scheduler.py`, `alw` = `scripts/analyze_late_window.py`,
`atl` = `scripts/analyze_twap_lock.py`, `log` = `docs/audit/data/polybot.log`.

---

## 1. Data ingestion

### 1.1 ChainlinkFeed (`polybot/feeds/chainlink_feed.py`)

**Connection.** `RTDS_WS_URL = "wss://ws-live-data.polymarket.com"` [cl:27];
`websockets.connect(..., ping_interval=PING_INTERVAL_S=5, compression=None)` [cl:28, cl:518];
an application-level `"PING"` string is sent every `APP_PING_INTERVAL_S = 10` s [cl:29, cl:502-510].
TCP_NODELAY is set via `enable_nodelay` [cl:521; `polybot/feeds/_socket.py:11-30`].

**Subscription payload** (one `action: subscribe` message, four subscriptions) [cl:522-543]:

| topic | type | filters |
|---|---|---|
| `crypto_prices_chainlink` | `"*"` | none |
| `crypto_prices_twap_sixty` | `update` | `{"symbol":"btc/usd"}` |
| `crypto_prices_twap_thirty` | `update` | `{"symbol":"btc/usd"}` |
| `crypto_prices` | `update` | `{"symbol":"btcusdt"}` |

**Routing** (inside `_run`'s receive loop) [cl:513-624]:
- `payload.symbol == "btcusdt"` and `topic == "crypto_prices"` → `ingest_binance` (value + payload
  `timestamp`/`ts`); missing fields → `_note_drop("crypto_prices")` [cl:558-574].
- any other symbol ≠ `"btc/usd"` → silently skipped [cl:575-576].
- payload timestamp normalised by `_epoch_seconds` (ms → s when `> 1e11`; must fall in
  `(EPOCH_MIN_S=1e9, EPOCH_MAX_S=1e10)` or the report is dropped whole) [cl:59-60, cl:327-336, cl:582-588].
  The envelope `msg.timestamp` is carried as `pub_ts` (recorded, never decided on) [cl:589-601].
- `topic == "crypto_prices_twap_thirty"` → `on_twap30` hook only; touches no state [cl:604-611].
- `topic == "crypto_prices_twap_sixty"` → `ingest_sixty` [cl:612-614].
- everything else (`crypto_prices_chainlink`) → `ingest_raw`; this is the only place the
  reconnect backoff resets [cl:615-616].

**The three ingest methods** (also the replay seam used by the decision-parity test) [cl:352-356]:
- `ingest_binance(bts_s, value, now)` [cl:357-376]: rejects ticks dated more than
  `BINANCE_RING_S = 10.0` s ahead of receipt; appends to `_binance` (payload-ts, price), re-sorts
  on out-of-order arrival, trims to the newest−10 s; fires `on_spot`.
- `ingest_sixty(observed_ts, value, pub_ts, now)` [cl:378-395]: updates `_twap_value_since`
  when the value moved by ≥ `TWAP_FROZEN_EPS_USD = 0.005` [cl:57]; sets `twap_official`,
  `twap_official_ts`, `twap_official_rx`; calls `_record_boundary(observed_ts, value)`; fires
  `on_twap`.
- `ingest_raw(observed_ts, value, pub_ts, now)` [cl:397-415]: sets `_price`, `_last_update = now`,
  `staleness.observe(now)`, `_last_report_rx`, `_last_report_obs_ts`; appends `(now, price)` to
  the `_reports` ring trimmed to `RAW_RING_S = 75.0` s [cl:45]; sets `report_event` (the
  sniper's decision clock); fires `on_report`.

**Boundary capture rule** — `_record_boundary` [cl:417-435]: ignores `value <= 0`;
`boundary_ts = int(observed_ts // 300) * 300`; FIRST write wins (`if boundary_ts not in
self._boundary_prices`); meta stored as `(observed_ts, self._last_twap_ts)`; then
`_last_twap_ts = observed_ts`; both dicts pruned to the last 7200 s. Only the sixty topic
reaches this method (the raw stream never records a boundary).

**Trust rule** — `strike_reliable(window_ts)` [cl:299-316]: requires `boundary_captured`
(captured AND not the feed's start window [cl:292-297]), a meta entry, `prev_ts is not None`
(the topic's first-ever report is untrusted), and `(first_ts − window_ts) <= STRIKE_TRUST_GAP_S =
0.5` [cl:33]. Payload clock only; delivery lag does not enter. `boundary_snapshot()` returns only
trusted captures [cl:318-325]. `get_strike(window_ts)` [cl:139-149]: `None` for the start window;
the captured value (trusted or not); else a cold-start fallback of `twap_official` if its receipt is
younger than `STALE_TIMEOUT_S = 60` [cl:30]; else `None`.

**`running_avg(start, end)` clock convention** [cl:151-176]: time-weighted step function over
`_reports` on the RECEIPT clock (`rx`), seed = last report at/before `start`, or the first report
within 2.0 s after `start`; otherwise `None`. `twap_60(end_ts)` wraps it over
`[end − TWAP_HORIZON_S, end]` [cl:178-185].

**Projection guards** — `projected_final_twap(close_ts, now, bridged)` [cl:219-259]:
`t0 = close − TWAP_HORIZON_S (60.0)` [cl:39]; `None` if `t <= t0` or `t > close_ts`; `None` if
`_price <= 0`, `_last_update <= 0`, or `(t − _last_update) > SPOT_STALE_S = 3.0` [cl:37, cl:238];
coverage guard: any consecutive raw-receipt gap inside `(t0, t]` greater than `RAW_GAP_MAX_S = 10.0`,
or `t − last_rx > 10.0`, returns `None` [cl:40, cl:244-253]; `w = (t − t0)/60`;
`proj = w·running_avg(t0,t) + (1−w)·spot` with `spot = _price + spot_bridge_delta()` when
`bridged` [cl:254-259].

**Bridge constants** — `spot_bridge_delta()` [cl:187-217]: returns `0.0` if the Binance ring is
empty, if its newest payload ts ≤ the last raw report's payload ts, if no anchor at/before that ts
exists, if `last_report_obs_ts − anchor_ts > BRIDGE_ANCHOR_MAX_AGE_S = 2.0` [cl:48], or if
`|delta| > BRIDGE_MAX_DELTA_FRAC (0.01) × anchor` [cl:50] (one-time `BRIDGE OFF` warning).
Otherwise `delta = newest_px − anchor_px`.

**twap_frozen constants** — `twap_frozen(now)` [cl:261-290]: true only when the official value
has been unchanged (within `TWAP_FROZEN_EPS_USD = 0.005`) for ≥ `TWAP_FROZEN_S = 20.0` s AND the raw
reports received in that span (≥ 2 of them) travelled ≥ `TWAP_FROZEN_RAW_MOVE_USD = 2.0` [cl:52-57].

**Staleness / reconnect.** Watchdog task [cl:458-500]: warm-up phase (no report ever) polls
every 2 s and force-closes a socket connected > `2 × STALE_TIMEOUT_S` = 120 s with zero reports;
steady state polls every 10 s and closes the socket when the raw feed is idle > 60 s OR
`twap_official_rx` is older than 60 s (once the sixty topic has ever delivered), unless the
connection is younger than 60 s. Reconnect loop [cl:513-647]: backoff starts at
`RECONNECT_BASE_S = 5.0`, doubles to `RECONNECT_MAX_S = 60.0` [cl:31-32]; a `"429"` in the error
jumps it to ≥ 30 s [cl:627-628]; backoff resets only on a raw-data report, not on connect
[cl:615]. `StalenessTracker("chainlink")` observes raw reports only; snapshots (p50/p95/p99/max
inter-arrival) are flushed to `memory/state/feed_staleness.json` every 60 s by
`_flush_staleness_loop` [main.py:2622-2633; `polybot/feeds/_staleness.py:431-468`].
Unparseable reports are counted per topic and logged once per `DROP_LOG_EVERY_S = 300` [cl:61, cl:344-350].

**Exposed to the decision path:** `price`, `age_seconds`, `last_report_rx`, `report_event`,
`twap_official/_ts/_rx`, `get_strike`, `boundary_captured`, `strike_reliable`, `boundary_snapshot`,
`running_avg`, `twap_60`, `projected_final_twap`, `spot_bridge_delta`, `twap_frozen`, `staleness`,
`drops`; hooks `on_report`, `on_twap`, `on_twap30`, `on_spot` are wired to `MicroTape`
[main.py:2665-2668].

### 1.2 ClobWebSocket (`polybot/feeds/clob_ws.py`)

`WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"` [ws:23] (overridable from
`market.clob_ws_url`, same value [settings.yaml:209; main.py:2590-2591]). Constants:
`HEARTBEAT_INTERVAL = 10`, `HEARTBEAT_TIMEOUT = 25`, `RECONNECT_BASE = 1`, `RECONNECT_MAX = 30`,
`TRADE_BUFFER_MAXLEN = 500`, `FRESHNESS_S = 10.0`, `DROP_LOG_EVERY_S = 300.0` [ws:24-30].

**Connect / subscribe.** `_run_forever` waits until at least one token is subscribed [ws:169-172];
`websockets.connect(url, ping_interval=None, compression=None)` [ws:177]; on every (re)connect all
per-token state is cleared (`books`, `best_bid_ask`, `last_trade`, `trade_buffer`) [ws:159-165,
ws:183]; initial payload `{"assets_ids": [...], "type": "market", "initial_dump": True,
"level": 2, "custom_feature_enabled": True}` [ws:186-192]; incremental
`{"operation": "subscribe", "assets_ids": new_ids, "level": 2, "custom_feature_enabled": True}`
[ws:97-102]; `unsubscribe` also drops the token's local state [ws:108-124]. Heartbeat: send
`"PING"` every 10 s; if no `"PONG"` for > 25 s, close the socket (reconnect) [ws:220-232]. On any
exception: `connected = False`, `staleness.mark_disconnected()`, **`last_print_gap_ts =
time.time()`** [ws:211], warning `CLOB feed dropped — prints are lost until it reconnects`,
sleep `backoff`, double to 30 s [ws:204-215].

**Message handling.** `"PONG"` refreshes the pong clock [ws:244-246]; JSON via orjson when
available [`_json.py`]; `staleness.observe()` per message [ws:252]; lists are fanned out
[ws:253-257]. `_dispatch` by `event_type` [ws:261-277]:
- `book` → `books[asset_id] = {bids, asks, hash, timestamp, market, ts=now}`; `book_updated.set()`
  [ws:287-301].
- `price_change` → per entry `best_bid_ask[asset_id] = {best_bid, best_ask, price, size, side, ts}`;
  `on_bba` hook; `book_updated.set()` [ws:303-326].
- `best_bid_ask` → `{best_bid, best_ask, spread, ts}`; `book_updated.set()`; `on_bba` [ws:328-347].
- `last_trade_price` → trade `{price, size, side, timestamp=time.time(), exchange_ts, fee_rate_bps}`;
  `last_trade[asset_id]`; `on_trade` hook (tape + maker matcher); `trade_buffer` deque(500);
  per-token `asyncio.Event` [ws:349-377].
- `market_resolved` → `market_resolved.set()` [ws:271-272]; `tick_size_change` → debug log
  [ws:273-275]; anything else → `_note_drop("event:<type>")` [ws:276-277]. The log shows this
  counter growing on `event:new_market` (5,603 → 28,077 in one day) [log:2, log:815].

**feed_delay stamping.** `_stamp_feed_delay` sets `feed_delay_ms = now·1000 − msg.timestamp` on
`book`, `price_change`, `best_bid_ask` messages (not trades) [ws:279-285, ws:292, ws:305, ws:333].

**Exposed:** `books`, `best_bid_ask`, `last_trade`, `trade_buffer`, `get_book`,
`get_trade_history`, `trades_since`, `book_fresh(token, 10 s)`, `both_books_fresh`,
`trade_event_for`, `book_updated`, `market_resolved`, `feed_delay_ms`, `last_print_gap_ts`, `drops`,
`staleness`, and the `on_trade`/`on_bba` hooks. `main` multiplexes `on_trade` into
`TapeRecorder.on_trade` and `MakerBidManager.on_print` [main.py:2661-2665].

### 1.3 BTCMarketScanner (`polybot/feeds/market_scanner.py`)

`GAMMA_API = "https://gamma-api.polymarket.com"`, `CLOB_API = "https://clob.polymarket.com"`,
`WINDOW_SECONDS = 300` [ms:40-42]; slug `btc-updown-5m-{window_ts}` [ms:71-72].

**Gamma endpoints.** `gamma_events_by_slug` [ms:74-99]: `GET /events?slug=` first; a non-2xx
other than 429/5xx latches `_gamma_events_gone` and every later call uses
`GET /events/slug/{slug}` (404 → `[]`); 429/5xx raise.

**`parse_contract(event)`** [ms:101-180] reads `markets[0]`: `outcomes`, `outcomePrices`,
`clobTokenIds` (JSON strings decoded); maps `"up"`/`"down"` outcomes to `price_up/price_down` and
`token_id_up/token_id_down`; `endDate` → `seconds_remaining`; `conditionId`, `negRisk`; and
`eventMetadata.priceToBeat` / `finalPrice` → `event_metadata = {"price_to_beat", "final_price"}`
(only when `priceToBeat` is present; `final_price` may be `None`) [ms:154-164]. Returns also
`slug`, `question` (event `title`), `end_date`, `closed`, `active`. A market with fewer
prices/tokens than outcomes is logged at WARNING and still returned [ms:121-130].

**CLOB REST.** `fetch_clob_book` → `GET /book?token_id=`, 2 s cache, `{}` on failure [ms:189-208,
ms:59]. `fetch_fee_rate` returns the constant `DEFAULT_FEE_RATE = 0.07` with no HTTP call
[ms:210-217; `polybot/execution/base.py:140`]. `fetch_tick_size` → `GET /tick-size` field
`minimum_tick_size`, 1 h cache, `"0.01"` on error [ms:219-237, ms:61]. `snap_to_tick(price, tick)`
rounds DOWN (`int(price/tick)*tick`) and clamps to `[tick, 1 − tick]` [ms:240-249].
`clob_best_ask(book)` → `(asks[0].price, Σ ask sizes)` or `(0.0, 0.0)` [ms:252-259].
`fetch_market_price` → `GET /price?token_id&side` [ms:263-281]; `get_spread` → `GET /spread`
[ms:285-295].

**Discovery.** `find_active_contract` is stale-while-revalidate: a cached contract whose
`end_date` is in the future is served immediately (with `seconds_remaining` recomputed) and a
background refresh is spawned once `cache_seconds` (5) has elapsed [ms:297-338; settings
`scan_cache_seconds: 5`]. `_fetch_active_contract` tries the current and next window slug, requires
`event.active` and `seconds_remaining > min_time_remaining` (0 in settings), and pre-warms the tick
size for both tokens on a new `condition_id` [ms:340-387].

---

## 2. Resolution detection under the current rule

### 2.1 Position resolution (`polybot/main.py`)

**`_resolved_exit_price(live, side, market_id)`** [main.py:1673-1716] — oracle-first order:
1. `live.event_metadata.final_price` and `.price_to_beat` both present → `up_won = final_price >=
   strike` (tie → Up) [main.py:1692]; if the CLOB `price_up` is at an extreme (≥0.99 / ≤0.01) and
   disagrees, a `RESOLVE disagreement` WARNING is logged but the oracle decides [main.py:1697-1704];
   returns `(1.0|0.0, "UP|DOWN <window> | $strike → $final")`.
2. Else if `live.closed` and `0.98 <= price_up + price_down <= 1.02` and `price_up >= 0.99 or
   <= 0.01` → the book decides, `oracle_log=None` [main.py:1707-1714].
3. Else `(None, None)` — caller keeps waiting.
Binance is never consulted.

**`_resolve_expired_position(...)`** [main.py:1719-1820] — called when the contract's
`seconds_remaining <= 0`, after `db.mark_pending_resolution` [main.py:2311-2323]:
- Pulls our own tape strike/final via `chainlink_feed.get_strike(w)` / `get_strike(w+300)` but
  ONLY when `strike_reliable` for each [main.py:1733-1741].
- If `exit_price is None`: logs `WAITING FOR RESOLUTION` once per market; prints `TAPE VERDICT`
  once when both trusted captures exist [main.py:1745-1758]; returns unresolved.
- Once resolved: `RESOLVED …` logged once; if `|tape_final − Gamma final| > 0.005` →
  `RESOLUTION DRIFT` WARNING (once per market) [main.py:1768-1774]; `trader.resolve_position`;
  `pending` → retry next tick; success → banner, `send_trade_closed`, circuit-breaker update,
  `_record_outcome` (outcome JSON), `prev_resolution_margin` persisted [main.py:1777-1819].

**`_manage_orphaned_position(...)`** [main.py:1823-1956] — when the contract can no longer be
found via Gamma [main.py:2301-2309]: age < 600 s → skip [main.py:1838]; direct Gamma fetch →
`_resolved_exit_price` [main.py:1844-1858]; else if age > 1800 s: both `window_ts` and
`window_ts+300` captures must be `strike_reliable`, otherwise it logs
`ORPHAN … Waiting for resolution (boundary captures incomplete)` and keeps waiting
[main.py:1859-1882]; with both, `up_won = final >= strike`, `RESOLVE ORPHAN` WARNING and a Discord
`send_error` [main.py:1883-1900]; else age > 3600 s → ERROR log + `send_trade_closed(reason
"orphaned — awaiting resolution")` [main.py:1905-1911]. The fallback never uses `get_strike`'s
live value for both ends (that would fabricate a tie).

**Strike used by the legs** — `_compute_strike` [main.py:1395-1457]: Gamma's `price_to_beat`
wins whenever served (sticky per window, `_strike_trusted[ts] = True`, logs `Strike Corrected` if it
differs from our capture by > 0.005) [main.py:1414-1427]; otherwise our capture, with
`_strike_trusted[ts] = chainlink_feed.strike_reliable(ts)` [main.py:1428-1443]. (The docstring at
[main.py:1401] still says "30s-TWAP stream".)

### 2.2 Labels (`polybot/recording.py`, WindowPathRecorder)

- Window discovery: each new `window_ts` spawns `_discover` → Gamma by slug → tokens → subscribe
  both tokens; the previous window is queued in `_pending_label[market_id] = window_ts + 300`
  [rec:229-258, rec:466-476].
- Cadence: `run()` spawns `_label_pass` at most every `_LABEL_RETRY_S = 60.0` s while anything is
  pending [rec:34, rec:484-486]. `_label_pass` [rec:294-323]: drops a window after
  `_LABEL_GIVE_UP_S = 2400.0` s (40 min) past its end [rec:35, rec:297-299]; does not ask before
  `end + 30 s` [rec:300]; fetches the contract; requires BOTH `final_price` and `price_to_beat`;
  writes `INSERT OR REPLACE INTO window_labels (window_id, resolved_up = 1 if fp >= ptb else 0,
  final_price, price_to_beat, labeled_at = now, token_up, token_down)` [rec:315-319]; then calls
  `_check_resolution_source(market_id, fp, ptb)` [rec:323].
- Boot recovery: `_recover_orphan_labels` re-seeds unlabeled `window_paths` windows whose end is
  between 30 s and 2400 s ago [rec:260-292].
- Schema: `window_labels(window_id PK, resolved_up, final_price, price_to_beat, labeled_at)` +
  appended `token_up`, `token_down` [rec:124-140]; lives in the per-mode DB (`polybot_paper.db` in
  paper mode), so labels accrue in the ACTIVE mode's DB only.

### 2.3 Per-window SOURCE hard gate (`rec._check_resolution_source`) [rec:325-360]

Runs once per labeled window; disabled after the first trip in a process
(`_source_mismatch_fired`) [rec:333-334]. Compares `("strike", ptb, ep)` and
`("final", fp, ep + 300)` against `chainlink_feed.boundary_snapshot()` (TRUSTED captures only)
[rec:337, rec:345-348]. Threshold: `abs(served − cap) > 0.005` [rec:350] → sets the latch and calls
`on_source_mismatch(window_id, kind, served, cap)`. If neither value had a trusted capture:
`source_unchecked += 1` and `SOURCE CHECK SKIPPED … no trusted boundary capture` at ERROR
[rec:356-360]; a parse failure also increments `source_unchecked` [rec:338-343].

The handler `main._on_source_mismatch` [main.py:2679-2698] (the one wired exception to
"watches never flip config"): `config["late_window"]["trading_enabled"] = False` in-process
[main.py:2685] (settings.yaml on disk is untouched), `logger.critical("RESOLUTION SOURCE MISMATCH
… trading HALTED in-process …")` [main.py:2686-2690], and pages Discord via
`alert_manager.send_health("🚨 TRADING HALTED — resolution source mismatch …")` [main.py:2691-2697].

### 2.4 Nightly mechanism / chain reads (`scripts/analyze_late_window.py`)

- **`resolution_snapshot_read(db_path, hours=26.0)`** [alw:333-399]. Input: `window_labels`
  rows with `labeled_at >= now − (hours+1)·3600` from the given DB (`LIVE_DB` default; the
  nightly passes `PAPER_DB` in paper mode) [alw:344-356]. Skips windows with `ts <
  TWAP_SWITCH_TS = 1786060800` (2026-08-07 00:00Z) [alw:30, alw:370-371]. Chain invariant: for
  each window with a labeled successor, `abs(final_price − next.price_to_beat) < 0.005` counts as
  matched; up to 3 mismatches are returned with `worst` [alw:374-385]. Regime: `|fp − ptb|` over
  labels inside the cutoff; when ≥ 24 gaps, `gap_p25/p50/p75` and `photo_finish_pct` (share
  `< 1.0`) [alw:372-373, alw:390-397]. Output: `{checked, matched, worst, mismatches, regime}`.
  Alert-only. (Docstring at [alw:337-339] still describes the 30s stream; its `TWAP_SWITCH_TS`
  is the 08-07 date while `atl.TWAP_SWITCH_TS = 1786665600` is 08-14 [atl:52].)
- **`mechanism_read(boundaries, db_path, unchecked, t3_records)`** [alw:198-251]. Input:
  `boundaries = chainlink_feed.boundary_snapshot()` (trusted captures, last ~2 h) plus
  `window_labels` rows with `labeled_at >= min(boundaries) − 3600` [alw:222-225]. For each label,
  compares `price_to_beat` to `boundaries[ts]` and `final_price` to `boundaries[ts+300]`;
  `d < 0.005` → exact, else tracks `worst`/`worst_ts` [alw:233-247]. Returns `None` when no
  boundaries or nothing compared; else `{checked, exact, worst, worst_ts, unchecked, t3_records}`.
- **`live_health_read`** — see §4.3 (it is the kill-rule read, not a mechanism read).
- The offline harness has its own bit-exact check `atl.mechanism_check` (strike/final/chain
  with `< 1e-9`) [atl:307-330], used by `run_replay` [atl:333-365].

---

## 3. Recorders (`polybot/recording.py`)

### 3.1 WindowPathRecorder [rec:56-500]

- Cadence: `run()` samples every 1.0 s, and every 0.2 s while `255 <= elapsed <= 300`
  [rec:490-494]; rows buffer in memory and flush every `_FLUSH_EVERY_S = 10.0` s [rec:32,
  rec:480-483, rec:446-464].
- Store: `polybot/db/window_paths.db` (`PATHS_DB`, gitignored via `.gitignore` `polybot/db/window_paths.db*`),
  WAL, `busy_timeout 15000` [rec:30, rec:100-106]. Labels stay in the per-mode DB [rec:124-140].
- Columns: base `window_id, ts, elapsed_s, bid_up, ask_up, bid_down, ask_down, depth3_bid_up,
  depth3_ask_up, depth3_bid_down, depth3_ask_down, coinbase_price, strike, traded` [rec:107-117] +
  appended `binance_price, binance_cvd_10s, binance_cvd_30s, atr, model_prob_up, chainlink_price,
  chainlink_age_s, book_age_up_s, book_age_down_s, coinbase_bid, coinbase_ask, coinbase_cvd_10s,
  coinbase_cvd_30s, bid_sz_up, ask_sz_up, bid_sz_down, ask_sz_down, depth20_bid_usd,
  depth20_ask_usd, strike_trusted` [rec:150-175]. Per `_sample` [rec:362-444]: coinbase_*,
  binance_*, depth20_*, atr, model_prob_up record `NULL` by design (feeds/model deleted); depth3 is
  the top-3 USD notional nearest the touch (sorted, because the WS delivers both sides ascending)
  [rec:38-53]; `strike` comes from `get_strike` and `strike_trusted` records `1/0` via
  `strike_reliable` [rec:384-389]; `traded` from `mark_traded` [rec:206-210; main.py:1357].
- Retention: `cleanup_job(db, retention_days=90)` deletes `window_paths` rows with
  `ts < now − 90d` [rec:792-805].

### 3.2 TapeRecorder [rec:504-555]

Wired to `clob_ws.on_trade` (via the mux) [main.py:2661-2665]. Each print →
`{"ts" (local receipt, 3 dp), "token", "price", "size", "side", "ets" (exchange ts), "fee_bps"}`
[rec:520-530]; flush at `_TAPE_FLUSH_ROWS = 200` rows or 10 s on a single-thread executor
[rec:33, rec:531-546]; appends to `memory/recordings/tape_YYYY-MM-DD.jsonl` (UTC day at write time)
[rec:547-555]. Sample from the corpus: `{"ts": 1787702401.216, "token": "5897…", "price": "0.43",
"size": "24", "side": "BUY", "ets": "1787702401069", "fee_bps": "0"}` (tape_2026-08-26).

### 3.3 MicroTape [rec:557-696]

Record kinds (each JSON line, same flush rule, file `micro_YYYY-MM-DD.jsonl` [rec:688-696]):

| k | source hook | fields | gating |
|---|---|---|---|
| `b` | `clob_ws.on_bba` | `ts, token, bid, ask` | only when `_late(now)`: `(ts % 300) >= _LATE_ELAPSED_S = 210.0` [rec:573, rec:585-586, rec:591-592] |
| `l` | `chainlink_feed.on_report` (raw) | `ts (payload), rx, p, pub` | always [rec:656-672] |
| `t` | `on_twap` (sixty) | `ts, rx, p, pub` | always [rec:602-622] |
| `t3` | `on_twap30` (thirty, retired) | `ts, rx, p` | always; increments `t3_records` [rec:624-639] |
| `s` | `on_spot` (Binance relay) | `src:"bz", ts, rx, p` | always [rec:641-654] |

Measured on `micro_2026-08-26.jsonl.gz`: 10,860,043 lines = b 10,540,293 · s 83,017 ·
l 78,896 · t 78,908 · t3 78,929. The thirty stream IS being served now (the 08-27 nightly ping
reported `30s A/B tape 78879 records` [log:812]).

### 3.4 Nightly maintenance jobs

- **`compress_recordings_job(level=3, budget_s=540.0)`** [rec:698-761]: gzips every
  `*.jsonl` in `memory/recordings/` except files whose name contains today's UTC date; deadline
  checked before each file AND inside the 1 MiB copy loop; an expired partial is deleted
  (`.gz.part`), the raw kept, and a WARNING names each file left; completed files are atomically
  renamed and the raw unlinked. Returns `{compressed, mb_saved[, left_raw]}`.
  08-27 run: `{'compressed': 2, 'mb_saved': 1663}` in 36 s [log:782].
- **`recordings_cleanup_job(retention_days=30, micro_retention_days=30)`** [rec:763-790]: unlinks
  `*.jsonl`/`*.jsonl.gz` by mtime older than 30 days (both tape and micro).
- **`cleanup_job(db, retention_days=90)`** — window_paths, above.

---

## 4. The nightly pipeline

### 4.1 Scheduler (`polybot/agents/scheduler.py`)

`NightlyScheduler` [sched:19]; `run_daily_loop` polls every 60 s and fires when ET hour ==
`daily_pipeline_hour` and `daily_pipeline_minute <= minute < minute + 5` [sched:83-106]; config
`agents.daily_pipeline_hour: 23`, `daily_pipeline_minute: 45` [settings.yaml:198-199], i.e. 23:45 ET
(03:45Z in the log). After the pipeline, when `_auto_shutdown` (set from `--auto-restart`
[main.py:2549]) it sets `_shutdown_requested` [sched:101-104]; the trading loop exits, `main()`
tears down, the process exits 0 and `run_polybot.sh` commits (§4.7).

`run_daily_pipeline` [sched:41-76]: (a) `outcome_reviewer.rollup_old_outcomes` and
`ghost_tracker.rollup_old_ghosts` under `_safe_rollup` (exceptions logged, return 0); (b) each
registered job in registration order under `asyncio.wait_for(job(), timeout=JOB_BUDGET_S = 600.0)`
[sched:16, sched:62]. `run_outcome_loop` is an empty heartbeat [sched:78-81]. The module docstring:
"Tunes NOTHING" [sched:1-4]. `--run-pipeline` runs the same `run_daily_pipeline` once with a
transient Discord connection [main.py:2375-2428].

### 4.2 Registered jobs, in order [main.py:2706-2964]

| # | name | reads | writes | returns |
|---|---|---|---|---|
| 1 | `compress_recordings` [main.py:2707] | `memory/recordings/*.jsonl` | `.jsonl.gz` (deletes raw) | `{compressed, mb_saved}` |
| 2 | `window_paths_retention` [main.py:2708] | `window_paths.db` | deletes rows > 90 d | `{rows_deleted}` |
| 3 | `price_sum_retention` [main.py:2710-2714] | `memory/state/price_sum_outliers.jsonl` | rewrites file, drops lines with `ts` > 90 d (`trim_jsonl_by_age`, `paths.py:58-86`) | `{price_sum_lines_dropped}` |
| 4 | `maker_ladder` [main.py:2716-2727] | labels (both DBs) + trailing 1 day of micro-tape via `atl.ladder_recalibrate` | nothing | `{n_locked, n_dips, applied: False[, dip_q][, partial]}` |
| 5 | `recordings_retention` [main.py:2729] | `memory/recordings/` | unlinks files > 30 d | `{recordings_deleted}` |
| 6 | `sniper_health` [main.py:2731-2964] | see §4.3 | log line + Discord | dict (`health`, `kill_rule_tripped`, `live`, `sim`, `legs`, `mechanism`) |

Observed 08-27 (UTC): start 03:45:52; `Rolled up: 9 outcomes, 0 ghosts`; jobs 1-3 done by
03:46:31; `maker_ladder` finished 03:55:31 (`n_locked 249, n_dips 28, applied False,
dip_q [0.5, 0.5, 0.5, 0.51, 0.51]`); `recordings_retention` 03:55:32; `sniper_health` finished
04:03:54 (`SIM read skipped (TimeoutError)` at 03:59:33; `queue depth read skipped` at 04:03:30);
`Nightly jobs complete` 04:03:54 — 18 minutes total [log:780-818]. During jobs 4 and 6 the feeds
starved: `CLOB WS no PONG for 107s`, `ChainlinkFeed idle for 102s`, then `no PONG for 172s`
[log:791-804].

### 4.3 `sniper_health` in full [main.py:2731-2964]

- Returns `{"skipped": "sniper disabled"}` when `late_window.trading_enabled` is false
  [main.py:2738-2739]. Loads `analyze_late_window.py` and `analyze_twap_lock.py` with importlib
  [main.py:2740-2744, 2755-2759].
- **SIM**: `atl.health_read(None, sniper_min_edge)` in a thread under `wait_for(timeout=240.0)`
  [main.py:2762-2763]; on any exception logs `sniper health SIM read skipped` and continues with
  `sim=None`. `health_read` replays the trailing 1 day (`days=1`) of micro-tape through the lock-dip
  taker model (`run_replay`, `DEFAULT_RTT 0.45`) and returns `n_fills/net_per_sh/...` [atl:439-452].
- **Realized ledger**: `alw.live_health_read(None, validation_epoch)` in live mode, or
  `live_health_read(PAPER_DB, validation_epoch)` in paper mode [main.py:2773-2782]. Current
  `validation_epoch: "2026-08-27T19:28:00+00:00"` [settings.yaml:79].
- **Chain watch**: `resolution_snapshot_read(_real_db)` [main.py:2790]. **Source watch**:
  `mechanism_read(chainlink_feed.boundary_snapshot(), _real_db, window_recorder.source_unchecked,
  micro_tape.t3_records)` [main.py:2799-2801]. **Queue depth**: `queue_depth_read(7.0, _real_db)`
  under `wait_for(timeout=120.0)` [main.py:2808-2809] — streams 7 days of tape files for prints at
  the five rung levels `[0.80, 0.65, 0.50, 0.35, 0.20]`, sums at-level volume and records it when a
  strictly-lower print follows within 60 s; needs ≥ 50 sweeps; returns `{n, med, p75, days}`
  [alw:254-330]. **Latency watches**: `_latency_watch(LATENCY_STATS_PATH)` (needs
  `post.n >= 10` and age ≤ 7 d) and `_gtc_watch` (needs `gtc.place.n >= 10`, age ≤ 14 d, adds a
  KS statistic against `PaperTrader._GTC_LATENCY_QUANTILES`) [main.py:461-525, 2814-2815].
- **Verdict**: `kt = live["kill_rule_tripped"] if (live and live["n_fills"] > 0) else None`
  [main.py:2825]; status `⏳ STILL ACCRUING` / `⚠️ KILL RULE TRIPPED` / `✅ HEALTHY` [main.py:2826-2827].
  The SIM never feeds the verdict.
- **Message**: money line, per-leg line, "Shut-off line" (displays `last-4-days … must stay ≥
  +2.0¢` and `8-day consistency … must stay ≥ 2.0` — display text only; these thresholds are not
  used in `kill_rule_tripped`) [main.py:2838-2846], SIM context, regime line (HOSTILE if
  `gap_p50 < 6` or `photo_finish_pct > 15`) [main.py:2868-2882], chain watch line, source watch line
  (includes `unchecked` and the t3 count) [main.py:2884-2919], ops watch line (POST p50 vs 436 ms
  ±25 %; queue p75 vs 135 sh ±25 %; GTC p50 vs 56 ms ±25 % or KS > 0.30; owned-latency breaches
  over `_OWNED_LAT_BUDGET_MS = 25.0`) [main.py:528-576], then the action line: on `kt` true it
  prints `→ ACTION: … Set trading_enabled: false in settings.yaml and restart.`
  [main.py:2921-2926].
- **Emission**: `logger.info("NIGHTLY PING:\n%s", msg)` first [main.py:2943], then
  `alert_manager.send_health(msg)` → `#polybot-daily` with 3 attempts, 20 s apart, ERROR
  `NIGHTLY PING LOST` on total failure [`polybot/discord_bot/alerts.py:118-136`;
  settings `daily_channel_name: polybot-daily`].
- **On trip it does NOTHING to config.** The job's docstring says "Alert-only — never flips
  config" [main.py:2732-2737] and no code path in it assigns to `config` or writes settings. The
  only in-process config flip in the codebase is the per-window SOURCE gate (§2.3).

**Kill rule as computed** — `alw.live_health_read(db_path, since_iso)` [alw:64-194]:
```
SELECT t.pnl, t.exit_timestamp, p.shares_held, p.indicator_snapshot
FROM trade_history t JOIN positions p ON COALESCE(t.position_id, t.id) = p.id
WHERE t.exit_timestamp IS NOT NULL AND p.shares_held > 0 [AND t.exit_timestamp >= since_iso]
```
[alw:91-105]; per fill `nps = pnl / shares` (pnl already net of fees) [alw:118]; leg =
`indicator_snapshot.trade_context.signal_leg` or `"unstamped"` [alw:120-124]; ET-day buckets.
```
breach_losses = number of lock_dip fills with pnl <= 0                        [alw:158]
trailing4_days = the 4 calendar ET days ending on the LAST fill day           [alw:146-149]
n_cal_days     = calendar span first fill day .. last fill day                [alw:150]
per leg: skip if n_cal_days < 4 or fills in trailing4 < 5;                    [alw:166-169]
         trip if mean(daily $ over trailing4_days, zero-filled) < 0.0         [alw:170-171]
tripped = True  if breach_losses or tripped_legs                              [alw:172-173]
        = None  elif n_cal_days < 4 or fills_trailing4 < 5                    [alw:174-175]
        = False otherwise                                                     [alw:176-177]
```
Returned as `kill_rule_tripped` alongside `n_fills, n_days, win_rate, mean_net_day, t_day, p10,
net_per_sh, net_sum, days_pos, series, day_detail, trailing4_mean, trailing8_t, usd_per_day,
usd_p10, trailing4_usd, breach_losses, trailing4_days, tripped_legs, legs` [alw:183-194]. The
docstring states "Alert-only — the caller never flips config" [alw:84].

08-27 ping (paper mode, epoch then 08-18): `+1.6¢/share over 15 fills, 3 days … STILL
ACCRUING`; chain `311/311`; source `45/45 bit-exact · 3 window(s) could not be checked · 30s A/B
tape 78879 records`; ops `POST p50 unknown — only 2 order samples · GTC RTT unmeasured — only 0 GTC
samples` [log:805-817]. The audit copy of `latency_stats.json` confirms `post.n = 2`,
`last_updated 2026-08-13` and no `gtc` section.

### 4.4 `maker_ladder` job — report only

`_maker_ladder_job` calls `atl.ladder_recalibrate()` with defaults `days=1, write=False,
budget_s=480.0` [main.py:2716-2726; atl:372]. The function's docstring: "REPORT-ONLY … It never
writes the ladder … `write` is retained for call-shape parity and ignored" [atl:373-382]; every
return path carries `"applied": False` [atl:387, 429-430, 434-436]; no file write exists in the
function. It streams the trailing day's micro-tape with a `deadline` (`load_windows(...,
deadline=now+480)`) and returns `partial: True` when cut short [atl:389-391, atl:139-197].

**Does anything write `maker_ladder.json`?** No. `MAKER_LADDER_PATH` is defined at
[`polybot/paths.py:56`] and READ by `MakerBidManager.ladder()` as an operator override (prices
clamped to `[LADDER_PRICE_MIN, LADDER_PRICE_MAX]`, fractions/headroom frozen, applied only if the
rung count equals the seed) [`polybot/execution/maker_bid.py:114-135`]. A repo-wide grep for
`maker_ladder.json` / `MAKER_LADDER_PATH` finds only `paths.py`, `maker_bid.py` (reader +
docstring) and the `main.py` job/comment lines; no writer. The file is absent from both state
snapshots (`docs/audit/data/`, `scripts/research/data/vps-0821/state/`). The running ladder is
therefore `settings.yaml maker.maker_ladder` [settings.yaml:161-166], validated by
`loader.py:91-97`.

### 4.5 Scar / adverse / SPRT / counterfactual state files — orphaned

| file | referenced by code? | content in audit snapshot |
|---|---|---|
| `memory/state/scar_gates.json` | **No.** No `.py/.sh/.yaml` mentions `scar_gates`; only a stale `polybot/core/__pycache__/scar_scan.cpython-314.pyc` exists (no `scar_scan.py`; `polybot/core/` holds only `signal_engine.py`). | version 1, one gate `atr_regime=LO` status `shadow`, discovered 2026-07-27, restarted 2026-08-02 |
| `memory/state/sprt_burst.json` | **No** references. | `frozen_sigma 9.7085`, `mu1 6.0`, frozen 2026-07-25 |
| `memory/state/cf_watchlist.json` | **No** references. | `{"saved_at": 1786106165.99 (2026-08-03), "watchlist": []}` |
| `memory/state/adverse_state.json` | Path constant `ADVERSE_STATE_PATH` exists [`paths.py:35`] and is listed in `tests/test_paths.py:8`; **no reader or writer** in `polybot/` or `scripts/`. | schema 2, `saved_at 1786112404.7` (2026-08-03), a list of fills with mid-price drift stamps |

All four are inert artifacts of deleted subsystems; they persist because `run_polybot.sh` stages
the whole `polybot/memory` tree nightly (§4.7). Live state files still written at runtime:
`feed_staleness.json` (every 60 s, main.py:2622-2633), `fill_stats.json` (live_trader.py:254) /
`fill_stats_paper.json` (paper_trader.py:261), `latency_stats.json` (live_trader.py:180-250;
`smoke_gtc_test.py` also writes the `gtc` section), `orphan_positions.json` (live_trader.py:1435),
`day_open_bankroll.json` (main.py:724-745), `prev_resolution_margin.json` (main.py:339-370),
`gate_stats.json` / `gate_stats_current.json` (main.py:599-690, paths.py:89-119),
`price_sum_outliers.jsonl` (main.py:284-300; gitignored).

### 4.6 Rollups, "retraining", and the promotion gate

- **`OutcomeReviewer.rollup_old_outcomes`** [`agents/outcome_reviewer.py:103-151`]: every
  per-trade `memory/outcomes/<pid>_<market>_<ts>.json` whose ET exit date is before today is
  merged (dedup key `(position_id, market_id)`) into `rollup_YYYY-MM-DD.json` via tmp+replace and
  the individual files are unlinked. Per-trade files are written by `record_outcome` on every
  resolution [outcome_reviewer.py:38-79; main.py:861-893].
- **`GhostTracker.rollup_old_ghosts`** [`agents/ghost_tracker.py:165-218`]: same for resolved
  ghosts in `memory/ghost_outcomes/` (ghosts are gate-vetoed signals resolved against Gamma
  metadata within 20 min, polled every `_CF_CHECK_INTERVAL = 30.0` s [ghost_tracker.py:74-146;
  main.py:774, 2327-2335]). The local `ghost_outcomes/` directory does not exist (0 files).
- **Consumers of the rollups at runtime: none.** `load_all_outcomes` [outcome_reviewer.py:81]
  and `load_all` [ghost_tracker.py:148] have no callers anywhere in `polybot/` (grep). The rollups
  are git-archived records only.
- **What retrains: nothing.** The scheduler "tunes NOTHING" [sched:1-4]; no nightly job writes
  any file the decision path reads. Margin tables are module constants
  (`signal_engine.TWAP_MARGIN_P995/_MAX`), the ladder comes from `settings.yaml`, `need`/k bounds
  from `settings.yaml`; nightly outputs are log lines, one Discord message, returned dicts, and
  destructive retention.
- **What gates promotion: the operator.** No code computes a pass/fail on the paper bar.
  `live_health_read` returns raw metrics with no threshold comparison [alw:183-194];
  `scripts/sniper_shadow_status.py` prints the same read and a bar in prose ("≥6 clean days, ≥40
  fills, equal-weight net/sh ≥ +0.02, t_day≥2, p10>0, AND zero lock-breaches")
  [sniper_shadow_status.py:11-14, 93-95]; the nightly "Shut-off line" text uses +2.0¢ / t ≥ 2.0
  [main.py:2838-2846]; `settings.yaml` comments cite "≥20 filled windows, EW ≥ +5c/sh"
  [settings.yaml:5-6, 137-138]. The three code-side texts disagree with each other; none is
  enforced. The live switch is the manual edit of `mode`, `late_window.trading_enabled`, and
  `validation_epoch` in `settings.yaml` [settings.yaml:1, 68, 79].

### 4.7 Daily commit / push (`scripts/run_polybot.sh`)

Each cycle: `git pull --rebase --autostash origin main` (abort on failure, continue on existing
code) [run_polybot.sh:16-24]; `pkill -f 'polybot\.main'`; `python -m polybot.main --mode "$mode"
--auto-restart` [31-35]. On exit code 0 only: `git add polybot/config/settings.yaml polybot/memory
polybot/db`; commit `auto: daily pipeline update <date>`; `git push origin main` with one 10 s
retry [41-49]. With `.gitignore` excluding `polybot/memory/recordings/`,
`polybot/memory/state/price_sum_outliers.jsonl`, `polybot/db/window_paths.db*`, `*.db-shm`,
`*.db-wal`, `polybot.log`, the commit carries: `settings.yaml`; `memory/outcomes/` (rollups + any
same-day per-trade JSON); `memory/state/*.json` (including the four orphaned files); and the
binary `polybot_paper.db` (~12 MB) and `polybot_live.db` (~2 MB). Nonzero exits skip the commit
and restart after 60 s if before 23:30 ET [54-63]; otherwise sleep to 00:01 ET [69-79].

---

## 5. Failure modes of the nightly

- **Job exceeds `JOB_BUDGET_S` (600 s).** `wait_for` raises `TimeoutError`; the scheduler logs
  `Nightly job '<name>' abandoned after 600s — it may still be running` and moves on
  [sched:64-67]. The `asyncio.to_thread` body is NOT stopped (comment at sched:65). Internal
  deadlines exist for `compress_recordings` (540 s, per-chunk) and `ladder_recalibrate` (480 s via
  `load_windows(deadline=…)`); the SIM read inside `sniper_health` has a 240 s `wait_for` but
  `health_read → run_replay → load_windows(labels, since_ts)` passes no deadline [atl:333-336,
  atl:447], so an abandoned SIM thread streams the whole day's tape to completion; likewise
  `queue_depth_read` (120 s `wait_for`, no internal deadline). A lingering worker thread cannot
  block exit for more than 30 s: `main()`'s finally block arms a daemon `threading.Timer(30.0,
  os._exit(0))` before teardown [main.py:3041-3059]. The 08-27 log shows both inner timeouts
  firing and the pipeline still completing [log:802-804].
- **Job raises.** Caught per job; ERROR log and `alert_manager.send_error` (control channel)
  [sched:68-74]; the next job runs; `_auto_shutdown` still triggers; the process exits 0, so the
  daily commit still happens (`run_polybot.sh:39-40` comments on exactly this).
- **Discord down.** `send_health` retries 3×/20 s then logs `NIGHTLY PING LOST`; the full ping is
  already in the log as `NIGHTLY PING:` [main.py:2943; alerts.py:118-136]. `send_error` is
  fire-and-forget [alerts.py:112-116]. The Discord client reconnects with backoff to 120 s and the
  trading loop starts after a 15 s wait regardless [main.py:2966-2984]. The SOURCE gate's config
  flip and CRITICAL log do not depend on Discord [main.py:2685-2697].
- **Gamma down at label time.** `_label_pass` retries every 60 s until `end + 2400 s`, then drops
  the window from the queue with no `window_labels` row [rec:294-323]; `_fetch_contract` swallows
  errors [rec:212-219]. Because `_check_resolution_source` runs only after a successful label, an
  unlabeled window is never compared and does not increment `source_unchecked` (that counter only
  covers labeled windows with no trusted capture) [rec:323, 356-360]. Open positions keep logging
  `WAITING FOR RESOLUTION`; if Gamma returns no contract at all the orphan path applies its 600 s /
  1800 s / 3600 s ladder (§2.1). Pending ghosts are dropped after 1200 s without metadata
  [ghost_tracker.py:105-109].
- **Tape hole → `print_gap`.** A CLOB disconnect stamps `clob_ws.last_print_gap_ts` [ws:211];
  when a ladder books, `maker_bid._book` writes `snapshot["print_gap"] = int(gap_ts >=
  a["placed"])` (or `None` if the attribute is missing) into the position's `indicator_snapshot`
  [maker_bid.py:338-343]; nothing reads it at runtime (grep: writer only). Every reconnect also
  clears `books`/`trade_buffer` [ws:183]. The audit log shows the CLOB feed dropping with
  `1013 (try again later) slow consumer: send buffer full` and `ChainlinkFeed idle for 6x s —
  Reconnecting` repeatedly through 08-26/27, roughly every 10-20 minutes [log:26-652].
- **A nightly producing garbage.** No runtime consumer exists for nightly outputs: the kill-rule
  dict is returned and logged, `maker_ladder` returns a dict, and nothing is written that the
  decision path reads (§4.6). The only nightly→runtime couplings are destructive: retention deletes
  (`recordings` 30 d, `window_paths` 90 d, `price_sum_outliers` 90 d) and the rollup rewrite
  (atomic, deduplicated). A garbage ping can only change behaviour through the operator acting on it.

---

## 6. Data available today for recalibration (inventory)

### 6.1 `scripts/research/data/vps-0821/` (VPS pull, 08-21 + top-up 08-27)

**Micro-tape** (`micro_YYYY-MM-DD`): 08-14, 08-15, 08-16, 08-17, 08-18, 08-19 as `.jsonl.gz`
(27.8-55.2 MB each); 08-20 raw `.jsonl` 2,229,665,142 B; 08-21 raw `.jsonl` 1,638,206,784 B AND
`.jsonl.gz` 56.8 MB; 08-22 → 08-26 `.jsonl.gz` (42.2-53.8 MB); 08-27 raw `.jsonl` 1,478,742,386 B
(partial day, pulled ~14:39 local). Continuous coverage 2026-08-14 → 2026-08-27.
**Tape** (`tape_YYYY-MM-DD`): 08-14 → 08-19 `.gz` (4.3-8.3 MB); 08-20 raw 113.3 MB; 08-21 raw
82.6 MB and `.gz` 9.2 MB; 08-22 → 08-26 `.gz` (6.5-8.6 MB); 08-27 raw 71.8 MB (partial).
`tape_2026-08-26` holds 399,980 prints spanning the full UTC day.

**`window_paths_60s.db`** 198,574,080 B: table `window_paths`, 983,434 rows, 2,088 distinct
windows, `ts` 2026-08-14 00:00:01Z → 2026-08-21 16:35:50Z; 33 columns (the `strike_trusted`
column added after this snapshot is absent).

**`token_map.json`** 378,802 B: `{"map": 1972 windows, "missing": 0}`; keys 1786665600
(08-14 00:00Z) → 1787329200 (08-21 16:20Z), each `{up, down}` token id (`token_map.log`: "1972
mapped, 0 missing").

**DB snapshots**: `polybot_paper_0821.db` 11,952,128 B; `polybot_live_0821.db` 2,011,136 B;
`paper_0824.db` 12,173,312 B; `paper_0825.db` 12,259,328 B; `paper_0827.db` 12,423,168 B.
**`state/`**: 08-21 copies of the 13 `memory/state/*.json` files listed in §4.5.
**Data-API pulls**: `h1_pm_trades/` 56 per-window `.jsonl` (~1.25 MB each); `r5_pm_trades/` 160 files.
**Other artifacts**: `h1_cellstats.pkl` 3.0 MB, `h1_rank_results.json`, `h1b_results.json` 3.0 MB,
`h3_arb_micro_2026-08-14..21.json`, `r1_*.{json,log}` (incl. `r1_oos_ref.json` 7.6 MB),
`r23_results.json` 18.3 MB / `r23_results_v0.json`, `r4_results.json` 25.5 MB /
`r4_results_unconditional_v0.json`, `r5_*.json` (census, gamma/clob/dapi probes, series), pull logs.

**Reports (titles only):**
- `h1_report.md` — H1 — Full-market P&L decomposition, 60s-TWAP era (08-14..08-21)
- `h1b_extended_rungs.md` — H1B — Extended high rungs (0.95/0.90/0.85): engine-true replay vs the pre-registered bar
- `h2_report.md` — H2 — N+1 opening mispricing vs the known incoming strike
- `h3_report.md` — H3 — Complement structure (Up+Down): verdicts
- `h4_report.md` — H4 — Sell the boundary-certain winner post-close
- `latency_report.md` — Signal-to-execution latency — measured from recorded stamps (2026-08-21)
- `r1_report.md` — R1 — 60s margin-table re-fit at 14 real-final days (RESEARCH.md #2) + floor re-decision (#1)
- `r23_report.md` — R2 / R3 — deep_proj ladder on the RE-FIT p99.5 tables (08-27 pass); `r23_tables.md` — Table A — k_place_max x need, RE-FIT p99.5
- `r4_report.md` — R4 — Candidate A (cushion dip-buyer): engine-true replay vs the pre-registered bar
- `r5_report.md` — R5 — field scan + weekly census (08-21..27) — written 2026-08-27 ~18:45Z

### 6.2 `scripts/research/data/` (top level)

- `win_streams.jsonl.gz` 22,208,270 B: **5,495 windows**, `ep` 1786060800 (2026-08-07 00:00Z) →
  1787854200 (2026-08-27 18:10Z); per-window keys `ep, strike, final, up, token_up, token_down, l,
  bz, cb, t` (`cb` empty in the 60s era).
- `boundaries.json` 440,725 B: `boundary_ts → [first_ts, rx, value, prev_ts]`.
- `ws1_errors60.csv` 13,361,232 B: 105,170 data rows (`ep,k,final,strike,src,served,up,w,veto,
  gap,nrep,plain,bz,cb,kl`).
- `polybot_paper.db` 12,423,168 B (08-27 14:31) and `polybot_live.db` 2,011,136 B (08-21) copies.

### 6.3 `docs/audit/data/`

**`polybot_paper_audit.db`** 12,435,456 B — tables `positions, trade_history, bankroll,
peak_bankroll, window_labels, wallet_stats`:
- `window_labels`: 13,851 rows; window epochs 2026-06-11 14:40Z → 2026-08-27 20:15Z;
  **60s-era labels (epoch ≥ 1786665600): 3,720** (`labeled_at ≥ 1786665600`: 3,722); `token_up`
  NULL on 6,485 older rows.
- `positions`: 38 rows, `entry_timestamp` 2026-08-18T21:44:54Z → 2026-08-27T05:46:00Z, all
  `status = closed`; **since 2026-08-14 by `trade_context.signal_leg`: `deep_proj` 38** (no
  `lock_dip`, no unstamped); since the current `validation_epoch` (2026-08-27T19:28Z): **0**.
- `trade_history`: 38 rows, `exit_timestamp` 2026-08-18T21:49:56Z → 2026-08-27T05:51:46Z.
- `bankroll`: 396.366. `wallet_stats`: 71,035 rows — not created by `polybot/db/models.py` and
  not referenced by any `.py` under `polybot/` or `scripts/` (origin not determinable from code).

**`polybot_live_audit.db`** 2,011,136 B — `positions` 337 rows, 2026-07-05T00:44:58Z →
2026-08-15T00:24:47Z (5 since 08-14, all `deep_proj`, all closed); `trade_history` 337 rows,
exits 2026-07-05T01:22Z → 2026-08-15T00:31Z; `window_labels` 4,997 rows (2026-07-04 18:55Z →
2026-08-15 00:45Z; 111 in the 60s era); `bankroll` 123.399.

**`polybot.log`** 212,207 B, 1,664 lines, 2026-08-26 13:31:00 → 2026-08-27 20:26:00 (box clock
= UTC); contains one full nightly run (§4.2), 364 `NEW WINDOW`, 2 `TAPE VERDICT`, 2 `MAKER FILLED`,
0 `MAKER BID REJECTED`, dozens of CLOB/Chainlink reconnects.

**State JSONs** (08-27 copies): `feed_staleness.json` (chainlink p50 0.938 s / p99 2.162 s /
max 8.537 s; clob_ws p50 0.0 / p99 0.021 s; both `connected: true`, `updated_at` 1787862424);
`latency_stats.json` (`post.n 2`, p50 302.9 ms, `last_updated 2026-08-13`, no `gtc`);
`fill_stats.json` (live FOK ledger: 1,529 attempts / 359 fills, `last_updated 2026-08-13`);
`fill_stats_paper.json` (2,063 / 358, `2026-08-12`); `orphan_positions.json` (0 orphans,
`2026-08-14`); `day_open_bankroll.json` (`2026-08-27`, 393.514); `prev_resolution_margin.json`
(11.08, `saved_at` 1787809906); `gate_stats_current.json` (08-27: `book_freshness_skew 17398,
stale_prices 261, stale_feed 3`); `gate_stats.json` lifetime accumulator; `price_sum_outliers.jsonl`
29,900,507 B, 198,190 lines, `ts` 1781156060 (2026-06-11) → 1787862473 (2026-08-27); plus the
four orphaned files of §4.5.

### 6.4 In-repo working copies

`polybot/memory/outcomes/`: `rollup_2026-08-18/19/20/21/24/25.json` + two un-rolled per-trade
files from 08-26 (positions 9619, 9620). `polybot/memory/ghost_outcomes/`: absent.
`polybot/memory/recordings/` (gitignored, local): `micro_2026-08-07..27` and `tape_2026-08-07..22`
(mix of `.gz` and raw, including raw `micro_2026-08-18/20/27.jsonl` and `tape_2026-08-18/20.jsonl`),
plus pre-TWAP artifacts `box_arb.jsonl` (07-01) and `late_collector.log` (07-03).
