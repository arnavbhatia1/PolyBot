# 01d — Tests, money-path coverage, dead code, CI

Phase 1 / track D of the documentation audit. Read-only survey of `polybot/tests/`,
the production import graph, and `.github/workflows/tests.yml` as of `main`
(HEAD `4fc8f847`, 2026-08-27). Description only.

**Method.** `python -m pytest polybot/tests -q --co` collected **481 tests in 33
files** (local interpreter is CPython 3.14 per `__pycache__` tags; CI pins 3.12).
`pytest-cov` is **not installed** (`pip show pytest-cov` → not found) and nothing
was installed, so §2 is a **static** mapping: for every money-path function, the
tests that call it by name (direct) or reach it through a tested caller
(indirect), from a whole-word reference scan of `polybot/tests/*.py` cross-checked
against the test bodies. No line-coverage percentages are available. §3 builds
the import graph from `polybot/main.py` + `scripts/*.py` + `scripts/research/*.py`
and then counts whole-word references to every `def`/`class` name across
production code (`polybot/` minus `tests/`), scripts, and tests separately.
Generic names (`run`, `start`, `flush`, `reset`, …) were resolved by reading the
call sites rather than trusting counts.

---

## 1. Test map

| Test file | Tests | Components covered | Property enforced |
|---|---:|---|---|
| `test_base_trader.py` | 39 | `execution/base.py` via a `StubTrader`: `open_trade`, `close_trade`, `resolve_position`, `book_maker_fill`, `taker_fee`/`entry_fee_shares`/`exit_fee_usdc`, `FillResult`; `db/models.py` | Rejection gates (duplicate market, max positions, bankroll cap incl. deployed); fee math; bankroll deltas use the fill price not the requested price; maker fills keep every share, respect the deployed cap, and are flagged `maker_fill` on the history row [polybot/tests/test_base_trader.py:110-463] |
| `test_boot_sweep.py` | 4 | `main._boot_order_sweep` | Every boot-sweep outcome (nothing carried, N cancelled, refused cancel = ERROR, failed sweep loud) reaches the log at the documented level [test_boot_sweep.py:26-49] |
| `test_chainlink_feed.py` | 46 | `feeds/chainlink_feed.py`: `_record_boundary`, `get_strike`, `boundary_captured`, `strike_reliable`, `_epoch_seconds`, `twap_60`, `running_avg`, `projected_final_twap`, `twap_frozen`, `spot_bridge_delta`, drop counters, `_run` handshake/429 path | First-at/after-boundary strike; payload-clock trust (±0.5s) and delivery-hole veto; ms→s normalisation and out-of-band timestamps cannot evict captures; projection refuses stale spot and >10s raw holes; bridge collapses to the plain projection on every failure mode; stall detector re-arms only on real moves; 429 backs off hard [test_chainlink_feed.py:11-649] |
| `test_circuit_breaker.py` | 39 | `execution/circuit_breaker.py` (every public method + `_locked_tier` indirectly) | Tier floor ratchets up and never down; `kelly_multiplier` concave interpolation to `min_multiplier`; streaks never touch kelly; `reset` keeps drawdown/scaling; `restore_from_peak` [test_circuit_breaker.py:11-395] |
| `test_clob_ws.py` | 20 | `feeds/clob_ws.py`: `_handle_message`/`_dispatch`/`_on_book`/`_on_price_change`/`_on_best_bid_ask`/`_on_last_trade`, `subscribe`/`unsubscribe`, trade buffer, disconnect handling | Book/BBA/price-change/last-trade parsing, feed-delay stamp from exchange ts, per-token isolation, `print_gap` stamped + WARNING on disconnect, unreadable frames counted [test_clob_ws.py:13-272] |
| `test_config.py` | 29 | `config/loader.py`: `load_config`, `get_config`, `get_secret`, `validate_config` | Missing/out-of-range/wrong-type fields reported together; kelly floor; sniper knob ranges; production `settings.yaml` validates [test_config.py:35-199] |
| `test_consistency.py` | 1 | `discord_bot/bot.py::create_bot` vs CLAUDE.md §11 | Every documented Discord command resolves to a registered handler and the in-bot help lists only real commands [test_consistency.py:12] |
| `test_correlation.py` | 6 | `execution/correlation.py`: `concurrent_multiplier`, `estimate_correlation` | Same-market ignored, same-side/other-market deepest discount, worst ρ wins, case-insensitive sides [test_correlation.py:8-34] |
| `test_db.py` | 8 | `db/models.py`: `initialize`, `open_position_and_debit_bankroll`, `get_open_positions`, `close_position`, `has_position_for_market`, `get_open_position_count`, bankroll setters | Tables created; atomic open+debit; close writes the `position_id` link on history [test_db.py:25-87] |
| `test_decision_parity.py` | 9 | `main._compute_strike`, `main._evaluate_signal_and_enter`, `ChainlinkFeed.ingest_raw/ingest_sixty/ingest_binance`, `MakerBidManager.consider_placement/on_print/maintain/_retire/_book`, real `PaperTrader` (latency/fail-rate stubbed) and real `LiveTrader` (MagicMock CLOB client, wire capture), `SignalEngine`, `compute_buy_vwap`; fixture `tests/fixtures/parity_windows.json.gz` (850 KB, 4 recorded 60s-era windows) | Paper and live produce bit-identical gates, signals, sizing and order intents per window×variant; wire == intents; the replay exercises the ladder [test_decision_parity.py:477-502] |
| `test_ghost_tracker.py` | 6 | `agents/ghost_tracker.py` | Rollup skips the current ET day and unresolved ghosts; per-gate dedup; `watched_markets`; resolution persists per gate [test_ghost_tracker.py:28-99] |
| `test_hot_mirror.py` | 8 | `db/models.py` hot mirror (`preflight_peek`, `has_open_or_pending_market`, `open_or_pending_count`, `_rebuild_hot_mirror` via `initialize`), `main._pregate_should_eval`, `main._twap_hot` | Mirror equals SQL through every transition and after reconnect; pre-gate never throttles a fire-adjacent wake, throttles cold in-zone ticks, 1 Hz outside zone; `_twap_hot` fires at 90 % of margin and survives cold inputs [test_hot_mirror.py:27-129] |
| `test_integration.py` | 1 | `PaperTrader.open_trade` → `resolve_position` with `SignalEngine` + `Database` | One full paper trade books a $1 win [test_integration.py:17] |
| `test_integration_fixes.py` | 6 | Static source assertions on `main.py`, `live_trader.py`, `scheduler.py`, `settings.yaml` (no runtime) | Orphan-file strings point to `memory/state/`; exactly one background `flush_gate_stats` inside `_record_outcome`; bybit gone; paper boot WARNs about carried positions [test_integration_fixes.py:13-64] |
| `test_late_sniper.py` | 22 | `core/signal_engine.py`: `evaluate_twap_lock`, `twap_margin`, `_kelly`, `TWAP_MARGIN_P995/_MAX`; `config/loader` `trading_enabled`/`sniper_enabled` alias; AST checks on `main._evaluate_signal_and_enter` | Max-tier-only gate (p99.5 tier refused, beyond-MAX fires); knot interpolation exact/linear/clamped/monotone; ask cap derives from the edge floor; kelly on market-anchored prob; k-floor and zone gates; `phase` set before any `_ghost`; open-position read precedes `consider_placement` [test_late_sniper.py:20-257] |
| `test_latency_swr.py` | 4 | `main._get_contract_prices`/`_fetch_contract_prices`, `BTCMarketScanner.find_active_contract` stale-while-revalidate | Stale served instantly with background refresh; blocks only when no servable cache [test_latency_swr.py:38-95] |
| `test_live_health_read.py` | 22 | `scripts/analyze_late_window.py` (`live_health_read`, `mechanism_read`, loaded via `spec_from_file_location` [test_live_health_read.py:19]); `recording.MicroTape.on_twap30_report` | Per-fill net = pnl/shares_held, equal-weight then day-clustered; kill rule: one `lock_dip` loss trips, trailing-4d dollars < 0 trips only with ≥4 ET days and ≥5 fills, per-leg breakdown, calendar-day window, `position_id` join; `mechanism_read` states the t3 record count [test_live_health_read.py:59-330] |
| `test_live_trader.py` | 46 | `execution/live_trader.py`: `__init__`/creds, `open_trade`→`_submit_fok_order` (retries, sign exceptions, unmatched→`_settle_unmatched_order`, FOK kill), `_get_fill_price(_ex)`, `close_trade`, `resolve_position`→`_resolve_bankroll`, `detect_orphan_positions`, `reconcile_dust`, `prewarm_http`, `_audit_entry_fill`, `_audit_sell_fill`/`_schedule_sell_audit`, `warm_buy_signature`/`_await_buy_warmup_inflight`, `place_gtc_bid` rejection log, HTTP singleton | Only provably-unposted failures retry; a FOK kill is a definitive no-fill; unmatched orders are cancelled not resubmitted; +8s audit corrects to chain truth; orphan detection fails closed; winner past deadline stays pending; unrestable rung logs `MAKER BID REJECTED` [test_live_trader.py:37-1047] |
| `test_main_shutdown.py` | 3 | `main._make_sigint_handler`; static check of `main()` | First Ctrl-C raises, repeat force-quits; the shutdown watchdog is armed before anything that can hang [test_main_shutdown.py:7-33] |
| `test_maker_bid.py` | 28 | `execution/maker_bid.py`: `consider_placement`, `on_print`, `maintain`, `_retire`, `_book`, `certain_winner`, `legal_price`, `min_need`, `resting_on`, `holding_tokens`, `MAKER_LADDER_PATH` override; `PaperTrader._simulate_gtc_latency` (1 test). Uses `FakeTrader`/`FakeChainlink` stubs [test_maker_bid.py:24-71] | Full ladder places; starved budget skips the top rung only; sign below need places nothing; fill rule (strictly-below full, at-price beyond 135 sh, above never); noise/flip/cold cancels everything; post-close hold gated on `certain_winner` and fails closed on a hole; `_retire` under cancellation or poll failure still books; `print_gap` stamp; booked notional == rung sum; nothing writes the ladder file [test_maker_bid.py:88-424] |
| `test_market_scanner.py` | 31 | `feeds/market_scanner.py`: `parse_contract`, `in_entry_window`, `_make_slug`, `clob_best_ask`, `snap_to_tick`, `get_spread`, `gamma_events_by_slug` | Gamma slug fallback semantics (legacy ok/empty, HTTP error→fallback, 404 = no event, 429/5xx fail fast, redirect sunset, latch after enforcement); tick snapping rounds down and clamps [test_market_scanner.py:19-246] |
| `test_mirror_invariants.py` | 4 | Cross-module constant pairs: `sniper_min_edge` vs engine floor, `EFFECTIVE_FEE_PEAK` = rate/4, paper realism defaults vs `settings.yaml`, `PaperTrader._precheck_rejects` vs `LiveTrader._estimate_fok_walk` | Documented mirrored pairs have not drifted [test_mirror_invariants.py:23-48] |
| `test_ops_watch.py` | 14 | `main._latency_watch`, `_gtc_watch` (+`_gtc_table_ks`), `_ops_watch_line`; `live_trader._record_gtc_latency`; `PaperTrader` GTC table | Thin/stale/missing samples are named, not silent; ±25 % drift warns; queue watch warns on shrink and growth; GTC watch dark until samples; KS flags a shape drift p50 misses [test_ops_watch.py:22-147] |
| `test_outcome_reviewer.py` | 10 | `agents/outcome_reviewer.py` | Record file content, correctness flag, `exit_reason` default; rollup skips current day; dedup by (position, market) and legacy position-only [test_outcome_reviewer.py:18-105] |
| `test_paper_trader.py` | 15 | `execution/paper_trader.py`: `open_trade`→`_execute_buy`/`_precheck_rejects`/`_retry_walk`/`_walk_book`/`_compute_fail_rate`, `close_trade` (+`_scalp_residual_credit`), `resolve_position`→`_resolve_bankroll`, `_draw_latency`, `_record_stats` schema | Bankroll deltas, fee-in-shares PnL, retry-walk is one-shot, `None` book rejects without raising, latency drawn from the live empirical table, `fill_stats_paper.json` schema == live [test_paper_trader.py:32-254] |
| `test_paths.py` | 3 | `paths.py` STATE_DIR layout, `fold_gate_day` | Rolling state lives under `memory/state/`; gate-day accumulator folds and no-ops on empty [test_paths.py:7-25] |
| `test_recording.py` | 21 | `recording.py`: `WindowPathRecorder` (`ensure_tables`, `_sample`, `_flush`, label write, `strike_trusted`), `TapeRecorder`, `MicroTape` (phase gating, schema, bz/t/t3/l records, never raises), `compress_recordings_job`, `recordings_cleanup_job`, `_check_resolution_source`, `_top3_usd`; `scripts/analyze_twap_lock._open_tape` (file-loaded [test_recording.py:370]) | Full-capture columns, NULL on cold inputs, gz-first compression of finished days within budget, replay reads `.jsonl.gz`; SOURCE gate: unparseable slug is an ERROR and counted unchecked, missing captures counted, numeric slug mismatch fires [test_recording.py:49-428] |
| `test_resolution.py` | 15 | `main._resolved_exit_price`; `main._manage_orphaned_position` (with `_get_contract_prices` monkeypatched to None) | Oracle-first binary payoff, tie → Up, coherent book fallback pays 1/0 not book price, incoherent/mid/unclosed book waits, oracle beats book with a logged disagreement; orphan waits on an untrusted boundary capture and resolves on a trusted one [test_resolution.py:22-155] |
| `test_scheduler.py` | 1 | `agents/scheduler.py::run_daily_pipeline` job budget | An overrunning job is logged as ABANDONED, not skipped [test_scheduler.py:20] |
| `test_sizing_chain.py` | 6 | `SignalEngine._kelly` | Positive for a real edge, zero for none/negative, scales with edge, safe at extreme prices [test_sizing_chain.py:12-46] |
| `test_staleness.py` | 4 | `feeds/_staleness.py::StalenessTracker` | Snapshot distinguishes connected-quiet from never-connected; `n_total` survives `reset`; percentiles present with gaps [test_staleness.py:10-49] |
| `test_strike_source.py` | 6 | `main._compute_strike`, `main._strike_trusted` | Gamma `price_to_beat` wins / bootstraps / settles over a seeded Chainlink strike; Chainlink carries when Gamma absent; a delivery hole is untrusted; neither source → None [test_strike_source.py:48-85] |
| `test_ws1_freeze.py` | 4 | `scripts/research/ws1_interval_max.py`, `ws1_freeze_tables.py` (file-loaded [test_ws1_freeze.py:20]); `signal_engine.TWAP_MARGIN_*` | Knots bound both adjacent intervals, monotone and rounded up; the freeze script sources MAX from interval maxima; the engine tables equal the frozen values [test_ws1_freeze.py:27-58] |
| **Total** | **481** | | |

