# Polybot Codebase Audit — 2026-05-27

## Summary

- **38 items found** across 6 categories.
- **Critical: 2** (would affect live trading or block development).
- **Important: 14** (cleanup / prevents future bugs).
- **Trivial: 12** (cosmetic / defer).
- **Open Questions: 10** (route to user before action).

The bot is in remarkable shape. **Zero `TODO/FIXME/HACK/XXX` markers** across the whole repo. **Zero `pytest.skip`/`xfail`** markers across 478 collected tests. The Probability Model in CLAUDE.md matches `signal_engine.py` layer-for-layer with no divergence. The daily pipeline in `scheduler.py` matches CLAUDE.md's documented stage order. All 24 pipeline-tunable params + 8 L6 weights agree between `param_registry`, `settings.yaml`, and the `claude_client` validator with correct bounds. SQLite paper and live DB schemas are byte-identical. All 10 "What NOT to Change" invariants intact. All single-source-of-truth claims (`lag1_autocorr`, fee/vwap/slippage primitives, `default_for`, atomic position transaction) verified. No dead top-level classes/functions found across core/, agents/, execution/, feeds/, indicators/, db/, discord_bot/, config/.

The findings below are real but small. Two genuine bugs (`kelly_fraction` preflight path — benign-leaning; system-prompt `momentum_weight` sign hint), a handful of orphan files / write-only DB columns that are intentional audit logs (route those to Open Questions), and a few unreferenced/hardcoded constants worth tidying.

---

## Critical findings

### Config drift: live-mode preflight reads `kelly_fraction` from wrong YAML path
- **Location:** `polybot/main.py:2697`
- **Evidence:**
  ```python
  _kelly_fraction = config.get("signal", {}).get("kelly_fraction", 0.15)
  ```
  - `kelly_fraction` lives at `math.kelly_fraction` per `param_registry.py:37` (`yaml_key="math.kelly_fraction"`) and `settings.yaml:7`.
  - `config["signal"]["kelly_fraction"]` does not exist → the lookup always falls back to `0.15`.
  - Registry default is `0.08`; pipeline range `0.05–0.18`. The 0.15 fallback is neither — it's a stale hardcode.
- **Impact:** The live-mode preflight computes `_min_allowance = bankroll × _kelly_fraction × max_concurrent × 10`. With `_kelly_fraction = 0.15` (vs real 0.08) the allowance check is **1.87× too strict** — live mode can fail `verify_auth` even when actual trading allowance is sufficient. The runtime trading loop reads `signal_engine.kelly_fraction` correctly (`_build_signal_engine` at `main.py:351` uses `config["math"]["kelly_fraction"]`), so the bug is **limited to startup gating** and **benign-leaning** (over-demands allowance, never under-requests). `LiveTrader` does NOT read `_kelly_fraction`; trade sizing uses `signal_engine.kelly_fraction`. Crisis-reduced kelly (0.04) is also invisible to the preflight — the gate remains stuck at 0.15.
- **Why dead/stale:** Code-side fallback drift from registry default — same class of issue CLAUDE.md's "Parameter Ownership" section is designed to prevent.
- **Recommended action:** Replace with `config.get("math", {}).get("kelly_fraction", _d("kelly_fraction"))` to match `_build_signal_engine`. The fallback should use `_d("kelly_fraction")` so the registry default is single-source-of-truth.
- **Confidence:** high.

### System prompt tells Claude `momentum_weight` is signed; registry forbids negatives
- **Location:** `polybot/agents/claude_client.py:103`
- **Evidence:**
  ```
  - momentum_weight (-0.10 to +0.10; NEGATIVE = fade indicators)
  ```
  vs registry (`param_registry.py:35`):
  ```python
  ParamSpec("momentum_weight", ..., 0.0, 0.10, float, 0.04, ...)
  ```
  vs CLAUDE.md ("magnitude only — sign is dead, polarity is regime-conditional per group inside `compute_momentum`").
- **Impact:** Claude can propose `momentum_weight = -0.05` (it has been told negatives are legal). The validator silently clamps to `0.0` at `claude_client.py:376`. The adopted value is `0` (layer dampened) but the directional table at `pipeline_run_log.json` records the *clamped* value, so future cycles attribute the result to `0.0` rather than the negative proposal. Net effect: Claude wastes cycles proposing impossible values. Also `signal_engine.effective_momentum_weight` takes `abs(...)` (line 226), so even if a negative leaked through it would be made positive — confirming the sign is genuinely dead at runtime.
- **Why dead/stale:** Leftover system-prompt text from before the polarity-split refactor. Bridges past behavior into current model in a misleading way.
- **Recommended action:** Update `claude_client.py:103` to `momentum_weight (0.0 to 0.10; magnitude only — polarity is regime-conditional)`. Also remove the dead negative-handling branch at `claude_client.py:381-385` (`abs(clamped) >= min_edge_live ... (1.0 if clamped >= 0 else -1.0)` — the `else -1.0` branch is unreachable after the floor-0 clamp).
- **Confidence:** high.

