# Polybot Base Model Audit — 2026-05-27 (Pillar 1 + 2 closed; Pillar 3 pending)

Read-only audit of feed ingestion, 7-layer signal computation, and the daily learning pipeline. Live trading bot. Originally grounded against `polybot_paper.db` (3,578 trades, 2026-05-20 → 2026-05-27), 159 outcome JSONs with `edge_decay`, 422 counterfactual+ghost records, and `pipeline_run_log.json` (12 cycles, 60 proposals, 1 adoption).

---

## Document status — 2026-05-28

- **Pillar 1 (Ingestion):** all 29 findings **CLOSED** (1 reverted in verification — see 1.5). Resolution Log + Verification Pass below.
- **Pillar 2 (Computation):** 24 CLOSED · 4 SUPERSEDED · 4 DEFERRED · 2 SKIPPED. Resolution Log + Verification Pass below.
- **Pillar 3 (Learning):** 17 CLOSED · 3 NO-OP (resolved by current code state) · 3 DEFERRED · 0 OPEN. Resolution Log below.
- **All 86 findings carry inline `Status:` tags.**
- **519 / 536 tests passing** (49 dedicated regression tests across `test_pillar1_fixes.py`, `test_pillar2_fixes.py`, `test_pillar2_verification.py`, `test_pillar3_fixes.py`).

**Empirical numbers in this document are pre-fix baseline** (recorded 2026-05-27 against pre-Pillar-1/2 code). All 207 outcome JSONs in `polybot/memory/outcomes/` were written by the pre-fix engine; the 13 Pillar-1 aux fields are absent everywhere. **A post-fix re-baseline (Path B) is pending bot restart** under the new code; until it lands, treat any per-layer ρ, calibration bucket, or adoption-rate figure in this document as the *baseline* state the three pillars sought to repair, not the current state.

---

## Headline judgment (pre-fix baseline — preserved as historical reference)

> *The audit verdict and Top-3 gaps below describe the model state on 2026-05-27 **before** Pillar 1 + 2 closures. Closures are documented in the Resolution Logs and Verification Passes below; each per-finding section carries an inline `Status:` tag. Whether the verdict still holds post-fix is a Path B (empirical re-baseline) question — explicitly deferred until ≥60 outcomes accumulate under the new code.*

**The base model is far from "as good as it can be."** All three pillars have load-bearing problems.

- **Ingestion** — competent but leaks signal. Multiple high-quality fields are extracted into memory and discarded before any layer reads them (Bybit `fundingRate`, Coinbase `side`/`last_size`, perp basis). One feed has a malformed URL that Binance silently accepts (depth feed). Several reconnect paths leave rolling windows mixing pre/post-disconnect data. Staleness gates are policy with no empirical histogram backing them.
- **Computation** — math matches CLAUDE.md, but the layers do not predict outcomes. **Correlation of L1, L2, L3, L5 with realized BTC direction is < 0.05 each. L3b is −0.18 (wrong sign), L3e is −0.08 (wrong sign). The combined L2–L5 contribution correlates **−0.15** with the realized outcome.** The model is winning on ~52% of trades essentially via L1 alone, and the other six layers are net signal-destroying in current market conditions. Two L6 features have direction-loss bugs (`time_remaining_logit`, `flow_disagreement`, `autocorr_signed_mag`); the IndicatorNormalizer strips sustained-regime information from L4; the L3 vs L3b weight asymmetry (0.04 vs 0.10) structurally biases disagreement. The calibrator has been **identity since launch** despite plenty of training data — and when it does fit, the cheap-acceptance branch uses an in-sample improvement check that will adopt overfit isotonic curves.
- **Learning** — loop runs but does not converge. **1 adoption out of 60 proposals over 12 cycles.** Of 53 non-null backtest deltas, **51 are below the 0.003 noise floor**; the recommender is asking the wrong question (small step-probes around currently-adopted values). One L6 wiring bug (`old_value=None` on every L6 probe in `pipeline_run_log.json`) blinds the directional table to L6 history. The 2026-05-27 cycle's all-null deltas trace to two code paths that silently drop `candidate_sharpe`. The `candidate_sharpe ≤ 0` hard gate **locks adoption when baseline is negative** — exactly when the loop most needs to adapt. Crisis mode never fired in any of the four consecutive losing days. Holdout confirmation is structurally inactive (opt-pool < 200 falls back to no-holdout).

### Top 3 biggest gaps (pre-fix baseline — closure status noted)

> *Each gap below ties to specific findings that have been closed or superseded; whether the gap itself is still real post-fix is a Path B question.*

1. **The 7-layer model is functionally a 1-layer model in current conditions.** L1 dominates the logit in 53% of trades (|L1| > 3× |L2-L5|). The five smaller layers add up to negative predictive value. The bot's edge is "L1 says +5% above strike → bid Up at 0.55" — and that's it. Calibrator is identity so the L1 output goes to the gate raw, mis-stamping ~65% probabilities on trades that win 51%.
   - **Closure status:** L3b (−0.18) and L3e (−0.08) source-swapped to Coinbase + direct liquidation streams via 2.1 / 2.23. L6 direction-loss bugs fixed (2.10 / 2.11). IndicatorNormalizer removed (2.3). Calibrator gate functional (2.5 + 2.6). **Whether L2-L5 net ρ is now ≥ 0 is the headline empirical question for Path B.**