Fixtures: `conftest.py` sets `POLYBOT_MEMORY_DIR` to a temp dir before any import
[conftest.py:11-12] and provides `sample_config`/`loaded_config` [conftest.py:70-85];
`tests/fixtures/parity_windows.json.gz` is the only data fixture. No `pytest.ini` /
`pyproject.toml`; async tests use `pytest-asyncio` markers per file.

Three files load production scripts by path rather than import: `test_live_health_read.py`
(`scripts/analyze_late_window.py`), `test_recording.py::test_replay_loader_reads_gzipped_tape`
(`scripts/analyze_twap_lock.py`), `test_ws1_freeze.py` (`scripts/research/ws1_*.py`). Four
files assert on source text/AST instead of behaviour: `test_integration_fixes.py`,
`test_mirror_invariants.py`, `test_late_sniper.py:20,257`, `test_main_shutdown.py:33`.

---

## 2. Money-path coverage (static mapping — no `pytest-cov`)

Legend: **direct** = a test calls the function by name; **indirect** = reached only
through a tested caller; **ZERO** = no test reaches it. Line refs are to the
definition.

### 2.1 `polybot/main.py`

| Function | Covered by | Status |
|---|---|---|
| `_evaluate_signal_and_enter` [main.py:895] | direct: `test_decision_parity::test_paper_and_live_decide_identically`, `::test_replay_exercises_the_ladder` (real call over 4 recorded windows, both traders) [test_decision_parity.py:47 of `_replay`]; static AST: `test_late_sniper::test_phase_assigned_before_any_ghost_call` [20], `::test_ladder_checks_open_positions_before_placing` [257] | Covered by replay. Which internal branches the fixture windows reach (the `taker_dormant` rewrite [1027-1045], the `lock_dip` fire path [1095-1110], the taker sizing gates [1135-1300], `open_trade` [1310]) is not determinable without line coverage; no unit test drives the taker fire path in isolation. |
| `_compute_strike` [1395] | direct: `test_strike_source` (6) [48-85], `test_decision_parity` [33] | Covered |
| `_resolve_expired_position` [1719] | — | **ZERO COVERAGE** (its helper `_resolved_exit_price` is tested; the booking tail — `mark_pending_resolution`, `trader.resolve_position` [1777], day stats, breaker win/loss [1806-1807], `_record_outcome` [1810], TAPE VERDICT/RESOLUTION DRIFT logging [1739-1741] — is never exercised) |
| `_resolved_exit_price` [1673] | direct: `test_resolution` 13 tests [22-93] | Covered |
| `_manage_orphaned_position` [1823] | direct: `test_resolution::test_orphan_waits_on_an_untrusted_boundary_capture` [148], `::test_orphan_resolves_on_a_trusted_boundary_capture` [155] (stub trader returns `pending=True`) | Covered for the boundary-trust decision only; the booking tail past `resolve_position` [1915-1948] is not reached |
| `trading_loop` [2025] / `_entry_pass` [2119] | — | **ZERO COVERAGE**. Also ZERO: `_check_trading_schedule` [1958], `_fetch_market_prices` [1459], `_discover_contract_and_subscribe` [1610], `_check_ghosts` [2005]. Only the loop's pre-gate helpers are tested: `_pregate_should_eval` [389], `_twap_hot` [405] (`test_hot_mirror` [73-129]). |

