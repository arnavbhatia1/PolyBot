# 02 — Data facts (snapshot 2026-08-27 20:27 UTC, box HEAD 03349951)

Sources: `docs/audit/data/` — `polybot_paper_audit.db`, `polybot_live_audit.db`
(SQLite online backups taken 20:27Z), `polybot.log` (08-26 13:31 → 08-27 20:26,
1,664 lines) + `polybot.log.1` (07-13 00:26 → 08-26 13:30, 45,959 lines),
`memory/state/*.json` copies. Era split = 2026-08-14 00:00 UTC (epoch 1786665600).

## DB — paper (`polybot_paper.db`)
- bankroll $396.37, peak_bankroll $400.00 (operator set both to 400 on 08-25 18:53Z with the bot stopped; a running-bot write on 08-24 was clobbered by the in-memory mirror).
- window_labels: 13,851 rows; 3,720 in the 60s era, all with final_price.
- positions: 38, all `signal_leg=deep_proj`, 2026-08-18 → 2026-08-27; sum pnl +$23.51; 0 open; every trade_history exit_reason = `resolution` (no other exit path recorded). The paper DB was reset 08-17 (box `backups/reset_20260817_*`); earlier paper history is in git-committed DB snapshots only.
- fills by validation epoch (entry_timestamp ≥ epoch): 08-19T13:00Z → 36 fills, +$3.17, 27 wins; 08-22T02:40Z → 19, +$41.51, 15W; 08-24T15:40Z → 16, +$21.12, 12W; 08-27T19:28Z (current) → 0.

## DB — live (`polybot_live.db`)
- bankroll $123.40, peak $153.21. positions 337, 2026-07-05 → 2026-08-15, 0 open.
- 60s era: 5 fills, all deep_proj (08-14 live probe), −$34.18. Pre-era: 331 positions with no signal_leg (the deleted base strategy) +$20.50, and 1 lock_dip +$0.47.
- exit_reasons: resolution 316, `scalp` 19 (pre-07-08 exit engine era), `reconcile_recovery_gamma_win` 2.
- No live order has been placed since 08-15; `fill_stats.json` last updated 08-13 (1,529 attempts / 359 fills, fok_killed 566, precheck_depth 361).

## Logs — event counts (both files, 07-13 → 08-27)
| event | count | when / note |
|---|---|---|
| RESOLUTION SOURCE CHANGED (hard gate) | 0 | never fired |
| SOURCE CHECK SKIPPED | 32 | no trusted boundary to compare (restart-straddling windows) |
| RESOLUTION DRIFT (per-window warn) | 6 | 5 on 08-14 22:30 → 08-16 17:11 (the wrong-stream days) + 1 on 08-07 11:56Z (pre-era) |
| TAPE VERDICT | 379 | |
| KILL RULE TRIPPED (nightly) | 5 nights | 07-31 → 08-04, pre-era live (old strategy) |
| LATENCY BUDGET | 0 | shipped 08-22; no stamped taker fill since |
| MAKER BID REJECTED | 0 | |
| MAKER LADDER (rests) | 3,944 | |
| MAKER FILLED (banner) | 321 | all eras incl. pre-reset paper |
| MAKER OFF | 3,125 | |
| MAKER RUNG SKIPPED | 1,355 | 5-share minimum starvation, concentrated 08-22..24 |
| MAKER UNBOOKED / CRITICAL no slot | 2 | 08-11 19:16, 08-12 13:36 — live post-close 0.99 probe fills that could not book |
| MAKER CANCEL failed | 2 | 08-13 exchange 500 "cancel request failed" / 503 "cancels are disabled" |
| WINNING REDEEM STUCK (CRITICAL) | 1 | 07-30, position 207 |
| GUARD oracle relay froze (twap_frozen) | 68 | |
| SKIP stale chainlink | 488 | |
| strike unverified skips | 638 | |
| Reconnecting (any feed) | 928 | CLOB no-PONG 107 |
| FEED DROPS / CLOB DROPS warnings | 207 / 1,467 | CLOB drops are `event:new_market` messages |
| Nightly job timed out (600s, pre-fix wording) | 5 | maker_ladder 08-20/21, sniper_health 08-17/21, compress 08-21 |
| Nightly job abandoned (600s) | 4 | maker_ladder 08-23/24/25/26 |
| EXIT FORCED (shutdown watchdog) | 3 | 08-20, 08-21, 08-25 |
| Tracebacks | 27 | all in `trading_loop` (log.1 line refs 2913/2951/2977/3023 — pre-refactor numbering) |
| Boot banners ("ready") | 127 | |

## memory/state files
- `gate_stats.json` (cumulative): dominated by pre-rip ML-era gate names (`model:below min prob 56%` 5,503,173; `loss_cut_fired` 1,269,585; `cvd_decel` …) — history from deleted code. `gate_stats_current.json` (08-27): book_freshness_skew 17,398 / stale_prices 261 / stale_feed 3 of 17,662 skips — the WS-freshness gate is the dominant live skip.
- `feed_staleness.json` (08-27): chainlink inter-report gaps p50 0.938s / p95 1.715 / p99 2.162 / max 8.537 (n 2,000 of 4,606); clob_ws p50 0.0 / p99 0.021.
- `scar_gates.json`: one shadow gate `atr_regime=LO` (discovered 07-27, status shadow, n=18) — dimension from a deleted feed. `adverse_state.json` last saved 08-03; `sprt_burst.json` frozen 07-25; `cf_watchlist.json` empty (08-03). `orphan_positions.json` 08-14: 0 orphans.
- `latency_stats.json`: FOK n=2 (08-13); `gtc` section absent (no live GTC since instrumentation).
- `prev_resolution_margin.json`: $11.08 (08-27). `day_open_bankroll.json`: 08-27 $393.51.

## Host facts (read-only, 2026-08-27 20:44 UTC)
- Host `polybotvcn`: up 25 days (last boot 2026-08-02 04:05 UTC); RAM 954 MB total, ~483 MB available at check; swap 4 GB (55 MB used at check), swappiness 60.
- `polybot.service` accounting at its 08-27 06:45:53Z stop: 2h49m CPU, **711.2 MB memory peak, 1.6 GB swap peak** (journalctl -u polybot).
- **08-27 06:45:53Z mid-session restart attributed**: `apt-daily-upgrade.timer` fired 06:45:17Z; unattended-upgrades installed libssl3t64/openssl 3.0.13-0ubuntu3.15 (apt history 06:45:39–06:45:46) and needrestart cycled `polybot.service` (systemd "Deactivated successfully" → "Started" 06:45:53). The wrapper's boot `git pull` failed ("Could not resolve host: github.com" — networkd was restarting) and the bot came up on existing code at 06:46:13Z. A paper ladder resting at 06:44:35 ($58 Down) was lost with the process; no bot-side shutdown line exists for this event. The timer runs daily at ~06:45 UTC (02:45 ET), inside trading hours; the 08-26 run upgraded nothing that linked the service.
- Unit starts since 08-15 (journalctl): 14 — 08-15 21:22, 08-17 ×6 (14:34–20:17, the reset/zombie incident), 08-18 16:20, 08-19 12:30, 08-22 02:21, 08-24 15:07, 08-25 18:53, 08-27 06:45 (needrestart), 08-27 19:03. All but 08-27 06:45 are operator/deploy actions.
- Kernel OOM history: one kill, 2026-08-13 17:16 UTC — an interactive `python` under a user session scope (analysis run on the box), not the service.
- `crash_native.log` at the repo root is 0 bytes (created 07-13); no native crash has ever been recorded there.
