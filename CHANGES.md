# CHANGES — Final Pass-Through

Final pass over the codebase against `CLAUDE.md` (§1–§19) + the layout, guardrail, and
invariant audits. Phase-1 audit reports are in `tasks/audit/A1–A8.md`. All changes landed
on branch `final-pass-cleanup`, one concern per commit, full suite green throughout.

Headline: the codebase was already in strong shape — **no money-losing bug in the core
model / sizing / order-placement / resolution math, and every §13 guardrail and §17
invariant has a real enforcement point.** The fixes below are a money-path correctness
hardening, a latent pipeline-revert bug, a real packaging bug, and doc/registry reconciliation.

---

## ⚠️ Money-path / safety review (read first)

**No change reduces a documented live-money safety margin.** One money-path *behavior*
changed — it strictly **increases** safety — and is called out here for explicit review:

- **`95387f4` — Resolution exit-price decision (`main.py`).** *Behavior change (safer).*
  Previously `_resolve_expired_position` checked a `closed`+extreme `price_up` **first** and
  resolved the *other* side via `price_down` blindly, with the authoritative Chainlink oracle
  (`event_metadata`) branch only **second**. On incoherent Gamma prices a winning side could
  resolve at the wrong value (silent loss in paper; wrong recorded PnL in live). Now decided
  **oracle-first**, then a **coherent** resolved CLOB book (sum ∈ [0.98,1.02], one side
  extreme), paying the **binary $1/$0** derived from the winner — exact payoff, zero taker fee
  at the extreme, no `price_down` dependency. Incoherent books defer to the oracle/orphan path.
  Same latent bug fixed in the orphan path. Net effect: a winner can no longer mis-resolve;
  payouts are exact. Covered by `tests/test_resolution.py` (14 cases). CLAUDE.md §8 updated.

Also touching money-adjacent code but **not** a sizing/safety-margin change:
- `b9c4873` aligns the circuit-breaker `wins_to_restore` default (alert-only, no sizing impact).
- `d25618f` fixes the live-mode CLOB dependency in `requirements.txt` (install correctness).

---

## Bugs fixed (with tests)

| Commit | File | What | Behavior change? |
|---|---|---|---|
| `95387f4` | `main.py`, `tests/test_resolution.py` | Resolution oracle-first + coherent-book + binary payoff (see safety review) | **Yes (safer)** |
| `005fcbc` | `agents/scheduler.py`, `tests/test_pipeline_revert.py` | Auto-revert recorded the **new** value as "old" (read `getattr(signal_engine,…)` *after* the mutation loop) → revert was a no-op for every param, and `None`/skipped for L6 weights (live in `derived_weights`). Now reuses the pre-mutation, L6-aware `old_value`; extracted `_record_run_adoption`. Broader than first flagged (all params, not just L6). | Yes (revert now works) |
| `d25618f` | `requirements.txt` | Declared `py-clob-client` (v1, unimported) but the code imports `py_clob_client_v2` everywhere → fresh `pip install` broke live mode. Now declares `py_clob_client_v2>=1.0.0`. | Install correctness |

## Discrepancies reconciled (doc ↔ code; doc was wrong unless noted)

| Commit | File(s) | What |
|---|---|---|
| `1f934c8` | `CLAUDE.md §15` | "Chainlink via Eth RPC / `latestRoundData()`" is fiction — it's the Polymarket RTDS WS (`crypto_prices_chainlink`); no web3 anywhere. Dropped the non-existent `/fee-rate` feed (constant, no HTTP); added the real `/tick-size`. |
| `0442619` | `CLAUDE.md §13`, `main.py` comment | Guardrail #7 said entry-edge uses `GET /price`; the code deliberately uses the executable **book BBO** and avoids `/price` (phantom near expiry). `/price` is only an exit-side phantom cross-check. Fixed the guardrail + the stale `main.py:1158` comment. |
| `ab25a1b` | `CLAUDE.md §18,§14`, `bot.py` | §18/§14 listed 4 phantom Discord commands (`!positions/!performance/!agents/!lessons`) and omitted the real `!pipeline`. Reconciled all three sites to the actual 8 handlers. |
| `b9c4873` | `CLAUDE.md §9`, `settings.yaml`, `circuit_breaker.py`, `main.py` | §9 said "2 wins"; runtime is 3 (settings). Aligned the class default + `main.py` fallback to 3; rewrote the settings comments that falsely implied streaks scale Kelly (alert-only). |
| `c2ccc82` | `settings.yaml` | `deep_loss_hold_threshold` was tagged `[P]` inside the `[P]` block but is MANUAL-ONLY; moved next to its `[M]` exit-policy siblings. |
| `9572ffb` | `param_registry.py`, `claude_client.py`, `main.py`, `scheduler.py` | Schedule fallback literals were inconsistent/wrong (minute 15/59, hour 22) vs the documented 0:01/23:30. Added the 4 schedule defaults to the registry and routed every fallback through `default_for`. |
| `ef26bb7` | `CLAUDE.md §6,§8,§14`, `scheduler.py`, `claude_client.py` | Comment said log-loss gate ≥0.010 (it's 0.005); "14-day half-life" label (it's ~11-day); §6 "PnL"→`gain_pct`; §8 orphan window "30+ min past expiry"→"~30 min after entry"; §14 feeds line omitted `_json.py`. |
| `2e71ffb` | `CLAUDE.md §16` | `verify_keys.py` (live pre-flight) was undocumented; added to Running. |