### 2.2 `polybot/core/signal_engine.py`

| Function | Covered by | Status |
|---|---|---|
| `evaluate_twap_lock` [84] | direct: `test_late_sniper` [63-232] (13 tests), `test_integration` [17], `test_decision_parity` | Covered |
| `_kelly` [151] | direct: `test_sizing_chain` (6) [12-46], `test_late_sniper::test_kelly_sized_on_market_anchored_prob_not_tier_prob` [209] | Covered |
| `twap_margin` [53] + `TWAP_MARGIN_P995/_MAX` | direct: `test_late_sniper` [102-115], `test_hot_mirror` [129], `test_ws1_freeze::test_engine_tables_untouched` [58] | Covered |

### 2.3 `polybot/feeds/chainlink_feed.py`

| Function | Covered by | Status |
|---|---|---|
| `projected_final_twap` [219] | direct: `test_chainlink_feed` [336-395, 649] | Covered |
| `running_avg` [151] | direct: `::test_running_avg_accepts_anchor_shortly_after_start` [325]; indirect via projection tests | Covered |
| `spot_bridge_delta` [187] | direct: `TestSpotBridge` (10) [579-649] | Covered |
| `_record_boundary` [417] | direct: [29-58, 123-158] | Covered |
| `strike_reliable` [299] | direct: [58-103] (5); indirect via `test_strike_source` | Covered |
| `ingest_raw` [397] / `ingest_sixty` [378] / `ingest_binance` [357] | direct: `test_decision_parity` only [`_replay` lines 69-75] | Covered by parity only; no unit test |
| `twap_frozen` [261] | direct: `TestTwapFrozen` (8) [506-558] | Covered |
| `get_strike` [139], `boundary_captured` [292], `_epoch_seconds` [327] | direct: `test_chainlink_feed` | Covered |

