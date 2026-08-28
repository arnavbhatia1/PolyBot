# 01b — Money path, traced from code (main @ 15471a9a, 2026-08-27)

Ground truth is the code at the cited lines. Nothing here is taken from
CLAUDE.md, RESEARCH.md, or docstrings; where a docstring or config comment
disagrees with the executing code, the disagreement is stated as such.
Config values are quoted from `polybot/config/settings.yaml` as checked in.

Notation: `[file:line]` cites `polybot/...` unless another root is given.
`main` = `polybot/main.py`, `cl` = `polybot/feeds/chainlink_feed.py`,
`se` = `polybot/core/signal_engine.py`, `mb` = `polybot/execution/maker_bid.py`,
`base` = `polybot/execution/base.py`, `pt` = `polybot/execution/paper_trader.py`,
`lt` = `polybot/execution/live_trader.py`, `ms` = `polybot/feeds/market_scanner.py`,
`ws` = `polybot/feeds/clob_ws.py`, `db` = `polybot/db/models.py`,
`test` = `polybot/tests/test_decision_parity.py`.

---

## 1. Decision clock, window discovery, strike

### 1.1 What wakes the loop

`trading_loop` [main:2025] runs `while True` [main:2203]. Each iteration
blocks on `asyncio.wait(..., timeout=0.1, return_when=FIRST_COMPLETED)`
[main:2225-2226] over:

| Waker | Set by | Cleared by |
|---|---|---|
| `clob_ws.book_updated` [main:2220] | `_on_book` [ws:301], `_on_price_change` [ws:326], `_on_best_bid_ask` [ws:340] | [main:2232-2233] |
| `clob_ws.market_resolved` [main:2221] | `event_type == "market_resolved"` [ws:271-272] | [main:2237-2241]; also clears `_contract_price_cache` |
| `chainlink_feed.report_event` [main:2224] — only when `_sniper_wake` | `ingest_raw` on every raw `crypto_prices_chainlink` report [cl:409] | [main:2229-2231]; `_sig_woke` records whether this wake was a report |
| 100 ms timeout [main:2226] | — | housekeeping tick |

`_sniper_wake = chainlink_feed is not None and bool(config["late_window"]["trading_enabled"])`
[main:2212-2213], re-read from the live config dict every iteration — so the
in-process brake flip in `_on_source_mismatch` [main:2685] also removes the
report-event waker. With no `clob_ws` the loop polls at `sleep(0.1)` [main:2245].

Per-iteration order after wake [main:2247-2339]:
1. `_check_trading_schedule` → `in_trading_hours` [main:2250-2254; 1958-2002]
   (`schedule.trading_start 0:01 ET`, `end 23:30 ET` [settings.yaml:190-194]).
2. `_MAKER_MGR.maintain()` every tick [main:2261-2265] (§5.4).
3. **Fast path**: if (`_sig_woke` or `_hot_fp`) and `db.open_market_count() == 0`
   → `_entry_pass([])` runs before position management [main:2274-2280].
   `_hot_fp = _sniper_wake and _twap_hot(...)` [main:2274-2275]. If the mirror
   isn't built (`None`), falls back to the cached-positions check [main:2284-2290].
4. Position management over `_get_open_positions_cached(db)` [main:2284,
   2293-2325]: `_get_contract_prices` per position (SWR cache, TTL 5 s / 2 s
   near expiry [main:112-113, 795-823]); no contract → `_manage_orphaned_position`
   [main:2301-2309]; `seconds_remaining <= 0` → `mark_pending_resolution` +
   `_resolve_expired_position` [main:2311-2323]. Open positions are never
   re-evaluated — there is no other branch [main:2324-2325].
5. Ghost resolution every 30 s in background [main:2328-2335].
6. `_entry_pass(positions)` if the fast path did not already run it [main:2338-2339].

### 1.2 `_entry_pass` [main:2119-2201] — the pre-gates before any signal

1. **µs pre-gate** `_pregate_should_eval(now, last_full_eval, 300 − now%300, hot, zone)`
   [main:2128-2133; 389-402]: `hot` → always evaluate; else throttle to one
   evaluation per **0.25 s** when `sec_rem <= zone_s` and per **1.0 s** otherwise.
   `zone_s = config.late_window.twap_zone_s` = **58.0** [main:2115-2116; settings.yaml:100].
   `hot = _sniper_wake and _twap_hot(...)` [main:2129]; `_twap_hot` [main:405-426]
   is True iff `sec_rem <= zone_s`, `window_strikes[w_ts] > 0`, the **plain**
   `projected_final_twap(w_ts+300, now)` is not None, and
   `|proj − strike| >= 0.9 × twap_margin(TWAP_MARGIN_P995, sec_rem)` [main:426].
2. `is_paused_fn()` → return [main:2136-2137] (Discord `!pause`).
3. `not in_trading_hours` → return [main:2138-2139].
4. `active_count >= execution.max_concurrent_positions` (**2**) → return
   [main:2141-2144; settings.yaml:22]; only `status == "open"` counts, so a
   `pending_resolution` position does not block.
5. `_discover_contract_and_subscribe` [main:2146-2149] (§1.3); no contract → return.
6. Live only: `prewarm_market_info(condition_id)` spawned [main:2156-2157; lt:425-440].
7. Already `open` in this `cid` → return [main:2160-2161].
8. `_fetch_market_prices` [main:2166-2168] (§1.4); None → return.
9. `_compute_strike` [main:2181-2185] (§1.5); None → return.
10. `bankroll = _get_bankroll_cached(db)` [main:2189] → `_evaluate_signal_and_enter`
    [main:2191-2201] (§3).

### 1.3 Window discovery [main:1610-1670]

- `market_scanner.find_active_contract(http_client)` [main:1620; ms:297-329]:
  serves the cached contract while its `end_date` is in the future, recomputing
  `seconds_remaining` locally [ms:307-315]; kicks a background Gamma refresh
  once per `cache_seconds` (**5**, `market.scan_cache_seconds`) [ms:316-323].
  Blocks in `_fetch_active_contract` only with no live cache [ms:329, 340-387]:
  tries slugs for `window_ts` and `window_ts + 300` [ms:342-344], requires the
  event to be `active` [ms:364] and `seconds_remaining > min_time_remaining`
  (**0**, `market.min_time_remaining_seconds`) [ms:368; settings.yaml:206].
- `cid = contract["slug"]` is the market id everywhere [main:1624].
- First entry into a window: `db.has_open_or_pending_market(cid)` (sync mirror)
  or `has_position_for_market` → if held, returns no contract [main:1628-1635].
- WS subscription set: unsubscribes tokens not in `{token_up, token_down} ∪ _MAKER_MGR.holding_tokens()`
  [main:1649-1656; mb:106-109], subscribes new [main:1658-1660], pre-warms
  `fetch_tick_size` for both tokens [main:1664-1668].
- `parse_contract` [ms:101-180] derives `token_id_up/down` from `outcomes`
  "Up"/"Down" [ms:132-140], `seconds_remaining` from `endDate` [ms:142-149],
  `event_metadata = {price_to_beat, final_price}` from `eventMetadata.priceToBeat/finalPrice`
  [ms:154-164] (None when `priceToBeat` absent).

### 1.4 Prices and book gates [main:1459-1607]

