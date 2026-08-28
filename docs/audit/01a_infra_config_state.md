# 01a — Infrastructure, configuration, state (Phase 1, track A)

Audited revision: `main` @ `15471a9abb9f5d4a48a6a362dba621d93df56d62` (2026-08-27 15:08 -0400), working tree clean except the untracked `docs/audit/` directory.
Evidence hierarchy applied: code > data (`docs/audit/data/*`, `polybot/memory/*`) > tests > live config (`settings.yaml`, `.env.example`) > docs. Every statement below cites `path:line` or a data file. Descriptive only — no recommendations.

---

## 1. Repository tree (top 3 levels)

```
PolyBot/
  AUDIT.md  CLAUDE.md  README.md  REFUTATIONS.md  RESEARCH.md  WALLETS.md
  requirements.txt  polybot.log (gitignored, .gitignore:24)
  .claude/settings.local.json (gitignored, .gitignore:42)
  docs/
    INTERVIEW.md
    audit/            (untracked; data/ holds copies of box state used below)
    doc-gen/          (empty)
  polybot/
    __init__.py  main.py  paths.py  recording.py
    agents/       __init__.py ghost_tracker.py outcome_reviewer.py pipeline_analytics.py scheduler.py
    config/       __init__.py loader.py settings.yaml .env (gitignored) .env.example
    core/         __init__.py signal_engine.py
    db/           __init__.py models.py polybot_live.db polybot_paper.db (+ -shm/-wal, gitignored .gitignore:20-21)
    discord_bot/  __init__.py alerts.py bot.py
    execution/    __init__.py base.py circuit_breaker.py correlation.py live_trader.py maker_bid.py paper_trader.py
    feeds/        __init__.py _json.py _socket.py _staleness.py chainlink_feed.py clob_ws.py market_scanner.py
    indicators/   __init__.py (1 line, comment only)
    memory/       outcomes/ recordings/ (gitignored .gitignore:34) state/
    tests/        __init__.py conftest.py fixtures/ + 36 test_*.py (listed below)
  scripts/
    run_polybot.sh  polybot.service
    analyze_late_window.py  analyze_twap_lock.py  reset_paper_clean.py
    smoke_gtc_test.py  smoke_order_test.py  sniper_shadow_status.py  verify_keys.py
    research/  .gitignore (data/)  README.md  data/ (gitignored)  + 44 research scripts
```

**Every Python module under `polybot/`** (line counts from `wc -l`):
`main.py` 3209 · `paths.py` 119 · `recording.py` 805 · `agents/ghost_tracker.py` 223 · `agents/outcome_reviewer.py` 151 · `agents/pipeline_analytics.py` 32 · `agents/scheduler.py` 114 · `config/loader.py` 180 · `core/signal_engine.py` 160 · `db/models.py` 503 · `discord_bot/alerts.py` 221 · `discord_bot/bot.py` 267 · `execution/base.py` 556 · `execution/circuit_breaker.py` 161 · `execution/correlation.py` 58 · `execution/live_trader.py` 2063 · `execution/maker_bid.py` 370 · `execution/paper_trader.py` 402 · `feeds/_json.py` 10 · `feeds/_socket.py` 30 · `feeds/_staleness.py` 91 · `feeds/chainlink_feed.py` 647 · `feeds/clob_ws.py` 377 · `feeds/market_scanner.py` 388 · `indicators/__init__.py` 1 · empty `__init__.py` in `polybot/`, `agents/`, `config/`, `core/`, `db/`, `discord_bot/`, `execution/`, `feeds/`, `tests/`.

Tests (`polybot/tests/`): `conftest.py`, `test_base_trader.py`, `test_boot_sweep.py`, `test_chainlink_feed.py`, `test_circuit_breaker.py`, `test_clob_ws.py`, `test_config.py`, `test_consistency.py`, `test_correlation.py`, `test_db.py`, `test_decision_parity.py`, `test_ghost_tracker.py`, `test_hot_mirror.py`, `test_integration.py`, `test_integration_fixes.py`, `test_late_sniper.py`, `test_latency_swr.py`, `test_live_health_read.py`, `test_live_trader.py`, `test_main_shutdown.py`, `test_maker_bid.py`, `test_market_scanner.py`, `test_mirror_invariants.py`, `test_ops_watch.py`, `test_outcome_reviewer.py`, `test_paper_trader.py`, `test_paths.py`, `test_recording.py`, `test_resolution.py`, `test_scheduler.py`, `test_sizing_chain.py`, `test_staleness.py`, `test_strike_source.py`, `test_ws1_freeze.py`.

**Every Python module under `scripts/`**: `analyze_late_window.py` 401 · `analyze_twap_lock.py` 484 · `reset_paper_clean.py` 118 · `smoke_gtc_test.py` 199 · `smoke_order_test.py` 141 · `sniper_shadow_status.py` 100 · `verify_keys.py` 15.
`scripts/research/`: `h1_pnl_decompose.py h1_pocket_detail.py h1_pockets.py h1_rank_cells.py h1_wallets_attrib.py h1_wallets_pull.py h2_open_mispricing.py h2_preopen_leak.py h2_primary_se.py h3_arb_adjudicate.py h3_complement_arb.py h3_crossing_dt.py h3_invisible_fills.py h3_live_fill_check.py h3_route_and_crossing.py h4_postclose_bid.py h4_redeem_lag.py klines_download.py parity_fixture_gen.py pm_trades_download.py r1_diag.py r1_oos_ref.py r1_refit.py r1_regime.py r23_ladder_grid.py r23_report_tables.py r23_tape_coverage.py r4_candidate_a_report.py r5_census.py r5_census_pull.py token_map_fetch.py ws1_boundary_autopsy.py ws1_diag.py ws1_final_hunt.py ws1_freeze_tables.py ws1_interval_max.py ws1_measure60.py ws1_oos.py ws1_reduce.py ws2_ladder_replay.py ws2_regime.py ws2_supply.py ws2_supply_attrib.py ws3_behavior.py ws3_books_reduce.py ws3_census.py ws3_dips.py ws3_queue.py ws4_k25.py`.

Two scripts are loaded by the running bot at nightly time, not only offline: `scripts/analyze_twap_lock.py` (`ladder_recalibrate`, `health_read`) and `scripts/analyze_late_window.py` (`live_health_read`, `mechanism_read`, `queue_depth_read`, `resolution_snapshot_read`) are imported via `importlib.util.spec_from_file_location` inside `main.py` [polybot/main.py:2721-2726, 2740-2744, 2754-2757].

Runtime dependencies [requirements.txt:1-22]: `httpx[http2]`, `discord.py`, `orjson`, `aiosqlite`, `python-dotenv`, `pyyaml`, `websockets`, `pytest`, `pytest-asyncio`, `py_clob_client_v2>=1.0.0,<1.1.0` (pin rationale at 10-13), Linux-only `coincurve` and `uvloop`, Windows-only `tzdata`.

---

## 2. Entry points and daemons

### 2.1 `polybot/main.py`

**argparse** [polybot/main.py:2361-2372]:

| Flag | Effect |
|---|---|
| `--mode {paper,live}` (default None) | Overrides `settings.yaml` `mode` [2434-2435]; `run_polybot.sh` always passes it [scripts/run_polybot.sh:35] |
| `--auto-restart` | Sets `scheduler._auto_shutdown = True` [2549]; after the nightly pipeline the scheduler sets `_shutdown_requested` [polybot/agents/scheduler.py:101-104], the trading loop breaks [2205-2206], `main()` returns, process exits 0 |
| `--run-pipeline` | Runs `run_pipeline()` once and exits; no trading, no WebSockets, no single-instance lock [3162-3163, 2375-2428] |
| `--allow-orphans` | Live only: `detect_orphan_positions(allow_orphans=True)` logs CRITICAL and proceeds instead of raising [2560, polybot/execution/live_trader.py:1489-1496] |