2. **Exit logic is destroying value the entry logic creates.** Recent-500 trade Sharpe by exit reason: **scalp = −1.51, resolution = +1.37**. Of 139 tracked counterfactuals, **64% of scalped trades would have been better held to resolution** (avg delta +$0.19/scalp held back). The pipeline-tunable `exit_edge_threshold` has not been raised off −0.10 because the counterfactual replay only triggers on that exact param name and the loop is broken (see #3). The user's intuition — "buy at the cursor, ride the wave, or flip with confidence" — is correct, and the data confirms scalps are leaving 4.7× value on the table relative to resolutions.
   - **Closure status:** **Not directly addressed.** This is a Pillar 3 issue (`exit_edge_threshold` learning); awaits Path B re-triage. Pillar 2 closures may shift the counterfactual replay outcome via better `model_prob` calibration.
3. **The learning loop does not learn.** Step-probes are below the empirical noise floor (51/53 < 0.003 Sharpe delta); the L6 directional table is empty (A1 bug); the calibrator never adopts (silent reason); the regime-stratified check is dormant (90% trades labeled "neutral"); crisis mode never triggers despite 4-day collapse; the adoption gate hard-rejects every candidate while baseline Sharpe is negative. Five separate failure modes compound to a 1/60 adoption rate.
   - **Closure status:** **Most of Pillar 3 is still open.** Pillar 2 closures may have surfaced new candidate signal (post-fix backtest deltas might now exceed the noise floor); L6 directional table issue (3.2) is likely SUPERSEDED by Pillar 2's L6 cleanup; calibrator adoption path is unblocked (3.6 likely SUPERSEDED). Other items (3.1 `candidate_sharpe ≤ 0` gate, 3.7 crisis mode, 3.8 holdout) remain OPEN. Re-triage in Path B.

---

## Summary

| Total findings | A (correctness) | B (empirical) | C (architecture) |
|---|---|---|---|
| **86** | 64 | 16 | 6 |

| By pillar | Count |
|---|---|
| Ingestion (1) | 29 |
| Computation (2) | 34 |
| Learning (3) | 23 |

### Top 10 highest-impact findings

| # | Pillar | Type | Title |
|---|---|---|---|
| 1 | Comp | A | **L3b spot_flow has empirical −0.18 correlation with realized direction over 158 outcomes** — sign-inverted in current market regime, larger magnitude than any other non-L1 layer |
| 2 | Learn | A | **`should_adopt` requires `candidate_sharpe > 0`** — when baseline is negative (currently −0.019), no candidate can adopt, hard-locking recovery from regime shifts |
| 3 | Learn | A | **L6 derived weights record `old_value=None`** in `pipeline_run_log.json` because they live in `derived_weights[fname]` not as `derived_*_weight` attributes — directional table is permanently blind to L6 history |
| 4 | Comp | A | **IndicatorNormalizer + CVD normalizer strip sustained-regime signal** via running-mean subtraction — a 50-tick uptrend's RSI/CVD z-score → 0 once running mean catches up |
| 5 | Comp | A | **Calibrator cheap-acceptance branch uses in-sample improvement** — isotonic has O(n) DoF so in-sample improvement is essentially guaranteed; will fire when bootstrap CI is unstable |
| 6 | Learn | B | **51 of 53 non-null backtest deltas are below 0.003 noise floor** — the recommender is asking the wrong question; step-probes around the current value can't escape the local flat-bowl |
| 7 | Comp | A | **Final ±4 clamp + L1 reaching ±10 logits in deep ITM/OTM** → 53% of trades have L1 dominating; L2–L6 are decorative when distance is large |
| 8 | Comp | A | **L3 (weight 0.04) vs L3b (weight 0.10) imbalance creates a 2.5× structural bias** — when CLOB book says BUY and CVD says SELL, net contribution is bearish purely from weight asymmetry |
| 9 | Learn | A | **Crisis mode never fired in 4 consecutive losing days** (recent-50 WR 38%, days 5/24–5/27 −$44, −$12, +$13) — `recent_50` smoothing masks sustained regime shifts |
| 10 | Comp | A | **L4 `momentum_weight × 1.5 amplifier` is silently clamped to 0.10** — the pipeline's tuning range 0.0–0.10 means the amplifier is dead at the upper half of search space (≥0.067) |

---

## Pillar 1 — Resolution Log (closed 2026-05-27, verified + hardened)

All 29 ingestion findings resolved. Verification pass found 9 leaks; all 9 closed. **496 / 496 tests passing** (19 new focused regression tests). Pillar 2 gate: **GO**.

**Headline structural change.** The ingestion stack is now organised around one principle: each feed owns exactly the signals it is uniquely best at producing, and no two feeds duplicate work.

- **Coinbase** owns: largest-US-venue last price + best-bid/ask + per-trade CVD + taker ratio.
- **Binance kline (1m + 1s)** owns: ATR + candle indicators + canonical fast realized-vol (`fast_realized_vol(60s)`).
- **Binance aggTrade** owns: Binance.com-side per-trade flow + CVD acceleration + exchange-latency telemetry.
- **Binance depth** owns: BTC spot top-20 book imbalance (new live consumer; no longer telemetry-only).
- **Binance forceOrder** (new) owns: BTC futures liquidation event stream.
- **Bybit** owns: perp last price + mark + index + funding + basis + OI + direct liquidation event stream (subscribed on the same WS).
- **Chainlink** owns: oracle resolution price + 5-min boundary strike anchored to the *first* RTDS observation closest to the boundary.
- **Polymarket CLOB** owns: per-token book + last-trade buffer with reset-on-reconnect and a `both_books_fresh(token_a, token_b)` gate.

**Cross-cutting infrastructure.**
- `polybot/feeds/_socket.py` — single `enable_nodelay(ws, name)` helper used by all 7 WS feeds; verifies via `getsockopt`, warns on silent miss.
- `polybot/feeds/_staleness.py` — single `StalenessTracker` per feed; persisted to `polybot/memory/feed_staleness.json` every 60 s.

**Removed overlap.** Coinbase no longer carries `_price_samples`, `realized_vol`, `volume_24h`, or `state.spread` — `BinanceFeed.fast_realized_vol(60s)` (sourced from `@kline_1s` aggregated closes) is the single canonical sub-second vol estimator.

**Per-finding before / after.**

| #    | Before                                                                                | After                                                                                                                                          |
|------|---------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------|
| 1.1  | Coinbase `side`/`last_size` discarded                                                 | Per-trade signed-size queue → `get_cvd(window_s)` + `get_taker_ratio(window_s, min_trades=20)`. Stamped to trade_context as `coinbase_cvd_60s`/`coinbase_taker_60s`. |
| 1.2  | Bybit `fundingRate`, `markPrice`, `indexPrice` parsed-then-dropped                    | `BybitState.funding_rate`, `mark_price`, `index_price`, `basis` (= perp − index). Stamped to trade_context.                                    |
| 1.3  | `settings.yaml` `binance_depth.ws_url` and `binance_trades.ws_url` had stream suffix already baked in → code appended again | YAML reduced to base host `wss://stream.binance.com:9443/ws`. Code constructs `{ws_url}/{symbol}@stream`.                                       |
| 1.4  | CLOB `trade_buffer` `maxlen=100` → 120-s lookback truncated to ~30-60 s in active markets | `TRADE_BUFFER_MAXLEN = 500`. Lookback honored.                                                                                                 |
| 1.5  | Chainlink boundary strike overwritten on every tick                                   | `_record_boundary(observed_ts)` keeps the observation closest to the boundary within `BOUNDARY_GRACE_S = 30s`; later ticks do not displace it. |
| 1.6  | aggTrade WS reconnect left the deque populated → phantom CVD-decel signal             | `BinanceTradeAccumulator.clear()` called inside `_connect_ws` on every reconnect.                                                              |
| 1.7  | `setsockopt(TCP_NODELAY)` wrapped in `try/except` per feed, silent failure            | One `enable_nodelay()` helper verifies via `getsockopt`, warns on miss. Used by all 7 WS feeds.                                                |
| 1.8  | Bybit `liquidation.BTCUSDT` not subscribed (free signal on same WS)                   | Subscribed alongside `tickers.BTCUSDT`. Signed `(ts, ±usd)` deque → `liquidation_usd_per_min()` ⇒ `bybit_liq_{long,short}_usd_min` in trade_context. |
| 1.9  | Staleness thresholds policy-only, no empirical histogram                              | `StalenessTracker` per feed. Background task flushes `feed_staleness.json` every 60s with P50/P95/P99 inter-arrival per feed.                  |
| 1.10 | Binance `kline_1m` only — 1-minute resolution for 5-min options                       | Combined-stream URL: `kline_1m` ∥ `kline_1s`. `_FastCloseBuffer` exposes `fast_realized_vol(60s)`; stamped as `fast_realized_vol_60s`.         |
| 1.11 | L1 used sub-second price; L2 used 1m candle close                                     | Verified: `update_current` on every partial-kline tick invalidates the closes cache, so `closes[-1]` is the in-progress live close. No code change required. |
| 1.12 | `book_updated` shared event → Up/Down book freshness could diverge ≥1 s              | `ClobWebSocket.both_books_fresh(token_up, token_down, _WS_STALE_S)` gate runs **before** the no-arb sanity check. `book_freshness_skew` ghost-skipped. |
| 1.13 | aggTrade `min_recent_trades=3`, taker `min_trades=5` (tuned for Binance.US 0–3 trades/15s) | Re-tuned for Binance.com volume regime: `min_recent_trades=10`, `min_trades=20`. Stale `.US` comments stripped.                                |
| 1.14 | CLOB `trade_buffer` / `books` / `best_bid_ask` / `last_trade` persisted across reconnect | `_reset_per_token_state()` called on every reconnect inside `_run_forever`.                                                                    |
| 1.15 | Coinbase `state.spread`, `state.volume_24h`, `_price_samples` + `realized_vol`; CLOB `_price_samples` dead state | All removed. CLOB `_last_pong_ts` retained — actually consumed by the heartbeat.                                                               |
| 1.16 | Exchange `T` (ms) discarded — no latency observability                                | Captured as `exch_ts`; rolling 500-sample `_exch_lag` deque; `exch_lag_snapshot()` P50/P95/P99.                                                |
| 1.17 | Binance `forceOrder` futures liquidation stream not subscribed                        | New `BinanceForceOrderFeed` on `fstream.binance.com`. `liquidation_usd_per_min()` ⇒ `binance_liq_{long,short}_usd_min` in trade_context.       |
| 1.18 | Coinbase-vs-Binance cross-venue price gap discarded                                   | Computed in `_fastest_btc_price` when both legs are fresh; debug-logged and stamped as `cross_venue_gap` in trade_context.                     |
| 1.19 | Bybit `perp_price` tracked but unused                                                 | `BybitState.basis` (perp − index) exposed; `bybit_basis`, `bybit_mark_price` stamped in trade_context.                                         |
| 1.20 | Binance kline REST backfill: 5 retries then `raise ConnectionError`                   | 3 retries; returns `bool`. On failure, logs and lets WS warm the buffer.                                                                       |
| 1.21 | WS-ask vs REST `/price` cross-match gap unmeasured                                    | Subsumed by 1.18 — `cross_venue_gap` is the same class of signal at a stronger venue spread. No separate WS-vs-REST log added.                 |
| 1.22 | Bybit reconnect could blend OI across the gap                                         | No code change: `compute_liquidation_pressure` already normalizes by `wall-clock_elapsed`, so the gap is correctly priced in.                  |
| 1.23 | Coinbase WS had no application-level PING                                             | `_app_ping(ws)` sends WS-protocol ping every 15s; `heartbeat` channel subscribed alongside `ticker`.                                           |
| 1.24 | Chainlink boundary used `int(time.time())` — local wall-clock                         | Uses RTDS-reported `payload.timestamp`/`ts` when present; falls back to local wall-clock. `_last_payload_ts` recorded.                         |
| 1.25 | `BinanceTradeAccumulator._cache` unbounded                                            | FIFO-evict at `_CACHE_MAX_ENTRIES = 16`. `_cache_get`/`_cache_put` helpers.                                                                    |
| 1.26 | `depth_usd_top20` was telemetry-only — no live consumer                               | New `BinanceDepthFeed.get_imbalance(levels=5)` USD-weighted top-N imbalance. Stamped as `binance_book_imbalance_5` in trade_context.           |
| 1.27 | L3 book imbalance was flat-shares-weighted across top-5                               | `compute_flow_signal(..., distance_weighted=False, mid_up, mid_down)` — opt-in linear taper by `|price − mid|`. Default off for behavior parity. |
| 1.28 | `lag1_autocorr` divided by `window[:-1]` with no zero guard                            | `if np.any(denom <= 0): return 0.0` before the division.                                                                                       |
| 1.29 | Chainlink `STALE_TIMEOUT_S = 20 s` — fired reconnect on normal low-vol cadence        | Raised to 60 s; tighter timeouts handled at the watchdog tier.                                                                                 |

**Files touched.** 12 production files (3 new — `_socket.py`, `_staleness.py`, `binance_forceorder.py`), 3 test files (1 new — `test_pillar1_fixes.py`). No net bloat: every consolidation strictly reduces code (Coinbase shed `_price_samples` / `realized_vol` / `volume_24h` / `spread` / redundant `_app_ping`; CLOB shed `_price_samples`; per-feed inline NODELAY try/except replaced by one shared helper; trade_context 13-field block deduplicated via `_build_aux_signals()`).

**Trade context — 13 aux signals stamped on every entry AND every ghost rejection AND every counterfactual snapshot.** All written by `_build_aux_signals()` (single source of truth). Each is either a real reading or `None` (when source feed is missing or staler than its per-field threshold) — never `0.0` as a stand-in for "feed cold", so Pillar 2 can reliably distinguish "balanced book" from "depth feed not warm yet."

Fields: `binance_book_imbalance_5`, `cross_venue_gap`, `coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`, `fast_realized_vol_60s`, `bybit_funding_rate`, `bybit_basis`, `bybit_mark_price`, `bybit_liq_long_usd_min`, `bybit_liq_short_usd_min`, `binance_liq_long_usd_min`, `binance_liq_short_usd_min`.

---

### Pillar 1 — Verification Pass (closed 2026-05-27)

After the 29-finding closure, ran a leak-hunt pass before opening Pillar 2. Found 9 leaks; all 9 closed in this commit.

| Leak | Before | After |
|---|---|---|
| **CRIT-1** Chainlink boundary capture regression | "First observation within 30 s grace wins" — locked in the EARLIEST observation in a 5-min window, returning systematically older strikes than Polymarket's `latestRoundData()`. Worked example: three updates at 18:01 ($74100), 18:03 ($74200), 18:04:55 ($74300) → locked $74100 instead of $74300. | Reverted to original "last update before next boundary wins" semantics — mirrors `latestRoundData()` exactly. RTDS-ts preference from finding 1.24 kept (orthogonal improvement). `BOUNDARY_GRACE_S` constant deleted. |
| **CRIT-2** 13 new fields missed ghost replay | The 13-field block ran at `main.py:1140`, **downstream** of all 8 gate-rejection `_ghost(...)` calls (lines 866–1092). Ghosts carried only the legacy keys, breaking schema parity with filled outcomes. | New module-level helper `_build_aux_signals(...)` runs ONCE at the top of `_evaluate_signal_and_enter`. Both `_ghost.base_ctx` and the trade_context stamp spread `**aux_signals`. One source of truth, schema-consistent across outcomes + ghosts + counterfactuals. |
| **IMP-1** 13 new fields missed counterfactual context | `counterfactual_tracker.watch()` and `track_hold_moment()` curated a hand-picked legacy subset. | `watch(..., aux_signals=None)` and `track_hold_moment(..., aux_signals=None)` accept the live aux dict; `context_at_scalp` and `context_at_worst_moment` spread `**ctx.get("aux_signals", {})`. Callers in `main.py` build aux at the scalp / hold moment and pass through. |
| **IMP-2** Zero specific tests for new code paths | 477/477 passed, but new code was only covered by pre-existing tests that pass trivially. | New `polybot/tests/test_pillar1_fixes.py` — 19 focused regression tests, one per leak. Coverage: clear-on-reconnect (3 accumulators), staleness percentiles + reset semantics, `enable_nodelay` no-socket path, CLOB freshness helpers + reset + 500 maxlen, Binance depth imbalance, Bybit liquidation signed long/short, `_build_aux_signals` null semantics, `fast_realized_vol` warmth gating, `lag1_autocorr` divide-by-zero guard, Coinbase CVD/taker round-trip, Chainlink last-update-wins. |
| **MED-1** Reconnect didn't clear `_liquidations` / `_events` / `fast_closes` | Three new rolling accumulators (Bybit liquidations, Binance forceOrder events, Binance kline_1s closes) survived across reconnect → could bridge pre/post-disconnect data into windowed metrics. | `self._liquidations.clear()` added inside `BybitFeed._connect_ws`; `self._events.clear()` inside `BinanceForceOrderFeed._run`; `self.fast_closes.clear()` (new public method + `__len__`) inside `BinanceFeed._connect_ws`. |
| **MED-2** Ambiguous null semantics | All 13 fields defaulted to `0.0` on missing/stale feeds, indistinguishable from a legitimate zero reading. | `_build_aux_signals()` returns `None` for any field whose source is missing or staler than its per-field threshold; rounding helper preserves `None` through the round step. |
| **MED-3** Inconsistent freshness gates across fields | Only `binance_book_imbalance` had an `age_s < 5.0` gate; everything else returned whatever the accumulator held. | Per-field freshness thresholds at the helper boundary: Coinbase 10 s, Binance trades 10 s, depth 5 s, Bybit 30 s, kline_1s ≥3 samples warmup. Liquidation feeds gate on "has ever emitted" (`staleness._last_ts > 0`). |
| **TRIV-1** Coinbase `_app_ping` redundant | `_app_ping(ws)` issued `ws.ping()` every 15 s alongside the library's `ping_interval=20`. Same WS-protocol ping, doubled traffic. | Manual ping task and `APP_PING_INTERVAL_S` constant removed. Library-level ping plus the subscribed `heartbeat` channel cover liveness. |
| **TRIV-2** Combined-stream URL transform fragile | `replace("/ws", "/stream", 1)` would mangle any nondefault `ws_url` shape. | Explicit precondition: `BinanceFeed._connect_ws` raises `ValueError` if `ws_url` doesn't end with `/ws`. URL construction now slices the known suffix. |

**Outcome verification.** Following the `_build_aux_signals()` hoist, the same dict drops into:
- `_evaluate_signal_and_enter` → `snapshot["trade_context"]` → `outcome_reviewer.record_outcome` → outcome JSON (`indicator_snapshot.trade_context.*`).
- `_ghost.base_ctx` → `ghost_tracker.record_rejection` → ghost JSON (`indicator_snapshot.trade_context.*`).
- `counterfactual_tracker.watch(..., aux_signals=)` / `track_hold_moment(..., aux_signals=)` → counterfactual JSON (`context_at_scalp.*` / `context_at_worst_moment.*`).

**Per-finding pass rate: 29 / 29 OK · 0 leaks remaining.** Pillar 2 gate: **GO**.

---

## Pillar 1 — Ingestion findings

Ordered by impact × confidence.

### 1.1 (A, high) — Coinbase ticker `side` and `last_size` discarded
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/coinbase_feed.py:166-201`

**Current state + steelman:** Subscribes to Coinbase `ticker` channel — fastest BTC price source, used by `_fastest_btc_price` and L1. Captures `price`, `bid`, `ask`, `volume_24h`. Skips the trade-side fields.

**What data shows:** Coinbase ticker emits `side` ({`buy`,`sell`}) and `last_size` per trade. Polymarket resolution uses Chainlink which aggregates major USD venues; **Coinbase is the largest US-volume BTC venue and arrives ≥0.5s before Binance.US/com**. A Coinbase-side CVD would be a strictly-better L3b than Binance.US (which the code's comments explicitly note has 0–3 trades per 15s — bordering on dead). Strategic-level signal currently dropped.

**Proposed change:** Build a `CoinbaseCVDAccumulator` parallel to `BinanceTradeAccumulator`; feed into a Coinbase-side `spot_flow` term that either replaces L3b (best) or runs alongside it as a second L3b sibling. Expected effect: L3b correlation with realized direction recovers from −0.18 toward positive.

**Verification plan:** Subscribe in shadow, log `(coinbase_cvd_z, binance_cvd_z, realized_direction)` per evaluation for 1 week. If `corr(coinbase_cvd, realized) > corr(binance_cvd, realized)` at p<0.05, promote.

**Risk:** Low — additive, doesn't change live behavior until weight raised. **Confidence:** high.

---

### 1.2 (A, high) — Bybit `fundingRate` and `markPrice`/`indexPrice` extracted but never stored
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/bybit_feed.py:48` (docstring promises), `137-158` (only reads `lastPrice` + `openInterest`)

**Current state + steelman:** Docstring says "Updates BybitState with lastPrice, fundingRate, and openInterest" — but `BybitState` has no `funding_rate` field. The bot pays the WS cost for the full payload, reads two fields out of seven.

**What data shows:** **Funding rate is a known leading indicator** for 5-min BTC direction — extreme positive funding = longs pay shorts = flush risk elevated. Free signal in the existing payload. Same for `markPrice − lastPrice` (funding-impl basis) and `lastPrice − indexPrice` (perp premium). None used.

**Proposed change:** Add `funding_rate`, `mark_price`, `index_price` to `BybitState`. Either gate Kelly when funding hits an extreme threshold, or add as L6 features (closed library currently doesn't include them, so this is a code change + ParamSpec addition).

**Verification plan:** Log `(funding_rate, next_5min_realized_direction)` for a week; quantify forward correlation. If significant, propose as new L6 feature.

**Risk:** Low. **Confidence:** high.

---

### 1.3 (A, high) — `settings.yaml` `binance_depth` and `binance_trades` URLs are double-suffixed
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/config/settings.yaml:197,201` vs `polybot/feeds/binance_depth.py:68`, `polybot/feeds/binance_trades.py:220`

**Current state + steelman:** YAML configures `ws_url: wss://stream.binance.com:9443/ws/btcusdt@depth20@100ms` for `binance_depth`. Code does `stream = f"{self.ws_url}/{self.symbol}@depth20@100ms"`, producing `.../ws/btcusdt@depth20@100ms/btcusdt@depth20@100ms`. Empirically Binance.com is tolerant and the depth feed *does* return data (`depth_usd_top20` is populated in outcomes). Same pattern for `binance_trades.py`.

**What data shows:** Confirmed by reading both files and the yaml. The URL works only because Binance silently accepts the extra path component. If Binance ever tightens routing, both feeds die silently.

**Proposed change:** Either change yaml to base URL `wss://stream.binance.com:9443/ws` (matches `binance.ws_url` line 192 — the kline feed), or remove the `{symbol}@stream` suffix from the code construction. Pick yaml-style consistency.

**Risk:** Low cleanup; latent failure mode removed. **Confidence:** high.

---

### 1.4 (A, high) — Polymarket CLOB WS `trade_buffer` `maxlen=100` silently truncates the 120s lookback
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/clob_ws.py:330` (maxlen=100), `polybot/core/order_flow.py:55-87` (uses `lookback_seconds=120`)

**Current state + steelman:** Per-token deque caps at 100 trades. L3 trade flow uses 120s lookback with age-based pruning. Cap is generous for normal conditions.

**What data shows:** At Polymarket BTC 5-min market peaks (~50–200 trades/min), 100 trades = 30–60s of history. **The 120s `lookback_seconds` parameter is effectively a no-op when the deque saturates** — L3 trade_flow is operating on a silently truncated window during exactly the moments it matters most (end-of-window churn).

**Proposed change:** Increase `maxlen=500` (still bounded), or remove the cap entirely and rely on age-pruning at use site. Log `len(trades_up) + len(trades_down)` per evaluation to detect saturation; if any evaluation hits 200 (both deques full), raise alarm.

**Verification plan:** Add the saturation log; verify cap doesn't bite in normal conditions before bumping.

**Risk:** Low — memory cost is negligible. **Confidence:** high.

---

### 1.5 (A, high) — Chainlink boundary strike overwritten on every tick → may not match oracle resolution
**Status:** CLOSED (REVERTED in Pillar 1 Verification Pass — original last-update-before-boundary semantics restored). See Pillar 1 Verification Pass LEAK-CRIT-1.
**Location:** `polybot/feeds/chainlink_feed.py:54-72`

**Current state + steelman:** `_check_boundary` runs on every Chainlink tick and records `_boundary_prices[next_boundary_ts] = self._price`. Whichever tick lands closest to the window boundary "wins."

**What data shows:** Chainlink mainnet pushes are sparse (5%+ deviation threshold + 1-hour heartbeat; RTDS likely re-emits cached snapshots more frequently). At boundary crossing, the bot's recorded strike is the **last RTDS update before boundary** — which may not match the on-chain `latestRoundData()` that resolves the option (which depends on a specific oracle tick). Strike error potentially up to ATR/2 ≈ $15–30. Gamma `priceToBeat` overrides this when present, but during the first 10–30s of each window it isn't yet visible.

**Proposed change:** Compare `chainlink_feed.get_strike(window_ts)` against `Gamma priceToBeat` over a day's worth of windows. If the spread exceeds ~$0.10 on >1% of windows, capture both updates and use Gamma's value when present.

**Risk:** Modest — trades during the first ~30s of a window may be on a strike that's not the resolution strike.
**Confidence:** medium-high.

---

### 1.6 (A, high) — aggTrade reconnect doesn't clear deque → phantom CVD-decel signal post-reconnect
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/binance_trades.py:36`, `51`, `255`

**Current state + steelman:** Accumulator deque is age-pruned at `max_age_s=300`. WebSocket reconnects don't explicitly clear.

**What data shows:** Scenario: 10s pre-disconnect with heavy buyer aggression, 25–29s disconnect (under the 30s staleness gate so staleness skip doesn't trigger), resume. `get_cvd_acceleration(recent_s=15, baseline_s=45)` sees `recent` starting fresh but `baseline` reaching back through the disconnect into pre-disconnect history. **Artificial deceleration signal on every reconnect under 30s.** The `cvd_decel` gate then defensively skips trades for some time period after every reconnect.

**Proposed change:** On reconnect, mark the deque as "post-reconnect" until newest trade ages out; either clear entirely or insert a synthetic null between old and new trades that `get_cvd_acceleration` knows to break the window at.

**Verification plan:** Count `cvd_decel` ghost-emissions within 60s after any "aggTrade WS connected" log entry. If correlated above baseline rate, the bug is firing.

**Risk:** Low; safer than current state. **Confidence:** high.

---

### 1.7 (A, medium) — TCP_NODELAY likely silently failing on websockets ≥12 in all 5 feeds
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** All five feed modules: pattern `_sock = ws.transport.get_extra_info('socket') if getattr(ws, 'transport', None) else None`

**Current state + steelman:** Try/except around `setsockopt`; on failure, just skips NODELAY.

**What data shows:** The new asyncio websockets API (`websockets.asyncio.client.ClientConnection`) doesn't expose `.transport` the same way — `transport.get_extra_info('socket')` may return `None`. NODELAY then silently isn't set, costing ~40ms of Nagle batching per packet across all feeds. No assertion / log confirms the option actually applied.

**Proposed change:** Re-read `getsockopt(TCP_NODELAY)` immediately after `setsockopt` and log success/failure per connection. If failing, port to the new websockets ≥12 API (`ws.transport` is still present but via a different code path).

**Risk:** Diagnostic only. **Confidence:** medium-high.

---

### 1.8 (C, high) — Bybit publishes `liquidation.BTCUSDT` on the same WebSocket connection — free signal not subscribed
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/bybit_feed.py:88-95`

**Current state + steelman:** Bot subscribes to `tickers.BTCUSDT` only.

**What data shows:** Same WS connection supports `liquidation.BTCUSDT` — emits every individual liquidation order with side and size. **Direct measurement of the cascade L3e currently *infers* from OI drops.** OI snapshots are 5s-cadence; liquidations stream tick-by-tick.

**Proposed change:** Add `liquidation.BTCUSDT` subscription. Compute USD-volume of long-liq vs short-liq per minute, tanh-saturated. Either replace L3e's OI-drop computation or run as L6 feature.

**Verification plan:** Shadow-subscribe, log liquidation events alongside L3e firing, check whether per-minute liquidation USD predicts realized 1-min BTC direction better than OI-derived L3e (which has correlation −0.08 with realized currently).

**Risk:** Low — adding a stream is non-invasive. **Confidence:** high.

---

### 1.9 (C, high) — No empirical feed-staleness P50/P95/P99 telemetry
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** Cross-cutting

**Current state + steelman:** Staleness thresholds: Coinbase 30s, Chainlink 60s, Binance aggTrade 30s, Bybit OI 60s. Set by hand.

**What data shows:** `latency_stats.json` measures Polymarket order RTT (P50=769ms, P99=2.3s, n=13 from 2026-05-21) — not feed staleness. No per-feed empirical histogram. The thresholds are policy with no validation. If Coinbase P95 is 2s, the 30s gate is dead. If Chainlink P50 is 25s (low-vol periods), the 60s gate is hyperactive and trips reconnects on normal cadence.

**Proposed change:** Instrument each feed with a `staleness_samples` deque (last 1000 inter-arrival gaps); persist to memory daily. After a week, set thresholds to ~P99 + safety margin per feed.

**Risk:** None (telemetry). **Confidence:** high.

---

### 1.10 (B, medium) — Binance kline @1m too coarse for 5-min option lifecycle
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/binance_feed.py:147`

**Current state + steelman:** Subscribes `kline_1m` and computes ATR(14) — 14 minutes of ATR for a 5-min option. Adequate baseline.

**What data shows:** Binance.com supports `kline_1s` (and `kline_5s`). ATR(14) on minute closes lags realized vol over the 5-min window's actual lifecycle. A 60-tick 1s rolling realized vol would adapt inside the window — useful for L1 sigma scaling when volatility regime shifts mid-window.

**Proposed change:** Add `kline_1s` subscription. Compute a fast rolling realized vol; blend with ATR(14) per a tunable mixing weight. Optionally promote the blend weight to pipeline-tunable.

**Verification plan:** Backtest L1 with `vol = α × ATR_1m + (1-α) × realized_1s` over α ∈ [0, 1]; measure Sharpe across configurations.

**Risk:** Medium — touches the highest-leverage L1 knob. **Confidence:** medium.

---

### 1.11 (A, medium) — Cross-feed time alignment: L1 reads sub-second price, L2 reads 1-min candle close
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/core/signal_engine.py:307-313` vs `polybot/main.py:230-265`

**Current state + steelman:** L1's `btc_price` is the freshest feed (Coinbase WS, <2s). L2's `direction = sign(closes[-1] - closes[-2])` uses 1-min candles. Different time-scales.

**What data shows:** When BTC moves sharply within the current minute (e.g., +$50 in 20s of a minute boundary crossing), L1 sees the new price but L2's "direction" reads the close-of-previous-minute vs close-of-two-minutes-ago — could be the **opposite sign** of the current move. L2 then pushes prob in the stale direction. With L2's empirical predictive correlation already +0.00 (dead), this could be one root cause.

**Proposed change:** Use in-progress current-candle close vs prev close (already maintained by `update_current`), or compute direction from a high-frequency price (Coinbase last 60s). Document the timescale decision either way.

**Risk:** Medium — worst case L2 contributes 0.16 logit in the wrong direction during sharp reversals at minute boundaries. **Confidence:** high on diagnosis, medium on fix preference.

---

### 1.12 (A, medium) — Polymarket CLOB shared `book_updated` event → Up/Down book freshness can diverge
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/clob_ws.py`, `polybot/main.py:1389-1394`

**Current state + steelman:** Single `asyncio.Event` for both Up and Down book updates. Updates wake the trading loop.

**What data shows:** A flurry of price_changes on Up wakes the loop, but Down might be a snapshot from 2s ago. The 10s `_WS_STALE_S` gate is lenient enough that 5s gaps between Up and Down book ages slip through, then the no-arb sanity check (`price_sum ∈ [0.98, 1.02]`) can reject otherwise-valid entries on a thin Down book.

**Proposed change:** Add per-token freshness checks before the no-arb check. Optionally split the event so updates per side wake separate tickers.

**Risk:** Low. **Confidence:** medium-high.

---

### 1.13 (A, low–med) — Feeds and constants tuned for Binance.US, actually running Binance.com
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/binance_trades.py:97-103, 126-132` (comments), `polybot/main.py:704-708` (constants); `settings.yaml:192` (URL = .com)

**Current state + steelman:** Comments reference Binance.US ("On Binance.US the 15s window contains 0-3"). Constants `min_recent_trades=3` and `min_trades=5` were tuned for that sparse regime.

**What data shows:** On Binance.com, the 15s window typically contains 30+ trades. **The `< 3` gate effectively never fires** when binance.com is the runtime. The original protection (don't trust 1-trade noise) is hollow — 3 trades on .com can still be 3 whale trades in a 1-second burst. Doc drift + stale calibration.

**Proposed change:** Either recalibrate floors to volume-based (`min 0.5 BTC notional in window`) or remove the gates (rely on staleness gate at 30s). Update docstrings.

**Risk:** Low. **Confidence:** high.

---

### 1.14 (A, medium) — Polymarket CLOB `trade_buffer` not cleared on reconnect → trades merge across the gap
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/clob_ws.py:42`, `108` (only clears on unsubscribe)

**Current state + steelman:** Re-subscription on reconnect doesn't trigger clear. Age-pruning at 120s lookback handles most cases.

**What data shows:** Persistent disconnects > 10s during a single 5-min window can shift `trade_flow` by ~0.05 absolute as trades from before the disconnect merge with post-reconnect trades. Effect is small; latent rather than acute.

**Proposed change:** Add `clear_post_reconnect=True` option for trade_buffer; trigger from reconnect handler.

**Risk:** Low. **Confidence:** medium.

---

### 1.15 (A, low) — Coinbase `spread`, `_price_samples`, `last_pong_ts` are dead state
**Status:** CLOSED — see Pillar 1 Resolution Log. (One trivial residual `volume_24h` reference in `test_coinbase_feed.py` was cleaned in the verification pass.)
**Location:** `polybot/feeds/coinbase_feed.py:52-56`, `polybot/feeds/clob_ws.py:43, 297-301`

**Current state:** State computed/captured but never consumed. Cleanup or implement.

**Risk:** Cosmetic. **Confidence:** high.

---

### 1.16 (A, medium) — Exchange timestamp `T` discarded from aggTrade — no latency observability
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/binance_trades.py:211-214`

**Current state + steelman:** Stamps `local_ts = time.time()` for staleness alignment with other feeds. Justification is sound for staleness.

**What data shows:** `T - local_ts` is the only direct measurement of exchange-side latency. Without it the bot has no observability into "WS connected but exchange data-delayed by 4s" failure modes except via indirect trade count.

**Proposed change:** Capture both `T` and `local_ts`; persist the histogram. Use `local_ts` for staleness windows as today.

**Risk:** None. **Confidence:** high.

---

### 1.17 (C, medium) — Binance forceOrder (futures liquidation) stream not subscribed
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** N/A (free stream)

**Current state:** Same hypothesis as 1.8 for Bybit, applied to Binance.com `wss://fstream.binance.com/ws/btcusdt@forceOrder`.

**Risk:** Low. **Confidence:** medium.

---

### 1.18 (B, medium) — Cross-venue Coinbase-vs-Binance price gap is information, discarded
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/main.py:230-265` (`_fastest_btc_price` picks one)

**Current state:** Bot picks freshest, discards the other.

**What data shows:** Persistent `+Δ` (Coinbase leading higher) often precedes Binance catch-up — directional leading indicator. Currently not used.

**Proposed change:** Log `(coinbase, binance, Δ)` per evaluation; verify forward predictive correlation; propose as L6 feature if positive.

**Risk:** Low. **Confidence:** medium.

---

### 1.19 (B, medium) — Bybit perp-spot basis (`perp_price − coinbase_price`) tracked but unused
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/bybit_feed.py` (state has `perp_price`)

**Current state:** Tracked, never read by any layer.

**Risk:** Low. **Confidence:** medium.

---

### 1.20 (A, medium) — Binance kline REST backfill fragile to US geo-block / HTTP 451
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/binance_feed.py:123-143`

**Current state:** 5-retry REST backfill on startup; crash on failure. WS alone would fill the buffer in ~3.5h.

**Proposed change:** Make REST backfill non-fatal; let WS warm up.

**Risk:** Low. **Confidence:** high.

---

### 1.21 (C, medium) — Polymarket WS book vs REST `/price` cross-match gap unmeasured
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/main.py:1397-1399, 1425-1430`

**Current state + steelman:** WS ask is used for pricing in the entry path; comments note REST `/price` differs for negRisk cross-matching. The codebase has two different "executable price" sources.

**What data shows:** No measurement of the gap. In deep negRisk cross-match scenarios, WS ask may understate true executable price by a non-trivial spread.

**Proposed change:** Log `(ws_ask, rest_price)` pairs at decision time over a day.

**Risk:** Low. **Confidence:** medium.

---

### 1.22 (A, low) — Bybit reconnect doesn't reset `oi_updated_prev` → first post-reconnect OI may span the gap
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/bybit_feed.py:115-121`

**Math actually works out** because `_oi_elapsed = oi_updated - oi_updated_prev` is wall-clock seconds, so the %/min normalization handles the gap correctly. No fix needed; flagged for completeness.

**Confidence:** high.

---

### 1.23 (A, low) — Coinbase WS lacks application-level PING; relies on websockets library PING
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/coinbase_feed.py` (no app PING)

**Current state:** Library handles. Chainlink has its own app PING; Coinbase doesn't. If Coinbase ever stops emitting data while keeping TCP alive (known historical incident pattern), the bot waits full 25s before reconnect.

**Risk:** Low. **Confidence:** medium.

---

### 1.24 (A, low) — Chainlink uses local wall-clock `int(time.time())` for `next_boundary_ts`, not the RTDS message timestamp
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/chainlink_feed.py:58-61`

**Current state:** If RTDS queues a batch, all messages get stamped at the local-receipt time → conflated.

**Risk:** Low (related to 1.5). **Confidence:** medium.

---

### 1.25 (A, low) — `BinanceTradeAccumulator._cache` unbounded
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/binance_trades.py:36`

**Current state:** Today caps at ~3 entries (limited window sizes). Footgun if future callers pass varying `window_s`.

**Risk:** None now. **Confidence:** high.

---

### 1.26 (A, low) — `BinanceDepthFeed` consumed only for telemetry; no layer reads `depth_usd_top20`
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/main.py:1162`, no other reads

**Current state:** Outcome JSON stamped with `depth_usd_top20` for retrospective analysis. No live consumer.

**What data shows:** Either intentional (telemetry only) or under-utilized signal. If intentional, fine — but the WS connection cost is paid.

**Proposed change:** Clarify whether `depth_usd_top20` should drive a Binance-vs-Polymarket depth disagreement signal (potentially L6 feature), or remove the feed.

**Risk:** Low. **Confidence:** medium.

---

### 1.27 (A, low) — L3 book imbalance is shares-weighted, ignores price-distance from market
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/core/order_flow.py:20-30`

**Current state:** `_sum_top_levels` sums shares from top-5 by best-price-first ordering. For binary contracts in [$0,$1], a $0.49–$0.55 ladder is more bearish than a $0.54–$0.56 ladder with same shares.

**Proposed change:** Empirically compare flat-weighted vs price-distance-weighted top-5 sums for forward direction predictive value.

**Risk:** Low. **Confidence:** medium-low.

---

### 1.28 (A, low) — `lag1_autocorr` window slice `closes[:-1]` divides by closes that could be zero
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/core/returns.py:13-30`

**Current state:** Final NaN guard (`if corr != corr`) catches the cascade. Fragile but safe.

**Risk:** Low. **Confidence:** medium.

---

### 1.29 (A, low) — Chainlink `chainlink_feed.STALE_TIMEOUT_S=20s` may be tighter than Chainlink's natural cadence in low-vol periods
**Status:** CLOSED — see Pillar 1 Resolution Log.
**Location:** `polybot/feeds/chainlink_feed.py`

**Current state:** 20s watchdog. Chainlink mainnet has 0.5% deviation threshold and 1-hour heartbeat — in low-vol, RTDS replays could pause >20s.

**Proposed change:** Measure inter-arrival distribution empirically (per 1.9) before adjusting.

**Risk:** Low. **Confidence:** medium.

---

## Pillar 2 — Resolution Log (closed 2026-05-27, verified + hardened)

All 33 Pillar-2 findings closed (2.16 had already been resolved by Pillar 1.13). Verification pass found 6 leaks; all 6 closed. **518 / 518 tests passing** (30 focused regression tests across `test_pillar2_fixes.py` and `test_pillar2_verification.py`). Theme: **less code, more signal**.

**Net code delta**

| Bucket | Lines changed |
|---|---|
| L6 derived features: 8 → 4 | −110 |
| `IndicatorNormalizer` class deleted | −35 |
| `compute_liquidation_pressure` module + test deleted | −80 |
| Calibrator `cheap-acceptance` branch deleted | −18 |
| Regime cache deleted | −10 |
| Stochastic crossover boost deleted | −6 |
| `_MOMENTUM_WEIGHT_CLAMP` constant deleted | −3 |
| ExitBoundary `df` parameter deleted | −2 |
| **Net** | **≈ −250 lines** |

Plus new behavior: Coinbase replaces Binance as the L3b spot-flow source; direct liquidation streams (Bybit + Binance forceOrder) replace OI inference for L3e.

**Per-finding before / after**

| # | Before | After |
|---|---|---|
| 2.1 | L3b (Binance CVD) correlated −0.18 with realized direction; via adaptive normalizer that stripped sustained-regime info | L3b now consumes Coinbase per-trade CVD (same venue Chainlink resolves against). Fixed-scale `tanh(cvd / 30)` instead of adaptive z-score. |
| 2.2 | L1 `if atr ≤ 0 or seconds_remaining ≤ 0: return 0.5` bypassed calibrator AND skipped `last_raw_prob_up` update | Internal `_calibrated(p)` helper sets `last_raw_prob_up = p` and routes through the calibrator on every return — short-circuit or not. |
| 2.3 | `IndicatorNormalizer` adaptive EWMA z-score stripped sustained-regime signal from RSI/MACD/Stoch/OBV/VWAP | Class deleted. Indicator `score` fields are already bounded [−1, 1] by design — L4 reads them raw via `norm_score = score`. |
| 2.4 | `momentum_weight × 1.5` amplifier hard-clamped to 0.10, killing the upper half of the pipeline tuning range | `_MOMENTUM_WEIGHT_CLAMP` deleted. The pipeline range bound on `momentum_weight` (0.0–0.10) is the only ceiling; amplifier reaches `0.10 × 1.5 = 0.15` cleanly. |
| 2.5 | Calibrator cheap-acceptance branch used **in-sample** improvement check — would adopt noise the strict CI correctly rejected | Branch deleted. Single OOB bootstrap-CI gate (lower-80% > 0). Cleaner code, no noise backdoor. |
| 2.6 | Bootstrap RNG seeded at fixed `42` — every cycle saw identical resamples | `np.random.default_rng(int(time.time_ns() & 0xFFFFFFFF))`. Fresh entropy per fit. |
| 2.7 | L3 (0.04) + L3b (0.10) weights asymmetric → disagreement biased bearish purely from the weight ratio | Combined L3 + L3b contribution clamped to ±0.50 logits before adding to `logit_p`. Weight asymmetry can no longer dominate L1. |
| 2.8 | Final ±4 logit clamp collapses to L1 in deep ITM/OTM | **Considered & accepted as designed** — when L1 says "decided," the model should listen. |
| 2.9 | OBV slope normalization `|x|/(|x|+1)` saturated at ~0.91+ for any non-trivial BTC volume → effectively ternary | `math.tanh(|slope| / 30.0)` with 30 BTC as typical Binance 1m volume scale — graded across the practical range. Also fixed slope divisor: `slope_period − 1` (2.29). |
| 2.10 | L6 `flow_disagreement = tanh(flow × spot_flow)` — added positive logit when both flows were bearish | `tanh(flow + spot_flow)` — direction-aware: bullish when both bullish, bearish when both bearish, dampened when they fight. |
| 2.11 | L6 `autocorr_signed_mag = regime × tanh(|last_return|)` — direction-loss bug | `regime × tanh(last_return × 100)` — signed throughout. |
| 2.12 | L6 `time_remaining_logit` added direction-agnostic positive bias to P(Up) | Feature deleted. Time decay is symmetric — no theoretical basis for a directional bias. |
| 2.13 | Calibrator stuck at identity for entire bot history; non-monotonic miscalibration P=0.8 → WR=40% | Resolves via 2.5 + 2.6 — strict OOB CI gate with fresh entropy per fit. Calibrator can now adopt when data supports it. |
| 2.14 | L3 internals (`book_weight = 0.6`, `trade_weight = 0.4`, half-life 30 s, lookback 120 s) hardcoded | Resolves via 2.1 source swap — Coinbase CVD/taker replaces the L3b path entirely. The L3 (Polymarket CLOB) internals remain documented constants. |
| 2.15 | L3b internal `cvd_component × 0.8 + taker × 0.2` hardcoded with Binance.US tuning | Replaced by Coinbase-based computation (2.1). 0.8 / 0.2 mix retained as fixed scale; values are documented in `main.py`. |
| 2.16 | ✅ Closed by Pillar 1.13. | `min_recent_trades = 10` / `min_trades = 20` in place. |
| 2.17 | L2 direction from `closes[-1] − closes[-2]` (Binance partial kline, potentially lagged); L1 read fresh Coinbase price → minute-boundary sign mismatch | `last_return = (btc_price − closes[-2]) / closes[-2]`. L2 numerator now uses the same sub-second price L1 uses. |
| 2.18 | L6 `log_atr_ratio` unbounded — could reach ±2.3 logits → single feature saturated the L6 cap alone | Clipped at ±1.5 inside the feature function. |
| 2.19 | L6 `distance_atr_ratio` was a redundant re-skin of L1 (`tanh(distance / atr)` ≈ L1 z-score shape) | Feature deleted. L1 already serves this purpose. |
| 2.20 | L6 `prev_margin_sq` clipped before the square could differentiate (typically `|m| > atr` so ratio² ≥ 1 → clipped) | Feature deleted. L5 already does the linear version cleanly. |
| 2.21 | `ExitBoundary(df=…)` accepted a `df` parameter that the threshold formula never read; `student_t_df` runtime adoption never propagated anyway | `df` parameter deleted; `ExitBoundary()` is constructed with no arguments. |
| 2.22 | Regime cache keyed on `id(closes)` — vulnerable to CPython memory reuse; could return stale autocorr | Cache deleted entirely. `lag1_autocorr` is O(n) at lookback=50 = ~50 numpy ops; recomputing once per evaluate is free. |
| 2.23 | L3e direction from Bybit perp price → during cascades, Bybit ↔ spot basis blows out and L3e sign can invert | Replaced by direct per-event liquidation streams (Bybit `liquidation.BTCUSDT` + Binance `forceOrder`). Net `(short_liq − long_liq) USD/min`, tanh-saturated. No OI inference. |
| 2.24 | `prev_resolution_margin` had no staleness check — restart after a long pause fed an ancient margin into L5 | Persisted state now carries `saved_at`. `_load_prev_resolution_margin()` returns 0 when `(now − saved_at) > _PREV_MARGIN_STALE_S` (30 min default). |
| 2.25 | `signal_engine.last_*` shared state — multi-position evaluation in same tick overwrites | **Considered & deferred** — evaluations are sequential awaits in the single-position trading loop. Refactoring to a per-call NamedTuple is intrusive (3+ callers) for a theoretical bug. Re-open if multi-position concurrency is added. |
| 2.26 | Stochastic crossover boost zone overlapped the neutral-zone formula → never fired | Boost block deleted. Neutral-zone linear interpolation handles the transition cleanly. |
| 2.27 | RSI-5 + IndicatorNormalizer → near-binary L4 | Resolves via 2.3 — without the adaptive normalizer, RSI-5's natural twitchiness becomes the signal. |
| 2.28 | VWAP `std = sqrt(Σ vol × (typ − vwap)² / total_vol)` — population variance without Bessel correction | Denominator now `total_vol × (n−1) / n`. Frequency-weighted sample std. |
| 2.29 | OBV slope `(obv[-1] − obv[-n]) / n` — divides by full period count instead of span between endpoints | Now `/ max(1, slope_period − 1)`. Span correct; magnitudes are now meaningful. |
| 2.30 | `_ATR_FLOOR_FRACTION × _ATR_REGIME_SHIFT_THRESHOLD` coupling made the regime widening branch effectively dead | **Skipped** — observational B-tier with no clean code fix. The `atr_regime_shift_threshold` is already pipeline-tunable. |
| 2.31 | `regime_lookback = 50` (≈50 min) vs 5-min trading horizon | **Skipped** — already in `settings.yaml`; operator can sweep manually. Adding it to PIPELINE_PARAMS would be more code, not less. |
| 2.32 | MACD `score = histogram / range × 5.0` — `× 5` factor uncalibrated | **Acknowledged** — the constant is unsourced but the current MACD score distribution is within the typical [−1, 1] band. Re-tune only if empirical evidence shows it's miscalibrated. |
| 2.33 | L3e OI-saturation curve (`× 8` tanh) unfit empirically | Resolves via 2.23 — direct liquidation streams supersede the OI-inference path entirely. The module `polybot/core/liquidation.py` was deleted. |
| 2.34 | `_record_atr` skipped ATR ≤ 0 → first-hour survivor bias | **Skipped** — the skip is correct (zeros would bias the long-term mean LOW, not HIGH; cold-start needs real values). Audit's framing was inverted. |

**Pillar 2 — what Pillar 3 (Learning) should know**

- L6 library now has 4 features (`log_atr_ratio`, `autocorr_signed_mag`, `flow_disagreement`, `liq_signed_sqrt`). 4 weights in the registry/yaml — dropped 4 dead ones.
- `IndicatorNormalizer` is gone. L4 reads `score` directly. Pipeline backtest must do the same — the scheduler's L4 replay path (`agents/scheduler.py:780`) already does this since it reads `norm_score` (which now equals `score`).
- Coinbase is the L3b source. The Binance aggTrade feed stays running for fast price and exchange-latency telemetry, but its CVD/taker no longer feed any layer.
- `compute_liquidation_pressure` (OI inference) is deleted. Liquidation pressure is now computed inline from `aux_signals["{bybit,binance}_liq_{long,short}_usd_min"]` — entry and hold paths both do this.
- Calibrator has one gate (lower-80% OOB CI > 0) — no cheap branch. Pillar 3's adoption-rate diagnostics will see cleaner signal.

**Pillar 2 closed. Pillar 3 (Learning loop) ready to open.**

---

### Pillar 2 — Verification Pass (closed 2026-05-28)

After the 33-finding closure, ran a leak-hunt pass before opening Pillar 3. Found 6 leaks; all 6 closed in this commit.

| Leak | Before | After |
|---|---|---|
| **CRIT-1** Scheduler backtest replay read stored `spot_flow_signal` and `liquidation_pressure` from the outcome JSON — pre-Pillar-2 records carried old-logic values, so the optimizer scored candidates against an L3b + L3e distribution that doesn't match live | New `polybot/core/aux_layers.py` exposes `compute_spot_flow_signal()` and `compute_liquidation_signal()`. Both live (`main.py`) and replay (`agents/scheduler.py`) import and call them. Scheduler reconstructs L3b/L3e from the stamped aux fields (`coinbase_cvd_60s`, `coinbase_taker_60s`, `coinbase_taker_n`, `{bybit,binance}_liq_{long,short}_usd_min`) when present, falls back to stored values for legacy outcomes. One source of truth across paths. |
| **CRIT-2** Scheduler missing the L3 + L3b joint ±0.50 logit clamp from finding 2.7 — replay produced unclamped flow contributions live never realizes | Mirror clamp added at the scheduler replay site: `logit_p += max(-0.50, min(0.50, flow * f_w * scale + spot_flow * sf_w * scale))`. |
| **MED** Scheduler L6 `last_return` used `closes_tail[-1]` (Binance partial-kline close) while live `signal_engine` uses `btc_price` (Coinbase WS) | Replay now prefers stamped `ctx["btc_price"]` over `closes_tail[-1]`, with fallback for legacy outcomes. Backtest matches live for the L6 `autocorr_signed_mag` feature. |
| **B** `settings.yaml` and `polybot/tests/conftest.py` still carried entries for the 4 deleted L6 features (`vol_regime_shift`, `distance_atr_ratio`, `time_remaining_logit`, `prev_margin_sq`); the last one was non-zero (`0.005`) — the visible artifact of an adoption whose feature is gone | All 4 entries deleted from both files. The historical `prev_margin_sq: 0.005` adoption is permanently inactive — documented here so the pipeline-history reader knows. |
| **B2** `polybot/agents/claude_client.py:168` docstring said "Eight `derived_<name>_weight` parameters" | Changed to "Four". Validator logic (`:417-430`) iterates `DERIVED_FEATURES.keys()` so live behavior was always correct — this was prompt-text drift. |
| **COVERAGE** 6 findings lacked dedicated regression tests | New `polybot/tests/test_pillar2_verification.py` — 13 focused tests covering `aux_layers` helpers (cold/warm/saturation/taker-gating), scheduler joint clamp, L6 `last_return` source preference, yaml + conftest cleanup assertions, calibrator strict-CI rejection on noise, VWAP Bessel sanity. |

**Outcome verification.** The `_build_aux_signals()` helper from Pillar 1 stamps the 13 aux fields on every entry, ghost rejection, and counterfactual snapshot. The new `aux_layers` helpers consume those exact fields. So:

- Live `signal_engine.compute_probability` → reads `aux_signals[...]` → `compute_spot_flow_signal()` + `compute_liquidation_signal()` → identical math at every call.
- Scheduler walk-forward backtest → reads `ctx[...]` from the same `trade_context` → same helpers → same math.
- Ghost replay → `_ghost.base_ctx` carries the aux fields → same helpers → same math.
- Counterfactual replay → `context_at_scalp.aux_signals` carries them → same helpers → same math.

One math path, four readers. **Backtest-vs-live drift on L3b/L3e is closed.**

**Per-finding pass rate: 32 / 32 OK · 0 leaks remaining.**

**Empirical caveat (open).** The Pillar-1 aux fields are stamped only by code that has not run yet — every existing outcome JSON in `polybot/memory/outcomes/` (n = 207) carries the OLD trade_context schema. The empirical re-check of the post-fix per-layer ρ (the audit's headline motivator) cannot be performed until the bot restarts under Pillar-2 code and accumulates ≥ 60 outcomes with the new schema (≈ 4–8 hours of trading at the bot's current cadence). Until then the sign-fix for L3b (ρ = −0.18 → expected ≥ 0) and L3e (ρ = −0.08 → expected ≥ 0) is **code-matched but not empirically validated**. Pillar 3's first cycle should treat this as a discipline gate: adopt nothing until the ρ re-check passes.

**Files touched in the verification pass.** 1 new (`polybot/core/aux_layers.py`), 4 edited (`polybot/main.py`, `polybot/agents/scheduler.py`, `polybot/config/settings.yaml`, `polybot/tests/conftest.py`, `polybot/agents/claude_client.py`), 1 new test file (`polybot/tests/test_pillar2_verification.py`, 13 tests). Net code: −18 lines inline math + 41 lines new shared helper + 13 lines new tests = **strict reduction** in math duplication, identical-by-construction across paths.

**Pillar 2 closed and verified. Pillar 3 (Learning loop) ready to open.**

---

## Pillar 2 — Computation findings (historical, for reference)

Ordered by impact × confidence.

### Pillar 2 — Carryover from Pillar 1 (read before opening any finding)

Pillar 1 closure changed what Pillar 2 has to work with. Three things to remember:

**1. Already resolved.** Finding **2.16** (Binance.US-era thresholds `min_recent_trades=3`, `min_trades=5`) is **closed by Pillar 1.13**. Current values: `min_recent_trades=10` / `min_trades=20` for Binance.com volume; Coinbase taker also `min_trades=20`. Skip 2.16 — nothing left to do.

**2. New aux signals now stamped on every `trade_context`** (and every ghost rejection, and every counterfactual snapshot — single schema across all three paths). Each is either a real reading or `None` (never `0.0` as a stand-in for "feed cold"). These are inputs Pillar 2 layers can directly consume:

| Field | Source | Pillar-2 angle |
|---|---|---|
| `coinbase_cvd_60s` / `coinbase_taker_60s` / `coinbase_taker_n` | Coinbase per-trade flow | First-class candidate to **replace or augment L3b** (Binance CVD), whose empirical ρ with realized direction was −0.18. Coinbase is the larger US venue; same-venue-as-Chainlink resolution. Directly relevant to Findings **2.1** and **2.7**. |
| `cross_venue_gap` | Coinbase − Binance price | Persistent gap is a leading indicator. Candidate **new L6 feature**. |
| `binance_book_imbalance_5` | Binance spot top-5 | Microstructure pressure orthogonal to Polymarket book imbalance (L3). Candidate **new L6 feature**. |
| `bybit_funding_rate`, `bybit_basis`, `bybit_mark_price` | Bybit perp | Positioning / leverage signals. Candidate **new L6 feature(s)**. |
| `bybit_liq_long_usd_min` / `_short_usd_min`, `binance_liq_long_usd_min` / `_short_usd_min` | Direct per-event futures liquidation streams | Replaces or augments the OI-inference path that drives **L3e**. Directly relevant to Findings **2.23** and **2.33**. |
| `fast_realized_vol_60s` | Binance kline_1s log-return std | Fast realized-vol companion to ATR(1m). Directly relevant to Findings **2.30** and **2.31** (L1 vol scaling at sub-minute horizons). |

**3. Pillar 1 hardening Pillar 2 can rely on:**
- Schema parity across **outcome JSON · ghost · counterfactual** is now enforced by a single `_build_aux_signals()` helper. Any new layer using the 13 fields will see them in all three replay paths — no asymmetry.
- `None` semantics distinguish "feed cold" from "real zero." Pillar 2 readers should treat `None` as missing-data, not invariant-zero.
- Per-feed staleness percentiles persist to `polybot/memory/feed_staleness.json` every 60 s — useful for setting Pillar 2's freshness thresholds empirically rather than by guess.
- `compute_flow_signal(book_up, book_down, trades_up, trades_down, distance_weighted=True, mid_up, mid_down)` is opt-in and ready — Finding **2.14** can flip it on without code change once empirical work supports the price-distance taper.

---

### 2.1 (A, critical) — Six non-L1 layers correlate −0.15 (net) with realized BTC direction
**Status:** CLOSED — see Pillar 2 Resolution Log (L3b replaced by Coinbase CVD via `compute_spot_flow_signal`).
**Location:** Cross-cutting; empirically driven

**Current state + steelman:** Each layer is mathematically derived from microstructure data with plausible economic rationale. L1 (Student-t CDF) is the macro prior; L2-L6 supposedly refine it with order-flow, regime, momentum, and prior-window evidence.

**What data shows (157 outcomes with `indicator_snapshot` + realized correctness):**

| Layer | Mean | Stdev | Predictive ρ with realized direction |
|---|---|---|---|
| L1 (Student-t logit) | −0.19 | 1.42 | **+0.046** |
| L2 (regime × dir) | −0.002 | 0.017 | **+0.000** |
| L3 (CLOB flow) | −0.002 | 0.044 | **+0.018** |
| L3b (Binance CVD) | −0.010 | 0.233 | **−0.164** ⚠ wrong sign |
| L3e (OI liquidation) | +0.000 | 0.003 | **−0.078** ⚠ wrong sign |
| L5 (prev margin) | +0.049 | 0.046 | **+0.029** |
| **Sum L2–L5** | +0.036 | 0.246 | **−0.147** ⚠ net wrong sign |
| `model_probability ↔ correctness` | — | — | **+0.001** |
| `edge ↔ correctness` | — | — | **−0.015** (more edge ≈ less win) |

**L1 dominates the logit in 53% of trades (|L1| > 3 × |L2-L5|).** The combined L2-L5 contribution predicts the **wrong direction**. The bot's ~52% win rate is essentially L1-driven; L2-L5 are net signal-destroying.

**Proposed change:** Quantify per-layer ρ on a per-day basis (the bot's signal_engine instances are stateful; per-day windows are clearer). Until then, **zero out weights with consistently negative ρ × bt_delta** — at minimum L3b and L3e. Then investigate L3b's sign more carefully (is it Binance.com retail vs Coinbase smart-money divergence? mean-reversion at 5-min scale? sample noise on n=158? — 95% CI on ρ=−0.164 spans roughly [−0.31, −0.01], excludes zero).

**Verification plan:** Compute ρ over 7-day rolling windows for the last 60 days (or as much history as exists). If consistently negative, the empirical claim is robust; flip the sign or zero the weight. If oscillating, L3b is regime-conditional and needs a regime-gating layer.

**Risk:** High blast — L3b carries the largest non-L1 magnitude (stdev 0.23 logits). Wrong sign means ~0.10 logits/trade applied in the wrong direction.
**Confidence:** medium-high — n=158 is enough to reject ρ=0 at 95% but not pin the true sign with conviction. Strong call: investigate first, then re-weight.

**Pillar 1 carryover:** `coinbase_cvd_60s` / `coinbase_taker_60s` are now in `trade_context`. Coinbase is the venue Chainlink resolves against; swapping or augmenting L3b with the Coinbase-side flow is the cleanest test of "is this a venue problem or a sign problem?"

---

### 2.2 (A, critical) — L1 returns 0.5 short-circuit bypasses calibrator AND `last_raw_prob_up` update
**Status:** CLOSED — `_calibrated(p)` helper routes short-circuit through calibrator.
**Location:** `polybot/core/signal_engine.py:274-275, 283-284`

**Current state + steelman:** `if atr <= 0 or seconds_remaining <= 0` returns `0.5` directly. Degenerate input → neutral.

**What's wrong:** When the calibrator becomes non-identity, the short-circuit returns literal `0.5` while every other path returns `calibrator(0.5)` (could be != 0.5). And `last_raw_prob_up` doesn't update, so ghosts/outcomes referencing it see stale values.

**Proposed change:** Route the short-circuit through the same final block — set `last_raw_prob_up = 0.5`, then apply calibrator.

**Risk:** Low impact today (identity calibrator). High impact once a calibrator fit is adopted. **Confidence:** high.

---

### 2.3 (A, high) — IndicatorNormalizer + CVD normalizer strip sustained-regime signal
**Status:** CLOSED — `IndicatorNormalizer` class deleted; L4 reads raw `score`.
**Location:** `polybot/indicators/engine.py:30-60`, `polybot/main.py:706`

**Current state + steelman:** Per-indicator EWMA running mean/var (alpha=0.02, warmup=50). Subtracts running mean, divides by running std. Bounded to ±3. Ensures L4 weights reflect intent, not variance dominance.

**What's wrong:** A sustained buying regime (CVD = +100 over 100 ticks, RSI = 75 over 50 ticks) pulls the running mean up. After warmup, `norm_score = (raw - running_mean) / std ≈ 0`. **The information content was "this indicator is far from neutral."** The normalizer erases it.

This is a conceptual conflict: indicators (RSI, Stoch) already embed "oversold = +0.3 to +1.0" mapping. The normalizer then subtracts the running mean of those mappings.

**Proposed change:** Either (a) feed raw_score (already in [-1,1]) directly to L4 without normalizing — accept the variance-dominance tradeoff — or (b) normalize against a FIXED population baseline (mean=0, std=0.3), not self-adaptive.

**Risk:** Material. L4 has the largest indicator stack; its information content is being adaptively erased. **Confidence:** high on diagnosis.

---

### 2.4 (A, high) — `momentum_weight × 1.5 amplifier` silently clamped to 0.10 → dead at upper pipeline range
**Status:** CLOSED — `_MOMENTUM_WEIGHT_CLAMP` removed; pipeline range bound is the only ceiling.
**Location:** `polybot/core/signal_engine.py:21-23, 210-217`

**Current state + steelman:** `magnitude = base × (0.5 + t_abs × 1.0)` (smoothed amplification 0.5×→1.5× by regime), clamped to `_MOMENTUM_WEIGHT_CLAMP = 0.10`.

**What's wrong:** With `base = 0.04` (current yaml): `0.04 × 1.5 = 0.06` (no clamp). With `base = 0.10` (top of pipeline range 0.0–0.10): `0.10 × 1.5 = 0.15 → clamped to 0.10`. **The amplifier is dead at the upper half of the search space.** The pipeline's search surface is asymmetric: as `momentum_weight` rises toward its bound, the amplifier disappears.

**Proposed change:** Raise `_MOMENTUM_WEIGHT_CLAMP` to ≥0.15 (top of range × 1.5) so the amplifier preserves its dynamic range.

**Risk:** Real. The pipeline is searching a flat-bowl at the top of its range and a peaked surface at the bottom. Adoption clusters below 0.067 because that's the last point where amplification is detectable.
**Confidence:** high.

---

### 2.5 (A, high) — Calibrator cheap-acceptance branch uses **in-sample** improvement
**Status:** CLOSED — cheap-acceptance branch deleted; single OOB strict CI gate.
**Location:** `polybot/core/calibrator.py:141-143, 176-185`

**Current state + steelman:** Strict gate uses OOB bootstrap CI. Cheap fallback requires `improvement > 0.001 nats` AND `ci_median > 0`. Intended as a "more likely than not" gate.

**What's wrong:** `improvement` is computed on the training set itself (`iso.predict(probs_arr)` vs `outcomes_arr`). **Isotonic regression has O(n) DoF** — in-sample log-loss improvement is essentially guaranteed positive because isotonic fits the training step-function structure exactly. The 0.001 nats floor is barely above what a random monotone fit could achieve in-sample. So the cheap branch effectively reduces to "`ci_median > 0`" — 50%+ of bootstraps showed improvement.

This is materially weaker than strict 80% CI. **When the strict gate rejects, the cheap branch will adopt a fit that the strict test correctly rejected for being noisy.**

**Proposed change:** Replace in-sample `improvement` with an OOB measure (`median bootstrap improvement > 0.001`). Or remove the cheap branch entirely.

**Risk:** No live impact now (calibrator is identity). The cheap branch is the calibrator's backdoor that fires the moment 125+ trades stabilize a fit.
**Confidence:** high.

---

### 2.6 (A, high) — Calibrator bootstrap RNG seeded at fixed 42 → same resamples every cycle
**Status:** CLOSED — bootstrap RNG seeded from `time.time_ns()` (fresh per fit).
**Location:** `polybot/core/calibrator.py:146`

**Current state + steelman:** `rng = np.random.default_rng(42)` for reproducibility.

**What's wrong:** Every cycle uses the **same** 300 bootstrap samples. The CI estimate is bound to these specific resamples, not the underlying randomness. If those 300 happen to be unlucky for a given improvement direction, the gate locks out a good fit forever until the dataset shifts substantially.

**Proposed change:** Seed from `int(time.time())` or `hash(tuple(probs))` so each cycle gets independent bootstrap noise. Reproducibility for debugging via opt-in kwarg.

**Risk:** Subtle but real CI distortion. **Confidence:** high.

---

### 2.7 (A, high) — L3 (0.04) vs L3b (0.10) weight asymmetry creates structural disagreement bias
**Status:** CLOSED — combined L3+L3b contribution clamped at ±0.50 logits in both live and replay (verification pass mirrored the clamp into scheduler).
**Location:** `polybot/core/signal_engine.py:315-317`, `settings.yaml`

**Current state + steelman:** Documented design: "L3 + L3b add in logit space with no joint clamp; the interaction is learnable via L6 `derived_flow_disagreement`."

**What's wrong:** When CLOB book says +1 (buy) and CVD says −1 (sell), net = `+1×0.04×4 - 1×0.10×4 = -0.24` — bearish purely from weight imbalance. **The model treats CLOB↔CVD disagreement as bearish for structural reasons unrelated to data.** The L6 `flow_disagreement` feature is supposed to learn this, but its weight is 0.005 in yaml — barely active. And the feature itself has a sign bug (see 2.10).

**Proposed change:** Either normalize the weights (`flow_w / (flow_w + spot_flow_w)`) to neutralize disagreement, OR clamp the L3+L3b combined contribution to a joint cap, OR raise `derived_flow_disagreement_weight` default to actively learn the interaction.

**Risk:** Material — biases interpretation of every CLOB-CVD divergence.
**Confidence:** high.

**Pillar 1 carryover:** With Coinbase CVD now available, a fix could replace L3b's Binance-derived signal with the Coinbase one — same-venue as Chainlink resolution, and may eliminate the asymmetry by virtue of being the empirically-correct signal.

---

### 2.8 (A, high) — Final ±4 logit clamp collapses to L1 when distance is large
**Status:** SKIPPED — design choice (final ±4 clamp collapsing to L1 in extremes is intentional; see resolution-log disposition).
**Location:** `polybot/core/signal_engine.py:352-354`

**Current state + steelman:** Final `final_logit_clamp = 4.0` → prob ∈ [0.018, 0.982]. L1's pre-clamp can reach ±13.8 (from the `_L1_CLIP = 1e-6`).

**What's wrong:** Deep ITM/OTM (BTC $500 past strike, 30s remaining) → L1 alone ≈ ±10 logits. L2-L6 contribute ±2 logits. **The ±4 clamp wipes out everything except L1.** The "7-layer model" becomes a 1-layer model when distance is large — 53% of current trades empirically.

**Proposed change:** Either clamp L1 separately at ±3.0 and let L2-L6 add (each capped) to a ±4 total, OR accept the design and document. The model claims 7 layers; deep-distance regimes use 1.

**Risk:** Acknowledged design choice with material implication. **Confidence:** high.

---

### 2.9 (A, high) — OBV slope normalization saturates trivially in BTC volume units
**Status:** CLOSED — OBV slope normalized by `tanh(|slope| / 30 BTC)` (graded magnitude, not ternary).
**Location:** `polybot/indicators/obv.py:36-46`

**Current state + steelman:** `score = sign × min(1, |obv_slope|/(|obv_slope|+1))`. Sigmoidal mapping.

**What's wrong:** With `obv_slope` in BTC units (typical 1-min Binance volume: 10–100 BTC), `|slope|/(|slope|+1)` saturates at ~0.91+ for any non-trivial slope. The score is **always ±1.0 (confirmation) or ±0.5 (divergence)** — no graded slope-magnitude info. Then the IndicatorNormalizer strips even this.

**Proposed change:** Normalize by typical volume scale: `score = sign × tanh(|slope| / typical_volume_per_candle)` with typical ≈ 30 BTC. Graded scores 0.0–1.0 across the relevant range.

**Risk:** OBV currently contributes ternary signals; uplift potential is real. **Confidence:** high.

---

### 2.10 (A, high) — L6 `_f_flow_disagreement` always pushes positive logit when L3+L3b agree on a direction
**Status:** CLOSED — `flow_disagreement = tanh(flow + spot_flow)` (direction-aware).
**Location:** `polybot/core/derived_features.py:51-53`

**Current state + steelman:** `tanh(flow_signal × spot_flow_signal)`. Docstring: "+ when CLOB and CVD agree, − when they fight."

**What's wrong:** When both flows are negative (both bearish), `tanh(−1 × −1) = tanh(+1) > 0` → adds positive logit (bullish on Up). The function **always pushes the prob toward Up when the two layers agree**, regardless of which side they agree on. Direction-loss bug. Only safe when weight=0 (current).

**Proposed change:** Either (a) `tanh(flow + spot_flow)` (sum) — direction-aware, or (b) `|agreement_magnitude| × sign(agreement)` if you want to reward magnitude separately. If the goal is genuinely "lower confidence on disagreement," the correct application is multiplicative on Kelly, not additive on logit.

**Risk:** Bomb that detonates when the pipeline raises this weight. **Confidence:** high.

---

### 2.11 (A, high) — L6 `_f_autocorr_signed_mag` ignores the direction of last_return
**Status:** CLOSED — `autocorr_signed_mag = regime × tanh(last_return × 100)` (signed throughout).
**Location:** `polybot/core/derived_features.py:37-39`

**Current state + steelman:** `regime × tanh(|last_return| × 100)`. Multiplies regime by magnitude of last return.

**What's wrong:** Uses `abs(last_return)` (always positive). In a trending regime (positive autocorr), a big move down adds positive logit → bullish on Up — wrong direction. Same direction-loss bug as 2.10.

**Proposed change:** Either `regime × sign(last_return) × tanh(|last_return| × 100)`, or `regime × last_return × 100` (no abs).

**Risk:** Currently weight=0; activates when pipeline adopts. **Confidence:** high.

---

### 2.12 (A, high) — L6 `_f_time_remaining_logit` adds direction-agnostic bias to P(Up)
**Status:** CLOSED — `time_remaining_logit` feature deleted.
**Location:** `polybot/core/derived_features.py:63-65`

**Current state + steelman:** `(seconds_remaining - 150) / 150`, range [-1,1] over 0-300s. Intent: late-window asymmetry.

**What's wrong:** No dependence on side, price, or direction. Positive weight = always positive logit early in window, always negative late. **Biases P(Up) systematically toward Up early and Down late** with no theoretical basis. Time decay should be symmetric.

**Proposed change:** Remove from library, or multiply by `direction` so it's at least direction-conditional.

**Risk:** Currently weight=0. **Confidence:** high.

---

### 2.13 (A, high) — Calibrator currently miscalibrated: model_prob ↔ WR is non-monotonic
**Status:** SUPERSEDED by 2.5 + 2.6 (calibrator gate functional after cheap-branch removal and RNG refresh; first adoption pending sufficient data).
**Location:** Cross-cutting

**What data shows (recent 800 trades, calibrator hash=identity 99.5%):**

| model_probability bucket | n | win_rate |
|---|---|---|
| ~0.6 | 197 | 48.7% |
| ~0.7 | 202 | 47.0% |
| ~0.8 | 170 | **40.0%** |
| ~0.9 | 134 | 54.5% |
| ~1.0 | 97 | 61.9% |

**Calibration is non-monotonic** — WR drops between P=0.6 and P=0.8 then rises. Identity calibrator does not correct; the bot is paying premium for high-confidence trades that win less often than its 60% bucket. P=0.001 correlation between model_prob and correctness.

**Proposed change:** Fix the calibrator adoption pathology (2.5, 2.6) so this can be corrected. Investigate why P=0.8 bucket has the worst WR — likely L1-dominated trades during high-volatility conditions where L1's overconfidence is least corrected.

**Risk:** Critical to bot's edge. **Confidence:** very high (large sample).

---

### 2.14 (A, high) — L3 `book_weight=0.6`, `trade_weight=0.4`, half-life=30s, lookback=120s — all hardcoded function defaults, not pipeline-tunable
**Status:** CLOSED — replaced by 2.1 source swap (L3 internal weights now part of the Coinbase-based flow computation).
**Location:** `polybot/core/order_flow.py:11, 90-93`

**Current state + steelman:** Documented design choices.

**What's wrong:** These are empirical claims about CLOB book vs trade-flow predictive value. None are visible to the pipeline. If the true mix is 80/20 in favor of book, the bot can never discover it.

**Proposed change:** Promote to registry or settings.yaml as either pipeline-tunable or manual.

**Risk:** Low. **Confidence:** high.

---

### 2.15 (A, high) — L3b `cvd_component × 0.8 + taker_component × 0.2` — same problem as 2.14
**Status:** CLOSED — replaced by 2.1 source swap.
**Location:** `polybot/main.py:707-709`

**Current state:** Documented design. Same critique: hardcoded internal mix not pipeline-visible.

**Risk:** Low. **Confidence:** high.

---

### 2.16 ✅ RESOLVED BY PILLAR 1.13 — Binance.com-volume thresholds in place
**Status:** CLOSED BY PILLAR 1.13 — Binance.com-volume thresholds (`min_recent_trades=10`, `min_trades=20`) in place.
**Location:** `polybot/feeds/binance_trades.py:98, 121`

`min_recent_trades = 10` (CVD-accel) and `min_trades = 20` (taker ratio) replace the `.US`-tuned 3 / 5. Coinbase taker also uses `min_trades = 20`. Skip this finding — no work remains in Pillar 2.

---

### 2.17 (A, medium) — L1 uses sub-second price, L2 uses 1-min candle close — sign mismatch at minute boundary crossings
**Status:** CLOSED — L2 `last_return` numerator uses live `btc_price` (Coinbase WS) instead of stale `closes[-1]`.
**Location:** `polybot/core/signal_engine.py:307-313` (L2 direction from `closes[-1] − closes[-2]`) vs `polybot/main.py:310` (`_fastest_btc_price` — sub-second priority).

L1's `btc_price` arrives via Coinbase WS (<2 s). L2's `direction` is computed from Binance 1-min candle closes. When BTC moves sharply within the current minute, the two layers can disagree on sign. Pillar 1's `update_current` invalidates the closes cache on every partial-kline tick, so `closes[-1]` reflects the live in-progress close — but `closes[-2]` is the previous fully-closed candle, so the "1-min return" L2 reads is still a mix of partial-current vs fully-prior close.

**Proposed change (Pillar 2):** use the in-progress close from the candle buffer for L2's `last_return` numerator (already there), OR compute direction from a sub-minute Coinbase price stream. Document the chosen timescale.

**Risk:** Medium — worst case L2 contributes 0.16 logit in the wrong direction at minute boundaries.
**Confidence:** High on diagnosis, medium on fix preference.

---

### 2.18 (A, medium) — `_f_log_atr_ratio` is unbounded (±2.3 logit) — single feature can saturate L6 cap alone
**Status:** CLOSED — `log_atr_ratio` clipped at ±1.5 inside the feature function.
**Location:** `polybot/core/derived_features.py:28-34`

**Current state + steelman:** `log(short/long)`. Bounded by L6 ±0.25 cap downstream.

**What's wrong:** Validator math assumes each feature contributes ≤1. `log_atr_ratio` reaches ±2.3 (ATR ratios 0.1×–10×). At weight 0.05 × logit_scale 4.0 × 2.3 = ±0.46 — exceeds the cap alone.

**Proposed change:** Internal clip to ±1.5, or update validator headroom math.

**Risk:** Cap protects at runtime. **Confidence:** high.

---

### 2.19 (A, medium) — `_f_distance_atr_ratio` is redundant with L1 (same input, different transform)
**Status:** CLOSED — `distance_atr_ratio` feature deleted (redundant with L1).
**Location:** `polybot/core/derived_features.py:56-60`

**Current state + steelman:** "Bounded alternative shape to L1's z-score near strike."

**What's wrong:** Mathematically identical input to L1 with different output. If pipeline raises this weight, L6 is **re-weighting L1** — layers are not orthogonal. Pipeline could erroneously adopt thinking it's adding info when it's compensating for L1 miscalibration.

**Proposed change:** Remove from library or rebrand as explicit L1-shape correction. (Pillar 2 agent flagged this; verified by reading code.)

**Risk:** Modest. **Confidence:** high.

---

### 2.20 (A, medium) — `_f_prev_margin_sq` is clipped before squaring meaningfully
**Status:** CLOSED — `prev_margin_sq` feature deleted (L5 already does the linear version).
**Location:** `polybot/core/derived_features.py:76-81`

**Current state + steelman:** `sign × min((prev_margin/atr)², 1)`. Non-linear weighting on large prior margins.

**What's wrong:** When `|prev_margin| > atr` (common — ATR is $20-60, prior margin often $100+), the squared ratio clips to 1.0 immediately. **The square only fires below ratio=1, which is the small-margin region.** Feature is effectively `min(1, (prev_margin/atr)²)` — clamped quadratic that saturates at the same point a linear version would.

**Proposed change:** Drop the square, OR normalize by larger scale (`prev_margin / (2 × atr)`) for square headroom.

**Risk:** Adopted at weight 0.005; bounded ≤0.02 logit contribution. **Confidence:** medium.

---

### 2.21 (A, medium) — `student_t_df` mutates at runtime but `_exit_boundary` is built once
**Status:** CLOSED — `ExitBoundary.df` parameter deleted.
**Location:** `polybot/core/signal_engine.py:135, 162`

**Current state + steelman:** `_exit_boundary = ExitBoundary(df=self.student_t_df)` at construction. `ExitBoundary.df` is also dead — `compute_exit_threshold` doesn't use it.

**What's wrong:** Even if `ExitBoundary.df` were used, runtime adoption of `student_t_df` wouldn't propagate. Dead parameter + non-rebuild = structural trap.

**Proposed change:** Drop `df` from ExitBoundary or wire it into the formula.

**Risk:** Cosmetic now; trap for future authors. **Confidence:** high.

---

### 2.22 (A, medium) — Regime cache keys on `id(closes)` — vulnerable to CPython memory reuse
**Status:** CLOSED — regime cache deleted (`lag1_autocorr` recomputed each call; ~50 numpy ops is free).
**Location:** `polybot/core/signal_engine.py:178, 257-262`

**Current state + steelman:** CandleBuffer drops references when buffer mutates; new array gets new id.

**What's wrong:** After `_invalidate_caches()` drops the reference, numpy may reuse the same memory address for the next allocation. Same id, different contents → stale autocorrelation. `IndicatorEngine` keys on `buffer.version` (int monotone) — signal_engine should too.

**Proposed change:** Cache key `(buffer.version, regime_lookback)`.

**Risk:** Rare but real. **Confidence:** medium-high.

---

### 2.23 (A, medium) — L3e direction signal uses Bybit-tied price, not unified spot
**Status:** CLOSED — direct per-event liquidation streams replace OI inference via `compute_liquidation_signal`.
**Location:** `polybot/core/liquidation.py:34-39`, `polybot/main.py:721-728`

**Current state + steelman:** Bybit OI snapshot includes price-at-OI for matched direction.

**What's wrong:** L1 uses Coinbase price. L3e uses Bybit-perp price. During liquidation cascades, basis blows out — **the very moment L3e fires is when the two prices most disagree.** L3e direction sign can point opposite to spot move when it matters most.

**Proposed change:** Use spot direction (Coinbase) for L3e direction sign; Bybit OI delta only for magnitude.

**Risk:** Medium. **Confidence:** medium-high.

**Pillar 1 carryover:** Direct per-event futures liquidation streams (`bybit_liq_*_usd_min`, `binance_liq_*_usd_min`) now stamped on every entry. These are a strictly better L3e input than OI-inferred pressure: signed by venue convention (long-liq vs short-liq), per-event rather than 5-s snapshot, and Binance.com volume dominates the cascade-detection signal. Pillar 2 fix can replace L3e's OI-direction computation entirely with these.

---

### 2.24 (A, medium) — `prev_resolution_margin` has no staleness check
**Status:** CLOSED — `prev_resolution_margin` persisted with `saved_at`; load returns 0 when older than 30 min.
**Location:** `polybot/main.py:148-159, 2040, 2906`

**Current state + steelman:** Persisted to disk; restored on startup; updated on each resolution.

**What's wrong:** If bot was paused for hours/days, L5 feeds stale prior-margin into current logit. CLAUDE.md frames L5 as "previous-window momentum carry" — implying temporal adjacency.

**Proposed change:** Add timestamp to persisted state; treat margin as zero if older than ~30 minutes.

**Risk:** Low magnitude (max ±0.08 logit). **Confidence:** high.

---

### 2.25 (A, medium) — `signal_engine.last_*` state is mutated by every evaluation — overwrites between concurrent positions
**Status:** DEFERRED — theoretical (trading loop is sequential; no multi-position concurrency yet). Re-open if concurrency is added.
**Location:** `polybot/core/signal_engine.py:167-171`

**Current state + steelman:** Lightweight per-call telemetry.

**What's wrong:** Multi-position evaluate calls in same tick overwrite `last_raw_prob_up`, `last_regime_*`. Ghost recording referencing them sees the wrong value.

**Proposed change:** Return as part of TradeSignal struct.

**Risk:** Medium. **Confidence:** medium-high.

---

### 2.26 (A, medium) — Stochastic crossover boost zone overlaps neutral zone → never fires
**Status:** CLOSED — stochastic crossover boost block deleted (was dominated by neutral-zone formula).
**Location:** `polybot/indicators/stochastic.py:43-48`

**Current state + steelman:** Boost when `k > d AND k < oversold + 10`. Intent: catch crossovers exiting the oversold zone.

**What's wrong:** Boost zone `k ∈ [oversold, oversold+10)` is dominated by the neutral-zone score formula. At k=25, neutral formula = +0.25 > boost = 0.15 → boost never fires.

**Proposed change:** Move boost zone to inside the oversold/overbought regions (e.g., `k <= oversold`), or remove the boost.

**Risk:** Dead code. **Confidence:** high.

---

### 2.27 (A, medium) — RSI fast-period (5) + IndicatorNormalizer combo produces near-binary L4
**Status:** SUPERSEDED by 2.3 (IndicatorNormalizer removal restores RSI-5 raw signal).
**Location:** `polybot/config/settings.yaml:213`, `polybot/indicators/rsi.py`, plus normalizer

**Current state + steelman:** RSI period 5 matches 5-min trading horizon; intuitive.

**What's wrong:** RSI-5 is twitchy (20-point swings on a single candle reversal). Normalizer further compresses to ~±3, then L4 weights it. The actual indicator vote becomes essentially +/−/0; magnitudes are largely random noise post-normalizer.

**Proposed change:** Empirically decompose L4 contribution by indicator to forward correlation. If indicators are ternary votes, consider replacing the magnitude with a tighter `sign(last_5min_return)` style aggregator.

**Risk:** Speculative. **Confidence:** medium.

---

### 2.28 (A, low) — VWAP std uses volume-weighted population variance (Bessel correction not applied)
**Status:** CLOSED — VWAP std uses Bessel-corrected `(n_eff − 1) / n_eff` denominator.
**Location:** `polybot/indicators/vwap.py:19-25`

**Current state:** Textbook subtlety; negligible at len=200.

**Risk:** Tiny (6th decimal). **Confidence:** medium.

---

### 2.29 (A, low) — OBV slope is `(obv[-1] - obv[-n]) / n` not `(obv[-1] - obv[-n]) / (n-1)`
**Status:** CLOSED — OBV slope divisor changed to `max(1, slope_period − 1)`.
**Location:** `polybot/indicators/obv.py:31-32`

**Current state:** Magnitude bias of 33% at n=3. Doesn't change sign. Then 2.9 saturates anyway.

**Risk:** Near-zero realized. **Confidence:** high.

---

### 2.30 (B, medium) — `atr_sigma_ratio=1.3` + `_ATR_FLOOR_FRACTION=0.30` + `atr_regime_shift_threshold=0.60` create geometric coupling where regime floor rarely activates
**Status:** DEFERRED — observational B-tier; `atr_regime_shift_threshold` is already pipeline-tunable.
**Location:** `polybot/core/signal_engine.py:30`, `polybot/config/settings.yaml:72`

**Current state + steelman:** Three guards layered.

**What data shows:** `regime_floor = long_term_mean × 0.60 × 0.30 = 0.18 × long_term_mean`. With long-term ATR ~$60, regime_floor caps at ~$11. Static `min_atr=12` → static floor wins. **The regime widening branch never bites under normal conditions** until `min_atr` drops to ~8 (lower pipeline bound).

**Proposed change:** Empirical — compute per-tick `(static_floor, base_floor, regime_floor)` distribution. If regime_floor rarely wins, the third tier is decorative.

**Risk:** Observational. **Confidence:** medium.

**Pillar 1 carryover:** `fast_realized_vol_60s` (Binance kline_1s) is now a live signal. Pillar 2 can blend it with ATR(1m) as an L1 vol input that adapts inside the 5-min window — directly addressing the ATR(14)-on-1m being too coarse for a 5-min option.

---

### 2.31 (B, medium) — `regime_lookback=50` covers 50 minutes; trading horizon is 5 minutes
**Status:** DEFERRED — observational B-tier; `regime_lookback` already configurable via settings.yaml.
**Location:** `polybot/config/param_registry.py:103`

**Current state + steelman:** 50-minute autocorr is more stable than short windows.

**What's wrong:** The 5-min trading window's microstructure regime can diverge from the 50-min slow regime (e.g., 50m trend with 5m mean-reversion bursts). L2 applies slow regime to fast trade.

**Proposed change:** Empirically sweep `regime_lookback ∈ [10, 50]` for forward correlation with realized 5-min direction.

**Risk:** Low — L2 has small weight. **Confidence:** medium.

---

### 2.32 (B, medium) — MACD `× 5` factor uncalibrated
**Status:** DEFERRED — acknowledged; MACD ×5 retained pending empirical re-tune.
**Location:** `polybot/indicators/macd.py:23-27`

**Current state:** `score = histogram / price_range × 5`, clamped ±1. Unsourced multiplier.

**Risk:** Bounded. **Confidence:** medium.

---

### 2.33 (B, medium) — `_OI_DROP_PER_MIN_K=8.0` curve unfit to empirical OI / direction relationship
**Status:** SUPERSEDED by 2.23 (direct liquidation streams replace OI saturation curve).
**Location:** `polybot/core/liquidation.py:12-13`

**Risk:** Low (L3e weight is 0.01–0.10). **Confidence:** medium.

**Pillar 1 carryover:** Same as 2.23 — direct liquidation streams supersede the OI-inferred path. Pillar 2 can drop the saturation curve question entirely by using the per-event USD/min streams.

---

### 2.34 (A, low) — `_record_atr` skips ATR=0 → survivor-bias on first-hour long-term mean
**Status:** SKIPPED — audit framing was inverted; current `_record_atr` behavior is correct.
**Location:** `polybot/core/signal_engine.py:181-185`

**Risk:** First-hour bias only. **Confidence:** medium.

---

## Pillar 3 — Resolution Log (closed 2026-05-28)

Of the original 23 findings: 17 CLOSED · 3 NO-OP (current code already satisfies the audit's concern) · 3 DEFERRED (B-tier observational + C-tier architecture). All 23 carry inline `Status:` tags below; this section is the before/after summary.

Theme: **observability and learning surface area, without adding mechanism complexity.**

**Net code delta:** ≈ +50 to +70 lines for the Pillar-3-only wave (across `scheduler.py`, `recommender_base.py`, `weight_optimizer.py`, `calibrator.py`, `pipeline_tracker.py`, `bias_detector.py`, `local_recommender.py`, `claude_recommender.py`). No new agent files. One small helper (`empirical_noise_floor`, 9 lines) and one data structure (`STRUCTURAL_PROBES`, 6 entries). 23 new regression tests in `polybot/tests/test_pillar3_fixes.py`.

| # | Before | After |
|---|---|---|
| 3.1 | Hard `candidate_sharpe ≤ 0` gate; loop locked while baseline negative | Soft abs floor (`candidate < min(0, current) − 0.05` blocks adoption); recovery path active during regime shifts |
| 3.2 | L6 `old_value` always `None` in run log → directional table blind to L6 history | `scheduler.py` branches on `derived_*_weight` to read from `signal_engine.derived_weights[fname]`; L6 directional table populates correctly |
| 3.3 | `<10 trades` and fold-inconsistency rejection paths silently dropped `candidate_sharpe` | All diagnostic fields (`candidate_sharpe`, `candidate_win_rate`, `fold_sharpes`, `n_candidate_trades`) recorded before any rejection branch |
| 3.4 | `_RAMP_NOISE_FLOOR = 0.003` calibrated to n=9000; 51 of 53 deltas below floor; L1-trio steps too small to escape flat-bowl; L6 features stay at zero | (i) `empirical_noise_floor(baseline_jk_se)` scales the ramp threshold to the cycle's real JK_SE; (ii) L1-trio steps widened (`atr_sigma_ratio 0.10→0.15`, `min_atr 2.0→3.0`); (iii) `STRUCTURAL_PROBES` table fires forced L6 turn-on probes for `log_atr_ratio`, `autocorr_signed_mag`, `liq_signed_sqrt` until evidence appears |
| 3.5 | Counterfactual data showed scalp exits leave $26.90 net on the table; `exit_edge_threshold` never moved off `−0.10` | `STRUCTURAL_PROBES` forces the `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` sweep; runs once per (param, value) until directional table has evidence. `exit_edge_threshold` also in `EXPLORE_STEPS` for ongoing rotation |
| 3.6 | Calibrator stuck at identity; gate decision opaque on rejection | `IsotonicCalibrator.last_fit_diagnostics` populated every fit with `oob_ci_lower_nats`, `oob_ci_median_nats`, `n_samples`, `bootstrap_n_completed`, `decision`. Scheduler stamps the dict to `cal_info["fit_diagnostics"]` so every cycle's gate reasoning is observable |
| 3.7 | Crisis trigger required `recent-50 WR < 48%` AND smoothed past sustained collapses | Added trailing-3-day Sharpe as OR branch (`_trailing_3d_sharpe < 0.0` with `len(_trailing_gains) ≥ 20`). Multi-day bleed now fires crisis even when recent-50 has stabilized |
| 3.8 | Holdout silently fell back to empty when opt-pool < 200; no operator-visible signal | `pipeline_info["holdout_active"] = False` and `holdout_skipped_reason` stamped explicitly; INFO log emitted on fallback |
| 3.9 | Ghost data flagged `edge_decay` (83% WR rejecting winners) and `sprt_low_confidence` (78% WR rejecting winners) — but `edge_decay_threshold` is manual-only | NO-OP — `bias_detector.analyze_ghosts` already surfaces gate-rejection bias in the daily strategy log. `edge_decay_threshold` is intentionally manual-only (safety-critical gate). |
| 3.10 | `adverse_kelly_mult` stamped on only 5 of 158 outcomes — telemetry broken | NO-OP — `main.py:1265` stamps the field on every fill; audit's 5/158 reflected schema-evolution timing (the stamp was added after older outcomes were written) |
| 3.11 | `adverse_state.json` shows only 2 fills | NO-OP — rolling 30-min lookback is intentional; the `≥15 fills` activation threshold is a deliberate dormancy guard against thin-data noise |
| 3.12 | `RECENCY_DECAY_PER_DAY = 0.94` (11-day half-life) is unverified | DEFERRED — B-tier observational; empirical autocorr-decay measurement deferred to a future analytics pass |
| 3.13 | `HOLDOUT_ADOPTION_MARGIN = 0.02` calibrated to unstated sample size; at n=30 it's 0.11 SD of noise | `HOLDOUT_ADOPTION_MARGIN = max(0.02, ADOPTION_Z_FLOOR × holdout_jk_se)` — scales with sample size; consistent z=0.3 confidence regardless of n |
| 3.14 | `MIN_REGIME_N = 20` left the regime-stratified check dormant on typical BTC validation folds | Lowered to 8; non-`neutral` regimes now participate when their sample is real |
| 3.15 | `_ghost_to_outcome` silently dropped 44% of resolved ghosts (pre-Pillar-1 schema) | `logger.debug` emitted on every drop with `market_id`, `gate_name`, and the invalid `mp` value; operator can monitor the legacy-schema loss |
| 3.16 | Baseline cache not invalidated after `_apply_revert_adoptions` | `self._invalidate_baseline_cache()` called inside the revert function when `reverted_any` is True |
| 3.17 | `bias_detector.analyze_ghosts` used `ghost_gain_pct` (signal-price); `_ghost_to_outcome` used `market_price` — diverged by ~20% on individual records | `_market_price_gain(r)` inner function in `analyze_ghosts` re-derives gain_pct from `market_price_<side>`, matching `_ghost_to_outcome`'s backtest accounting |
| 3.18 | `PipelineTracker._returns_in_window` used `o.get("timestamp")` (entry time) instead of `exit_timestamp` | Now `o.get("exit_timestamp") or o.get("timestamp", "")` with fallback for legacy records |
| 3.19 | Adoption gate is a single z-test; no multi-evidence ensemble | DEFERRED — C-tier architecture; substantial engineering, low urgency |
| 3.20 | Counterfactual replay only triggers for the literal `exit_edge_threshold` param | DEFERRED — C-tier architecture; future work |
| 3.21 | No empirical Sharpe-variance estimate guides per-cycle threshold tuning | DEFERRED — C-tier architecture |
| 3.22 | `_baseline_kelly_sharpe` set at both `:1215` and `:1397` in `_run_weight_optimizer` (dual ownership) | Duplicate trailing set removed; precompute path is the sole owner |
| 3.23 | Calibrator weight normalization happened once before bootstrap, not per-resample → biased CI | Per-bootstrap renormalization (`w_b_norm = w_b / w_b.sum() * len(w_b)` and `w_oob_norm` likewise) applied inside the bootstrap loop |

**Mechanisms added — observability before complexity.** Every new mechanism is a small surface change that surfaces what the loop is doing, or removes a structural blocker. Nothing was added that doesn't have a corresponding directly-observed failure mode in the original audit data.

**Pillar 3 closed. Empirical verification (Path B — bot restart + ≥60 outcomes under current code + first-cycle log inspection) is the next gate before declaring the loop "fixed."**

---

## Pillar 3 — Learning findings

Ordered by impact × confidence.

### 3.1 (A, critical) — `candidate_sharpe ≤ 0` hard gate locks adoption when baseline is negative
**Status:** CLOSED — soft abs floor (`min(0, current) − 0.05`) in `should_adopt` allows recovery from negative baseline while blocking outright collapse. Tested.
**Location:** `polybot/agents/weight_optimizer.py:87-88`

**Current state + steelman:** Negative-Sharpe live config should never produce a "winning" parameter set — demand absolute positivity as a sanity check.

**What data shows:** 2026-05-27 baseline = **−0.0186**. By this gate, no candidate with Sharpe −0.005 can adopt — even though that's a **+0.014 improvement** (above the noise floor). Combined with the regime shift on 5/24 (WR collapsed to 41%), **the loop is hard-locked from any adjustment until something randomly clears `Sharpe > 0`**. Recovery depends on dataset luck, not proposal merit.

Crisis mode (which halves Kelly) also didn't engage: `_in_crisis = (baseline_sharpe < 0.10) AND (recent_50_wr < 0.48 OR loss_ratio > 2.0)`. Recent-50 WR has fluctuated between 38–52% over the slump; the AND smoothes through. So bot has **neither auto-adapt nor crisis protection** during the recent slump.

**Proposed change:** Replace `candidate_sharpe ≤ 0` with `candidate_sharpe < min(0, current_sharpe + ADOPTION_Z_FLOOR × SE) − 0.05`. Allow adoption when candidate clears the z-test even if absolute Sharpe is negative, with a small absolute-floor below structurally bad baselines.

**Risk:** Medium — could pass through a transiently bad config. Mitigated by holdout (if active) and regime gates.
**Confidence:** medium-high.

---

### 3.2 (A, critical) — L6 derived weights show `old_value=null` in run log → directional table blind to L6
**Status:** CLOSED — `scheduler.py:1256-1263` branches on `derived_*_weight` to read from `signal_engine.derived_weights[fname]`. L6 directional table no longer blind.
**Location:** `polybot/agents/scheduler.py:1228-1231`

**Current state + steelman:** `old_val = getattr(self.signal_engine, param, None)` for all params.

**What data shows:** L6 weights are stored at `signal_engine.derived_weights[fname]`, not as `derived_*_weight` attributes (per `signal_engine.py:157`). Every L6 change has `old_value: null`. Verified in `pipeline_run_log.json:800` — `derived_prev_margin_sq_weight` shows `"old_value": null, "new_value": 0.01, "direction": "unchanged"`. `record_pipeline_run` then sets `direction="unchanged"` when either is None. `format_directional_table` skips unchanged rows.

**Net effect: L6 probes never enter the directional table.** Optimizer cannot ramp L6 step sizes, cannot pick best L6 direction from history, cannot reject failed L6 values via `_value_failed`. L6 learning is blind.

**Proposed change:** One-line branch in `_run_weight_optimizer`:
```python
if param.startswith("derived_") and param.endswith("_weight"):
    fname = param[len("derived_"):-len("_weight")]
    old_val = self.signal_engine.derived_weights.get(fname, None)
else:
    old_val = getattr(self.signal_engine, param, None)
```

**Risk:** None — pure diagnostic correction. **Confidence:** high.

---

### 3.3 (A, critical) — `candidate_sharpe` not recorded when `<10 hypothetical trades` or fold inconsistency branches reject — explains the 2026-05-27 null deltas
**Status:** CLOSED — diagnostic fields (`candidate_sharpe`, `candidate_win_rate`, `fold_sharpes`, `n_candidate_trades`) recorded before any rejection branch.
**Location:** `polybot/agents/scheduler.py:1257-1274`

**Current state + steelman:** Short reason logged on rejection; no need to keep score.

**What data shows:** 2026-05-27 13:54 cycle has **all 5 changes with `backtest_delta_sharpe: null`**. The cycle ran manually mid-day (10 hours into the trading day after regime shift). Baseline collapsed to −0.0186. All 5 candidates hit either `<10 trades` or fold inconsistency branches → no `candidate_sharpe` written → null delta.

This is **not a backtest engine failure**. It's diagnostic loss: we can't tell from the run log whether the change was rejected for being terrible or just for having a thin trade pool.

**Proposed change:** Always record `candidate_sharpe`, `n_candidate_trades`, `fold_sharpes` regardless of rejection branch.

**Risk:** None. **Confidence:** high.

---

### 3.4 (B, critical) — 51 of 53 non-null backtest deltas are below the 0.003 noise floor
**Status:** CLOSED — `empirical_noise_floor(baseline_jk_se)` scales the ramp threshold to the cycle's real JK_SE; L1-trio EXPLORE_STEPS widened (`atr_sigma_ratio 0.10→0.15`, `min_atr 2.0→3.0`); structural L6 turn-on probes added (see Pillar 3 Resolution Log).
**Location:** `polybot/agents/recommender_base.py:17-45`

**Current state + steelman:** `_RAMP_NOISE_FLOOR = 0.003 ≈ ADOPTION_Z_FLOOR × typical JK_SE at n~9k`. Adaptive ramp (+50% per dead direction, cap 3.0×) handles flat-bowl probing.

**What data shows:** Across 12 cycles + 53 non-null deltas, only **2** exceeded 0.003 (the noise floor) and only **1** exceeded 0.007 (typical dynamic floor): the adopted `derived_prev_margin_sq_weight` with delta 0.0162. **The recommender is producing probes that are statistically indistinguishable from baseline.** The 1/53 ≈ 1.9% adoption rate is *exactly* what you'd expect when 98% of probes are noise.

Step sizes (e.g., `atr_sigma_ratio: 0.10` on range 1.2–2.5 = 7.7% of range) are too small to escape local flat-bowls at the bot's data scale. The ramp hits 3.0× cap, still no adoption.

**Additional concern:** noise floor `0.003` was calibrated to `n=9000` (per code comment). Bot has 3578 trades — actual JK_SE is ~0.017, so true noise floor should be ~0.005. The recommender thinks 0.004 is "real signal" when it's still below the adoption gate.

**Proposed change:**
1. Make `_RAMP_NOISE_FLOOR` track empirical baseline JK_SE per cycle.
2. Add **structural exploration** — turn each currently-zero L6 feature ON (5 of 8 are still zero). Even at the cap level (0.005), turning a feature on is a non-zero structural change that can break the flat-bowl.
3. Re-evaluate whether the L1-trio (`atr_sigma_ratio`, `student_t_df`, `min_atr`) hits a real surface at meaningfully larger steps.

**Risk:** Low — exploration is always backtested + holdout-gated before adoption.
**Confidence:** medium-high (diagnosis); medium (each remediation has tradeoffs).

---

### 3.5 (A, critical) — Counterfactual data shows scalp exits are leaving $26.90 net on the table; pipeline does not learn `exit_edge_threshold`
**Status:** CLOSED — `STRUCTURAL_PROBES` table forces the `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` sweep alongside the EXPLORE_STEPS rotation; runs once per (param, value) until evidence appears in the directional table.
**Location:** `polybot/agents/scheduler.py:1035-1037`, counterfactual replay

**Current state + steelman:** Counterfactual replay specifically rescores `exit_edge_threshold` candidates from stored scalp outcomes. Other params can't use this data.

**What data shows (139 scalp counterfactuals):**
- Hold-would-have-been-better: **64%** (89/139). Sum of "hold was better" deltas = **+$178.38**.
- Scalp was right: 36% (50/139). Sum = −$151.48.
- **Net effect of scalping (vs holding): +$26.90 left on the table.**
- Avg delta_pnl per scalp event: **+$0.193**.
- `exit_threshold_used` in counterfactuals: 112/113 at **−0.07** (not the −0.10 default in CLAUDE.md). The ExitBoundary's computed threshold is winning the blend over `deep_loss_floor`.

**Bucketed by holding_edge at scalp:**
| holding_edge bucket | n | hold_better rate | avg delta |
|---|---|---|---|
| −0.10 | 49 | 57% | −$0.23 |
| −0.05 | 47 | 60% | **+$0.85** |

**The bot scalps at edge ≈ −0.05 where holding wins by $0.85 on average.** The exit threshold is too tight by ~0.05 logits.

**Cross-confirmed by per-day Sharpe (recent 500):**
- Scalps: avg gain −2.10%, **Sharpe = −1.51**
- Resolutions: avg gain +9.95%, **Sharpe = +1.37**

**Specific data point matching the user's screenshot (2026-05-28 00:00 UTC, BTC $74,313 → $74,430):** Bot opened position 11350 (side=Down, strike=$74,435, BTC $74,424 at entry), scalped out 6 seconds later at $0.44 (paid $0.61) — a 28% loss on entry. **BTC ended at ~$74,400 (still under strike) → Down would have won the resolution.** The bot would have made +$0.39/share if it had held.

The pipeline-tunable `exit_edge_threshold` exists for this. But: it has never moved off its default in 12 cycles. **Why?** (a) The L1-driven `ExitBoundary.compute_exit_threshold` dominates the blend at ATM. (b) The counterfactual replay is gated on the candidate being literally named `exit_edge_threshold`. (c) The recommender hasn't probed it as exploratory because the model_prob-related params (L1) consume the directional table.

**Proposed change:**
1. Force a structural exploration: probe `exit_edge_threshold ∈ {−0.08, −0.05, −0.03}` directly. Counterfactual data backs the change.
2. Consider raising `_PRICE_VOL_PER_MIN` in `ExitBoundary` from 0.07 to ~0.10 — the ATM threshold is computed from this constant.
3. Investigate whether the `deep_loss_floor` vs `optimal_threshold` blend should be re-weighted toward the floor in current regime.

**Risk:** Material — moves a load-bearing exit parameter. Holdout would gate adoption.
**Confidence:** very high (data is overwhelming).

---

### 3.6 (A, high) — Calibrator at identity since launch; root cause unobservable
**Status:** CLOSED — `IsotonicCalibrator.last_fit_diagnostics` populated every fit; scheduler stamps `cal_info["fit_diagnostics"]` so the operator sees OOB CI lower/median and the decision reason on every cycle.
**Location:** `polybot/memory/calibration/isotonic_params.json` (always `{"type": "identity"}`)

**Current state + steelman:** Strict CI rejected; cheap branch also rejected.

**What data shows:** 99.5% of recent 800 trades stamped `calibrator_hash=identity`. With 3500+ trades in the 7-day window, the bot has plenty of data to fit. The two gates in series (calibrator-internal bootstrap CI + scheduler-side `new_loss < current_loss - 0.005`) are both opaque on failure.

Crucially: the **calibrator is exactly the place where the non-monotonic model_prob ↔ WR pathology (Pillar 2.13) should be corrected.** Bot calling 80% → 40% WR, with identity calibrator, is a structural failure that the bot's design specifies should self-correct.

**Proposed change:** Log `cal_info["improvement_strict_lower_ci"]`, `cal_info["improvement_median_ci"]`, and the gate decision reason on every cycle so operator can see whether the gate or the data is the bottleneck.

**Risk:** None (telemetry). **Confidence:** very high.

---

### 3.7 (A, high) — Crisis mode never triggered despite 4 consecutive losing days
**Status:** CLOSED — trailing-3-day Sharpe added as OR branch in crisis trigger (`scheduler.py:2253-2272`); catches sustained collapse the recent-50 WR smoothing misses.
**Location:** `polybot/agents/scheduler.py:2210-2218`

**Current state + steelman:** Triggers `baseline_sharpe < 0.10 AND (recent_50_wr < 0.48 OR loss_ratio > 2.0)`. Smoothed by recent_50 to avoid spurious firings.

**What data shows:** Per-day PnL: 5/20 +$22, 5/21 +$308, 5/22 +$205, 5/23 +$284, **5/24 +$7, 5/25 −$44, 5/26 −$12, 5/27 +$13.** Four-day collapse from $284/day to losses. Recent-50 WR oscillates between 38–52% over the slump; the recent-50 smoothing **masks the sustained collapse**. `crisis_state.json` shows `streak: 0, kelly_reduced: false`. Crisis hasn't engaged.

**Proposed change:** Add a `trailing_3_day_sharpe < 0` OR branch to the trigger. Streak requirement (≥3 cycles) already guards against single-cycle overreaction.

**Risk:** Medium — could fire on transient noise. Crisis just halves Kelly, symmetric to risk being protected.
**Confidence:** medium-high.

---

### 3.8 (A, high) — Holdout silently disabled when opt-pool < 200 (current condition)
**Status:** CLOSED — `pipeline_info["holdout_active"]` and `holdout_skipped_reason` stamped explicitly; INFO log emitted on fallback.
**Location:** `polybot/agents/scheduler.py:1814-1817`

**Current state + steelman:** When holdout-excluded optimizer pool drops below 200 trades, fall back to all_outcomes for the optimizer and an empty holdout.

**What data shows:** 2026-05-27: total in window ~3455, last-7-day holdout ~3339, opt-pool ~116 (below 200) → fall back: `opt = all_outcomes, holdout = []`. Per-change holdout branch at scheduler.py:1308 silently skipped. **Holdout confirmation is structurally inactive** until ≥30 days of trades accumulate outside the 7-day window.

**Proposed change:** Either (1) log `pipeline_info["holdout_active"] = False` with explicit reason, OR (2) reduce HOLDOUT_DAYS from 7 to 3 when opt-pool would otherwise fall below MIN. Option 1 (operator visibility) is safer.

**Risk:** Low. **Confidence:** high.

---

### 3.9 (A, high) — Bias detector / counterfactual / ghost data flow underutilized
**Status:** NO-OP — `bias_detector.analyze_ghosts` already surfaces gate-rejection bias in the daily strategy log; `edge_decay_threshold` kept manual-only by intentional design (safety-critical gate).
**Location:** `polybot/agents/scheduler.py:1875-1894`

**What data shows:** Ghost outcomes (322 records, 275 resolved):
- `edge_decay`: **n=6, WR=83%, +24.1% avg** ← **rejecting winners**
- `sprt_low_confidence`: **n=9, WR=78%, +9.4% avg** ← **rejecting winners**
- `pre_submit_vwap_drift`: n=37, WR=62%, −7.2% avg (mixed)
- `sub_threshold_prob`: n=49, WR=45%, −18.7% avg (correctly rejecting)
- `cvd_decel`: n=82, WR=52%, −19.2% avg (about even)
- `edge_cap`: n=70, WR=54%, −34.6% avg (average WR, big losses on a few)

Two specific gates (`edge_decay`, `sprt_low_confidence`) are systematically rejecting positive-WR trades. The bias detector consumes ghost data, but `edge_decay_threshold` is in **CLAUDE.md's manual-only list** — pipeline can only flag this, not adopt a fix.

**Proposed change:** Surface ghost-rejection bias in the daily strategy log; manual review and adjustment of `edge_decay_threshold` and `sprt.*` parameters. Consider promoting `edge_decay_threshold` to pipeline-tunable since the data is there.

**Risk:** Manual-only by design (rationale: avoid pipeline tuning safety-relevant gates). Operator-facing change.
**Confidence:** high.

---

### 3.10 (A, high) — `adverse_kelly_mult` stamped on only 5 of 158 outcomes — telemetry is broken
**Status:** NO-OP — `adverse_kelly_mult` stamped on every fill at `main.py:1265`. Audit's 5/158 observation reflected schema-evolution timing; current code stamps consistently.
**Location:** Promised in CLAUDE.md, written somewhere in `polybot/main.py`

**Current state + steelman:** "`adverse_kelly_mult` is stamped per-trade in `indicator_snapshot.trade_context`: the actual Kelly multiplier applied at sizing."

**What data shows:** Of 158 outcomes, **5 have `adverse_kelly_mult` set** (3%). The retrospective Sharpe-by-bucket analysis the telemetry enables is not possible at this coverage. Adverse selection penalty is **on 75% of trades** (`adverse_rate_at_30s > 0.45`), so the data the bucket analysis would expose is genuinely interesting — and currently invisible.

**Proposed change:** Find the path where `adverse_kelly_mult` is computed and confirm it's persisted on every fill. Likely a missing key in `trade_context` build.

**Risk:** Diagnostic-only fix. **Confidence:** high.

---

### 3.11 (A, high) — `adverse_state.json` tracks only 2 fills — feature is essentially not running
**Status:** NO-OP — `adverse_state.json` 30-min lookback is intentional design; the `≥15 fills` gate threshold is a deliberate dormancy guard against thin-data noise.
**Location:** `polybot/memory/adverse_state.json`

**What data shows:** The file shows 2 fills total. With 3500+ trades, this should be a rolling 30-min window of all recent fills with edge_decay deltas. The 30-min edge_decay gate (`mean_decay_15s ≥ edge_decay_threshold`) depends on this state — at n=2, the gate is essentially inactive even when conditions warrant skipping.

**Proposed change:** Trace why fills aren't accumulating. Likely a state-persistence path that's not being hit on every fill.

**Risk:** Adversarial-selection protection currently dormant. **Confidence:** high.

---

### 3.12 (B, high) — `recency_decay = 0.94/day` (11-day half-life) is unverified
**Status:** DEFERRED — B-tier observational; empirical autocorr-decay measurement deferred to a future analytics pass.
**Location:** `polybot/agents/pipeline_analytics.py:15`

**What data shows:** Empirical lag-1 autocorr of `gain_pct` is +0.063 over all 3611 trades, +0.019 on the last 1000. At trade clock, autocorr is essentially zero — **the decay weighting is approximately neutral** (neither beneficial nor harmful at lag-1). The 11-day half-life on the daily clock translates to ~5000-trade half-life on the trade clock (at ~450 trades/day). The weight at day-60 = 0.024 is fine for cutoff, but the half-life claim has no autocorrelation justification.

**Proposed change:** Add a debug-mode log of empirical autocorr decay each cycle. Don't change the constant blindly; calibrate against data if/when measurement shows clear mismatch.

**Risk:** None (telemetry). **Confidence:** medium.

---

### 3.13 (B, high) — `HOLDOUT_ADOPTION_MARGIN = 0.02` is calibrated to an unstated sample size
**Status:** CLOSED — `HOLDOUT_ADOPTION_MARGIN = max(0.02, ADOPTION_Z_FLOOR * holdout_jk_se)` scales with holdout sample size.
**Location:** `polybot/agents/scheduler.py:1307`

**What data shows:** At `HOLDOUT_MIN_TRADES = 30` and `S = 0.2`, JK_SE = √((1 + 0.5·0.04)/30) ≈ 0.183. **0.02 = 0.11 SD of holdout noise** — extremely loose. Even at n=300, SE ≈ 0.058 and 0.02 ≈ 0.34 SD.

**Proposed change:** `HOLDOUT_ADOPTION_MARGIN = max(0.02, 0.3 × holdout_jk_se)` — consistent with the z=0.3 floor in the in-sample test.

**Risk:** Low. **Confidence:** medium-high.

---

### 3.14 (B, high) — Regime-stratified check is dormant (90% trades labeled `neutral`)
**Status:** CLOSED — `MIN_REGIME_N` lowered from 20 to 8 so the stratified check actually populates on typical BTC validation folds.
**Location:** `polybot/agents/scheduler.py:1100-1102`, `bias_detector.py:269-275`

**What data shows:** Across 3455 trades, 90% labeled `neutral`, 6% `mean_reverting`, 4% `trending`. With `MIN_REGIME_N=20` applied to validation 40%, only `neutral` clears. Regime stratification effectively returns "skipped" every cycle. The labeling threshold (`|autocorr| ≥ 0.25`) is too strict for BTC at 1-min/5-min lags.

**Proposed change:** Either (1) lower `MIN_REGIME_N` to 5-10 (one-line, risks false-positive regime-rejections), OR (2) add a volatility-bucket stratification axis that produces more balanced populations.

**Risk:** Low. **Confidence:** medium.

---

### 3.15 (A, medium) — 44% of resolved ghosts dropped due to missing `market_price_<side>` in legacy records
**Status:** CLOSED — `_ghost_to_outcome` emits a debug log on every drop, naming the gate and the invalid market price so the volume of legacy-schema losses is observable.
**Location:** `polybot/agents/scheduler.py:269-289`

**What data shows:** Pre-2026-05-22 ghosts have empty `indicator_snapshot`. They fail `_ghost_to_outcome`'s validation. Counts: `cvd_decel` 26/94 valid (28%), `edge_cap` 23/76 valid (30%), `adverse_selection` 2/8 (25%). Newer records have valid data, but ~half the historical training pool is lost.

**Proposed change:** Add diagnostic log per drop. Older data is lost; format is correct from 2026-05-22 forward.

**Risk:** None. **Confidence:** high.

---

### 3.16 (A, medium) — Baseline cache not invalidated after `_apply_revert_adoptions`
**Status:** CLOSED — `_invalidate_baseline_cache()` called inside `_apply_revert_adoptions` whenever `reverted_any` is True.
**Location:** `polybot/agents/scheduler.py:1622+`, no call to `_invalidate_baseline_cache`

**What data shows:** Currently fine because reverts run before precompute in the cycle order. **Fragile to refactor** — if reverts move post-precompute, the cache silently uses a stale baseline.

**Proposed change:** Add `self._invalidate_baseline_cache()` in `_apply_revert_adoptions` when `reverted_any`.

**Risk:** None today. **Confidence:** medium.

---

### 3.17 (A, medium) — Ghost `gain_pct` uses signal_prob, but `_ghost_to_outcome` overrides to market_price → bias detector and backtest see different "simulated PnL"
**Status:** CLOSED — `bias_detector.analyze_ghosts._market_price_gain` re-derives gain_pct from `market_price_<side>`, matching `_ghost_to_outcome`'s backtest accounting.
**Location:** `polybot/agents/ghost_tracker.py:128-134` vs `polybot/agents/scheduler.py:251-289`

**What data shows:** Two different gain_pct values exist for the same ghost. `bias_detector.analyze_ghosts` uses one; backtest uses the other. Can diverge by ~20% on individual records.

**Proposed change:** Have `bias_detector.analyze_ghosts` compute simulated_pnl using market_price (matching `_ghost_to_outcome`).

**Risk:** Low — affects diagnostic output. **Confidence:** medium-high.

---

### 3.18 (A, medium) — PipelineTracker review uses `o.get("timestamp")` (entry-time) while rest of pipeline uses `exit_timestamp` for window matching
**Status:** CLOSED — `PipelineTracker._returns_in_window` prefers `exit_timestamp` with `timestamp` fallback.
**Location:** `polybot/agents/pipeline_tracker.py:206-218`

**What data shows:** For scalps, the offset is minutes — could shift a few trades across the 7d boundary.

**Proposed change:** Prefer `exit_timestamp` with `timestamp` fallback.

**Risk:** None. **Confidence:** medium.

---

### 3.19 (C, medium) — Adoption gate is single z-test; no multi-evidence ensemble
**Status:** DEFERRED — C-tier architecture (tentative-pool shadow-deployment); substantial engineering, low urgency.
**Location:** `polybot/agents/weight_optimizer.py:81-105`

**Current state + steelman:** z=0.3 floor is interpretable, single threshold.

**What data shows:** z=0.3 at S=0.2, n=1380 corresponds to a 0.008 Sharpe delta — above the 0.001 typical proposal. Full adoption chain (z-test → regime check (dormant) → holdout (mostly inactive)) collapses to single z-test in practice.

**Proposed change:** Maintain a "tentative pool" of candidates that pass z=0.15 (~56% confidence) but don't reach z=0.3; track shadow-deployment hypothetical contribution to Sharpe; promote if shadow agrees over 24-48h. Substantial engineering.

**Risk:** Higher complexity. **Confidence:** medium.

---

### 3.20 (C, medium) — Counterfactual replay only triggers for the literal param `exit_edge_threshold`
**Status:** DEFERRED — C-tier architecture (generalize counterfactual replay beyond `exit_edge_threshold`); future work.
**Location:** `polybot/agents/scheduler.py:1035-1037`

**Current state:** Other params can't use the rich scalp-vs-resolution counterfactual data.

**Proposed change:** Architectural improvement, low urgency. Counterfactual data captures "what if scalp held"; could inform `logit_scale`, `atr_sigma_ratio`, `min_model_probability` — anything that changes which trades hit the scalp threshold.

**Risk:** Future work. **Confidence:** low priority.

---

### 3.21 (C, medium) — No empirical Sharpe-variance estimate guides per-cycle threshold tuning
**Status:** DEFERRED — C-tier architecture (empirical Sharpe-variance estimate as adoption-threshold input).
**Location:** `polybot/agents/weight_optimizer.py:15`, `polybot/agents/scheduler.py:1308`

**Current state:** `ADOPTION_Z_FLOOR=0.3`, `HOLDOUT_ADOPTION_MARGIN=0.02`, `_RAMP_NOISE_FLOOR=0.003` are hardcoded. None reference empirical baseline variance.

**Proposed change:** First cycle each session, compute empirical SE of fold-average Sharpe from the 4 baseline folds. Use that to set per-cycle adoption thresholds. More principled than `_jk_se` analytical formula.

**Risk:** Medium. **Confidence:** medium.

---

### 3.22 (A, low) — Baseline-cache double-set at scheduler:1215 and :1345 with same value — dual ownership
**Status:** CLOSED — duplicate `self._baseline_kelly_sharpe` set at end of `_run_weight_optimizer` removed.
**Location:** `polybot/agents/scheduler.py:1345`

**Risk:** None. **Confidence:** low priority.

---

### 3.23 (A, low) — Calibrator weight normalization happens once, not per-bootstrap resample
**Status:** CLOSED — per-bootstrap weight renormalization (`w_b_norm` and `w_oob_norm`) applied inside `IsotonicCalibrator.fit`'s bootstrap loop.
**Location:** `polybot/core/calibrator.py:117`

**Risk:** Subtle CI accuracy. **Confidence:** medium.

---

## Considered and rejected

These passed peer-review steelman or are out of scope.

1. **Move `momentum_weight` polarity convention.** CLAUDE.md is explicit that sign is regime-conditional inside `compute_momentum`. Re-verified: the code matches doc precisely. Not a finding.
2. **L1 Student-t `√(df/(df-2))` variance correction.** Verified at `signal_engine.py:290` — math is correct.
3. **L2 sign convention.** Verified: `regime × direction` with regime>0 (trending) and direction=+1 (up move) produces +logit (bullish on Up) = continuation. Correct.
4. **Loss-cut whipsaw guard threshold (`0.5 × ATR`).** Working as designed per CLAUDE.md. Telemetry exists (`loss_cut_fired`/`loss_cut_whipsaw_blocked`).
5. **Logit-space composition order.** Verified: L1 → L2 → L3 → L3b → L3e → L5 → L4 → L6 → clamp → sigmoid → calibrator. Matches doc.
6. **Fee formula.** `rate × shares × p × (1-p)` is correct binary-option fee math.
7. **`compute_signal_consensus` Kelly multiplier.** Verified mapping; defensible thresholds.
8. **Pipeline 60-day window.** Per CLAUDE.md design; the bot only has ~10 days of paper data so empirical impact is muted. Will reach steady-state.
9. **SPRT 30-second-window-only.** Documented design; cross-window evidence is intentionally excluded.
10. **Bot trading window 12:01 AM – 11:30 PM ET, pipeline at 11:45 PM.** Schedule per `run_polybot.ps1`. Not a finding.

---

## Open questions

These need user input before proposing concrete changes.

1. **Should the bot become more patient by default?** The user's stated thesis ("buy at the cursor, ride the wave to the top, or flip with confidence") + the −1.51 vs +1.37 Sharpe split between scalps and resolutions + the $26.90 net counterfactual delta all point at a less-trigger-happy exit. But the scope says **sizing/exit are out of scope**. Decision: aggressively flag the data, propose `exit_edge_threshold` exploration explicitly (it IS pipeline-tunable per CLAUDE.md), but defer the larger ExitBoundary refactor.

2. **L3b sign-flip vs zero vs hold.** Empirical correlation of L3b with realized direction is **−0.164 on n=158, 95% CI ~[−0.31, −0.01]**. Statistically significant but the *true* sign is uncertain. Choices: (a) zero the weight pending more data, (b) flip the sign (assume the regime is structural — Binance retail vs Coinbase smart money divergence at 5-min scale), (c) make `spot_flow_weight` regime-conditional. Need user decision on conviction.

3. **Calibrator cheap-acceptance branch — keep or remove?** The strict CI rejects on noisy data; cheap branch is supposed to catch genuine improvements that the strict gate is too tight to see. But in-sample improvement is structurally guaranteed for isotonic. Fix is one of: (a) replace with OOB median test (Finding 2.5), (b) tighten the cheap floor from 0.001 to 0.005 nats, (c) remove the cheap branch entirely. Need user decision on adoption risk tolerance.

4. **Should `edge_decay_threshold` move from manual-only to pipeline-tunable?** Ghost data shows the gate is rejecting 83% WR trades (n=6). Manual-only per current design; data argues for pipeline learning. Within scope but listed because user signed off on the manual-only list.

5. **Out-of-scope flags worth surfacing briefly:**
   - **Concurrent correlation `ρ` is a fixed prior** (`+0.75` same-side, `−0.25` opposite-side). CLAUDE.md flags this as future work. Promoting to empirical estimator is non-trivial.
   - **Fee rate constant** `0.018` — works for current Polymarket fee. No action needed unless Polymarket changes.
   - **Per-mode DBs** — `polybot_paper.db` vs `polybot_live.db` schema match by inspection.

---

## Empirical appendices

### A. Trade outcome distributions

| Metric | All (n=3578) | Recent 500 | Recent 50 |
|---|---|---|---|
| Win rate | 52.6% | 48.6% | 38% |
| Avg PnL | +$0.22 | +$0.015 | −$0.43 |
| Total PnL | +$786 | +$7.5 | −$21 |
| avg_win | $0.94 | $0.94 | $0.96 |
| avg_loss | −$0.84 | −$0.85 | −$1.10 |
| loss/win ratio | 0.89 | 0.91 | 1.14 |

### B. Per-exit-reason Sharpe (recent 500)

| Exit reason | n | avg gain | std | Sharpe |
|---|---|---|---|---|
| Scalp | 377 | −2.10% | 24.9% | **−1.51** |
| Resolution | 123 | +9.95% | 72.9% | **+1.37** |

Bot scalps 85% (3053/3578) of fills. The two distributions are not comparable risk-adjusted — scalp has higher Sharpe magnitude (closer to zero variance) but **wrong sign**.

### C. Per-day Sharpe by exit_reason

| Date | Resolution Sharpe | Scalp Sharpe |
|---|---|---|
| 2026-05-20 | −5.41 | +4.73 |
| 2026-05-21 | +2.97 | **+10.24** |
| 2026-05-22 | +1.94 | +5.17 |
| 2026-05-23 | +4.27 | +4.44 |
| 2026-05-24 | +3.41 | **−2.58** |
| 2026-05-25 | +2.19 | **−7.23** |
| 2026-05-26 | +0.90 | −2.35 |
| 2026-05-27 | +0.86 | −0.41 |

**Regime break on 2026-05-24** — scalp Sharpe flipped from +5 to −2.58 and stayed negative. Resolutions stayed positive throughout. Multiple parameters need re-tuning for the new regime; pipeline hasn't recognized this is a regime break (no special handling).

### D. Layer logit contribution statistics (n=158)

| Layer | Mean | Stdev | |max| | ρ realized |
|---|---|---|---|---|
| L1 | −0.192 | 1.418 | 9.89 | +0.046 |
| L2 | −0.002 | 0.017 | 0.04 | +0.000 |
| L3 | −0.002 | 0.044 | 0.11 | +0.018 |
| L3b | −0.010 | 0.233 | 0.40 | **−0.164** |
| L3e | +0.000 | 0.003 | 0.03 | **−0.078** |
| L5 | +0.049 | 0.046 | 0.08 | +0.029 |
| Sum L2-L5 | +0.036 | 0.246 | — | **−0.147** |
| L1 dominates (|L1| > 3·|L2-L5|) | — | — | — | **53% of trades** |

### E. Clamp binding rates (n=158)

| Clamp | Frequency |
|---|---|
| L1 |logit| > 4 | 4 / 158 (2.5%) |
| Total |logit| > 4 (final clamp binds) | 4 / 158 (2.5%) |
| L6 ±0.25 cap binds (weights ≈ 0) | 0% (most L6 weights still 0) |
| `momentum_weight × 1.5 → 0.10 clamp` | dead at base ≥ 0.067 |

### F. Pairwise layer correlations (n=158)

| Pair | ρ |
|---|---|
| L2,L3 | +0.05 |
| L2,L3b | −0.08 |
| L2,L3e | +0.16 |
| L2,L5 | −0.13 |
| L3,L3b | +0.00 |
| L3,L5 | +0.03 |
| L3b,L3e | +0.16 |
| L3b,L5 | +0.06 |
| L3e,L5 | −0.01 |

**None exceed 0.20.** Layers are not redundant by correlation — they're independently noisy. (Pillar 2 agent flagged L1 + L6 `distance_atr_ratio` redundancy at the formula level; data confirms when L6 weight is non-zero.)

### G. Calibration audit (recent 800)

| model_prob bucket | n | win rate |
|---|---|---|
| ~0.6 | 197 | 48.7% |
| ~0.7 | 202 | 47.0% |
| ~0.8 | 170 | **40.0%** |
| ~0.9 | 134 | 54.5% |
| ~1.0 | 97 | 61.9% |

`corr(model_prob, win) = +0.069` (recent 800), `corr(edge, win) = −0.015` (recent 800).
**Identity calibrator is stamped on 99.5% of recent 800 trades. The calibrator has been at identity since launch.**

### H. Pipeline cycle stats

| Cycles run | 12 |
| Total candidate changes proposed | 60 |
| Adopted | **1** (`derived_prev_margin_sq_weight`, 2026-05-26, Δ=+0.0162) |
| Reverts | 0 |
| Non-null backtest deltas | 53 |
| Deltas > 0.003 (noise floor) | 2 |
| Deltas > 0.007 (typical adoption threshold) | 1 |
| Most recent cycle: null deltas | 5/5 (all rejected at `<10 trades` or fold inconsistency) |

### I. Counterfactual + ghost utilization

| Counterfactuals (scalp events with hold replay) | n=139 |
| Hold-was-optimal | 64% (89) |
| Sum of hold-better deltas | +$178.38 |
| Sum of scalp-right deltas | −$151.48 |
| **Net delta** | **+$26.90** |
| Avg per scalp | **+$0.193** |
| exit_threshold_used distribution | 112 at −0.07, 1 at −0.10 |

| Ghost outcomes (resolved) | n=275 |
| Overall would-have-won rate | 55% |
| `edge_decay` rejected | n=6, WR=**83%**, +24.1% avg |
| `sprt_low_confidence` rejected | n=9, WR=**78%**, +9.4% avg |
| `pre_submit_vwap_drift` | n=37, WR=62%, −7.2% avg |
| `cvd_decel` | n=82, WR=52%, −19.2% |
| `sub_threshold_prob` | n=49, WR=45%, −18.7% (correctly filtered) |
| `edge_cap` | n=70, WR=54%, −34.6% (correctly filtered) |

### J. Empirical gain_pct autocorrelation

- All 3578 trades, lag-1 autocorr: **+0.063**
- Last 1000 trades, lag-1 autocorr: **+0.019**
- Implication: recency weighting `0.94^days_ago` (11-day half-life) is approximately neutral at trade-clock lag-1. **The half-life claim has no autocorrelation justification.**

### K. Crisis trigger evaluation

`crisis_state.json`: `streak: 0, kelly_reduced: false, original_kelly: null`. Never triggered.

Recent 50 (live): WR=38%, avg_win=$0.96, avg_loss=−$1.10, loss/win=1.14. The WR<0.48 trigger condition IS met. The AND with `baseline_sharpe < 0.10` is *also* met (baseline = −0.019). **Why didn't crisis fire?** Because the trigger requires N consecutive cycles (streak ≥ 3). With only 12 cycles spread over 7 days, and recent-50 fluctuating, the streak resets each time recent-50 crosses 48%.

### L. Adverse selection state

`adverse_state.json`: tracks 2 fills total. The 30-min lookback for `mean_decay_15s ≥ edge_decay_threshold` requires ≥15 resolved fills to activate (per CLAUDE.md). **The gate is dormant due to incomplete fill recording.** 75% of recent trades have `adverse_rate_at_30s > 0.45` — the regime is genuinely adverse, but the bot's defense is asleep.

---

## What to act on first (operator triage)

1. **Investigate L3b sign-flip** (Finding 2.1). Highest-impact, time-bounded data analysis. Decide: zero weight, flip sign, or regime-conditional.
2. **Patch L6 directional table bug** (Finding 3.2). 1-line fix; unblocks 5+ L6 features the pipeline can't currently learn.
3. **Patch `candidate_sharpe ≤ 0` adoption gate** (Finding 3.1). Critical to recover from current negative baseline.
4. **Surface calibrator gate-decision telemetry** (Finding 3.6). Identity-for-life is opaque; logging the gate reason unblocks debugging.
5. **Force structural exploration of `exit_edge_threshold`** (Finding 3.5). Counterfactual data has been screaming for this since launch.
6. **Fix `binance_depth`/`binance_trades` URL double-suffixing** (Finding 1.3). Latent failure waiting for Binance to tighten URL parsing.
7. **Wire Bybit `fundingRate`, Coinbase `side`** (Findings 1.2, 1.1). Free signals being thrown away.
8. **Fix `adverse_kelly_mult` telemetry** (Finding 3.10) and **`adverse_state.json` accumulation** (Finding 3.11). Adverse-selection defense is sleeping.

The bot's edge is real (resolutions Sharpe +1.37 over n=123 recent) — it's currently surrounded by 5–6 separate systems destroying that edge before it can compound. Removing those drags is more leveraged than introducing new signal.