---

## Important findings

### `late_window_min_prob` exists in settings.yaml but not in `param_registry`
- **Location:** `polybot/config/settings.yaml:55` and `polybot/main.py:986`.
- **Evidence:**
  ```yaml
  late_window_min_prob: 0.4   # In last 2 mins: model must be at least x% to enter
  ```
  ```python
  late_underdog_floor = config.get("signal", {}).get("late_window_min_prob", 0.40)
  ```
  Grep: no `ParamSpec` entry for `late_window_min_prob` in `param_registry.py`. Not in `MANUAL_ONLY_PARAMS` either.
- **Why orphan:** A real entry gate ("late window underdog") with a configurable threshold, but bypassing the single-source-of-truth registry. Means the value can't be validated by `loader.validate_config`, can't be proposed by Claude/Local, can't be clamped, and isn't documented as a tunable in CLAUDE.md.
- **Risk if left:** Drift between yaml/code is undetectable; future optimizer can't reach this knob; operator changing it in yaml gets no validation.
- **Recommended action:** Either (a) add a `MANUAL_ONLY` ParamSpec to `_MANUAL_DEFAULTS` and reference via `_d("late_window_min_prob")`, or (b) hardcode it in `signal_engine` / `main.py` as a design constant and remove from yaml. Don't leave it as a half-managed knob.
- **Confidence:** high.

### `fok_spread_cross_floor` referenced in code but defined nowhere
- **Location:** `polybot/main.py:1809`
- **Evidence:**
  ```python
  fok_floor = config.get("execution", {}).get("fok_spread_cross_floor", 0.08)
  ```
  Grep: `fok_spread_cross_floor` is not in `settings.yaml`, not in `param_registry.py`, not anywhere else in the repo. Always falls back to the hardcoded `0.08`.
- **Why orphan:** Either intentional hardcode disguised as a config knob, or stale leftover from an extracted-to-config refactor that never completed.
- **Risk if left:** Same as above — looks tunable but isn't.
- **Recommended action:** Either delete the `.get()` indirection (just write `fok_floor = 0.08` with a comment) or add `execution.fok_spread_cross_floor: 0.08` to `settings.yaml`. Pick one source of truth.
- **Confidence:** high.

### Two direct `UPDATE positions` writes in `live_trader.py` — reconciliation-only, not money mutations
- **Location 1:** `polybot/execution/live_trader.py:1205-1208` — `UPDATE positions SET shares_held=?`
- **Location 2:** `polybot/execution/live_trader.py:1265-1268` — `UPDATE positions SET status='closed', exit_price=?, exit_timestamp=?` (exception fallback)
- **Evidence:** Both inside `_recover_missed_close` reconciliation flow.
  - L1206: adjusts `shares_held` to chain truth when DB drifted. Column-only update — not a money mutation.
  - L1266: documented exception path that fires only when `db.close_position(...)` already raised. Comment line 1261 calls it "fallback to status-only close".
- **Why important (downgraded):** Cross-verified by both DB and dead-symbols audits — these are NOT invariant violations. CLAUDE.md's "single atomic close path" applies to money mutations (positions + bankroll), and both of these are either column-only (shares_held) or post-failure fallback. The risk that the fallback at 1266 leaves trade_history unwritten is real but bounded: at that point `close_position` has already raised, so the operator sees the error in logs and can manually reconcile. Recovery is via `_recover_missed_close` itself on the next live-mode startup.
- **Risk if left:** A double-failure (close_position raises AND status fallback succeeds AND nobody notices the log) would leave a trade missing from trade_history. Operator-discoverable from DB rows where `status='closed'` but no trade_history row. Low risk in practice.
- **Recommended action:** Add a comment to `db/models.py` close path explicitly noting that the live_trader reconciliation paths are by-design exceptions to "single close path", OR mention them in CLAUDE.md's Live Execution & Safety section so future audits don't re-flag them.
- **Confidence:** high (paths exist and are documented in source comments).

### `READ_ONLY_PARAMS` is an empty set with a dead consumer
- **Location:** `polybot/agents/claude_client.py:280, 304`
- **Evidence:**
  ```python
  READ_ONLY_PARAMS: set[str] = set()
  ...
  if param in READ_ONLY_PARAMS:
      ...  # skip
  ```
- **Why dead:** Set is empty; the `if param in READ_ONLY_PARAMS` branch can never fire. Comment explains the params were promoted to pipeline-tunable.
- **Recommended action:** Delete the empty set + the unreachable branch.
- **Confidence:** high.