**`__main__` block** [3143-3209]: `faulthandler.enable(file=open("crash_native.log","a"))` [3148-3152]; `uvloop.install()` if importable [3155-3158]; `--run-pipeline` branch else: single-instance lock (`SystemExit(1)` if held) [3165-3169], `SIGINT` handler installed, `SIGTERM` routed to the same handler on POSIX [3170-3175], `asyncio.run(main())` [3176]. Exit paths: `KeyboardInterrupt` swallowed → exit 0 [3177-3178]; `OrphanPositionError` → stderr remediation text, `logging.shutdown()`, `os._exit(2)` [3179-3200]; any other `BaseException` → `logging.critical("FATAL ...")`, `os._exit(1)` [3203-3209]; second signal → `os._exit(130)` [3127-3133].

**Logging** [66-106]: `RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=2)` relative to CWD [69]; async `QueueHandler`/`QueueListener` [73-83]; root at ERROR, `polybot` and `polybot.discord_bot.bot` loggers at INFO [85-106]; `py_clob_client_v2`, `discord.gateway`, `discord.client` at CRITICAL [89-92]. Console handler formats `%H:%M:%S %(message)s` [67]; file handler strips ANSI [60-64, 70].

**`run_pipeline()`** [2375-2428]: `load_config()`; `OutcomeReviewer` + `GhostTracker` on `polybot/memory`; Discord optional (`get_secret("DISCORD_BOT_TOKEN")` failure → log-only) [2389-2392]; builds a `NightlyScheduler` and calls `run_daily_pipeline()` once, either inside Discord `on_ready` (then `discord_bot.close()`) or directly [2410-2426]. Note: only the rollups run here — the six jobs registered in `main()` [2707-2964] are **not** registered in `run_pipeline()`, so `--run-pipeline` executes rollups only [polybot/agents/scheduler.py:52-57 with an empty `nightly_jobs`].

**`main()`**: construction order and boot behavior are in §5.1.

### 2.2 `scripts/run_polybot.sh` — the daily supervisor

Verbatim logic (line numbers in the script):

- `cd "$(dirname "$0")/.."`, activate `.venv` [10-13]; infinite `while true` [15].
- **Pull**: `git pull --rebase --autostash origin main`; on failure `git rebase --abort` and continue on existing code [21-24].
- **Mode**: `mode="$(grep -E '^mode:' polybot/config/settings.yaml | head -1 | awk '{print $2}')"`, default `paper` [27-28].
- **Kill stragglers**: `pkill -f 'polybot\.main'`, `sleep 0.5` [31-32].
- **Run**: `python -m polybot.main --mode "$mode" --auto-restart`; capture `$?` [35-36].
- **Commit/push** only when exit code is 0: `git add polybot/config/settings.yaml polybot/memory polybot/db`; commit `"auto: daily pipeline update $(date '+%F')"`; `git push origin main`, one retry after 10s, then `"PUSH FAILED twice — records unpushed"` [41-52]. Nonzero exit → `"nonzero exit — skipping commit"` [50-52].
- **Crash restart**: if exit code ≠ 0 and ET `HHMM < 2330` → `sleep 60`, `continue` [56-63].
- **Wait for 12:01 AM ET**: `next = TZ=America/New_York date -d 'tomorrow 00:01'`; `wait = next - now`; **but** `if et_hm < 2330 then wait=0` (any exit during trading hours restarts immediately); `sleep $wait` if > 10s else `sleep 10` [69-79].

Start/stop times are not in this script: the bot process itself enforces the trading window from `schedule.*` [polybot/main.py:2045-2047, 1958-2002] and the scheduler exits the process after the 23:45 ET pipeline (§2.4). Data confirms the cycle: `Pipeline complete` 04:03:54Z → `PolyBot stopped — Bankroll $393.51` 04:04:14Z → `PolyBot [PAPER] ready` 04:05:05Z [docs/audit/data/polybot.log:819-824] — i.e. 00:04-00:05 ET, matching the `wait=0` branch because `et_hm=0004 < 2330` [scripts/run_polybot.sh:73].

### 2.3 systemd unit `scripts/polybot.service`

`User=ubuntu`, `WorkingDirectory=/home/ubuntu/PolyBot`, `ExecStart=/usr/bin/env bash /home/ubuntu/PolyBot/scripts/run_polybot.sh` [scripts/polybot.service:15-17]; `Restart=always`, `RestartSec=15`, `TimeoutStopSec=120`, `KillMode=control-group` (so `systemctl stop` SIGTERMs the python child) [18-23]; `StartLimitIntervalSec=0` (never locks out) [11]; `After/Wants=network-online.target` [7-8]. Install line at [4].

### 2.4 `NightlyScheduler` (`polybot/agents/scheduler.py`)

- Constructed with `outcome_interval_seconds`, `daily_pipeline_hour`, `daily_pipeline_minute` from `agents.*` [polybot/main.py:2541-2548].
- **Trigger** `run_daily_loop` [83-106]: every 60s checks ET clock; fires when `now.hour == daily_pipeline_hour and daily_pipeline_minute <= now.minute < daily_pipeline_minute + 5` (yaml: 23:45-23:49 ET). After the run: if `_auto_shutdown` → `_shutdown_requested = True` and return [101-104]; else sleep 3600.
- **Pipeline** `run_daily_pipeline` [41-76]: `rollup_old_outcomes` and `rollup_old_ghosts` (each wrapped, failures logged) [45-55]; then each registered job under `asyncio.wait_for(job(), timeout=JOB_BUDGET_S)` with `JOB_BUDGET_S = 600.0` [16, 62]. **Abandon semantics**: on timeout it logs `abandoned after 600s — it may still be running` and moves on; the comment states the `to_thread` body is not stopped [64-67]. Other exceptions → log + `alert_manager.send_error` [68-74].
- `run_outcome_loop` is a sleep loop only [78-81].
- **Registered jobs, in order** [polybot/main.py:2707-2964]: `compress_recordings` (own internal deadline 540s [polybot/recording.py:698, 718, 730-745]) → `window_paths_retention` (90 days) [recording.py:792-805] → `price_sum_retention` (90 days) [main.py:2710-2714] → `maker_ladder` (report-only, internal budget 480s) [main.py:2716-2727; scripts/analyze_twap_lock.py:372-382] → `recordings_retention` (30 days for both tape and micro) [recording.py:763-789] → `sniper_health` (inner `wait_for` 240s on the sim read, 120s on queue-depth read; Discord `send_health`) [main.py:2731-2964].
- Data: one full run took 03:45:52Z → 04:03:54Z (18 min) [docs/audit/data/polybot.log:780-818]; `maker_ladder` returned `{'n_locked': 249, 'n_dips': 28, 'applied': False, ...}` [793]; `sniper_health` returned `STILL ACCRUING` with `mechanism.checked 45 / exact 45 / unchecked 3 / t3_records 78879` [817].

### 2.5 Discord bot (`polybot/discord_bot/`)

`create_bot(db, scanner, config)` with prefix `!` and `message_content` intent [polybot/discord_bot/bot.py:23-26]. State it holds: `bot.is_paused` (in-memory, default False) [30], `bot.ready_event` [31], `bot.alert_manager` set by main [polybot/main.py:2539].

| Command | Reads | Mutates |
|---|---|---|
| `!commands` | — | — [bot.py:57-67] |
| `!status` | `db.get_bankroll`, `get_open_positions`, `get_day_stats`, `get_trade_history(999999)`, `scanner.find_active_contract` | — [69-156] |
| `!history [n]` (clamped 1..50) | `db.get_trade_history(n)` | — [158-193] |
| `!pause` / `!resume` | — | `bot.is_paused` True/False [195-203]; read by the trading loop through `is_paused_fn` [polybot/main.py:2997, 2136-2137] — halts **new entries only**; position management continues |
| `!clear [trades\|control\|all] confirm` | — | Deletes up to 200 Discord messages per channel via `AlertManager.purge_channel` [205-233; alerts.py:207-221]; never touches DB/state |
| `!pipeline` | — | — ; **hardcodes 23:45 ET** for the next-run display [235-255] rather than reading `agents.daily_pipeline_*` (see Drift) |
| `!session` | `db.get_bankroll` | sends a banner [257-265] |

Nothing in the Discord layer can change `trading_enabled`, the DB, or `settings.yaml`. Command errors collapse to one-line logs [38-55].

