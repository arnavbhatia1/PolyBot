# Critical + medium gap fixes batch

## Sizing (3)
- [x] Invert cap ordering: apply absolute caps EARLY (right after Kelly), then multiplicative discounts. So uncertainty/correlation discounts actually reduce below the cap instead of being no-ops when cap binds.
- [x] Continuous uncertainty discount: tighten regularization factor (0.50 → 0.35) and lower floor (0.50 → 0.40) so the function smoothly decays from ~0.40 at n=0 to ~1.0 at n=500 rather than being floor-pinned for the first 140 trades.
- [x] Size-weighted correlation: scale rho by `existing_position_size / max_single_position_usd`. A $0.50 position contributes `0.75 × (0.5/18) = 0.02` worth of correlation, not the full 0.75.

## Entry (3)
- [x] Pre-submit edge re-check: right before `trader.open_trade`, re-read CLOB ask, recompute `edge = signal.prob - current_ask`; skip if edge dropped below `min_edge`.
- [x] Spread gate uses execution cost: gate on `(ask - mid) / mid + fee_rate` instead of just `spread_pct`.
- [x] price_sum tolerance: tighten [0.98, 1.02] → [0.99, 1.01].

## Scalping (1)
- [x] Tighten evaluate_hold override: `hold_min_prob` 0.50 → 0.70, `panic_edge` -0.20 → -0.10.

## Flipping (1)
- [x] Spread-relative flip premium: flip gate becomes `min_edge + max(flip_premium, spread_pct)` instead of fixed additive.

## Loss handling (2)
- [x] Concave Kelly curve: `sqrt(ratio)` in circuit breaker between floor and tier. Shallow drawdowns penalized less, deep ones aggressively.
- [x] Persist adverse selection deque: save FillEvent list to `memory/adverse_state.json` on trade open/close; reload on startup.

## Pipeline (4)
- [x] Backtest execution penalty: new config `execution.backtest_realism_factor: 0.85`; multiplier on per-trade `gain_pct` inside `_kelly_bankroll_returns` to account for untraced slippage/latency/rejects.
- [x] Rollback detection: pipeline computes in-live Sharpe for adopted version; if worse than prior version's baseline after ≥7 days and ≥100 trades, log a prominent "ROLLBACK RECOMMENDED" warning. No auto-revert yet — user decides.
- [x] Platt min validation trades: 20 → 50. Also add z-test floor (z ≥ 1.0 on Sharpe improvement in addition to `new > old`).
- [x] Autocorrelation penalty on Sharpe z-test: compute 1-lag autocorr of returns, inflate SE by `sqrt(1 + 2 × max(0, autocorr))`.

## Verify
- [x] Run tests: `python -m pytest polybot/tests/`
- [x] Update CLAUDE.md