### `biases.json` written every pipeline cycle but never read by production code
- **Location:** Writer `polybot/agents/bias_detector.py:818-820`. Test reader `polybot/tests/test_bias_detector.py:66`.
- **Evidence:** Grep finds no production reader. `main.py:2536, 2755` only constructs the path; `BiasDetector.save()` writes; nothing consumes the file post-write.
- **Why orphan:** Snapshot persistence — written so the operator can inspect last cycle's bias detection, but every pipeline cycle uses fresh in-memory analysis, not the file.
- **Risk if left:** Disk waste (one snapshot per run, overwritten); slight confusion when grepping for biases consumers.
- **Recommended action:** Either delete the `save()` call and rely on logs + pipeline_run_log, or document the file as operator-facing-only (no code reader needed). User decision.
- **Confidence:** high.

### Backups folder holds ~70MB of stale paper DBs and rollups
- **Location:** `backups/pre_5_20_reset_20260526_110126/` and `backups/pre_5_20_reset_20260526_110153/`.
- **Evidence:** Each has a 35MB `polybot_paper.db` and ~80 daily rollup JSONs (counterfactuals, ghost_outcomes, outcomes) from 2026-04-13 through 2026-05-26. Two near-identical snapshots taken 27 seconds apart on 2026-05-26.
- **Why stale:** Explicit pre-reset snapshot. Not loaded by any code path (no readers grepping `backups/`).
- **Risk if left:** Disk waste (~70MB) and a second outdated DB schema sitting on disk. Otherwise inert.
- **Recommended action:** Compress + archive off-machine, or delete the older of the two snapshots since they're 27 seconds apart and presumably identical. User decision — these may have audit/recovery value.
- **Confidence:** high (orphan status); low (whether to delete).

### Code-side default for `signal.weights` dict differs from settings.yaml runtime
- **Location:** `polybot/config/param_registry.py:108` and `polybot/core/signal_engine.py:393-400` (mean_revert / trend_confirm defaults).
- **Evidence:**
  - Registry default: `{"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20}`
  - signal_engine compute_momentum fallbacks (`w.get(name, X)`): same as registry.
  - settings.yaml line 72-77: `rsi: 0.2, macd: 0.3, stochastic: 0.15, obv: 0.15, vwap: 0.2`.
- **Why important:** All three sum to 1.0 and pass weights validation, so this isn't a bug — but the registry's "default" weights dict diverges from the live yaml. If yaml is ever regenerated from the registry default (e.g., via a future migration), MACD weight silently drops from 0.30 to 0.25 and stochastic jumps from 0.15 to 0.20. The "single source of truth" intent of `param_registry` is violated.
- **Recommended action:** Sync the registry default to match settings.yaml, OR drop the per-indicator defaults from the registry and document `weights` as "yaml-supplied only, no code default".
- **Confidence:** high.

### Several `polybot/memory/` files are write-only telemetry without explicit operator-facing documentation
- **Files:** `biases.json`, `calibration/isotonic_rejected.json`, `dust_sweep_stats.json` (not yet on disk), `latency_stats.json`, `warmup_stats.json`.
- **Evidence:**
  - `isotonic_rejected.json` — writer at `scheduler.py:2039-2047`, no reader. Comment line 2036 explicitly says "kept for telemetry".
  - `latency_stats.json` — writer in `live_trader._record_submit_latency` (~lines 157-200), no reader. `paper_trader.py:25` comments reference it as operator-tuning reference.
  - `warmup_stats.json` — writer at `execution/base.py:173-174`, no reader.
  - `dust_sweep_stats.json` — writer at `execution/base.py:241` (`record_dust_sweep_outcome`), called from `live_trader.py:909/929/935/954`, no reader.
- **Why important:** Per CLAUDE.md ("Per-trade telemetry stamped at open, persisted at close — calibrator_hash is logged for audit only; no current consumer stratifies by it"), telemetry without a consumer can be intentional. But these files are not called out in CLAUDE.md the way `calibrator_hash` is.
- **Recommended action:** Route to **Open Questions** below — confirm each is operator-only audit and add a one-liner to CLAUDE.md's invariants section ("Per-pipeline telemetry — operator-readable, no code consumer expected").
- **Confidence:** medium (orphan); high (consumer absence).

### Write-only audit columns in `trade_history` and `positions`
- **Columns:**
  - `trade_history.position_id` — write-only (no SELECT against it by field name).
  - `trade_history.signal_strength`, `signal_score`, `log_return`, `entry_timestamp`, `market_id`, `question`, `id` — written but only `SELECT *` readers exist (callers `dict(row)` then never reference these keys).
  - `positions.signal_strength`, `log_return` — same pattern.