`AlertManager` [polybot/discord_bot/alerts.py]: channel lookup by name across all guilds, cached [24-33]; every send except `send_health` is fire-and-forget with a WARNING on failure [35-39]; `send_health` retries 3× with 20s sleeps and logs `NIGHTLY PING LOST` at ERROR [118-137]. Channels come from `discord.*` config [polybot/main.py:2535-2538].

Boot coupling: `run_discord()` restarts `discord_bot.start(get_secret("DISCORD_BOT_TOKEN"))` forever with backoff 5s→120s on any exception, including a missing token (`get_secret` raises `ValueError` [polybot/config/loader.py:176-180]) [polybot/main.py:2966-2977]; the trading loop starts after `ready_event` or a 15s timeout regardless [2980-2984].

### 2.6 Single-instance lock

`_acquire_single_instance(port=49653)` binds `127.0.0.1:49653` without `SO_REUSEADDR`; failure → returns False and `__main__` exits 1 [polybot/main.py:3092-3112, 3165-3169]. Second layer: `pkill -f 'polybot\.main'` before each launch [scripts/run_polybot.sh:31]. Third: systemd runs a single unit [scripts/polybot.service]. The `--run-pipeline` path bypasses the lock [3162-3163].

---

## 3. Configuration

### 3.1 `polybot/config/loader.py` behavior

- `load_config(config_path=None, env_path=None)`: `load_dotenv(<config_dir>/.env)` then `yaml.load(settings.yaml, Loader=_NoDuplicateKeysLoader)` then `validate_config` [158-169]. `get_config()` lazily loads once into module global `_config` [171-174].
- **Duplicate keys**: `_NoDuplicateKeysLoader` raises `yaml.YAMLError("duplicate key ... at line N")` for any repeated mapping key at any depth [13-34].
- **Alias**: if `late_window.trading_enabled` is absent and `late_window.sniper_enabled` is present, the value is copied to `trading_enabled` with a WARNING [111-119]. `trading_enabled` must then exist and be a bool [120-122].
- **Validation** (each `_check_*` appends `"<key>: missing from config"` when the key is absent — so every validated key is *required*, whether or not the runtime reads it) [45-156]. Ranges: `math.kelly_fraction` [0.04,0.18] [80]; `circuit_breaker.floor_pct` [0.50,0.95] [81]; `circuit_breaker.min_multiplier` [0.10,1.0] [82]; `execution.fok_spread_cross_floor` [0,0.20] [83]; `late_window.twap_zone_s` [5,60] [88]; `late_window.twap_k_min_s` [0,15] [89]; `late_window.sniper_min_edge` [0.02,0.10] [90]; `maker.maker_ladder` 1-5 rungs of `[price 0.15-0.95, frac (0,1], need 0.05-3]` [91-98]; `maker.maker_k_place_max` [5,29] [99]; `maker.maker_k_place_min` [1,15] [100]; `maker.maker_bankroll_frac` [0,0.5] [101]; `maker.post_close_hold_s` [0,120] [102]; `maker.maker_bid_enabled` bool [103-105]; `late_window.sniper_max_edge` [0.20,0.60] [106]; `late_window.sniper_fok_slip` [0,0.05] [107]; `late_window.require_max_tier` bool [108-110]; `late_window.validation_epoch` tz-aware ISO with `+00:00`, `Z` rejected, absent/None allowed [123-136]; `late_window.scar_enforce` list of str if present [137-140]; `execution.max_concurrent_positions` positive int [142]; `execution.max_bankroll_deployed` [0,1] [143]; `execution.max_book_fill_pct` [0,1] [144]; `execution.initial_bankroll` > 0 [145]; `execution.slippage_impact_pct` [0,0.20] [146]; `market.entry_window_seconds` > 0 [147]; `market.min_time_remaining_seconds` [0,120] [148]; `market.max_spread` [0,1] [149]; `circuit_breaker.losses_to_reduce` / `wins_to_restore` positive int [150-151]. All errors are collected and raised together as one `ValueError` [153-156].
- `get_secret(key)` = `os.environ.get` or `ValueError("Missing required secret")` [176-180].

### 3.2 Every key actually read by code

Column "code default if absent" is what happens when the key is missing from `settings.yaml`: **KeyError** means the runtime indexes with `[]` (boot crash unless the validator already rejected the missing key; "required by validator" is noted where that applies).