- Books: WS book if `asks` present and `ts` within `_WS_STALE_S` = **10 s**
  [main:120, 1470-1474], else REST `fetch_clob_book` [main:1475]. Sequential
  awaits by design [main:1479-1483].
- Price source priority: fresh WS `best_bid_ask` → HTTP book best ask → Gamma
  `outcomePrices` [main:1505-1510]. `clob_best_ask` = `(asks[0].price, Σ asks.size)`
  [ms:252-259]; `depth_usd = depth × ask` [main:1542-1543].
- Gates, in order (each `_record_skip(...)` and returns None):
  - `book_freshness_skew`: source is CLOB and `both_books_fresh(10 s)` is False [main:1514-1520].
  - `stale_prices`: `price_up + price_down` outside `[0.98, 1.02]` [main:1526-1538].
  - `thin_clob_depth`: **both** sides `< market.min_book_depth_usd` (**50**) [main:1545-1553].
  - Spread: per-side spread from WS BBA (`spread` field or `ask − bid`) else REST
    `/spread` [main:1557-1582]; both unavailable → `spread_unavailable`, fail closed
    [main:1584-1589]; `effective_cost = max(spread)/2 + EFFECTIVE_FEE_PEAK (0.0175) > market.max_spread (0.1)`
    → `spread_too_wide` [main:1593-1600].

### 1.5 Strike [main:1395-1455] and its trust flag

- `contract_window_ts = int(cid.rsplit("-",1)[-1])` [main:1407].
- `cl_strike = chainlink_feed.get_strike(w)` [main:1411]; `ptb = contract.event_metadata.price_to_beat` [main:1412-1413].
- **Gamma wins**: if `ptb > 0` → `window_strikes[w] = ptb`, `_strike_trusted[w] = True`,
  `w` added to sticky `_gamma_strikes` [main:1414-1427]; a change `> 0.005` from a
  previously held value logs "Strike Corrected" [main:1419-1420].
- Else if `cl_strike > 0` and the window is not already Gamma-pinned →
  `window_strikes[w] = cl_strike` **every loop**, `_strike_trusted[w] = strike_reliable(w)`
  [main:1428-1435].
- Retention: `window_strikes`, `_strike_trusted`, `_gamma_strikes`, `_strike_logged`
  pruned at 600 s [main:1442-1447]. `strike <= 0` → None [main:1449-1454].

`ChainlinkFeed`:
- `get_strike(w)` [cl:139-149]: None for the feed's own start window [cl:140-141];
  the locked boundary value if captured [cl:142-144]; else the **cold-start
  fallback** — latest `twap_official` if received within `STALE_TIMEOUT_S` (60 s)
  [cl:147-148]. That fallback is what `window_strikes` holds before the boundary
  locks; it is untrusted (`strike_reliable` False), so the trust gate in §3 blocks capital.
- `_record_boundary(observed_ts, value)` [cl:417-436], called from `ingest_sixty`
  [cl:390] on topic `crypto_prices_twap_sixty` only [cl:612-613]: `boundary_ts = floor(observed_ts/300)×300`
  [cl:424]; first write wins [cl:425-427]; meta `(observed_ts, prev_twap_ts)`
  [cl:427]; 2 h retention [cl:429-436].
- `boundary_captured(w)` [cl:292-297]: `w != start_window and w in _boundary_prices`.
- `strike_reliable(w)` [cl:299-316]: captured, meta present, `prev_ts is not None`
  (not the topic's first-ever report), and `first_ts − w <= STRIKE_TRUST_GAP_S` (**0.5 s**,
  payload clock) [cl:33, 316].
- Docstring drift: `_compute_strike` still says "official 30s-TWAP stream"
  [main:1401]; the code path is `ingest_sixty` / `crypto_prices_twap_sixty`.

---

## 2. Signal

### 2.1 `SignalEngine.evaluate_twap_lock` [se:84-149]

Constructed with `min_edge = late_window.sniper_min_edge` (**0.04**) and
`kelly_fraction = math.kelly_fraction` (**0.08**) [main:2457-2459; settings.yaml:12,108].

Inputs (from [main:1019-1026]): `projected_twap` (**plain** projection), `strike`,
`seconds_remaining`, `market_ask_up`, `market_ask_down`, `zone_s` = 58.0,
`k_min_s` = 6.0, `sniper_min_edge` = 0.04, `fee_rate` = 0.07,
`require_max_tier` = True [settings.yaml:99-108].

Steps:
1. `projected_twap is None or strike <= 0` → SKIP [se:109-110].
2. `k < k_min_s or k > zone_s` → SKIP [se:112-114].
3. `disp = proj − strike`; `up = disp >= 0` (tie → Up) [se:115-116].
4. `mmax = twap_margin(TWAP_MARGIN_MAX, k)` [se:118];
   `need = mmax if require_max_tier else twap_margin(TWAP_MARGIN_P995, k)` [se:121].
5. `|disp| < need` → SKIP "not locked" with `side` set [se:122-125].
6. `deterministic = |disp| >= mmax`; `prob = 0.999` if deterministic else `0.995` [se:50-51, 126-127].
7. `ask` = side's ask; not in (0,1) → SKIP [se:128-131].
8. `edge = prob − ask` [se:132]; `edge < sniper_min_edge` → SKIP [se:133-137].
   The ask cap is therefore `ask <= prob − 0.04` (≤ 0.959 on the max tier).
9. `kelly = _kelly(ask + sniper_min_edge, ask, fee_rate)` [se:141] — sized on
   the **defended edge**, not the tier prob.
10. Returns `LATE_SNIPE_YES` / `LATE_SNIPE_NO` with `prob`, `edge`, `kelly`, `side` [se:142-149].

### 2.2 Kelly [se:151-160]

```
if ask <= 0.01 or ask >= 0.99: return 0
b      = (1 − ask) / ask
net_b  = b × max(1e-6, 1 − fee_rate)              # fee_rate = 0.07
raw    = (p × net_b − (1 − p)) / net_b            # p = ask + sniper_min_edge
return max(0, raw × kelly_fraction)               # kelly_fraction = 0.08
```

Textbook Kelly for a binary bet at net odds `b`: `f* = (b·p − q)/b`, `q = 1 − p`.
The code is exactly that form with two substitutions: (i) `b → net_b = b(1 − 0.07)`,
i.e. the fee is modelled as a flat 7 % haircut on the win payoff — the raw
`feeRate` coefficient, not the `rate·p·(1−p)` per-share curve used everywhere
else in booking (`taker_fee` [base:160-162]); (ii) `p → ask + 0.04`, so the
implied win probability is the market price plus the edge floor, not the tier
probability. The result is then scaled by 0.08.

### 2.3 Margin tables as they exist in code today [se:34-45]