### 2.4 `polybot/execution/maker_bid.py` (`MakerBidManager`)

All tests in `test_maker_bid.py` use `FakeTrader`/`FakeChainlink` [24-71]; the manager
code itself is real. Trader-side GTC methods are covered in §2.6/§2.7.

| Method | Covered by | Status |
|---|---|---|
| `consider_placement` [138] | direct: `_place` helper → tests [88-166, 297-311]; parity | Covered |
| `on_print` [191] | direct: [128, 311, 386]; parity | Covered |
| `maintain` [244] | direct: [179-286] | Covered |
| `_retire` [298] | direct: [332-411]; indirect via `maintain` cancels | Covered |
| `_book` [333] | indirect via `_retire` tests [350, 370, 386, 411] | Covered (indirect) |
| `certain_winner` [230] | indirect via post-close tests [238-277] | Covered (indirect) |
| `legal_price` [83] | direct: `::test_price_clamped_to_the_exchange_range` [145], `::test_rungs_below_the_exchange_min_size_are_skipped` [158] | Covered |
| `min_need` [222], `resting_on` [103], `holding_tokens` [106] | direct | Covered |

### 2.5 `polybot/execution/base.py` (`BaseTrader` + fee helpers)

| Function | Covered by | Status |
|---|---|---|
| `open_trade` [293] | direct: `test_base_trader` [153-256] (13), `test_paper_trader`, `test_live_trader`, `test_integration`, parity | Covered |
| `book_maker_fill` [379] | direct: `test_base_trader` [418-463] (4), parity, `test_late_sniper` (AST) | Covered |
| `close_trade` [442] | direct: `test_base_trader::TestCloseTrade` (9) [257-354], `test_live_trader` [365-385, 959-1045], `test_paper_trader` [93-131] | Covered — **but has no production caller** (§3.1) |
| `resolve_position` [518] | direct: `test_base_trader::TestResolvePosition` (5) [355-405], `test_live_trader` [397-587], `test_paper_trader` [157], `test_integration` [38] | Covered |
| `taker_fee` [160], `entry_fee_shares` [165], `exit_fee_usdc` [171] | direct: `test_base_trader::TestFeeMath` (6) [123-152] | Covered |
| `_entry_fee_usd_from_position` [213] | indirect via `close_trade`/`resolve_position` tests | Covered (indirect) |
| `compute_buy_vwap` [176] | direct: parity, `test_mirror_invariants` | Covered |
| `slippage_pct` [146] | — | **ZERO** (called from `main.py:1178,1203`) |
| `update_fill_stats` [59] / `categorize_failure` [39] | indirect: `test_paper_trader::test_paper_writes_fill_stats_same_schema_as_live` [254] | Covered (indirect) |

