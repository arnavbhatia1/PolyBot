# Integration Fixes — 2026-05-28

End-to-end trace walk after Pillar 1+2+3 closure. Fix-on-the-fly pass. Tests went 542 → 552 green.

## What got fixed

1. **Stage 2 — `sub_threshold_prob` ghost path constructed `trade_context` manually without `**aux_signals`; now splats the 13 Pillar-1 aux fields so schema parity matches the downstream `_ghost()` helper** (`polybot/main.py:932`).
2. **Stage 4 — `IndicatorEngine.compute_all` was copying `score → norm_score`; legacy historical outcomes carry stale `norm_score: 0.0` (pre-Pillar-2 IndicatorNormalizer artifact) and the readers preferred `norm_score`, silently zeroing L4 for every pre-fix trade in the 60-day backtest window. Removed the duplicate write; readers in `signal_engine._s` and `scheduler._ind_score` now read `score` directly** (`polybot/indicators/engine.py:67-70 removed`, `polybot/core/signal_engine.py:361-362`, `polybot/agents/scheduler.py:789-790`).
3. **Stage 5 — `flip_insufficient_edge` skipped without writing a ghost record while every other post-signal cost gate ghosted; added `_ghost()` call to match `edge_cap`/`adverse_selection`** (`polybot/main.py:1028`).
4. **Stage 5 — `sprt_side_mismatch` skipped without ghosting while its siblings `sprt_skip` and `sprt_low_confidence` both ghosted; added `_ghost()` call for symmetry** (`polybot/main.py:1056`).
5. **Stage 11 — `flush_gate_stats` fired only after resolution / orphan paths; scalp closes left intraday telemetry stale until the next resolution. Centralized the background flush into `_record_outcome` (single source of truth) and removed the two duplicate resolution-path calls** (`polybot/main.py:662-665`, `polybot/main.py:2149 removed`, `polybot/main.py:2278 removed`).
6. **Cross-cutting — Bybit staleness gates checked `state.oi_updated`, which only advances when the OI value changes (signal-conditional, can be silent for minutes with a live stream). Switched to `state.perp_age_s` / `state.perp_updated`, which advances on every ticker tick — matches what `_build_aux_signals` already uses for the per-field staleness check** (`polybot/main.py:758-763`, `polybot/main.py:1749-1752`).
7. **Stage 13 — Two `PIPELINE_PARAMS` descriptions described pre-Pillar-2 signal sources: `spot_flow_weight` said "L3b Binance CVD" (it's Coinbase now), `liquidation_weight` said "L3e Bybit OI" (it's direct liquidation streams now). Brought them in line with code** (`polybot/config/param_registry.py:32-33`).

## Schema/contract changes

None. The only behavioral schema touch is `sub_threshold_prob` ghosts now carrying the same 13 aux fields every other ghost carries — readers default-on-missing, so old ghost records are still consumable.

## CLAUDE.md updates

- L1 paragraph now documents the canonical BTC price source (`_fastest_btc_price`: Coinbase WS <2s → Binance aggTrade <3s → Binance kline receipt <5s; all-stale skips the decision).
- Feed staleness skip table replaced "Bybit OI >60s" with "Bybit ticker >60s (funding/mark/basis aux signals + liquidation-stream liveness)" — matches the new `perp_age_s` gate.
- "WS is the only OI source (Bybit ticker stream)" → clarified that Bybit ticker supplies funding/mark/index/basis aux signals; OI is captured but no longer fed to any layer (L3e moved to direct liquidation streams in Pillar 2).

## Tests added or modified

`polybot/tests/test_integration_fixes.py` — 10 new regression tests, one per fix above. Each would have failed against pre-fix code. Full suite: **552 / 552 passing** (was 542).

## Decisions needed from you

None. Every fix was mechanical (schema parity, telemetry holes, doc drift, stale field references). No design calls touched live trading behavior or exit logic.

## What I did NOT touch

- `polybot/execution/*` (out of scope per prompt).
- Asymmetric pre-signal validity gates (`thin_book_depth`, `spread_too_wide`, `min_size`, `regime`, `book_freshness_skew`, `stale_prices`, `thin_clob_depth`) — these don't ghost today; opinion-call whether to expand ghost coverage to "market not ready" gates. Current pattern: gates that ghost are about model-signal quality vs. market conditions; gates that don't ghost are pre-signal validity. Left as-is.
- Redundant second `_build_aux_signals` call at the HOLD-path counterfactual snapshot (`main.py:1872`) — wasteful but not a leak; reusing `_hold_aux_local` from `main.py:1814` would be a refactor.
- Refactoring `sub_threshold_prob` ghost path to call `_ghost()` directly — `_ghost()` gates on `signal.action in ("BUY_YES", "BUY_NO")` and sub_threshold_prob has `action == "SKIP"`. Generalizing would expand scope beyond the leak (which is now fixed in place with the `**aux_signals` splat).
- Pipeline rebaseline / bot operation — explicitly excluded ("no bot reset, just implement the learning pipeline for now").