## Dead code removed

Method: objective static analysis (`ruff` F-rules + `vulture`) → an 8-agent verification
workflow that adversarially proved each candidate dead/used across **all** call paths
(incl. decorators, cross-module, dynamic dispatch, tests) and hunted tool-blind-spots
(dead config keys, legacy shims, dead/skipped tests, orphan modules) → loop-until-dry
re-runs after each removal to catch transitively-exposed dead code. Two workflow verdicts
were **rejected on manual review** (false positives): `live_trader._clob_helpers._http_client`
(a deliberate keepalive monkeypatch — removing it would regress live-order TLS) and the
`config/__init__` re-exports (the agent called them "used"; nothing actually imports
`from polybot.config import …`, so they were genuinely dead and removed).

| Commit | What |
|---|---|
| `4a9184a` | `MarketScanner._fee_rate_cache` + `_fee_rate_cache_seconds` — never read since `fetch_fee_rate` returns the constant. |
| `0cfecb9` | `db/models.py` completed `ALTER TABLE DROP COLUMN` migrations (11 columns no longer in the schema) — permanent no-op. |
| `5e6b5c3` | Dead **kline_1s** chain (`@kline_1s` subscription, `_FastCloseBuffer`, `fast_closes`, `_handle_kline_1s`, `realized_vol`) — `fast_realized_vol` had zero callers. Dead **exch_ts** chain (`AggTrade.exch_ts`, `_exch_lag`, `add_trade` param, `data["T"]` parse) — `exch_lag_snapshot` was its only reader. |
| `644b539` | Dead methods/attrs: `BinanceDepthFeed.get_imbalance/age_s`+`_book_side_usd`, `ChainlinkFeed._last_payload_ts`, `MarketScanner.DATA_API`, `SPRTAccumulator.lower_bound`, `ClaudeRecommender._value_already_tested`, `BiasDetector.REGIME_NAMES`, dead scheduler/main locals, `weight_optimizer` unused `sharpe` import, four superseded `alerts` report helpers, `main._fast_dumps`. Dropped the unused `config/__init__` re-exports + unconsumed `market.contract_type`. ruff F401/F811/F541 hygiene across production. |
| `fdd3f40` | Unused test imports + dead test locals (ruff F401/F841) across the suite. |

After the sweep: `ruff check polybot/ --select F` → **All checks passed**; every `polybot.*`
module imports clean; `vulture` residue is only library-attribute / interface-param false
positives (`ryaml.width`, mock `_execute_buy(token_id,…)`, `mock_ta_evolver(analysis,…)`,
`__aexit__(self,*a)`), which are signature-required, not dead code.

## Deliberately NOT changed (verified, with rationale)

- **`compute_time_multiplier` `max(0.40, …)` floor** — *not* dead. It binds only if an operator
  sets `late_max_penalty > 0.60` (defaults 0.30); it's a defensive size floor. Removing it would
  weaken a safety clamp → kept.
- **`StalenessTracker.mark_connected/mark_disconnected`** — coherent, tested, documented capability
  that's simply unwired in the feeds (not legacy cruft). Kept; wiring it into the feeds is logged as
  an improvement (Stage 1) rather than removed-then-readded.
- **`scripts/holdout_kill_l4.py`** — a standalone research/diagnostic one-off (not imported anywhere).
  Kept; its duplication of `compute_momentum` (drift risk) is noted as an improvement.
- **Coinbase `feed_staleness` blends heartbeat + ticker** — defensible: it tracks socket liveness
  (low false-positive). Ticker-only would raise false "degraded" flags in quiet markets, and the
  trading path already uses ticker-only `state.age_seconds`. Reclassified from "bug" to improvement.
- **Inline indicator-weight fallbacks in `compute_momentum`** — defensive defaults; kept.

## Test deltas
- Added `polybot/tests/test_resolution.py` (12 cases — oracle/book/incoherent resolution paths).
- Added `polybot/tests/test_pipeline_revert.py` (2 cases — pre-adoption old-value capture + L6 revert).
- Baseline 497 → **511** passing (full suite green, exit 0).