- **Why important:** Bloat on every trade row, undocumented why they're kept. CLAUDE.md's invariants and `models.py:81-94` migration shows precedent for `ALTER TABLE DROP COLUMN`.
- **Recommended action:** Route to **Open Questions**. These likely exist for backwards compat with old rows and operator-facing exports (e.g., the discord bot `!history` SELECTs all columns and shows some via `dict(row)`).
- **Confidence:** high (write-only); low (whether to drop).

### Hardcoded magic number `0.53` for opposite-side flip prob threshold
- **Location:** `polybot/main.py:849`
- **Evidence:**
  ```python
  if signal.prob < 0.53:
      _record_skip("opposite_flip_weak_prob")
  ```
- **Why important:** Live entry-gate parameter not in yaml/registry. CLAUDE.md's Entry Gates section doesn't mention the 0.53 opposite-flip floor. Operator can't tune it; pipeline can't propose it.
- **Recommended action:** Add `opposite_flip_prob_threshold` to `_MANUAL_DEFAULTS` (route to manual-only — operator-tuned), OR document as a design constant in CLAUDE.md.
- **Confidence:** high.

### Inconsistency between CLAUDE.md and code for several hardcoded constants
- CLAUDE.md says ATR floor widens when `rolling_20 < 60% of long-term_200`. Code: `atr_regime_shift_threshold` default 0.60 is pipeline-tunable in `[0.40, 0.80]`. Wording is fine (default is 60%) but slightly misleading — "60%" reads as a constant.
- CLAUDE.md ATR rolling-20 references 20-period; code (`signal_engine.py:29`) `_ATR_HISTORY_SIZE = 20` and long-term `_ATR_LONG_TERM_SIZE = 200`. Min samples: `_ATR_HISTORY_MIN_SAMPLES = 5`, `_ATR_LONG_TERM_MIN_SAMPLES = 50`. These are not documented but are reasonable warmup floors.
- **Confidence:** low — minor wording.
- **Recommended action:** Optional: update CLAUDE.md to clarify the 60% is the default of a pipeline-tunable in `[0.40, 0.80]`.

### Documentation drift — Entry Gates list in CLAUDE.md is missing several real gates
CLAUDE.md's "Entry Gates" section lists prob/edge/Kelly/spread/depth/price_sum/edge_cap/adverse/edge_decay/SPRT/ATR/CVD-decel. The code (`main.py:_evaluate_signal_and_enter`) also runs the following gates that aren't documented:

| Gate | File:line | Documented in CLAUDE.md? |
|---|---|---|
| `stale_feed` (Coinbase/Chainlink/Bybit/Binance staleness) | main.py:592-619 | Yes (in Safety section, not Entry Gates) |
| `opposite_flip_weak_prob` (prob<0.53 on flip side change) | main.py:846-853 | No |
| `flip_insufficient_edge` (flip premium hurdle) | main.py:856-880 | Yes — "(+1.5% per flip)" |
| `layer_disagreement` (momentum_score opposes side) | main.py:912-927 | No |
| `thin_book_depth` (chosen-side depth gate) | main.py:1015-1031 | Partial (depth ≥ $50 in CLAUDE.md is a both-sides gate; this is the per-side variant) |
| `net_edge_after_slippage` (edge − price×slippage) | main.py:1033-1041 | No (the pre-submit re-check at :1135 is what CLAUDE.md mentions; this one runs earlier) |
| `min_size` ($1 CLOB floor) | main.py:1047-1050 | No (mentioned in Live Execution section) |
| `late_window_underdog` (prob < 0.40 in final 2 mins) | main.py:985-994 | No |
| `regime` gate (skip if `regime_state.skip`) | main.py:961-964 | No |

- **Recommended action:** Update CLAUDE.md's Entry Gates list to include the undocumented ones, OR move all "design-detail" gates into a "Code-only safety gates" subsection.
- **Confidence:** high.

### `entry_timing` schedule defaults differ between main.py loops
- **Location:** `polybot/main.py:2205-2206` vs `2576-2577` vs `settings.yaml:137-140`.
- **Evidence:**
  - `trading_loop` (main.py:2205): `(0, 15)` to `(23, 59)`.
  - `scheduler` setup (main.py:2576): `(0, 15)` to `(23, 59)`.
  - settings.yaml: start `(0, 1)`, end `(23, 30)`.
- **Why important:** The yaml drives runtime via `scheduler._trading_start / _trading_end`, but the in-code fallbacks differ from yaml. If yaml goes missing, the bot trades from 12:15 to 23:59 instead of 00:01 to 23:30. Per CLAUDE.md, the bot stops at 23:30 ET — yaml is the source of truth; the fallbacks are dead.
- **Recommended action:** Align fallbacks with yaml or drop the fallback `.get(..., default)` calls in favor of letting config validation fail loudly when yaml is missing.
- **Confidence:** high (drift); low (severity — yaml has never gone missing in practice).