### 2.6 `polybot/execution/live_trader.py` (`LiveTrader`)

| Method | Covered by | Status |
|---|---|---|
| `_submit_fok_order` [1728] | direct: `::test_submit_awaits_inflight_warmup_instead_of_double_signing` [919]; indirect via `open_trade` tests [126-335]; parity live variant | Covered on the **BUY** side. The **SELL** branches [1787-1788, 1919-1923] are ZERO (no test sells; see §3.1) |
| `_execute_buy` [500] | indirect via `open_trade` tests | Covered |
| `place_gtc_bid` [643] | direct: `::test_unrestable_rung_logs_the_documented_rejection` [1047]; parity live variant (`test_replay_exercises_the_ladder`) | Covered |
| `cancel_gtc` [669], `poll_gtc_fill` [674] | parity live variant only (MagicMock `cancel_orders`/`get_order` [test_decision_parity.py:231-232]) | Covered by parity only |
| `_resolve_bankroll` [685] | indirect via `resolve_position` tests: winner, redeemed-before-first-check, deadline stays pending, chain API failure, loser [397-587] | Covered |
| `_settle_unmatched_order` [562] | indirect: `::test_open_trade_unmatched_status_cancels_instead_of_retrying` [311] | Covered (indirect, BUY only) |
| `_ws_vwap_since` [528] | — parity **replaces** it with a stub [test_decision_parity.py:258-260]; no live-trader test attaches a CLOB WS (`set_clob_ws` appears only in `test_paper_trader` and parity) | **ZERO** (real body never runs) |
| `warm_buy_signature` [1127] | direct: [919] | Covered |
| `_audit_entry_fill` [869] / `_audit_entry_fill_inner` [894] / `_schedule_fill_audit` [859] | direct: [501, 779-919] (5) | Covered |
| `detect_orphan_positions` [1325] | direct: [638-744] (10) | Covered |
| `reconcile_dust` [1264] | direct: `::test_reconcile_dust_skips_resolved_markets` [538] | Covered |
| `reconcile_open` [1504], `_recover_missed_close` [1562], `_gamma_recovery_exit_price` [1659], `_infer_recovery_exit_price` [1699] | — | **ZERO** (live boot path, `main.py:2571`) |
| `verify_auth` [316], `_create_clob_client` [261], `_get_balance_and_allowance_usd` [297] | — patched in every test | **ZERO** |
| `_sweep_residual` [1217] | referenced once in the `reconcile_dust` test; the network body is not exercised | Effectively ZERO |
| `_audit_sell_fill` [815] / `_schedule_sell_audit` [805] | direct: [959-1045] (3) | Covered — production-unreachable (§3.1) |
| `_estimate_fok_walk` [962] | `test_mirror_invariants` (source comparison), indirect via `open_trade` when a WS book is present | Covered (weak) |
| `prewarm_http` [411] | direct [760-770] | Covered |

### 2.7 `polybot/execution/paper_trader.py` (`PaperTrader`)

| Method | Covered by | Status |
|---|---|---|
| `_execute_buy` [44] | indirect: `test_paper_trader` [32-92, 205-238], parity | Covered |
| `_precheck_rejects` [195], `_retry_walk` [246], `_walk_book` [333], `_compute_fail_rate` [160] | indirect via `_execute_buy` tests [205-238]; `_precheck_rejects` source compared in `test_mirror_invariants` [48] | Covered (indirect) |
| `place_gtc_bid` [63], `cancel_gtc` [75], `poll_gtc_fill` [83] | parity paper variant; `_simulate_gtc_latency` [301] direct in `test_maker_bid::test_gtc_placement_pays_the_measured_post_rtt` [166] | Covered |
| `_simulate_latency` [321], `_draw_latency` [312] | direct: `TestRealismShim::test_latency_drawn_from_live_empirical_distribution` [240] | Covered |
| `_resolve_bankroll` [271] | indirect: `test_paper_trader::test_pnl_realistic_with_fee_in_shares` [147], `test_integration` | Covered |
| `_record_stats` [260] | direct: `TestRealismShim::test_paper_writes_fill_stats_same_schema_as_live` [254] | Covered |
| `_execute_sell` [86], `_scalp_residual_credit` [263] | indirect via `close_trade`: `::test_close_trade_updates_bankroll` [93], `::test_scalp_residual_credited_to_bankroll_not_pnl` [105] | Covered — production-unreachable (§3.1) |
| `warm_sell_signature` [107], `_take_sell_warmup` [132] | — | ZERO, and production-unreachable (§3.1) |

### 2.8 `polybot/execution/circuit_breaker.py`

Every method (`drawdown_pct` [76], `kelly_multiplier` [84], `update_bankroll` [100],
`restore_from_peak` [115], `record_win` [130], `record_loss` [141], `reset` [156]) is
directly covered by `test_circuit_breaker.py` (39 tests); `_locked_tier` [21] indirectly
via construction and `update_bankroll`. Covered.

### 2.9 `polybot/recording.py::_check_resolution_source` [325]

Direct: `test_recording::test_unparseable_slug_is_loud_and_counted` [402],
`::test_missing_captures_are_counted_unchecked` [414],
`::test_numeric_slug_still_fires_the_mismatch` [421]. Covered. Its only production
caller `_label_pass` [294] and the `on_source_mismatch` handler in `main.py:2679`
(the in-process `trading_enabled = False` flip) are ZERO.

### 2.10 `polybot/db/models.py`