Header comment: "60s-rule tables, re-fit 2026-08-27" [se:16-33]; last commit to
this file is `03349951 2026-08-27 15:03:31 -0400` ("Margin tables re-fit on 14
real-final days: every knot 2-4x wider").

```
TWAP_MARGIN_P995 = (
    (2.0, 2.5), (4.0, 3.5), (6.0, 4.0), (8.0, 5.0), (10.0, 7.5),
    (12.0, 9.0), (15.0, 12.5), (20.0, 20.0), (25.0, 28.5), (29.0, 36.0),
    (35.0, 48.0), (40.0, 57.0), (45.0, 68.5), (50.0, 88.0), (55.0, 107.5),
    (58.0, 107.5),
)
TWAP_MARGIN_MAX = (
    (2.0, 18.0), (4.0, 19.0), (6.0, 19.0), (8.0, 19.0), (10.0, 32.0),
    (12.0, 36.0), (15.0, 61.0), (20.0, 63.0), (25.0, 100.0), (29.0, 100.0),
    (35.0, 208.0), (40.0, 231.0), (45.0, 279.0), (50.0, 304.0), (55.0, 371.0),
    (58.0, 371.0),
)
```

`twap_margin(knots, k)` [se:53-60]: `k <= knots[0].x` → first y; otherwise
linear interpolation on the first interval with `k <= x1`; `k` past the last
knot → last y (so the tables clamp at 107.5 / 371.0 beyond k=58, and at
2.5 / 18.0 below k=2).

In the ladder path the relevant numbers are P995 at k∈[6,25]: $4.0 at k=6,
$5.0 at 8, $7.5 at 10, $9.0 at 12, $12.5 at 15, $20.0 at 20, $28.5 at 25.

### 2.4 Projection [cl:219-259]

`projected_final_twap(close_ts, now=None, bridged=False)`:
```
t0 = close − TWAP_HORIZON_S (60)
None if t <= t0 or t > close                                   [cl:234-235]
None if _price <= 0 or _last_update <= 0
        or (t − _last_update) > SPOT_STALE_S (3.0)             [cl:237-238]
None if any raw-report receipt gap > RAW_GAP_MAX_S (10.0)
        in (t0, t], including t0→first report and last→t       [cl:243-253]
w   = (t − t0) / 60                                            [cl:254]
avg = running_avg(t0, t); None → None                          [cl:255-257]
spot = _price + (spot_bridge_delta() if bridged else 0)        [cl:258]
return w·avg + (1 − w)·spot                                    [cl:259]
```
`_price`/`_last_update` are the latest raw report and its **receipt** time
[cl:401-402]; so "spot" here is the Chainlink raw value, not Binance.

`running_avg(start, end)` [cl:151-176]: receipt-clock (`rx`) zero-order hold.
Seed = last report with `rx <= start`, else the first report within 2 s after
`start`, else None [cl:159-169]; integral of step function over `[start, end]` [cl:170-176].

`spot_bridge_delta()` [cl:187-217] returns **0.0** (bridge falls back to plain) when:
Binance ring empty or no raw payload ts [cl:194-195]; newest Binance ts ≤ last
raw payload ts [cl:197-198]; no Binance tick at/before the raw payload ts
[cl:199-206]; anchor older than the raw ts by > `BRIDGE_ANCHOR_MAX_AGE_S` (2.0)
[cl:207-209]; `|delta| > BRIDGE_MAX_DELTA_FRAC (0.01) × anchor` [cl:210-216]
(one-time "BRIDGE OFF" warning). Otherwise `delta = newest_binance − anchor`.
Binance ring: topic `crypto_prices`, symbol `btcusdt`, payload clock, 10 s
retention, sorted on out-of-order arrival [cl:357-376, 558-573].

`twap_frozen(now)` [cl:261-290]: False unless the official value has been
unchanged (within `TWAP_FROZEN_EPS_USD` 0.005 [cl:382-385]) for
`>= TWAP_FROZEN_S` (20 s) **and** at least two raw reports arrived in that span
**and** their range `>= TWAP_FROZEN_RAW_MOVE_USD` (2.0).

---

## 3. Gate stack in `_evaluate_signal_and_enter` [main:895-1392], in execution order

Prerequisites already satisfied on entry (§1.2/1.4/1.5): pre-gate throttle,
not paused, in hours, `< 2` open positions, no position in this window, book
gates, non-None strike.

| # | Gate | Input / threshold | Source | On failure |
|---|---|---|---|---|
| 1 | Stale Chainlink | `chainlink_feed.age_seconds > 60` (literal) | [main:969-973] | `_record_skip("stale_feed")`, log once per window, return |
| 2 | Official TWAP frozen | `chainlink_feed.twap_frozen()` (§2.4) | [main:979-985] | `_record_skip("twap_frozen")`, log once, return |
| 3 | Fee rate | `fetch_fee_rate` → constant `DEFAULT_FEE_RATE` 0.07 | [main:995; ms:210-217] | — |
| 4 | Leg block armed | `lw_cfg["trading_enabled"]` and feed present and `seconds_remaining <= twap_zone_s` (58) | [main:1003-1005] | neither leg evaluates; `signal` stays SKIP "no leg armed" [main:929] |
| 5 | Strike trust | `_strike_trusted.get(w_ts, False)` | [main:1009-1014] | `_emit_gate_skip("sniper_strike_unverified")`; no signal, **no ladder** |
| 6 | Maker resting exclusion | `_MAKER_MGR.resting_on(w_ts)` | [main:1015-1016; mb:103-104] | silent `pass` — nothing else this tick for this window |
| 7 | Taker signal | `evaluate_twap_lock(plain proj, strike, k, ask_up, ask_down, 58, 6.0, 0.04, 0.07, require_max_tier=True)` | [main:1018-1026] | SKIP → no taker; ladder still considered |
| 8 | Taker dormant remap | `action != SKIP and not taker_enabled` (`taker_enabled: false`) | [main:1033-1041; settings.yaml:72] | `_emit_gate_skip("taker_dormant")`; signal replaced by SKIP keeping prob/edge/side. **Every taker fire is neutralised here in the checked-in config.** |
| 9 | Ladder placement | all of: `_MAKER_MGR` present; bridged `_proj_fast` not None; `_snipe.action == "SKIP"`; `maker_k_place_min (6.0) <= k <= maker_k_place_max (25.0)` | [main:1050-1055; settings.yaml:167-174] | not considered |
| 9a | Ladder side | `_mdisp = _proj_fast − strike`; Up iff `>= 0` | [main:1056-1057] | — |
| 9b | Ladder has-position | `db.has_open_or_pending_market(cid)` (mirror) → fallback `has_position_for_market` | [main:1065-1071] | `_emit_gate_skip("maker_position_open")` |
| 9c | Ladder floor | `_mmargin = twap_margin(P995, k) > 0` and `|_mdisp| >= min_need × _mmargin`; `min_need` = min rung `need` = **1.0** | [main:1072; mb:222-228; settings.yaml:161-166] | silent |
| 9d | Ladder budget | `round(bankroll × maker_bankroll_frac (0.15) × breaker.kelly_multiplier, 2)` | [main:1076-1078] | → `consider_placement(...)` [main:1079-1096] with `headroom_mult = |_mdisp| / _mmargin`, snapshot `trade_context.signal_leg = "deep_proj"`, `twap_proj` (bridged), `twap_proj_plain`, `twap_disp`, token ids |
| 10 | Taker action remap | `LATE_SNIPE_YES/NO → BUY_YES/NO`, `_signal_leg = "lock_dip"`, `phase = "late_sniper"` | [main:1097-1101] | — |
| 11 | Not a BUY | `signal.action not in (BUY_YES, BUY_NO)` | [main:1122-1126] | return None (the normal state) |
| 12 | Edge cap | `signal.edge > sniper_max_edge` (0.50) | [main:1133-1136] | `_record_skip("edge_cap")`, ghost, return |
| 13 | Base size | `size = round(bankroll × signal.kelly_size × breaker.kelly_multiplier, 2)` | [main:1145-1147] | — |
| 14 | Concurrency multiplier | `concurrent_multiplier(side, cid, open positions)`; ρ same-side 0.75 → ×0.35; opposite −0.25 → ×0.90; same market ignored | [main:1149-1154; execution/correlation.py:177-219] | — |
| 15 | Deployment cap | `size <= bankroll × max_bankroll_pct` (0.80) | [main:1158-1159] | clip |
| 16 | Thin side depth | `side_depth < min_book_depth_usd` (50) | [main:1163-1171] | `_record_skip("thin_book_depth")`, ghost, gateskip, return |
| 17 | Book-fill cap | `size <= side_depth × max_book_fill_pct` (0.50) | [main:1172-1174] | clip |
| 18 | Net edge after slippage | `est_slip = slippage_pct(size, side_depth, 0.03)` = `f·0.03·(1+f)`, `f = min(size/depth, 1)`; `net_edge = edge − price × est_slip < min_edge (0.04)` | [main:1177-1184; base:146-157] | `_record_skip("net_edge_after_slippage")`, ghost, gateskip, return |
| 19 | CLOB minimum | `size < 1.0` | [main:1188-1194] | `_record_skip("min_size")`, ghost, gateskip, return |
| 20 | Tick + fresh ask | `fetch_tick_size` (1 h cache, "0.01" on error); `fresh_ask` from WS BBA if `ts` within 10 s | [main:1196-1202; ms:219-237] | — |
| 21 | FOK limit | `price = snap_to_tick(max(ask, min(ask + sniper_fok_slip (0.01), prob − min_edge (0.04))), tick)`; `snap_to_tick` rounds **down**, clamps `[tick, 1 − tick]` | [main:1209-1212; ms:240-249] | — |
| 22 | Snapshot | `trade_context` with strike, k, both asks, prob, edge, size, phase, aux, latency stamps, `signal_leg`, `twap_proj/disp` (lock_dip only), `twap_tier`, chainlink price/age at fire, token ids | [main:1223-1272] | — |
| 23a | Pre-submit VWAP drift | book ≤ 10 s old → `fok_vwap = compute_buy_vwap(book, size)`; `prob − fok_vwap < min_edge` or `> sniper_max_edge` | [main:1276-1290; base:176-210] | `_record_skip("pre_submit_vwap_drift")`, ghost (with snapshot), return |
| 23b | Pre-submit fresh-ask drift (only when 23a had no VWAP) | `fresh_ask > 0 and fresh_ask != price`: `gross = prob − fresh_ask`, `net = gross − fresh_ask × slip`; `net < min_edge` or `gross > max_edge` | [main:1291-1299] | `_record_skip("pre_submit_edge_drift")`, ghost, return |
| 24 | Warm sign | `hasattr(trader, "warm_buy_signature")` → background task | [main:1305-1308] | live only (§7) |
| 25 | `trader.open_trade(...)` | see below | [main:1310-1320] | — |
| 25a | Duplicate market | `preflight_peek(cid)` / `get_open_trade_preflight`: `has_pos` (open **or** pending) | [base:309-315; db:135-149, 389-404] | `TradeResult(False, "Duplicate market …")` |
| 25b | Max positions | `pos_count (open only) >= max_concurrent_positions` (2) | [base:316-317] | `"Max positions reached"` |
| 25c | Deployed cap | `deployed + size > (bankroll + deployed) × max_bankroll_deployed (0.80)` | [base:321-326] | `"Bankroll limit …"` |
| 26 | Execute | `_execute_buy(token, price, size, fee_rate)` (§4) | [base:329] | `TradeResult(False, fill.reason)` |

Post-result handling [main:1322-1392]: rejection → `_record_killed_ask` when
the reason contains "no fill" or starts with "price moved" [main:1326-1329],
one "OPEN … REJECTED" line per reason per window [main:1330-1334]. Success →
cache invalidate [main:1338], LATENCY BUDGET warning when owned segments
`> 25 ms × 1.5` [main:528, 1340-1355], `_window_recorder.mark_traded(cid)`
[main:1356-1357], banner path (§7).

`_ghost` [main:931-965] records only when `sig.action` is a BUY, so gates 1-9
never produce ghosts; gates 12, 16, 18, 19, 23 do.

---

## 4. Order construction and placement

### 4.1 Live FOK — `LiveTrader._submit_fok_order` [lt:1728-2004]

Called from `_execute_buy` [lt:500-505] with `(token, BUY, size_usdc, limit, fee_rate)`.

1. Latched auth error → `AuthError` [lt:1742-1743] (latched by `prewarm_http` /
   keepalive [lt:419-423, 452-457]).
2. `notional < _MIN_ORDER_USD − 0.01` (0.99) → unfilled "below CLOB minimum" [lt:1747-1755].
3. Pre-check: if the WS book is ≤ 5 s old, `_estimate_fok_walk` [lt:962-1009]
   walks asks for `amount` USDC; `vwap > limit` → unfilled "skipped before
   sending" [lt:1760-1769]; insufficient depth or empty → None → proceed.
4. BUY setup: `balance_task = _get_token_balance(token)` (pre-fill balance),
   `ws_settle_event = clob_ws.trade_event_for(token)` cleared [lt:1771-1783];
   `submit_ts` captured before signing [lt:1778].
5. Presigned order: `_await_buy_warmup_inflight` (≤ 0.25 s, param-matched)
   then `_take_buy_warmup` (TTL 5 s, price drift ≤ 0.01, size drift ≤ 5 %)
   [lt:1787-1791; 1164-1195].
6. Retry loop `for attempt in 1..3` [lt:58, 1792]:
   - attempt 1 with a presigned order posts directly [lt:1794-1808]; otherwise
     `create_market_order(MarketOrderArgs(token, amount, side, price))` +
     `post_order(signed, OrderType.FOK)` in the sign executor [lt:1810-1827].
     Any `post_order` exception is wrapped as `_AmbiguousPostError` [lt:1802-1803, 1821-1822].
   - `resp.success` False: auth text → `AuthError` [lt:1831-1833];
     `errorMsg` containing one of `_NON_RETRYABLE_ERRORS` =
     `{INVALID_ORDER_NOT_ENOUGH_BALANCE, MARKET_NOT_READY, INVALID_ORDER_EXPIRATION}`
     → return unfilled, no retry [lt:62-66, 1835-1838]; else sleep
     `_retry_sleep(attempt)` (0.03 s × 2^(n−1) ± 20 %) and retry [lt:88-91, 1839-1843].
   - `status == "matched"` [lt:1845]: **fill price determination, in order**
     (a) `_await_buy_settle(ws_settle_event)` — floor 0.03 s, ceiling 0.15 s
         [lt:83-84, 507-526]; then `_ws_vwap_since(token, submit_ts, limit, amount)`
         [lt:528-560]: WS trades since `submit_ts − 0.05` with price
         `<= limit + 0.005`, total shares within `[0.85, 1.30] × amount/limit`;
         gross VWAP or None [lt:1855-1865];
     (b) balance delta: `fill_price = amount / (after − before)` if delta
         `> 0.01` sh [lt:1867-1888];
     (c) `_get_fill_price_ex(order_id, limit)` [lt:1892-1893; 2012-2063]:
         `get_order(...).associate_trades` VWAP, 8 attempts × 0.12 s; empty →
         `(limit, False)`;
     (d) any exception → `fill_price = limit`, `price_from_trades = False` [lt:1894-1899].
     Then: `_update_fill_stats(True)`, `_maybe_recheck_allowance` (every 10
     submits, warn `< $25`) [lt:67, 478-498, 1911-1914], `_cache_post_buy_balance`
     [lt:1915-1918]; returns `FillResult(filled=True, fill_price, fill_size=amount (USDC), order_id, price_from_trades)`
     [lt:1924-1931]. **Nothing after "matched" can re-enter the retry loop.**
   - accepted but not matched (e.g. "delayed") → `_settle_unmatched_order`
     [lt:1938-1940; 562-630]: `cancel_orders([oid])`; confirmed cancel → unfilled;
     else `_get_fill_price` → filled if `> 0`; else BUY: WS VWAP check; else
     unfilled "outcome unknown; not retried".
   - `_AmbiguousPostError`: auth → raise [lt:1945-1947]; `_exchange_rejected(cause)`
     (an `err.status_code` in 400-499) [lt:122-128, 1950] → **definitive kill**
     "book moved so no fill", RTT recorded as a POST sample, no retry [lt:1951-1962];
     else BUY → `_await_buy_settle` + `_ws_vwap_since`, filled if a VWAP is found
     [lt:1963-1974]; else unfilled "POST outcome unknown — not retried" [lt:1975-1983].
   - other exceptions (pre-POST: signing, local) → retry after backoff [lt:1984-1993].
7. Exhausted → unfilled "price moved before fill after 3 attempts" [lt:1995-2004].

Live GTC [lt:643-683]:
- `place_gtc_bid(token, price, shares)`: `create_order(OrderArgs(token, price, size=shares, BUY))`
  + `post_order(signed, GTC)` in a thread, `_record_gtc_latency("place", …)`;
  returns `orderID`/`id` or **None** with `MAKER BID REJECTED` at ERROR for
  both a rejecting response and an exception [lt:643-667].
- `cancel_gtc(oid)`: `cancel_orders([oid])`, latency recorded; exceptions propagate
  to the caller [lt:669-672] (caught in `_retire`, §5.5).
- `poll_gtc_fill(oid)`: `get_order(oid)["size_matched"]`; None on failure/empty [lt:674-683].

### 4.2 Paper FOK — `PaperTrader._execute_buy` [pt:44-57]

1. `_precheck_rejects` [pt:195-244]: book present and `ts` within 5 s, walk
   asks for `size_usd`; insufficient depth → abstain; `vwap > requested_price`
   → reject "not enough shares on the book at our price" [pt:50-52].
2. `_simulate_latency()` [pt:53; 321-331]: `sleep(max(latency_floor_s, latency_scale × draw))`
   with `latency_scale` = **0.95**, `latency_floor_s` = **0.32** [settings.yaml:44-48; main:2496-2501].
   `draw` is inverse-CDF over
   `_LATENCY_QUANTILES = ((0.00,0.405),(0.25,0.410),(0.50,0.436),(0.75,0.679),(0.99,1.646),(1.00,2.222))` [pt:288-291, 312-319].
3. Network-fail sim: `random() < _compute_fail_rate(token, "buy")` → unfilled
   "simulated network error" [pt:54-56]. `_compute_fail_rate` [pt:155-193]:
   `network_fail_rate` (**0.03**) when no usable book; else `0.005` base
   `+ 0.010` if `spread/mid > 5 %` `+ 0.010` if top-of-book `< $50`, capped `0.030`.
4. `_retry_walk` → one `_walk_book` [pt:246-257; 333-402]: `clob_ws is None`
   → synthetic fill at the requested price (test-only) [pt:341-342]; book older
   than 30 s → unfilled; empty asks → unfilled; walk asks ascending for
   `size_usd`; leftover → "Insufficient book depth"; `vwap > requested` →
   "Price moved before fill (simulated)"; else `FillResult(True, vwap, fill_size=spent)`.
   There is no in-submit retry (one-shot) [pt:248-253].

Paper GTC [pt:63-84]:
- `place_gtc_bid`: `_simulate_gtc_latency()` then returns `f"paper-{ms}"` —
  **never None, never rejects** [pt:63-73].
- `cancel_gtc`: `_simulate_gtc_latency()` only [pt:75-81].
- `poll_gtc_fill`: always None [pt:83-84]; fills come from `on_print` (§5.3).
- `_simulate_gtc_latency` [pt:301-310] inverse-CDF over
  `_GTC_LATENCY_QUANTILES = ((0.00,0.049),(0.50,0.056),(0.90,0.060),(1.00,0.170))`
  [pt:297-299]; `latency_scale` does not apply.

---

## 5. Maker ladder lifecycle — `MakerBidManager` [mb]

Constructed in `main()` iff `maker.maker_bid_enabled` (**true**) with
`paper = (mode != "live")` [main:2649-2652; settings.yaml:139]; wired:
`on_fill = _on_maker_fill`, `clob_ws`, `tick_fn = market_scanner.fetch_tick_size`
[main:2655-2659]; `clob_ws.on_trade = _on_trade_mux` feeds `on_print` (and the
tape recorder) on every `last_trade_price` event [main:2661-2665; ws:349-369].

### 5.1 Ladder definition [mb:111-134]

`ladder()` prefers `memory/state/maker_ladder.json` (clamped to `[0.15, 0.95]`,
fractions/need frozen from seed, rung count must match) [mb:119-134]. That
file **does not exist** in the checked-in tree (`polybot/memory/state/` listing),
so the seed from `settings.yaml` runs:
`[[0.80,0.20,1.0],[0.65,0.20,1.0],[0.50,0.20,1.0],[0.35,0.20,1.0],[0.20,0.20,1.0]]`
[settings.yaml:161-166]. `min_need()` = 1.0 [mb:222-228].

### 5.2 `consider_placement` [mb:138-187]

Called from gate 9d with `budget_usd = round(bankroll × 0.15 × kelly_mult, 2)`
and `headroom_mult = |disp| / P995(k)`.
1. `active is not None or budget_usd < MIN_NOTIONAL_USD (1.0)` → return [mb:144].
2. Per rung `[px, frac, need]`: `headroom_mult < need` → skip [mb:148-149];
   `usd = round(budget × frac, 2) < 1.0` or `px ∉ (0,1)` → skip [mb:150-152];
   `px = legal_price(token, px)` — round **down** to tick, clamp `[tick, 1−tick]`
   [mb:153; 83-99]; `shares = round(usd/px, 2) < MIN_SHARES (5.0)` →
   `MAKER RUNG SKIPPED` at INFO, skip [mb:154-162]; `place_gtc_bid` timed →
   rung `{price, shares, order_id, filled: 0.0, place_ms}` on a non-None id
   [mb:163-172]; a None id is not appended (live logs the rejection itself) [mb:173-174].
3. No rungs → return [mb:175-176]; else `active = {window_ts, market_id, question, side, token_id, rungs, placed, snapshot}`
   and one `MAKER LADDER` line [mb:177-187].

Arithmetic at the checked-in paper bankroll ($150 initial [settings.yaml:24;
main:2445-2446], multiplier 1.0): budget $22.50, $4.50/rung → 5.63 sh at 0.80,
6.92 at 0.65, 9.0 at 0.50, 12.86 at 0.35, 22.5 at 0.20 — all above 5 sh.

### 5.3 Paper fill rule — `on_print(asset_id, trade)` [mb:191-218]

Only when `active` is set, `self.paper`, and `asset_id == active.token_id`
[mb:199-201]. For each rung:
- `px < rung.price − 1e-9` → `filled = shares` (full) [mb:210-211];
- `|px − rung.price| <= 1e-9` → `at_px_vol += sz`; `credit = min(shares, max(0, at_px_vol − AT_PRICE_QUEUE_SH (135.0)))`;
  `filled = max(filled, credit)`, `filled_at_px = True` [mb:212-218].
The module docstring says at-price prints "never count" [mb:20-24]; the
executing rule credits them beyond 135 shares of accumulated at-price volume.

### 5.4 `maintain()` [mb:244-295], every loop tick

`close = window_ts + 300`, `k = close − now`.
- **Post-close** (`now >= close`): `now − close > post_close_hold_s (60.0)` →
  reason "post-close hold over" [mb:257-258]; else `certain_winner(window_ts)`
  [mb:230-242] — requires `boundary_captured` **and** `strike_reliable` for
  both `window_ts` and `close`, then `"Up" if final >= strike else "Down"`;
  None and `now − close > PC_VERIFY_GRACE_S (5.0)` → "outcome unverified"
  [mb:260-265]; winner ≠ side → "lock missed the winner" [mb:266-267].
- **Pre-close**: `proj = projected_final_twap(close, bridged=True)`; None →
  "projection cold" [mb:269-273]; `signed = ±(proj − snapshot.strike_price)`;
  `signed < min_need × P995(k)` → "projection flipped" if `signed <= 0` else
  "sign inside noise" [mb:275-282].
- **Live poll**: `not paper and reason is None and now − _last_poll >= 1.0` →
  `poll_gtc_fill` per rung, `filled = min(shares, matched)` if larger [mb:283-288].
- All rungs `filled >= shares − 1e-9` → "filled" [mb:291-293].
- Any reason → `_retire(reason)` [mb:294-295].

### 5.5 `_retire` / `_book` [mb:298-370]

- Cancel every rung via `cancel_gtc`, stamping `cancel_ms`; an exception logs
  `MAKER CANCEL failed` and continues [mb:304-312].
- Live: one final `poll_gtc_fill` per rung, taking the larger `matched` [mb:313-325].
- `finally: _book(a, reason)`; `finally: active = None` [mb:326-331] — booking
  runs even under shutdown cancellation.
- `_book`: `filled = Σ filled`, `notional = Σ filled × price` [mb:334-335]; if
  `filled > 0 and notional >= 1.0`: `vwap = notional / filled` (unrounded)
  [mb:339]; snapshot stamps `print_gap = int(clob_ws.last_print_gap_ts >= placed)`
  (None if no gap ever) [mb:340-344], `trade_context.gtc_place_ms` /
  `gtc_cancel_ms` lists [mb:345-347]; `trader.book_maker_fill(market_id, question, side, price=vwap, shares_gross=filled, token_id, indicator_snapshot)`
  [mb:349-352]; booked → `on_fill` (`_on_maker_fill`: yellow banner + Discord
  `send_trade_opened` with `fee=0.0`) [mb:353-362; main:224-250]; not booked →
  `MAKER UNBOOKED` warning [mb:365-367]. No fill → `MAKER OFF` [mb:369-370].

---

## 6. Fill handling, settlement, PnL

### 6.1 Taker booking — `BaseTrader.open_trade` [base:293-375]

After `_execute_buy` fills: `shares_ordered = fill_size / fill_price`;
`fee_in_shares = entry_fee_shares(shares_ordered, fill_price, fee_rate)`;
`shares_received = shares_ordered − fee_in_shares` [base:334-336].
`db.open_position_and_debit_bankroll(new_bankroll = bankroll − fill_size, …, entry_price = fill_price, size = fill_size, signal_score = prob, shares_held = shares_received)`
— one transaction: INSERT positions + UPSERT bankroll, rollback on any
`BaseException` [base:347-358; db:174-218]. DB failure → `CRITICAL` log and
`TradeResult(False)` while the exchange holds the position [base:359-367].
`_schedule_fill_audit` fires if the trader has one [base:371-373] (live only).

### 6.2 Maker booking — `book_maker_fill` [base:379-438]

Preflight identical to `open_trade` plus `notional > bankroll`; any failure →
`CRITICAL … shares are on the exchange unbooked`, returns False [base:394-410].
`fee_in_shares = 0.0` [base:415]; `trade_context.maker_fill = 1` stamped into
the snapshot [base:416-420]; insert with `signal_score = 0.0`, `entry_price = vwap`,
`size = notional`, `shares_held = shares_gross`, `new_bankroll = bankroll − notional`
[base:421-433]; DB failure → `CRITICAL`, False [base:434-437].

### 6.3 Resolution

Trigger: position management sees `live["seconds_remaining"] <= 0` →
`mark_pending_resolution` → `_resolve_expired_position` [main:2311-2323].

`_resolved_exit_price(live, side, market_id)` [main:1673-1716]:
1. `event_metadata.final_price` and `price_to_beat` both present →
   `up_won = final >= strike`; `exit = 1.0 if (side == "Up") == up_won else 0.0`
   [main:1691-1707]; logs a disagreement if the CLOB book is at an extreme on
   the other side (oracle still decides) [main:1696-1703].
2. Else `live.closed` and `0.98 <= price_up + price_down <= 1.02` and
   `price_up >= 0.99 or <= 0.01` → book decides [main:1710-1715].
3. Else `(None, None)` → caller waits [main:1716].

`_resolve_expired_position` [main:1719-1820]: tape strike/final from
`get_strike` **only if** `strike_reliable` [main:1738-1742]; unresolved →
"WAITING FOR RESOLUTION" once, `TAPE VERDICT` once when both tape values
exist, return [main:1745-1759]; resolved → `RESOLVED …` once [main:1760-1764];
`RESOLUTION DRIFT` warning if `|tape_final − Gamma final| > 0.005` (log only)
[main:1768-1775]; `trader.resolve_position(pos.id, exit_price)` [main:1777];
`pending` → retry next tick [main:1778-1780]; success → cache invalidate, day
stats from DB, RESOLVED banner, Discord `send_trade_closed`,
`breaker.update_bankroll(bankroll_after)` + `set_peak_bankroll`,
`record_win/record_loss` (alerts only), `_record_outcome` (outcome JSON;
`profitable = gain_pct > 0`) [main:1781-1811; 861-892].

`_manage_orphaned_position` [main:1823-1955] (no Gamma contract): age `< 600 s`
→ wait [main:1838-1839]; direct Gamma refetch → `_resolved_exit_price`
[main:1845-1858]; else age `> 1800 s` → both boundary captures must be
`strike_reliable`, `up_won = final >= strike` [main:1859-1893], Discord error
[main:1894-1901]; else wait, ERROR + Discord after `> 3600 s` [main:1902-1914].
Never fabricates from the `get_strike` fallback (both ends must be reliable).

`resolve_position` [base:518-556]: `shares = shares_held or size/entry`;
`entry_fee_usd = _entry_fee_usd_from_position` = `(size/entry − shares_held) × entry`
[base:213-219, 536] (0 for maker fills, since `shares_held = size/price`);
`exit_fee = exit_fee_usdc(shares, exit_price)` — `0.07·sh·p·(1−p)` = **0** at
p ∈ {0, 1} [base:537]; `revenue = shares × exit − exit_fee`; `pnl = revenue − size`;
`gain_pct = pnl / size` [base:538-540]; `new_bankroll = _resolve_bankroll(position, exit)`
[base:545]; None → `TradeResult(pending=True)` [base:546-548];
`db.close_position(pos_id, exit_price, new_bankroll=…, pnl, fees=entry_fee+exit_fee, exit_reason="resolution")`
[base:549-552].

`_resolve_bankroll`:
- Paper [pt:271-277]: `db.get_bankroll() + (shares × exit − exit_fee)` — pure arithmetic.
- Live [lt:685-758]: `exit < 0.99` → `get_balance()` (CLOB `/balance-allowance` USDC)
  [lt:701-704]; win → `_redeem_pending[pos_id]` with `next_check` every
  `_REDEEM_CHECK_EVERY_S` (10 s) [lt:706-714]; `_chain_token_shares(winning token)`
  via the public data API (`sizeThreshold 0.01`) [lt:716; 772-803]; unreadable
  → None (CRITICAL once past 600 s) [lt:717-729]; `held <= 0.01` →
  `update_balance_allowance(COLLATERAL)` then `get_balance()` returned [lt:730-744];
  else None, "Payout Settling" note at 120 s, `PAYOUT STUCK` CRITICAL at 600 s [lt:745-758].

### 6.4 `close_trade` — exit path exists in code, is never invoked

`BaseTrader.close_trade` [base:442-514] sells via `_execute_sell` with a fee
headroom hold-back and credits `_scalp_residual_credit`. Grep evidence over
`polybot/` and `scripts/` excluding tests:

```
grep -rn "close_trade\|_execute_sell\|warm_sell_signature" --include=*.py polybot scripts | grep -v polybot/tests/
```
returns only definitions/docstrings in `base.py`, `live_trader.py`,
`paper_trader.py`. `.close_trade(` has **zero** call sites outside
`polybot/tests/{test_base_trader,test_live_trader,test_paper_trader}.py`.
`_execute_sell` is called only from `close_trade` [base:475].
`warm_sell_signature` [lt:1091-1125; pt:107-130] has no caller in `main.py`
(grep of `main.py` for `warm_sell_signature`: no hits). `_sweep_residual`
[lt:1217-1262] is reachable only from a SELL through `_submit_fok_order`
[lt:1922-1923] or `_settle_unmatched_order` [lt:604] — i.e. only via
`close_trade`. `trader.resolve_position(` is called at [main:1777] and
[main:1915] only. No "exit engine" module exists; the loop comment states it
[main:2324-2325]. The remaining live booking path outside resolution is the
boot-time `reconcile_open` → `_recover_missed_close` [lt:1504-1657], which
replicates the `close_trade` PnL arithmetic against a Gamma/CLOB-inferred exit
and writes `exit_reason = reconcile_recovery_*` with **no** bankroll delta
[lt:1589-1609].

### 6.5 `trade_history` row and the fee constants

Schema [db:54-62] plus additive columns `pnl`, `fees`, `exit_reason`,
`maker_fill`, `maker_rebate`, `position_id` [db:81-99]. Written by
`_close_position_and_history` [db:240-274]:
`(side, entry_price, exit_price, size, exit_timestamp, pnl, fees, exit_reason, position_id, maker_fill, maker_rebate=0.0)`;
`maker_fill = int(bool(trade_context.maker_fill))` parsed from the position's
snapshot at close [db:260-265]. `maker_rebate` is always 0.0 [db:91-94, 273].
`close_position` [db:276-319] takes at most one of `new_bankroll` (absolute,
resolution) / `bankroll_delta` (relative, `close_trade`) and commits
positions-UPDATE + history-INSERT + bankroll write atomically.

`gain_pct = pnl / size` [base:489, 540]; `log_return = ln(exit/entry)` is
telemetry only [base:10-15, 481, 543].

| Constant / fn | Value / formula | Applied at |
|---|---|---|
| `DEFAULT_FEE_RATE` | 0.07 [base:140] | `fetch_fee_rate` return [ms:217]; Kelly `net_b` [se:158]; every fee fn default |
| `EFFECTIVE_FEE_PEAK` | `round(0.07 × 0.25, 5)` = 0.0175 [base:144] | spread gate `effective_cost` only [main:1593] |
| `taker_fee(sh, p)` | `round(rate × sh × p × (1−p), 6)` [base:160-162] | via the two wrappers below |
| `entry_fee_shares(sh, p)` | `taker_fee / p` [base:165-168] | `open_trade` [base:335]; banner fee [main:1360, 195] |
| `exit_fee_usdc(sh, p)` | `taker_fee` [base:171-173] | `resolve_position` [base:537]; paper `_resolve_bankroll` [pt:274]; `close_trade` [base:482]; paper residual credit [pt:269]; sell audit [lt:840-843]; `_recover_missed_close` [lt:1591] |
| `slippage_pct(size, depth, 0.03)` | `f·0.03·(1+f)` [base:146-157] | net-edge gate + pre-submit slip [main:1178, 1203] |
| maker fee | 0 shares [base:415] | `book_maker_fill` |

---

## 7. LIVE vs PAPER — every divergence point

Type key: **D** = decision (what gets ordered), **F** = fill semantics,
**B** = booking, **T** = timing.

| # | Method / point | Live | Paper | Type |
|---|---|---|---|---|
| 1 | Trader construction | `LiveTrader` after `verify_auth`; bankroll set to wallet USDC [main:2462-2478, 2550-2552]; `_boot_order_sweep` cancels all resting orders [main:437-458, 2478] | `PaperTrader` with latency/fail knobs; bankroll seeded to `initial_bankroll` 150 only when the DB reads 0 [main:2445-2446, 2496-2501] | B |
| 2 | Boot reconciliation | `detect_orphan_positions`, `reconcile_open` (may write `reconcile_recovery_*` rows), `reconcile_dust` [main:2558-2577; lt:1504-1657] | none; warns about carried-over open positions [main:2581-2584] | B |
| 3 | `warm_buy_signature` | present; pre-signs the FOK concurrently with preflight [main:1305-1308; lt:1127-1162]; consumed in `_submit_fok_order` [lt:1790-1791] | attribute absent → `hasattr` False, nothing spawned [main:1305] | T |
| 4 | `_execute_buy` pre-check | `_estimate_fok_walk` on a book ≤ 5 s old [lt:1760-1769] | `_precheck_rejects` on a book ≤ 5 s old, same walk [pt:50, 195-244] | F (parallel) |
| 5 | Latency | real sign + POST RTT [lt:1805-1827] | `_simulate_latency` from `_LATENCY_QUANTILES` × 0.95, floor 0.32 s [pt:321-331] | T |
| 6 | Network-fail sim | none (real failures → `_AmbiguousPostError` paths) | `random() < _compute_fail_rate` (0.005-0.03 or 0.03) → unfilled [pt:54-56, 160-193] | F |
| 7 | Retry | up to 3 attempts on `success=False` non-fatal or pre-POST exception [lt:1792-1843, 1984-1993]; **no** retry on 4xx kill or ambiguous POST | one-shot walk, no retry [pt:246-257] | F |
| 8 | Fill price | WS VWAP → balance delta → `associate_trades` → limit [lt:1855-1899] | book-walk VWAP against the current WS book [pt:386-402] | F/B |
| 9 | `fill_size` | `amount` (the requested USDC) [lt:1924] | `spent` from the walk (equals `size_usd` when fully consumed) [pt:402] | B |
| 10 | `_schedule_fill_audit` | present; +8 s wallet audit may `sync_entry_booking` and fires `on_entry_settled` [base:371-373; lt:859-958] | absent | B/T |
| 11 | `on_entry_settled` banner path | attribute set in `main()` [main:2481]; entry logs "FILLED … price settling…", OPEN banner + Discord come from the audit callback [main:1375-1383; 253-277] | attribute absent → OPEN banner + Discord immediately at fill time with `settled="paper"` [main:1384-1391] | T (log only) |
| 12 | `place_gtc_bid` | real POST; returns **None** on rejection/exception with `MAKER BID REJECTED` [lt:643-667] → rung dropped [mb:170-174] | sleeps `_GTC_LATENCY_QUANTILES`, always returns an id [pt:63-73] | D (rung set can differ) |
| 13 | `cancel_gtc` | real cancel; exception propagates to `_retire`'s warning [lt:669-672; mb:310-312] | sleep only [pt:75-81] | T |
| 14 | GTC fill observation | `poll_gtc_fill` at 1 Hz in `maintain` + final poll in `_retire` [mb:283-288, 313-325; lt:674-683] | `on_print` print matcher (strictly-below full / at-price beyond 135 sh) [mb:191-218]; `poll_gtc_fill` → None [pt:83-84] | F |
| 15 | `_resolve_bankroll` | wallet balance; wins wait for on-chain redeem (pending) [lt:685-758] | `bankroll + revenue` arithmetic [pt:271-277] | B/T |
| 16 | `_sellable_shares` | chain balance (cached post-BUY, TTL 300 s) with 0.3×/3× sanity [lt:1025-1073] | DB `fallback_shares` [base:268-271] | B (only via `close_trade`, unreached) |
| 17 | `_scalp_residual_credit` | 0.0 (residual swept on-chain by `_sweep_residual`) [base:273-281; lt:1217-1262] | `residual × price − exit_fee` [pt:263-269] | B (only via `close_trade`, unreached) |
| 18 | `_schedule_sell_audit` | present [lt:805-857] | absent | B (only via `close_trade`, unreached) |
| 19 | `_maybe_recheck_allowance`, `_cache_post_buy_balance`, keepalive, `prewarm_*` | present [lt:411-498, 1075-1085, 1912-1918] | none | T |
| 20 | Fill-stats ledger | `memory/state/fill_stats.json` [lt:253-260] | `fill_stats_paper.json` [pt:259-261] | — |
| 21 | Ladder `paper` flag | `MakerBidManager(paper=False)` — `on_print` is a no-op [mb:200] | `paper=True` | F |

Shared and identical in both modes: everything in §1-§3 (gates, signal, sizing,
limit price), `open_trade` preflight and fee-in-shares booking [base:293-375],
`book_maker_fill` [base:379-438], `resolve_position`'s PnL arithmetic
[base:518-544], `MakerBidManager` placement/maintain/retire logic (except the
poll branch and `on_print`), and the DB layer.

---

## 8. The enforced parity contract — `polybot/tests/test_decision_parity.py`

Fixture: `polybot/tests/fixtures/parity_windows.json.gz` (present), real
60s-era windows (raw/sixty/Binance reports, CLOB BBO changes, prints)
[test:1-11, 48]. Each window is replayed twice — once with `PaperTrader`, once
with a `LiveTrader` whose py-clob client is a `MagicMock` [test:199-273] —
through the production `ChainlinkFeed.ingest_*`, `main._compute_strike`,
`main._evaluate_signal_and_enter`, and `MakerBidManager.maintain`
[test:348-471].

**Asserts** [test:474-497]: trace length equal and every element equal, where
the trace records `skip`/`gateskip` gate names, `strike` + trust flag,
`no_strike`, `signal` (k, action, prob, edge, kelly, side, reason),
`gtc_place` (token, price, shares, placed?), `gtc_cancel`, `book_maker`
(side, vwap, shares), `fok_buy`/`fok_result`, `retire` reason
[test:276-331, 402-437]; end-of-window bankroll equal to 1e-9; and, live
side only, that the wire (`post_order` args) equals the traced intents for
both GTC and FOK [test:215-226, 492-497]. A second test asserts each fixture
window actually places rungs and retires in paper [test:500-510].

**Excludes, by its own docstring and setup** [test:13-25]: latency sleeps
(both sims no-op'd, frozen clock) [test:185-193]; paper's network-fail RNG
(forced 0) [test:181-183]; live's +8 s fill audit (`_schedule_fill_audit = None`)
[test:272]; live's 1 Hz poll cadence (`_last_poll = 0.0` every tick) [test:435-436];
`_record_stats` / `_record_submit_latency` / `_record_gtc_latency` /
`_update_fill_stats` stubbed [test:194-195, 241-247]; `_ws_vwap_since`
replaced by `compute_buy_vwap` on the fake book, `_get_token_balance` → 0,
`_await_buy_settle` no-op, allowance recheck and post-buy balance cache no-ops
[test:249-270]. The fake CLOB serves synthetic 3-level depth from recorded
BBO only [test:87-131]. Live fills are served by a `FillOracle` that
re-implements the paper `on_print` rule (strictly-below full, at-price beyond
`AT_PRICE_QUEUE_SH`) [test:137-162] — so the suite asserts that both traders
make identical decisions **given** the paper fill rule; it does not test the
live exchange's fill behaviour, live GTC rejection (`post_order` always
returns an id) [test:215-222], or timing.

**Config used is not the production config**: `twap_zone_s 30.0`,
`sniper_min_edge 0.05`, `sniper_max_edge 0.30` [test:52-56]; ladder `need`
0.25 instead of the production 1.0 [test:57-65]; a "taker" variant with
`taker_enabled=True, require_max_tier=False, sniper_min_edge=0.02, sniper_max_edge=0.90`
[test:66-70]; `START_BANKROLL 150` [test:71]; `breaker.kelly_multiplier` fixed at 1.0 [test:374].

---

## Appendix — text that disagrees with the executing code (observed while tracing)

- `mb:20-24` docstring: at-price prints "never count"; `on_print` credits
  at-price volume beyond 135 sh [mb:212-218].
- `main:1401` (`_compute_strike` docstring) says "30s-TWAP stream"; the strike
  is written by `ingest_sixty` [cl:378-390].
- `main:911` docstring says "final 30s"; the zone is `twap_zone_s` = 58 [settings.yaml:100].
- `settings.yaml:130` (Section 3d header) says fills stamp `signal_leg="maker_bid"`;
  the code stamps `"deep_proj"` [main:1084].
- `settings.yaml:1-6` and `:133-138` describe a "$400 go-live bankroll" /
  "planned go-live funding ~$125-150" while `initial_bankroll: 150.0`
  [settings.yaml:24] is what seeds a fresh paper DB [main:2445-2446].
- `se:16-33` header says the tables were re-fit 2026-08-27; the P995 knot at
  k=6 is $4.0 and at k=25 is $28.5 in code today.