| Key | yaml value [settings.yaml line] | Code default if absent | Where read |
|---|---|---|---|
| `mode` | `paper` [1] | `"paper"` | main.py:2434 (`args.mode or config.get("mode","paper")`), bot.py:124, 264, run_polybot.sh:27-28 |
| `math.kelly_fraction` | 0.08 [12] | KeyError (validator-required) | main.py:2459, 2466 |
| `circuit_breaker.floor_pct` | 0.85 [15] | KeyError (validator-required) | main.py:2513 |
| `circuit_breaker.min_multiplier` | 0.4 [16] | KeyError (validator-required) | main.py:2514 |
| `circuit_breaker.losses_to_reduce` | 3 [17] | 3 (validator still requires it) | main.py:2515 |
| `circuit_breaker.wins_to_restore` | 3 [18] | 3 (validator still requires it) | main.py:2516 |
| `execution.max_bankroll_deployed` | 0.8 [21] | KeyError (validator-required) | main.py:2041, 2476, 2497 |
| `execution.max_concurrent_positions` | 2 [22] | KeyError at 2468/2477/2498; **1** at 2141 (validator-required) | main.py:2141, 2468, 2477, 2498 |
| `execution.max_book_fill_pct` | 0.5 [23] | 0.50 | main.py:1164 |
| `execution.initial_bankroll` | 150.0 [24] | KeyError (validator-required); only used when DB bankroll == 0 | main.py:2445-2446 |
| `execution.slippage_impact_pct` | 0.03 [37] | 0.03 | main.py:1177 |
| `execution.fok_spread_cross_floor` | 0.08 [38] | validator-required; **no runtime reader** | loader.py:83 only |
| `execution.paper_latency_scale` | 0.95 [44] | 0.95 (banner text uses 1.0 [2504]) | main.py:2499, 2504; paper_trader.py:29 |
| `execution.paper_latency_floor_s` | 0.32 [48] | 0.32 | main.py:2500; paper_trader.py:30 |
| `execution.paper_network_fail_rate` | 0.03 [49] | 0.03 | main.py:2501, 2505; paper_trader.py:33 |
| `late_window.trading_enabled` | true [68] | KeyError (validator-required; `sniper_enabled` alias) | main.py:1003, 2213, 2685 (set False by SOURCE gate), 2738 |
| `late_window.taker_enabled` | false [72] | **True** (not validated) | main.py:1034 |
| `late_window.validation_epoch` | `"2026-08-27T19:28:00+00:00"` [79] | None | main.py:2777, 2780; scripts/sniper_shadow_status.py:48 |
| `late_window.require_max_tier` | true [99] | True (validator-required bool) | main.py:1026 |
| `late_window.twap_zone_s` | 58.0 [100] | KeyError at 1005/1022; **60.0** at 2116 (validator-required) | main.py:1005, 1022, 2116 |
| `late_window.twap_k_min_s` | 6.0 [103] | KeyError (validator-required) | main.py:1023 |
| `late_window.sniper_min_edge` | 0.04 [108] | KeyError (validator-required) | main.py:1024, 2458, 2763 |
| `late_window.sniper_max_edge` | 0.50 [112] | KeyError (validator-required) | main.py:1133, 1276 |
| `late_window.sniper_fok_slip` | 0.01 [113] | KeyError (validator-required) | main.py:1209 |
| `late_window.scar_enforce` | **absent** | validated only if present; **no runtime reader** | loader.py:137-140 only |
| `maker.maker_bid_enabled` | true [139] | falsy → `_MAKER_MGR = None` (validator-required bool) | main.py:2652 |
| `maker.maker_ladder` | 5 rungs, need 1.0 [161-166] | seed `[[0.80,0.20,2.0],...]` (need **2.0**) in maker_bid.py:114-118 (validator-required) | maker_bid.py:114 |
| `maker.maker_k_place_max` | 25.0 [167] | 25.0 | main.py:1055 |
| `maker.maker_k_place_min` | 6.0 [174] | 6.0 | main.py:1054 |
| `maker.maker_bankroll_frac` | 0.15 [179] | 0.15 | main.py:1077 |
| `maker.post_close_hold_s` | 60.0 [185] | **0.0** | maker_bid.py:257 |
| `schedule.trading_start_hour_et` | 0 [191] | KeyError (not validated) | main.py:2046 |
| `schedule.trading_start_minute` | 1 [192] | KeyError (not validated) | main.py:2046 |
| `schedule.trading_end_hour_et` | 23 [193] | KeyError (not validated) | main.py:2047 |
| `schedule.trading_end_minute` | 30 [194] | KeyError (not validated) | main.py:2047 |
| `agents.outcome_reviewer_interval_seconds` | 3600 [197] | KeyError (not validated) | main.py:2405, 2545 |
| `agents.daily_pipeline_hour` | 23 [198] | KeyError (not validated) | main.py:2406, 2546 |
| `agents.daily_pipeline_minute` | 45 [199] | 0 | main.py:2407, 2547 |
| `market.entry_window_seconds` | 300 [205] | 120 (validator-required) | main.py:2450 → market_scanner.py:48; consumed only by `in_entry_window` [182-185], which has **no caller** |
| `market.min_time_remaining_seconds` | 0 [206] | 20 (validator-required) | main.py:2451 → market_scanner.py:49, 368 |
| `market.scan_cache_seconds` | 5 [207] | 5 | main.py:2452 → market_scanner.py:50, 316 |
| `market.clob_url` | `https://clob.polymarket.com` [208] | None → class constant `CLOB_API` [market_scanner.py:41, 54-55] | main.py:2454 |
| `market.clob_ws_url` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` [209] | same literal [main.py:2586]; also `clob_ws.WS_URL` [clob_ws.py:23] | main.py:2586 |
| `market.min_book_depth_usd` | 50 [210] | 50.0 | main.py:2453 → market_scanner.py:52; used main.py:1165, 1546 |
| `market.max_spread` | 0.1 [211] | 0.10 (validator-required) | main.py:2042 |
| `discord.trade_channel_name` | polybot-trades [214] | KeyError | main.py:2396, 2536 |
| `discord.control_channel_name` | polybot-control [215] | KeyError | main.py:2397, 2537 |
| `discord.daily_channel_name` | polybot-daily [216] | `"polybot-daily"` | main.py:2398, 2538 |
| `database.path` | `polybot/db/polybot.db` [219] | KeyError | main.py:2440 (`.replace(".db", f"_{mode}.db")` → `polybot/db/polybot_paper.db` / `polybot_live.db`, resolved against CWD = repo root [run_polybot.sh:10]) |

Config objects are passed whole into `MakerBidManager(cfg=config["maker"])` [main.py:2650-2651] and `create_bot(config)` [2534]; no other module reads the config dict.

### 3.3 Environment variables

| Variable | Loaded from | Read at | Required when |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | `polybot/config/.env` via `load_dotenv` in `load_config` [loader.py:161-163] | main.py:2390 (pipeline, optional), 2970 (`get_secret`, retried forever on failure) | Never fatal — see §2.5 |
| `POLYMARKET_PRIVATE_KEY` | same | live_trader.py:263-265 (`_create_clob_client`) | `mode: live` (verify_auth [316-328], `LiveTrader.__init__` [365]); scripts `verify_keys.py:9-13`, `smoke_*` [smoke_order_test.py:25,107; smoke_gtc_test.py:31,115] |
| `POLYMARKET_FUNDER` | same | live_trader.py:266-270, 340, 497, 776, 1340 | `mode: live`; orphan detection raises without it [1340-1346] |
| `POLYBOT_MEMORY_DIR` | process environment only | paths.py:17 (`MEMORY_DIR` override) | Optional; tests set a tempdir [polybot/tests/conftest.py:11-12] |

`.env.example` lists exactly the three secrets [polybot/config/.env.example:1-6]; `POLYBOT_MEMORY_DIR` is not in it. `.env` is gitignored [.gitignore:16-17].

### 3.4 Drift (code vs. `settings.yaml` vs. validator)

1. **`late_window.taker_enabled`** — yaml `false` [72]; code default when absent is `True` [main.py:1034]; the validator does not check the key [loader.py]. Deleting the key arms the taker.
2. **`maker.post_close_hold_s`** — yaml 60.0 [185]; code default 0.0 [maker_bid.py:257] (validator requires the key, so the default is unreachable via yaml omission).
3. **`maker.maker_ladder` seed** — yaml `need` 1.0 on all rungs [161-166]; the in-code seed uses `need` 2.0 [maker_bid.py:114-118] (validator-required, so also unreachable). Note the seed also anchors `MAKER_LADDER_PATH` overrides: fractions/need come from `cfg.get("maker_ladder")` when present [125-128].
4. **`late_window.twap_zone_s`** — one reader defaults to 60.0 [main.py:2116] while the others index directly [1005, 1022]; yaml is 58.0 [100] and the yaml comment says "Never raise above 58" [100-102] while the validator allows up to 60.0 [loader.py:88].
5. **`execution.max_concurrent_positions`** — yaml 2 [22]; one reader defaults to 1 [main.py:2141], others KeyError.
6. **`execution.fok_spread_cross_floor`** — yaml 0.08 [38], validator-required [loader.py:83], **never read by runtime code** (grep of `polybot/` and `scripts/`: only loader.py:83). Removing it fails boot; changing it does nothing.
7. **`late_window.scar_enforce`** — validated if present [loader.py:137-140]; absent from yaml; **no reader anywhere**.
8. **`market.entry_window_seconds`** — yaml 300 [205], validator-required [loader.py:147]; its only consumer `BTCMarketScanner.in_entry_window` [market_scanner.py:182-185] has no caller in `polybot/` or `scripts/`.
9. **`market.clob_url` / `market.clob_ws_url`** — both equal the compiled-in constants [market_scanner.py:41; clob_ws.py:23]; `chainlink_feed.RTDS_WS_URL` and `market_scanner.GAMMA_API` are constants only, not configurable [chainlink_feed.py:27; market_scanner.py:40].
10. **`!pipeline` next-run time** hardcodes 23:45 ET [bot.py:240] independent of `agents.daily_pipeline_hour/minute` [198-199].
11. **`execution.paper_latency_scale` boot banner** prints the default as 1.0 [main.py:2504] while the constructor default is 0.95 [2499; paper_trader.py:29]. Display only.
12. **`schedule.*`, `agents.*`, `discord.*`, `database.path`** are read with `[]` (KeyError on absence) but are not validated [loader.py has no checks for them].
13. **Comment vs. rule**: `settings.yaml` header says trading validates `mode: paper` against a `$400` bankroll [79-82] while `execution.initial_bankroll` is 150.0 [24]; the DB bankroll (paper copy 396.37, peak 400.0 [docs/audit/data/polybot_paper_audit.db bankroll/peak_bankroll rows]) governs because `initial_bankroll` is applied only when the DB bankroll is 0 [main.py:2445-2446].
14. **`.gitignore` references `polybot/db/late_window_collect.db*`** [.gitignore:37] — no code path references that file (grep of `polybot/`, `scripts/` returns nothing).

### 3.5 `polybot/paths.py` — persisted files, writers, readers

`MEMORY_DIR = $POLYBOT_MEMORY_DIR or polybot/memory` [paths.py:16-17]; `STATE_DIR = MEMORY_DIR/state` [34]. `write_json_atomic` = tmp + `replace` [20-31]. `trim_jsonl_by_age` [58-86] and `fold_gate_day` [89-119] are helpers.

| Constant → file | Writer | Reader | Purpose / data-copy observation |
|---|---|---|---|
| `ADVERSE_STATE_PATH` → `state/adverse_state.json` [35] | **none** (no importer in `polybot/` or `scripts/`) | none | Orphan of a deleted subsystem; file exists in `docs/audit/data/` (`schema 2`, `saved_at 1786112404` ≈ 2026-08-07) and is committed [git ls-files: 13 files in `polybot/memory/state`] |
| `FEED_STALENESS_PATH` → `state/feed_staleness.json` [36] | main.py:2616-2633 every 60s (`_staleness_write`, atomic [feeds/_staleness.py:85-91]) | none in code | Data: chainlink `n_total 4606, p50 0.938s, max 8.537s`; clob_ws `n_total 1,987,541`; both `connected: true` |
| `FILL_STATS_PATH` → `state/fill_stats.json` [37] | live_trader.py:253-254 → base.update_fill_stats [base.py:59-100] | none (comment: "offline calibration ledgers — no runtime consumer" [base.py:63]) | Data: 1529 attempts / 359 fills, last_updated 2026-08-13 |
| `FILL_STATS_PAPER_PATH` → `state/fill_stats_paper.json` [38] | paper_trader.py:260-261 | none | Data: 2063/358, last_updated 2026-08-12 |
| `LATENCY_STATS_PATH` → `state/latency_stats.json` [39] | live_trader.py `_record_submit_latency` [208-248] (rewrites from in-process deques of ≤200 samples, from the first sample [215-216]); `_record_gtc_latency` [178-205] (`gtc` section, deques ≤400); `smoke_gtc_test.py --samples` also feeds the gtc section [scripts/smoke_gtc_test.py:191] | main.py `_latency_watch` [461-478], `_gtc_watch` [502-522] for the nightly ops line [2813-2815] | Data: `n: 2`, `post.p50_ms 302.9`, no `gtc` key, last_updated 2026-08-13 → ping reads "POST p50 unknown — only 2 order samples · GTC RTT unmeasured — only 0 GTC samples" [docs/audit/data/polybot.log ~813] |
| `ORPHAN_POSITIONS_PATH` → `state/orphan_positions.json` [40] | live_trader.py:1435-1444 (live boot only) | operator (`main.py:3190` instructs `cat`) | Data: `checked_at 2026-08-14T17:34Z`, 0 orphans, `allow_orphans_flag false` |
| `PREV_MARGIN_PATH` → `state/prev_resolution_margin.json` [41] | main.py:355-359 (after every resolution, off-thread [1819, 1954]) | main.py:339-353 at boot [2601], discarded if older than 1800s [337, 348] | Data: margin 11.08, saved_at 1787809906 |
| `DAY_OPEN_PATH` → `state/day_open_bankroll.json` [45] | main.py:724-733 at day open [1981] and mid-day reconstruction [2086] | main.py:736-744 on mid-day restart [2078] | Data: `{"day":"2026-08-27","bankroll":393.514}` |
| `PRICE_SUM_OUTLIERS_PATH` → `state/price_sum_outliers.jsonl` [48] | main.py:284-304 (append; 1 line/market/s) | nightly trim 90 days [2710-2714] | gitignored [.gitignore:35]; data copy 198,190 lines / 29.9 MB |
| `GATE_STATS_PATH` → `state/gate_stats.json` [52] | `fold_gate_day` [paths.py:89-119] via main.py:632-634 on ET rollover / un-folded crash day [637-667] | itself (read-modify-write) | Data: `days_accumulated 90`, `20260528..20260826`, `total_skips 36,046,520`, plus a `retired_gates` note |
| `GATE_STATS_CURRENT_PATH` → `state/gate_stats_current.json` [53] | main.py:618-629 on every outcome [892], boot [2608], rollover [654, 663] | main.py:606-615 at first `_record_skip` / boot [657] | Data: `et_date 20260827`, `book_freshness_skew 17398, stale_prices 261, stale_feed 3` |
| `MAKER_LADDER_PATH` → `state/maker_ladder.json` [56] | **none** — `ladder_recalibrate` is report-only and "never writes the ladder" [scripts/analyze_twap_lock.py:372-382]; nightly job returns the dict [main.py:2716-2727] | maker_bid.py:111-134 (override if the file exists; prices clamped, fractions/need from seed) | Absent from `docs/audit/data/` and from `polybot/memory/state/`; the override path is inert unless the file is created by hand |

Other persisted artefacts not in `paths.py`: `polybot.log` (+`.1`, `.2`) in CWD [main.py:69]; `crash_native.log` in CWD [3150]; `polybot/memory/recordings/tape_YYYY-MM-DD.jsonl(.gz)` and `micro_YYYY-MM-DD.jsonl(.gz)` [recording.py:27, 551, 692, 728]; `polybot/db/window_paths.db` [recording.py:30]; per-mode DBs [main.py:2440]; `backups/reset_<ts>/` from `reset_paper_clean.py` [scripts/reset_paper_clean.py:5-8; .gitignore:39].
Files present in `state/` with **no code reference at all**: `cf_watchlist.json`, `scar_gates.json`, `sprt_burst.json` (grep `polybot/ scripts/`: none) — all three are committed and copied into `docs/audit/data/`.

---

## 4. State

### 4.1 Per-mode SQLite DB (`polybot/db/models.py`)

Connection: `aiosqlite`, `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` [models.py:31-35]; one connection, all commit-bearing writes serialized under `_write_lock` [22-25]. Path: `polybot/db/polybot_{mode}.db` [main.py:2440].

**`positions`** [37-52] + additive migrations [75-80]: `id INTEGER PK AUTOINCREMENT, market_id TEXT NN, question TEXT NN, side TEXT NN, entry_price REAL NN, size REAL NN, signal_score REAL NN, entry_timestamp TEXT NN, status TEXT NN DEFAULT 'open', exit_price REAL, exit_timestamp TEXT, indicator_snapshot TEXT, fee_rate REAL, shares_held REAL`. Statuses used: `open`, `pending_resolution`, `closed` [193, 232, 251]. Indexes `idx_positions_status`, `idx_positions_market_status` [101-107].

**`trade_history`** [54-62] + migrations [83-99]: `id, side NN, entry_price NN, exit_price NN, size NN, exit_timestamp NN, exit_reason NN DEFAULT 'resolution', pnl REAL DEFAULT 0, fees REAL DEFAULT 0, maker_fill INTEGER DEFAULT 0, maker_rebate REAL DEFAULT 0 (always 0.0 [92-94, 273]), position_id INTEGER`. Index `idx_trade_history_exit_ts` [108-111]. Join to positions is `COALESCE(t.position_id, t.id) = p.id` [438].

**`bankroll`** `(id=1 CHECK, amount REAL NN)` [64-67]; **`peak_bankroll`** same shape [69-72].

**`window_labels`** — created by the recorder, not by `Database.initialize` [recording.py:127-135] + `token_up TEXT`, `token_down TEXT` migrations [139-144]: `window_id TEXT PK, resolved_up INTEGER NN, final_price REAL, price_to_beat REAL, labeled_at REAL NN, token_up, token_down`. Written by `_label_pass` `INSERT OR REPLACE` [315-320].

Writers (all under `_write_lock`): `open_position_and_debit_bankroll` (position insert + bankroll upsert, one transaction) [174-218]; `mark_pending_resolution` [228-238]; `close_position` (+ history row; absolute or delta bankroll) [276-319]; `sync_entry_booking` [321-341]; `correct_sell_fill` [343-379]; `set_bankroll` [476-484]; `set_peak_bankroll` [491-498]. Two direct writes outside `Database`: `reconcile_open` `UPDATE positions SET shares_held` [live_trader.py:1547-1551] and `_recover_missed_close` fallback status-only close [1616-1623].

**Data check** (`docs/audit/data/polybot_paper_audit.db`, `polybot_live_audit.db`, read-only): schema matches the code above for both. Paper: 38 positions (all `closed`), 38 trade_history rows, 13,851 `window_labels` (`btc-updown-5m-1781188800` … `1787861700`), bankroll 396.366, peak 400.0. Live: 337 positions (all `closed`), 337 trade_history, 4,997 labels (`1783191300` … `1786754700`), bankroll 123.399, peak 153.210. The paper DB additionally contains **`wallet_stats`** (`wallet TEXT PK, n_trades, n_won, stake_usd, pnl_usd, classification, updated_at`; 71,035 rows) — **no code in `polybot/` or `scripts/` references this table** (grep hits only `.db` files). It rides in the nightly commit because `git add polybot/db` [run_polybot.sh:42].

### 4.2 Sidecar `window_paths.db` (`polybot/recording.py`)

`PATHS_DB = polybot/db/window_paths.db` [30], gitignored [.gitignore:36]; own `aiosqlite` connection with WAL / `busy_timeout=15000` [102-107].
`window_paths` base columns [109-119]: `window_id TEXT NN, ts REAL NN, elapsed_s REAL NN, bid_up, ask_up, bid_down, ask_down, depth3_bid_up, depth3_ask_up, depth3_bid_down, depth3_ask_down, coinbase_price, strike, traded INTEGER NN DEFAULT 0`; indexes on `window_id`, `ts` [120-123].
Appended (migrated) columns, in order [150-175]: `binance_price, binance_cvd_10s, binance_cvd_30s, atr, model_prob_up, chainlink_price, chainlink_age_s, book_age_up_s, book_age_down_s, coinbase_bid, coinbase_ask, coinbase_cvd_10s, coinbase_cvd_30s, bid_sz_up, ask_sz_up, bid_sz_down, ask_sz_down, depth20_bid_usd, depth20_ask_usd, strike_trusted`. Of these, `coinbase_*`, `binance_*`, `depth20_*`, `atr`, `model_prob_up` are written as NULL by design [382, 407, 420-424]. One-time migration moves any legacy `window_paths` rows out of the per-mode DB and drops that table [186-204]. Sampling 1 Hz, 5 Hz in the final 45s [490-494]; flush every 10s [32, 482-484]; 90-day retention nightly [792-805].

### 4.3 In-memory "hot mirror" (`Database._pos_mirror`, `_bankroll_mirror`)

- Contents: `position_id → (market_id, status, size)` for rows with status `open`/`pending_resolution`, plus the bankroll amount [models.py:26-28, 124-133].
- Built once in `initialize()` → `_rebuild_hot_mirror()` [113]; there is no periodic rebuild. Kept current by the writers: insert [208-211], pending [236-238], close [310-314], `correct_sell_fill` bankroll delta [374-375], `set_bankroll` [484], and the external hook `mirror_mark_closed` [169-172] used by the reconcile fallback [live_trader.py:1622-1623].
- Readers (all sync): `preflight_peek` → `(has_pos, open_count, bankroll, deployed)` [135-149] used by `BaseTrader.open_trade` and `book_maker_fill` with a DB fallback when `None` [base.py:309-313, 394-398]; `has_open_or_pending_market` [151-155] used at main.py:1065, 1631; `open_or_pending_count` [157-161] at main.py:715; `open_market_count` [163-167] at main.py:2276 (fast entry path).
- Returns `None` until built (`_bankroll_mirror is None`) [138-139, 153, 159, 165]; callers then fall back to SQL.

### 4.4 Other in-process caches (`polybot/main.py`)

`_open_positions_cache` (5s TTL, invalidated on open/close/resolve) [701-721, 747-755]; `_bankroll_cache` (5s) [758-770]; `_contract_price_cache` stale-while-revalidate (TTL 5s / 2s near expiry, never served past 900s) [110-117, 795-823]; `market_scanner._cached_contract` SWR [market_scanner.py:297-329]; book cache 2s and tick-size cache 3600s [58-61]; `window_strikes`, `_strike_trusted`, `_gamma_strikes`, `_strike_logged` (600s sweep) [1444-1447, 329]; `_window_killed_asks` [689-699]; `_pending_settled_banners` LRU 32 [180-181]; gate-skip LRUs [366-371].

### 4.5 `memory/state/*.json` — see §3.5 table. Summary of what each is for, from code:
`day_open_bankroll` (day P&L baseline), `gate_stats*` (skip counters), `prev_resolution_margin` (telemetry restore), `feed_staleness` (WS inter-arrival telemetry), `fill_stats*` / `latency_stats` (calibration ledgers; latency feeds the ops watch), `orphan_positions` (live boot audit), `maker_ladder.json` (optional override, never written). `adverse_state`, `cf_watchlist`, `scar_gates`, `sprt_burst` have no code reader or writer.

### 4.6 `memory/outcomes`, `memory/ghost_outcomes`, `memory/recordings`

- `OutcomeReviewer(outcomes_dir=polybot/memory/outcomes)` [main.py:2530]: one JSON per resolved trade `{position_id}_{market_id}_{ts}.json` [outcome_reviewer.py:76-77]; nightly `rollup_old_outcomes` folds them into `rollup_YYYY-MM-DD.json` [103-149]. Local checkout: 2 per-trade files + `rollup_2026-08-18/19/20/21/24/25.json`.
- `GhostTracker(memory_dir=polybot/memory)` writes `ghost_outcomes/{market}_{gate}_{ts}.json` [ghost_tracker.py:26-27, 222-223] and rolls up nightly [165-213]; ghosts are resolved every 30s from Gamma metadata [main.py:2005-2022, 2327-2335]. The `ghost_outcomes/` directory is not present in this checkout (`git ls-files polybot/memory` lists only `outcomes/` and `state/`).
- Recordings [recording.py:504-695]: `tape_*.jsonl` (every CLOB print), `micro_*.jsonl` (`b` BBO changes in final 90s, `l` raw reports, `t` 60s TWAP, `t3` 30s TWAP, `s` Binance). Single-thread writer executors; flush every 200 rows or 10s [32-33]. Nightly gzip of finished days [698-760] then 30-day deletion [763-789]. Gitignored [.gitignore:34]. Local checkout holds `micro_2026-08-07 … 08-27` and `tape_2026-08-07 … 08-27` (some days both `.jsonl` and `.jsonl.gz`; today's raw `micro_2026-08-27.jsonl` is 1.48 GB).

---

## 5. Restart and recovery

### 5.1 Boot (`main()`), in execution order [polybot/main.py:2431-3034]

1. `load_config()`; `mode = args.mode or yaml mode` [2432-2435].
2. `Database(polybot/db/polybot_{mode}.db).initialize()` (tables, migrations, indexes, hot mirror) [2440-2443]; if bankroll == 0 → `set_bankroll(execution.initial_bankroll)` [2445-2446].
3. `BTCMarketScanner(...)` from `market.*` [2448-2455]; `SignalEngine(min_edge, kelly_fraction)` [2457-2459].
4. **Live only**: `verify_auth(min_allowance_usd = bankroll × kelly_fraction × max_concurrent × 10)` — on failure logs `LIVE MODE preflight failed` and **returns** (exit 0: the supervisor commits, and because `et_hm < 2330` sets `wait=0` it relaunches after `sleep 10` — a ~10s-plus-boot relaunch loop until 23:30 ET, then a wait to 12:01 AM ET [scripts/run_polybot.sh:41-49, 69-79]) [2462-2473; live_trader.py:316-349]; `LiveTrader(...)` (creates the CLOB client, derives API creds) [2475-2477, live_trader.py:359-409]; `_boot_order_sweep(client)` = `cancel_all` with every outcome logged, failure → ERROR "cancel them by hand" [2478, 437-458]; settled-entry / exit-corrected hooks [2481-2492].
   **Paper**: `PaperTrader(...)` with the three realism knobs [2496-2506].
5. `CircuitBreaker(initial_bankroll=db bankroll, ...)`; if persisted `peak_bankroll` > current peak → `restore_from_peak(peak, current)` (tier/floor from the peak, sizing against the live balance) else `set_peak_bankroll(current)` [2509-2527; circuit_breaker.py:115-124]. Data: `Tier locked $400 -> floor $340.00` logged at each boot [docs/audit/data/polybot.log:822, 961, 1580].
6. `OutcomeReviewer`, `GhostTracker` [2530-2531]; Discord bot + `AlertManager` [2534-2539]; `NightlyScheduler` with `_auto_shutdown = args.auto_restart` [2541-2549].
7. **Live only**: `db.set_bankroll(live_balance)` [2550-2552]; `detect_orphan_positions(db, allow_orphans)` — raises `OrphanPositionError` on unresolved unknown tokens **or on any DB/API failure** (fail closed) unless `--allow-orphans` [2558-2564; live_trader.py:1325-1500]; then `reconcile_open` (missed-close recovery via Gamma or CLOB mid; share drift sync) and `reconcile_dust(max_age_hours=24)`, both non-blocking on error [2566-2577; live_trader.py:1504-1560, 1264-1319].
   **Paper**: logs `PAPER RESTART — N open position(s) carried over; any ladder resting at shutdown is gone` if any open/pending rows exist [2578-2584].
8. `ClobWebSocket(url).start()` [2586-2588] (connects only once a token is subscribed [clob_ws.py:168-172]); `trader.set_clob_ws`, `prewarm_http`, `start_keepalive` (live: 5s REST ping that latches auth errors) [2590-2596; live_trader.py:442-459].
9. Restore `_prev_resolution_margin` (≤30 min old) [2598-2603]; gate-stats current-day file load + flush [2605-2608].
10. `scheduler.start()`; `ChainlinkFeed().start()` (run loop + watchdog) [2610-2613; chainlink_feed.py:438-441]; staleness flush loop defined [2615-2633]; shared `httpx.AsyncClient(timeout=5)` [2636-2640].
11. Recorders and maker manager: `TapeRecorder`, `MakerBidManager` iff `maker.maker_bid_enabled` [2647-2659], print mux [2661-2665], `MicroTape` hooks [2666-2671], `WindowPathRecorder` [2672-2677], SOURCE-mismatch handler that sets `config["late_window"]["trading_enabled"] = False` in-process and pages [2679-2698].
12. Register the six nightly jobs [2707-2964]; start Discord and wait ≤15s [2966-2984]; `gc.collect(); gc.freeze(); gc.set_threshold(10_000, 20, 20)` [2989-2992].
13. `trading_loop(...)` task [2994-3001]; background tasks: outcome loop, daily loop, staleness flush, `window_recorder.run()` (creates tables, re-seeds unlabeled windows ≤40 min old [recording.py:260-292, 466-469]), `_book_warmer`, Discord [3026-3033].

Trading-loop day state at boot [2060-2091]: if ET time is 00:00-00:29 → fresh day (0W/0L, `current_trading_day=None`); otherwise **mid-day restart**: wins/losses/fees from `get_day_stats(today)`, `day_open_bankroll` from `day_open_bankroll.json` if it is today's, else reconstructed as `bankroll − day pnl_sum` and persisted. Data: `PolyBot [PAPER] ready | Bankroll $396.37 | Today: 1W/0L` at 06:46:13Z and 19:04:04Z [docs/audit/data/polybot.log:963, 1582] vs `Today: 0W/0L` at the 04:05Z scheduled boot [824].

The day open/close banners and `breaker.reset()` fire on the first loop tick inside trading hours for a new ET date [1958-1988]; the day closes (banner + `current_trading_day=None`) once outside hours and no `pending_resolution` rows remain [1989-2000].

### 5.2 Shutdown [3036-3089] and signal handling

- Triggers: scheduler `_shutdown_requested` (after 23:45 ET pipeline with `--auto-restart`) → loop `break` [2205-2206]; `SIGINT`/`SIGTERM` → `KeyboardInterrupt` raised from the handler [3123-3138] → propagates out of `asyncio.run` → `finally` block runs → `__main__` swallows it (exit 0) [3177-3178]; `AuthError` in the loop → `raise` [2341-2354] → FATAL path exit 1 [3203-3209].
- **Watchdog armed first**: `threading.Timer(30.0, _force_exit)` (daemon) that logs `EXIT FORCED — a worker thread outlived shutdown`, `logging.shutdown()`, `os._exit(0)` [3047-3058].
- Sequence, each time-boxed by `_stop_rec`/`_stop` (2s default): cancel background tasks and gather them (5s) [3059-3069]; `_MAKER_MGR._retire("shutdown")` (5s) — cancels rungs, live re-polls fills, books any accrued fill before clearing `active` [3072-3073; maker_bid.py:298-331]; `window_recorder.stop()` (final flush, close sidecar) [3074; recording.py:496-501]; `tape_recorder.flush()`, `micro_tape.flush()` (executor submit, inline if executor gone) [3075-3076; recording.py:537-545, 678-686]; `http_client.aclose()` [3077]; `trader.stop_keepalive()` [3081-3082]; `clob_ws.close()` [3083; clob_ws.py:75-89]; `scheduler.stop()` [3084]; `chainlink_feed.stop()` [3085; chainlink_feed.py:443-457]; `discord_bot.close()` [3086]; `db.get_bankroll()` then `db.close()` [3087-3088]; final line `PolyBot stopped — Bankroll $X · Feeds/WS/DB closed` [3089]. Data: 04:04:14Z and 19:03:55Z [docs/audit/data/polybot.log:821, 1579].
- Second Ctrl+C/SIGTERM → `os._exit(130)` [3127-3133] (nonzero → supervisor skips commit [run_polybot.sh:50-52]).
- systemd stop: `KillMode=control-group` delivers SIGTERM to python; `TimeoutStopSec=120` [polybot.service:20-23].

### 5.3 What survives a restart vs. what does not

| Survives (where) | Lost / rebuilt |
|---|---|
| Positions, trade_history, bankroll, peak_bankroll, window_labels (per-mode DB, WAL) | In-flight maker ladder (`_MAKER_MGR.active`) — cancelled and booked at graceful shutdown [3072-3073]; on a crash, live rungs are swept at next boot by `cancel_all` [437-458] and unbooked fills are only caught by `detect_orphan_positions` / `reconcile_open`; paper rungs simply vanish [2583-2584] |
| Circuit-breaker tier/floor via `peak_bankroll` [2522-2527] | Breaker streak counters (fresh object) [circuit_breaker.py:65-69] |
| Today's W/L/fees (recomputed from DB) and `day_open_bankroll.json` [2076-2091] | `current_trading_day` when the DB shows 0 trades today (banner may re-fire) [2077] |
| Gate-skip counters (`gate_stats_current.json`) [637-667] | Chainlink boundary captures / trust (`_boundary_prices`, `_boundary_meta`) — the feed's start window is never trusted [chainlink_feed.py:292-316]; `_strike_trusted`, `window_strikes`, `_gamma_strikes` [329, 2049] → first windows after boot cannot deploy capital and log `SOURCE CHECK SKIPPED … no trusted boundary capture` [recording.py:357-360]; the log copy has exactly 6 such lines, two after each of the three boots [data log 828, 830, 967, 971, 1587, 1592] |
| `prev_resolution_margin.json` if < 30 min old [339-353] | Latency deques (`_LATENCY_SAMPLES` etc.) — the JSON is rewritten from the empty deque on the first post-boot sample, so `n` restarts [live_trader.py:141-143, 174-175, 215-216] (data: `n: 2`) |
| Recordings on disk (jsonl/gz) | Recorder label queue `_pending_label` (re-seeded from unlabeled `window_paths` rows ≤40 min old) [recording.py:260-292]; `_window_tokens` (labels for restart-orphaned windows take token ids from the fresh Gamma fetch) [308-313] |
| `orphan_positions.json`, `fill_stats*.json`, `feed_staleness.json` (last written values) | CLOB WS books/BBO/trade buffers (also cleared on every reconnect) [clob_ws.py:159-165, 183]; `last_print_gap_ts` |
| `settings.yaml` (including any in-process `trading_enabled=False` flip is **not** written back) [2685] | Discord `is_paused` (resets to False) [bot.py:30]; the in-process SOURCE-gate halt (re-armed by restart, by design [2683-2684]) |
| Per-window ghost/outcome JSON files | `_contract_price_cache`, tick-size/book caches, `_open_positions_cache`, `_pending_settled_banners` (a live fill whose +8s audit had not run prints no settled banner) [180-181, 253-277] |

### 5.4 Mid-day crash restart path

`run_polybot.sh`: nonzero exit before 23:30 ET → `sleep 60` → `continue` (no commit) [56-63]; exit code 0 before 23:30 ET → commit/push, then `wait=0` → `sleep 10` → relaunch [41-49, 73-78]. The python side ensures crashes actually exit: `os._exit` on FATAL/orphan paths [3195-3200, 3205-3209] and the 30s shutdown watchdog [3047-3058]. Data: the 08-27 log copy shows two intra-day boots — 06:46:13Z with **no preceding `PolyBot stopped` or `FATAL` line** in the copy (previous process ended without the graceful path) [docs/audit/data/polybot.log:950-963], and 19:04:04Z nine seconds after a graceful `PolyBot stopped` (consistent with the exit-0 `sleep 10` branch) [1579-1582]. Both boots restored `Today: 1W/0L` and `Bankroll $396.37` from the DB.

Comment/script disagreement: `main.py:2342-2344` says an `AuthError` exit "won't retry until the next 12:01 AM ET start"; `run_polybot.sh:56-63, 73` restarts any nonzero exit after 60s and any zero exit after 10s whenever ET time is before 23:30.

---

## 6. Single points of failure

| SPOF | Handling in code | Behavior if the handling does not hold |
|---|---|---|
| **One host / one process** (systemd unit, `WorkingDirectory=/home/ubuntu/PolyBot`) | systemd `Restart=always`, `RestartSec=15`, `StartLimitIntervalSec=0` [polybot.service:11, 18-19]; supervisor crash-restart after 60s [run_polybot.sh:56-63]; single-instance socket lock [main.py:3092-3112]; shutdown watchdog [3047-3058]; `faulthandler` → `crash_native.log` [3148-3152] | A hung process that neither exits nor trips the 30s watchdog (only armed inside `finally`) keeps the supervisor waiting; no external health check exists in the repo. Host loss stops trading, recording, labeling and the nightly commit together. |
| **Chainlink RTDS WebSocket** (`wss://ws-live-data.polymarket.com`, one socket carrying raw, 60s TWAP, 30s TWAP, Binance topics) [chainlink_feed.py:27, 518-543] | Reconnect with backoff 5s→60s, 429 → jump to 30s [514-532, 620-632]; watchdog forces reconnect after 60s silence on raw **or** on the TWAP topic alone, with a bounded warm-up [458-500]; app-level PING every 10s [502-510]; entry gates: `age_seconds > 60` → `stale_feed` skip [main.py:969-973], `twap_frozen()` → skip [979-985], untrusted strike → no capital [1003-1014], projection refuses spot > 3s old or raw holes > 10s [chainlink_feed.py:37-44]; maker ladder retires on `projection cold` [maker_bid.py:269-273] | Boundary captures are lost across a reconnect (trust fails for windows whose boundary report was missed [299-316]) → those windows cannot trade and the SOURCE gate cannot check them (`source_unchecked` grows [recording.py:357-360]). Data: 43 `ChainlinkFeed idle for 6xs — Reconnecting` watchdog reconnects plus 1 `ChainlinkFeed disconnected (ConnectionClosedError)` in the 30h55m log copy (2026-08-26 13:31Z → 08-27 20:26Z, 1,664 lines) [docs/audit/data/polybot.log, e.g. 26, 38, 71, …, 1590; 795]; `feed_staleness.json` max gap 8.537s. |
| **CLOB WebSocket** (`ws-subscriptions-clob.polymarket.com`) [clob_ws.py:23] | Reconnect 1s→30s backoff [174-215]; PING/PONG heartbeat, no PONG for 25s → close and reconnect [24-25, 220-232]; per-token freshness gates (`_WS_STALE_S = 10`) with REST book fallback [main.py:120, 1466-1475]; `book_freshness_skew` skip [1512-1520]; `last_print_gap_ts` marks paper maker samples as `print_gap` [clob_ws.py:211; maker_bid.py:340-344] | Prints during the gap are never seen — paper fills are under-counted, live `poll_gtc_fill` is unaffected [maker_bid.py:283-288]; tape/micro-tape have holes. Data: 73 `CLOB feed dropped` events (mostly `1013 slow consumer: send buffer full`) and 3 `no PONG … Reconnecting` heartbeat trips in the same 30h55m log copy [e.g. 78, 89, 101, …; 786, 791, 803]; `CLOB DROPS — unreadable messages … event:new_market 28077` [~815]. |
| **Gamma API** (discovery, `price_to_beat`, resolution `final_price`, labels) [market_scanner.py:40, 74-99] | `/events` → `/events/slug/{slug}` fallback latched on non-transient error [83-97]; SWR caches so the wake path never blocks [297-329; main.py:795-823]; DNS-error log throttle [352-355]; resolution falls back to a coherent resolved CLOB book [main.py:1708-1715]; orphan path after 30 min uses **trusted** Chainlink boundary captures for both ends, else waits and pages hourly [1859-1914]; strike falls back to our own trusted capture [1428-1435]; labels retried every 60s for 40 min then dropped [recording.py:34-35, 294-307] | No new contract → no entries (`find_active_contract` returns None [1620-1622]); labels for windows unlabeled after 40 min are never written (kill-bar corpus gap); a position may sit `pending_resolution` until Gamma or captures resolve it, which blocks day-close [1989-2000]. |
| **Polymarket CLOB REST + `py_clob_client_v2`** (live orders, balance, allowance) | 3 FOK attempts, only provably-unposted failures retry [live_trader.py:58-66, 1792+]; auth errors latch → `AuthError` → loop exits with Discord alert, exit 1 [1742-1743; main.py:2341-2354]; allowance preflight at boot, recheck every 10 fills [2462-2473; live_trader.py:67] | Bot process stops on `AuthError` (FATAL path, exit 1 [main.py:3203-3209]); supervisor restarts in 60s; if preflight then fails, `main()` returns exit 0 → commit → `sleep 10` → relaunch, repeating until 23:30 ET [2471-2473; run_polybot.sh:41-52, 56-63, 69-79]. The comment at main.py:2342-2344 ("won't retry until the next 12:01 AM ET start") describes a wait the script no longer performs during trading hours [run_polybot.sh:73]. |
| **Discord** (alerts, nightly ping, `!pause`) | Every send swallowed with a WARNING [alerts.py:35-39]; ping retried 3× and logged verbatim to the journal first [main.py:2943; alerts.py:118-137]; bot reconnect loop 5s→120s [main.py:2966-2977]; trading starts without Discord after 15s [2980-2984] | Silent loss of alerts (including the SOURCE-mismatch page [2691-2697] and kill-rule verdict); `!pause` unavailable. No alternative alert channel exists in the repo. |
| **Git autocommit/push** (`polybot/config/settings.yaml`, `polybot/memory`, `polybot/db`) | Commit only on exit 0; push retried once after 10s; failure printed to the supervisor's stdout (journal) [run_polybot.sh:41-52]; failed pull → `rebase --abort`, run existing code [21-24] | Records accumulate unpushed; the next day's `pull --rebase --autostash` runs against local commits; nothing pages on `PUSH FAILED`. A nonzero exit (crash, `AuthError`, second SIGTERM) skips the commit entirely for that cycle [50-52]. |
| **Nightly scheduler window** (23:45-23:49 ET, checked every 60s) [scheduler.py:86-89] | Per-job 600s budget; job failures alert; abandoned `to_thread` bodies keep running [62-67] | If the process is not alive during the 5-minute window (e.g. mid-restart), the pipeline does not run that night, `_shutdown_requested` is never set, and the process runs until an external stop; the supervisor then commits whenever it eventually exits 0. |
| **Local disk (1 GB RAM / 45 GB host per code comments)** [recording.py:703-711; main.py:2758-2760] | gzip-first nightly with a 540s deadline and WARNING per file left raw [698-760]; 30-day recordings retention [763-789]; 90-day `window_paths` and outlier trims [792-805; main.py:2710-2714]; `write_json_atomic` for every state file [paths.py:20-31] | Raw days left uncompressed accumulate (local checkout shows 1.2-2.2 GB raw `micro_*.jsonl` alongside `.gz` copies for 08-18 and 08-20). |
| **`settings.yaml` on the box as the only brake** (`trading_enabled`) | In-process flip by the SOURCE gate [main.py:2679-2698]; validator rejects malformed files at boot [loader.py:153-156] | A boot-time validation failure raises `ValueError` from `load_config()` inside `main()` → FATAL exit 1 → supervisor crash-restart every 60s until fixed [3203-3209; run_polybot.sh:56-63]. |