---

## Verified invariants (no action needed — listed for confidence)

- **`lag1_autocorr` single source** — lives only in `polybot/core/returns.py:11`. `weight_optimizer.py:29` has an unrelated `_lag1_autocorr(values: list[float])` operating on **trade-return lists** (Newey-West SE), not candle closes — different domain. No duplicate.
- **`compute_buy_vwap`, `entry_fee_shares`, `slippage_pct`, `DEFAULT_FEE_RATE`** — defined exclusively in `polybot/execution/base.py` (lines 51, 54, 75, 86). Every other reference is an import.
- **`default_for` / `_d`** — defined only in `param_registry.py:117`. All consumers import as `_d`.
- **Atomic position transaction** — `open_position_and_debit_bankroll` / `close_position` in `db/models.py` are the only money-mutating writes to `positions`/`bankroll` outside of test files and the two reconciliation-only paths flagged in Important #4.
- **"What NOT to Change" — all 10 invariants intact:** Student-t CDF, momentum magnitude ≤0.10, no log_return for Sharpe (`pipeline_analytics` uses `gain_pct` only), pricing via `GET /price?side=` not raw CLOB (`market_scanner.fetch_market_price` at line 331), Polymarket binary-payoff fee formula, `DEFAULT_FEE_RATE = 0.018`, resolution via Gamma/Chainlink (no Binance resolution path), no circuit-breaker bypass, regime direction = sign(last_1min_return), all layer adjustments in logit space.
- **No dead top-level classes/functions** across `core/`, `agents/`, `execution/`, `feeds/`, `indicators/`, `db/`, `discord_bot/`, `config/`. All discord handlers decorator-registered. All pipeline stage classes instantiated from `scheduler.py`. All 37 `_section_*` helpers in `claude_client.py` consumed by `_format_strategy_context`.
- **CLI flags** — all four (`--mode`, `--auto-restart`, `--run-pipeline`, `--allow-orphans`) have real consumers.
- **`EXPLORE_STEPS["kelly_fraction"] = 0.02`** in `recommender_base.py:28` is the per-cycle PROBE STEP size, not a default — not a conflict with registry's 0.08.

---

## Trivial findings

### Validator's negative-momentum branch is unreachable post-floor-clamp
- `polybot/agents/claude_client.py:381-385`. After `clamped = max(0.0, min(0.10, raw_value))` on line 376, `clamped` is always ≥0, so `(1.0 if clamped >= 0 else -1.0)` is dead. Delete the `else -1.0` branch.

### `_DEFAULT_CONSENSUS_CONFIG` duplicated in signal_engine + settings.yaml
- `polybot/core/signal_engine.py:49-54` defines `{very_high_pct: 0.80, ...}`. `polybot/config/settings.yaml:107-113` also defines `signal.consensus.*` with identical values.
- Not a bug (yaml overrides), but matching dicts in two places means edits can drift. Recommend documenting yaml as the source and dropping the in-code defaults (or vice versa).

### Magic `0.5` spread cost coefficient in main.py:1448
- `effective_cost = spread_val * 0.5 + DEFAULT_FEE_RATE`. Reasonable (half-spread + fee proxy) but undocumented. Already adequate, just bare.

### `weights` is in `_MANUAL_DEFAULTS` but treated as pipeline-tunable
- `polybot/config/param_registry.py:108` and validator special-case at `polybot/agents/claude_client.py:327-369`.
- CLAUDE.md lists weights in pipeline-tunable. Validator handles via dedicated branch before the manual reroute. Works correctly — just a third "lane" outside the two documented buckets.

### `_OI_DROP_PER_MIN_K = 8.0` undocumented saturation coefficient
- `polybot/core/liquidation.py:13`. CLAUDE.md describes it as "tanh saturation × 8 per minute" — already matches. Could move to a registry ParamSpec if future-tunable.

### Comment typo `ost` in `main.py:98`
- `# Async logging: ost 1-5ms on disk and matter when scalp decisions chain 5-10 log lines.`
- Should be "costs". Cosmetic.

### Schedule alignment: scheduler.py extra stages not in CLAUDE.md
- Per the pipeline-wiring sub-agent: rollups (top of `run_daily_pipeline`), trends/regime snapshot, and crisis check run in `run_daily_pipeline()` but aren't called out in CLAUDE.md's documented sequence. They're context/safety steps, not adoption-changing.
- Recommended action: optionally extend CLAUDE.md's documented sequence to mention them — they exist and matter for understanding ordering (crisis runs BEFORE TAEvolver, which is correct but counter-intuitive).