| Method | Covered by | Status |
|---|---|---|
| `open_position_and_debit_bankroll` [174] | direct: `test_db` [33], `test_hot_mirror` [27-93], `test_live_trader` | Covered |
| `close_position` [276] (+ `_close_position_and_history` [240]) | direct: `test_db` [51, 87], `test_hot_mirror`, `test_live_trader` | Covered |
| `preflight_peek` [135] | direct: `test_hot_mirror` [27, 58, 93] | Covered |

### 2.11 Summary of money-path gaps (ZERO or effectively zero)

`main.trading_loop`/`_entry_pass`, `main._resolve_expired_position`,
`main._check_trading_schedule`, `main._fetch_market_prices`,
`main._discover_contract_and_subscribe`, `main._check_ghosts`,
`main._on_source_mismatch` (in-process brake), `recording._label_pass`,
`base.slippage_pct`, `LiveTrader._ws_vwap_since`, `LiveTrader.reconcile_open` and
its three recovery helpers, `LiveTrader.verify_auth`/`_create_clob_client`,
`LiveTrader._sweep_residual` body, `_submit_fok_order` SELL branches,
`ChainlinkFeed.ingest_*` outside parity, `LiveTrader.cancel_gtc`/`poll_gtc_fill`
outside parity. No per-line percentages are available without `pytest-cov`.

---

## 3. Dead code

### 3.0 Reachability graph

Entry points: `python -m polybot.main` (`main()` [main.py:2431]; `--run-pipeline` →
`run_pipeline()` [2375]) via `scripts/run_polybot.sh:35` (systemd unit
`scripts/polybot.service:17`), and the standalone scripts.

`main.py` imports [29-57]: `config.loader`, `paths`, `execution.base`, `db.models`,
`feeds.market_scanner`, `feeds.clob_ws`, `core.signal_engine`, `execution.paper_trader`,
`execution.live_trader`, `agents.outcome_reviewer`, `agents.scheduler`,
`agents.ghost_tracker`, `discord_bot.bot`, `discord_bot.alerts`,
`execution.circuit_breaker`, `execution.correlation`, `feeds._staleness`,
`agents.pipeline_analytics`; lazily `feeds.chainlink_feed` [2611], `recording`
[2645, 2706], `execution.maker_bid` [2646]. Transitively `feeds._json`, `feeds._socket`
(from `chainlink_feed`/`clob_ws`). `main()` also loads by file
`scripts/analyze_twap_lock.py` [2722, 2751] and `scripts/analyze_late_window.py` [2740]
and calls `ladder_recalibrate`, `health_read`, `live_health_read`, `mechanism_read`,
`queue_depth_read`, `resolution_snapshot_read`.

Scripts → package: `analyze_twap_lock` → `core.signal_engine` [scripts/analyze_twap_lock.py:41];
`smoke_gtc_test`, `smoke_order_test`, `verify_keys` → `execution.live_trader`
(`LiveTrader`, `_create_clob_client`, `verify_auth`); `sniper_shadow_status` →
`config.loader` + file-loads `analyze_late_window`; `reset_paper_clean` standalone;
`scripts/research/ws2_regime.py:24`, `ws3_dips.py:25`, `ws4_k25.py:76` →
`core.signal_engine`; the other 46 research scripts import nothing from `polybot`.

**Every module under `polybot/` is reachable from `main()` except `polybot/indicators/`
and `polybot/tests/`.**

### 3.1 DEAD — unreachable from any entry point