### `load_legacy_platt_file_falls_back_to_identity` test exercises removed Platt format
- `polybot/tests/test_calibrator.py` — keeps a backwards-compat reader test. Live calibration is isotonic-only per CLAUDE.md. Not strictly dead (it's a regression guard), but the Platt format is gone — if you're sure no calibration files in the wild use it, the test could go.

### Test fixture path `/tmp/fake_biases.json` in `test_counterfactual_tracker.py:150`
- Hardcoded `/tmp/...` on a Windows-primary project. Tests still pass because the path is only used as a constructor arg; the test doesn't write. Cosmetic.

### Two backup snapshots 27 seconds apart
- `backups/pre_5_20_reset_20260526_110126` and `..._110153` likely contain ~identical data. Half the 70MB on disk is redundant.

### Hardcoded ranges `[0.95, 1.05]` and `[0.98, 1.02]` for price_sum sanity gate
- `main.py:1397` uses `[0.98, 1.02]`; `main.py:1777` uses `[0.95, 1.05]`. Both correctly identified in comments as no-arb bounds with different tightness. Documenting in CLAUDE.md would explain why two thresholds exist.

### `compute_signal_consensus` re-defined dict in `_DEFAULT_CONSENSUS_CONFIG` (no functional issue)
- Already mentioned above. Listed for completeness.

---

## Open questions for the user

1. **Are write-only telemetry files (`biases.json`, `isotonic_rejected.json`, `latency_stats.json`, `warmup_stats.json`, `dust_sweep_stats.json`) intentional operator-facing audit?**  
   - If yes, add a one-line note to CLAUDE.md ("Per-pipeline operator telemetry — no code consumer expected") and we don't flag them next audit.  
   - If no, the writers can be deleted.

2. **Is the `biases.json` save call intentionally a "last cycle snapshot for operator inspection"?**  
   Same as #1 but specific — `bias_detector.save()` does write *every* pipeline cycle but no code reader exists. The pipeline already passes biases in-memory to the Claude/Local recommender.

3. **Should the backup snapshots in `backups/` be deleted, archived off-machine, or retained?**  
   Two 35MB DBs + ~240 rollup JSONs. Two snapshots 27 seconds apart — at least one is redundant.

4. **Write-only audit columns in `positions` / `trade_history` — keep or drop?**  
   `signal_strength`, `signal_score` (trade_history), `question` (trade_history), `log_return` (both tables) are inserted but never read by name. CLAUDE.md says "Never log_return for Sharpe" — implies `log_return` is intentionally retired. Drop with an ALTER TABLE migration, or keep for forensic value?

5. **Direct UPDATE in `live_trader.py:1206` (`shares_held`) and 1266 (status-only fallback) — by-design exceptions to the "single close path" invariant?**  
   First is reconciliation-only (chain truth correction); second is exception handler if `close_position` raises. Worth documenting in CLAUDE.md if intentional.

6. **`fok_spread_cross_floor` at main.py:1809 — hardcoded constant or missing yaml entry?**  
   Treated as `config.get(...)` but key doesn't exist anywhere. Either delete the indirection or add to yaml.

7. **`late_window_min_prob` at settings.yaml:55 — should it be a `MANUAL_ONLY_PARAMS` entry in `param_registry`?**  
   Currently a real entry gate (`late_window_underdog`) with a yaml-driven threshold but no registry presence.

8. **`opposite_flip_weak_prob` threshold 0.53 hardcoded — design constant or operator-tunable?**  
   If tunable, add to registry; if design, document.

9. **Validator system prompt at claude_client.py:103 — confirm intent to drop signed-momentum-weight semantics?**  
   CLAUDE.md already says "sign is dead, polarity is regime-conditional", which implies yes. Just want explicit signoff before the system-prompt edit.

10. **Are the registry's `signal.weights` defaults (rsi 0.20, macd 0.25, stochastic 0.20, obv 0.15, vwap 0.20) the intended canonical defaults, or should they sync to yaml (rsi 0.20, macd 0.30, stochastic 0.15, obv 0.15, vwap 0.20)?**  
   Both sum to 1.0. The drift is harmless but the "single source of truth" intent of `param_registry` is violated.

---

## Reconciliation appendices

### A — `param_registry` ↔ `settings.yaml` ↔ code

**Pipeline-tunable (PIPELINE_PARAMS — 25 entries + 8 L6 weights):**

| Param | Registry default | settings.yaml | Code-side fallback in `_build_signal_engine` | Status |
|---|---|---|---|---|
| atr_sigma_ratio | 1.3 | 1.3 | `_d("atr_sigma_ratio")` | ✅ live |
| student_t_df | 5 | 5 | `_d` | ✅ |
| min_atr | 12.0 | 12.0 | `_d` | ✅ |
| logit_scale | 4.0 | 4.0 | `_d` | ✅ |
| regime_weight | 0.03 | 0.03 | `_d` | ✅ |
| flow_weight | 0.04 | 0.04 | `_d` | ✅ |
| spot_flow_weight | 0.10 | 0.10 | `_d` | ✅ |
| liquidation_weight | 0.03 | 0.03 | `_d` | ✅ |
| prev_margin_weight | 0.02 | 0.02 | `_d` | ✅ |
| momentum_weight | 0.04 | 0.04 | `_d` | ⚠ system prompt mismatch — see Critical #2 |
| kelly_fraction | 0.08 | 0.08 (math.) | `_d` (correct in `_build_signal_engine`) | ⚠ wrong path in main.py:2697 — see Critical #1 |
| min_edge | 0.04 | 0.04 | `_d` | ✅ |
| min_kelly | 0.01 | 0.01 | `_d` | ✅ |
| min_model_probability | 0.56 | 0.56 | `_d` | ✅ |
| normal_fraction | 0.60 | 0.60 | `_d` | ✅ |
| late_max_penalty | 0.30 | 0.30 | `_d` | ✅ |
| flip_edge_premium | 0.015 | 0.015 | `_d` | ✅ |
| exit_edge_threshold | -0.07 | -0.07 | `_d` (but main.py:2200 has hardcoded fallback `-0.10`) | ⚠ minor drift, dead fallback |
| regime_momentum_threshold | 0.15 | 0.15 | `_d` | ✅ |
| flow_combined_cap | 0.35 | 0.35 | `_d` | ✅ |
| final_logit_clamp | 4.0 | 4.0 | `_d` | ✅ |
| deep_loss_hold_threshold | -0.10 | -0.10 | `_d` | ✅ |
| l5_regime_damp_cap | 0.7 | 0.7 | `_d` | ✅ |
| atr_regime_shift_threshold | 0.60 | 0.60 | `_d` | ✅ |
| derived_*_weight (8 L6) | 0.0 each | 0.0 each (except prev_margin_sq=0.005) | `_d` | ✅ |

**Manual-only (`MANUAL_ONLY_PARAMS`):**

All listed entries (`loss_cut_fraction`, `loss_cut_time_s`, `adverse_selection_threshold`, `edge_decay_threshold`, `max_edge`, `trading_*`, `max_concurrent_positions`, `max_bankroll_deployed`, `circuit_breaker.*`, `indicators.*`, `sprt.*`) exist in both settings.yaml and the claude_client reroute block. **Validator behavior matches registry behavior.**

**Orphan keys (in yaml, not in registry):**

- `late_window_min_prob` — see Important #1.
- `signal.consensus.*` (dict) — duplicated in signal_engine.
- `signal.consensus_dead_zone` — in `_MANUAL_DEFAULTS` as `consensus_dead_zone` (not dotted). The validator's `_check_range("signal.consensus_dead_zone", 0.0, 0.20)` reads from yaml directly; matches default in code.
- `signal.regime_lookback` — in `_MANUAL_DEFAULTS` as `regime_lookback`. Lives in yaml + registry. ✅

**Orphan keys (in registry/code, not in yaml):**

- `fok_spread_cross_floor` — see Important #2.

### B — `polybot/memory/` writers → readers

See dedicated audit (writers vs readers map). Summary:

- **Live (writers + active readers):** `adverse_state.json`, `calibration/isotonic_params.json`, `counterfactuals/`, `crisis_state.json`, `fill_stats.json`, `gate_stats.json`, `ghost_outcomes/`, `outcomes/`, `pipeline_history.json`, `pipeline_run_log.json`, `prev_resolution_margin.json`.
- **Orphan (writer only):** `biases.json`, `calibration/isotonic_rejected.json`, `dust_sweep_stats.json`, `latency_stats.json`, `orphan_positions.json`, `warmup_stats.json`. All likely intentional operator-only audit logs — see Open Questions #1.

### C — SQLite tables / columns (paper vs live diff)

**Both `polybot_paper.db` and `polybot_live.db` exist. Schemas are byte-identical between them and match `db/models.py`. No drift.**

**Write-only columns** (see Important #9):
- `positions`: `signal_strength`, `log_return`
- `trade_history`: `position_id`, `signal_strength`, `signal_score`, `log_return`, `entry_timestamp`, `market_id`, `question`, `id` (all readable only via `SELECT *` paths without specific field access).

**Indexes (all present in both DBs):** `idx_positions_status`, `idx_positions_market_status`, `idx_trade_history_exit_ts`.

**Migration block (`models.py:74-94`)** adds `fee_rate`, `shares_held` on positions; `pnl`, `fees`, `exit_reason` on trade_history; drops dead `ev_at_entry`, `exit_target`, `stop_loss`, `weight_version`.

### D — CLAUDE.md claims vs code reality (selected)

| CLAUDE.md claim | Code reality | Status |
|---|---|---|
| L1 Student-t CDF, df clamped ≥3, ATR floor `max(min_atr, 0.3 × rolling_20)`, prob clip [1e-6, 1-1e-6] | signal_engine.py:286-307 | ✅ matches |
| L2 single `lag1_autocorr` helper in returns.py | returns.py:11; SignalEngine + RegimeDetector both delegate | ✅ |
| L3 CLOB book imbalance × 0.6 + trade flow × 0.4, top-5 levels, 30s half-life inside 120s window | order_flow.py:10, 11, 55, 90-99 | ✅ |
| L3b CVD + taker (taker requires count ≥5), CVD-accel requires ≥3 trades in 15s | main.py:644-655; binance_trades.py:97-117, 126-151 | ✅ |
| L3e Bybit OI × direction, per-minute normalization, tanh × 8 | liquidation.py:13-39 | ✅ |
| L4 polarity-split, regime-conditional sign, tanh-smoothed, magnitude clamp ±0.10 | signal_engine.py:378-410, 222-229 | ✅ |
| L5 tanh(prev_margin/atr) × prev_margin_weight × logit_scale × (1−min(cap, |regime|)) | signal_engine.py:341-346 | ✅ |
| L6 closed library, default 0.0, combined ±0.25 cap | derived_features.py + signal_engine.py:260-263 | ✅ |
| L6 validator rejects sum(|w|)·logit_scale > 0.25 | claude_client.py:415-445 | ✅ |
| Calibration: 7d, ≥125 pool, ≥75 train, 300 bootstrap, lower 80% CI | calibrator.py:21-22, 149; scheduler.py:1843, 1852, 1870 | ✅ |
| Adoption: candidate_sharpe > 0, n ≥ 100, z ≥ 0.3 (Newey-West) | weight_optimizer.py:14-15, 44, 87-103 | ✅ |
| Worst-fold floor ≥ −0.10 | scheduler.py:1205-1212 | ✅ |
| Regime stratification (2-of-3 OR dominant-regime, ≥20 trades per bucket) | scheduler.py:1037-1071 | ✅ |
| Holdout 7d, ≥30 trades, margin 0.02 | scheduler.py:186-187, 1245-1261 | ✅ |
| Interaction back-out 0.7→0.9 + 0.05/step | scheduler.py:1382 | ✅ |
| Crisis: Sharpe<0.10 AND (WR<48% OR loss/win>2.0); ≥3 cycles halve kelly (floor 0.04) | scheduler.py:2174, 2198-2211 | ✅ |
| Atomic commit: calibrator save deferred to after weight_optimizer | scheduler.py:1842, 2006/2013/2022, 2258-2262 | ✅ |
| DEFAULT_FEE_RATE = 0.018 in base.py | base.py:51 | ✅ |
| Single close path through `close_position` / `_close_position_and_history` | models.py:114-234 | ⚠ two side-paths in live_trader.py see Important #4 |
| FOK-only via py-clob-client-v2 | live_trader.py (FOK paths) | ✅ (not separately audited here) |
| Entry Gates list | main.py:_evaluate_signal_and_enter | ⚠ several real gates missing from CLAUDE.md — see Important #11 |
| Pipeline stage order: tracker → bias → calibrator → KS → SPRT → TAEvolver → optimizer → deferred cal save | scheduler.py:1740-2262 | ✅ (with 3 undocumented but safe extras) |

### E — Pipeline stages: documented order vs scheduler wiring

| # | Documented | Code location | Status |
|---|---|---|---|
| 0 | (not documented) Rollups | scheduler.py:1695-1697 | extra (safe) |
| 1 | PipelineTracker review + auto-revert | scheduler.py:1740-1742 | ✅ |
| 2 | BiasDetector | scheduler.py:1760 | ✅ |
| 3 | Calibrator | scheduler.py:1839-2035 | ✅ |
| 4 | KS shift | scheduler.py:2060-2073 | ✅ |
| 5 | SPRT aggregate | scheduler.py:2076-2077 | ✅ |
| 5b | (not documented) Trends + regime snapshot | scheduler.py:2081-2098 | extra (safe) |
| 5c | (not documented) Crisis check | scheduler.py:2161-2250 | extra (runs BEFORE TAEvolver) |
| 6 | TAEvolver | scheduler.py:2153 | ✅ |
| 7 | WeightOptimizer | scheduler.py:2252 | ✅ |
| 8 | Deferred calibrator save | scheduler.py:2258-2262 | ✅ |

---

*Audit performed read-only on 2026-05-27. No files modified. `pytest --collect-only` ran cleanly (478 tests collected, 0 errors). `sqlite3` introspection was read-only via `?mode=ro` URI.*