| Item | Location | Evidence |
|---|---|---|
| `polybot/indicators/` package | `polybot/indicators/__init__.py` (one comment line: `# (empty)`) | Not imported anywhere in `polybot/` or `scripts/` |
| **`BaseTrader.close_trade`** and the whole sell/scalp chain beneath it | `execution/base.py:442` | Zero call sites in production code; the only textual mentions are docstrings/comments (`base.py:286,440`, `live_trader.py:808,1026,1589`). The loop comment states there is no exit engine [main.py:2324-2325]. Reachable **only from tests** (`test_base_trader`, `test_paper_trader`, `test_live_trader`). |
| ↳ `BaseTrader._execute_sell` (abstract) / `LiveTrader._execute_sell` / `PaperTrader._execute_sell` | `base.py:262`, `live_trader.py:632`, `paper_trader.py:86` | Only caller is `close_trade` [base.py:475] |
| ↳ `BaseTrader._sellable_shares` / `LiveTrader._sellable_shares` | `base.py:268`, `live_trader.py:1025` | Only caller `close_trade` [base.py:464] |
| ↳ `BaseTrader._scalp_residual_credit` / `PaperTrader._scalp_residual_credit` | `base.py:273`, `paper_trader.py:263` | Only caller `close_trade` [base.py:495] |
| ↳ `LiveTrader._schedule_sell_audit` → `_audit_sell_fill` → `Database.correct_sell_fill` → `on_exit_corrected` callback | `live_trader.py:805, 815, 850`; `db/models.py:343`; `main.py:2483-2492` (`_on_exit_corrected`, assigned but can never fire) | Chain roots at `close_trade` [base.py:507] |
| ↳ `_submit_fok_order` SELL branches; `_settle_unmatched_order` SELL branch | `live_trader.py:1787-1788` (`_take_sell_warmup`), `1919-1923` (`_invalidate_balance_cache`, `_sweep_residual` on SELL); `602-604` | `_submit_fok_order` is called with `SELL` only from `_execute_sell` [live_trader.py:637] |
| ↳ `LiveTrader._take_sell_warmup`, `LiveTrader._invalidate_balance_cache` | `live_trader.py:1197`, `1087` | Called only inside the SELL branches above |
| `LiveTrader.warm_sell_signature`, `PaperTrader.warm_sell_signature`, `PaperTrader._take_sell_warmup`, `PaperTrader._SELL_WARMUP_*` | `live_trader.py:1091`, `paper_trader.py:107, 132` | No caller anywhere (the docstring's "main.py calls this on HOLD ticks" [live_trader.py:1093] has no counterpart in `main.py`); paper `_take_sell_warmup` is called only from paper `_execute_sell` [paper_trader.py:98] |
| `BTCMarketScanner.fetch_market_price` | `feeds/market_scanner.py:263` | No reference in production, scripts, or tests |
| Regime-Kelly shadow constants `_REGIME_CUTS_ATR_REGIME`, `_REGIME_CUTS_ATR_SHORT`, `_REGIME_CUTS_FRV`, `_REGIME_BURST_HOT_RATIO`, `_REGIME_MULT_TABLE`, `_REGIME_MULT_CLAMP` | `main.py:165-170` | Defined under a "SHADOW stamps" banner [159-164]; no stamping code reads any of them (zero references outside the definitions) |
| `_AUX_FRESH_S_COINBASE`, `_AUX_FRESH_S_TRADES` | `main.py:124-125` | Never read |
| `_last_hold_log` | `main.py:308` | Never read (`_last_resolve_wait_log` on the next line is used) |
| `_last_adverse_skip_log_window` | `main.py:362` | Never read; no adverse-selection module exists |
| `paths.ADVERSE_STATE_PATH` | `paths.py:35` | No reader or writer anywhere |
| `validate_config` branch for `late_window.scar_enforce` | `config/loader.py:137-140` | The key is absent from `settings.yaml` and read nowhere else; the branch can only fire on a hand-added key |
| Orphan state files (tracked in git, no reader/writer in code) | `polybot/memory/state/adverse_state.json` (mtime 08-10), `cf_watchlist.json` (08-10), `scar_gates.json` (08-03), `sprt_burst.json` (07-26) | No code path references `cf_watchlist`, `scar_gates`, `sprt_burst`; `adverse_state` only via the dead constant above |
| Untracked/ignored stray files in `polybot/memory/` | `backfill_wallets.err`, `box_arb.jsonl`, `box_arb_monitor.err`, `late_collector.log`, `micro_2026-08-07.jsonl.gz` (at `memory/` root, not `recordings/`) | No code or doc references; not in `git ls-files` |
| Stale bytecode for deleted modules (local only, `__pycache__/` is gitignored) | `polybot/core/__pycache__/scar_scan.cpython-314.pyc`, `sprt.cpython-314.pyc`; `polybot/feeds/__pycache__/coinbase_feed.cpython-314.pyc`; `polybot/tests/__pycache__/test_scar_scan…`, `test_sprt_and_shadow…`, `test_coinbase_feed…` | No corresponding `.py`; `git ls-files | grep __pycache__` = 0 |

Dead-by-design **columns** (schema LIVE, writer always NULL): `window_paths.coinbase_price`
[recording.py:116], `binance_price`, `binance_cvd_10s/30s`, `atr`, `model_prob_up`
[151-155], `coinbase_bid/ask/cvd_10s/30s` [162-165], `depth20_*` [170]; the sampler
hard-codes `None` with comments "feed deleted; the column records NULL by design"
[recording.py:382, 406, 420, 423].

### 3.2 TEST-ONLY — production-unreachable but exercised by tests

| Item | Location | Tests |
|---|---|---|
| `Database.get_open_position_count` | `db/models.py:406` | `test_db.py:71` |
| `loader.get_config` | `config/loader.py:171` | `test_config.py:49` |
| `ChainlinkFeed.twap_60` | `feeds/chainlink_feed.py:178` | `test_chainlink_feed.py:307-318` |
| `BTCMarketScanner.in_entry_window` | `feeds/market_scanner.py:182` | `test_market_scanner.py:37-43` |
| `GhostTracker.load_all` | `agents/ghost_tracker.py:148` | `test_ghost_tracker.py:57` |
| `OutcomeReviewer.load_all_outcomes` | `agents/outcome_reviewer.py:81` | `test_outcome_reviewer.py:50, 96, 105` |
| The `close_trade` chain (§3.1) | as above | `test_base_trader.py:257-354`, `test_paper_trader.py:93-131`, `test_live_trader.py:365-385, 959-1045` |

### 3.3 DORMANT — reachable only behind a disabled config flag

Flag: **`late_window.taker_enabled: false`** [`config/settings.yaml:72`]. At
`main.py:1033-1040` any non-SKIP result of `evaluate_twap_lock` is rewritten to
`SKIP` when the flag is false, and the function returns before any taker code at
`main.py:1122-1126`. `trader.open_trade` [main.py:1310] is the **only** production
call of `open_trade` (the ladder books through `book_maker_fill`
[maker_bid.py:349]). Everything below is therefore dormant while the flag is false:

| Item | Location |
|---|---|
| `lock_dip` leg attribution and TWAP LOCK log | `main.py:1095-1110` |
| Taker gates/sizing: `edge_cap`, `kelly_multiplier`, `concurrent_multiplier` (→ `execution/correlation.py` entirely), `max_book_fill_pct`, `slippage_pct`, `min_size`, pre-submit VWAP/edge drift checks | `main.py:1135-1300`; `correlation.py:19-58`; `base.py:146` |
| `_ghost()` and `GhostTracker.record_rejection` (every `_ghost` call site is after the 1122 gate) | `main.py:931-964`, `1135, 1168, 1182, 1192, 1287, 1296`; `ghost_tracker.py:39` |
| `warm_buy_signature`, `open_trade` → `_execute_buy` → `LiveTrader._submit_fok_order` (BUY) / `PaperTrader._execute_buy` + `_precheck_rejects` / `_retry_walk` / `_walk_book` / `_compute_fail_rate` / `_simulate_latency` / `_draw_latency` | `main.py:1305-1310`; `base.py:293`; `live_trader.py:500, 1127, 1728`; `paper_trader.py:44-63, 160-260, 312-402` |
| Live BUY settle helpers: `_await_buy_settle`, `_ws_vwap_since`, `_settle_unmatched_order`, `_cache_post_buy_balance`, `_await_buy_warmup_inflight`, `_take_buy_warmup`, `_schedule_fill_audit` → `_audit_entry_fill(_inner)` → `Database.sync_entry_booking`, `on_entry_settled` → `main._on_entry_settled` → `_log_open_banner` / `_realized_entry_fee` | `live_trader.py:507, 528, 562, 1075, 1164, 1183, 859-960`; `db/models.py:321`; `main.py:184, 198, 253` |
| `_record_killed_ask`, `alerts.send_trade_opened` for taker fills, `_window_recorder.mark_traded` | `main.py:691/1329, 1387, 1357` |
| `require_max_tier: true` [settings.yaml:99] additionally keeps the p99.5-tier fire path inside `evaluate_twap_lock` dormant | `signal_engine.py:84-150` |

Other flag-gated items (currently **enabled**, so LIVE): `maker.maker_bid_enabled: true`
[settings.yaml:139] gates `MakerBidManager` construction [main.py:2650-2652];
`late_window.trading_enabled: true` [settings.yaml:68] gates the whole signal block
[main.py:1003], the Chainlink wake [2212-2213] and `_sniper_health_job` [2738].

### 3.4 LIVE — items the brief asked to check that are wired

| Item | Wiring |
|---|---|
| `agents/outcome_reviewer.py` | Constructed [main.py:2530, 2382]; `record_outcome` on every resolution via `_record_outcome` [main.py:861-879 ← 1810, 1948]; `rollup_old_outcomes` in `scheduler.run_daily_pipeline` [scheduler.py:52]; passed to `reconcile_open` [main.py:2571] |
| `agents/ghost_tracker.py` | Constructed [main.py:2531]; `check_resolutions` via `_check_ghosts` every 30 s [main.py:2005-2020, 2335] (no-op while `watched_markets` is empty); `rollup_old_ghosts` nightly [scheduler.py:53]. `record_rejection` itself is DORMANT (§3.3). Writes `memory/ghost_outcomes/` [ghost_tracker.py:26] — directory does not currently exist on disk |
| `agents/scheduler.py`, `agents/pipeline_analytics.py` | Background tasks [main.py:3027-3028]; `slug_to_window` used by `main`, `bot.py`, `market_scanner` |
| Scar / adverse-selection / SPRT machinery | **No source modules exist**; remnants are the dead constants, the `scar_enforce` validation branch, the orphan state files, stale `.pyc`s (§3.1), and three comments (`main.py:1207, 2234`; `paper_trader.py:251`) |
| Coinbase/Binance/Bybit remnants | Binance ring in `chainlink_feed.py` [95-101, 187-216, 357-371] is LIVE (bridge delta; RTDS `crypto_prices` relay). Coinbase: only the dead constant [main.py:124], dead columns (§3.1), and a `coinbase_feed` `.pyc`. Bybit: only the guard tests [test_integration_fixes.py:50-61] |
| `model_prob` / L1 / ATR / CVD | `model_prob=` is a live kwarg of `alerts.send_trade_opened` [alerts.py:49, 75] fed `signal.prob` [main.py:247, 272, 1390]; `"model_probability"` is a live `trade_context` key [main.py:940, 1231] read back in `_recover_missed_close` [live_trader.py:1635]. No L1/ATR/CVD code; only the NULL columns and the `_REGIME_*` constants |
| `LiveTrader._sweep_residual` | LIVE via `reconcile_dust` at boot [live_trader.py:1310 ← main.py:2575] (its `_submit_fok_order` SELL callers are dead) |
| `Database.get_trade_history`, `get_day_stats`, `set_peak_bankroll`/`get_peak_bankroll`, `mirror_mark_closed`, `open_market_count`, `has_open_or_pending_market` | `bot.py:77, 163`; `main.py:1789-1806, 1976-1998, 2076, 2276, 2522-2527, 1065, 1631`; `live_trader.py:1623` |
| Discord handlers `on_command_error`, `commands_list`, `clear_channels`, `pipeline_status`, `session_banner` | Zero name references by design — registered via `@bot.event`/`@bot.command` decorators [bot.py:33-258] |
| `MicroTape.on_twap30_report` (t3 A/B tape) | Wired [main.py:2654]; RTDS is not serving the 30 s topic so it records nothing (CLAUDE.md §6) |

---

## 4. CI

`.github/workflows/tests.yml` — single workflow `tests`, single job `pytest`:

| Field | Value |
|---|---|
| Trigger | `on: push` (all branches) and `on: pull_request` [tests.yml:3-5] |
| Runner | `ubuntu-latest`, `timeout-minutes: 20` [tests.yml:8-10] |
| Python | `3.12` via `actions/setup-python@v5` with `cache: pip` (comment: "the VPS runs 3.12 — test what deploys") [tests.yml:12-16] |
| Steps | `actions/checkout@v4` → `pip install -r requirements.txt` → `python -m pytest polybot/tests/ -q` [tests.yml:11-18] |
| Not run | No coverage, lint, type-check, or script tests beyond what `polybot/tests/` file-loads; no matrix; no artifact upload |

`requirements.txt` installs `pytest>=8.0.0` and `pytest-asyncio>=0.24.0` (no
`pytest-cov`); Linux-only markers pull `coincurve` and `uvloop` on the runner, so CI
exercises the coincurve signing backend the VPS uses while the Windows workstation
does not [requirements.txt:8-9, 18-21].
