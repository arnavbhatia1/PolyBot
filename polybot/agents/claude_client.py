"""ClaudeClient: nightly strategy analysis. Builds prompt, calls Claude, validates/clamps response."""
from __future__ import annotations
from typing import Any
from polybot.config.param_registry import (
    CLAMP_RANGES as _CLAMP_RANGES,
    MANUAL_ONLY_PARAMS as _MANUAL_ONLY_PARAMS,
    default_for as _d,
)
import asyncio
import json
import logging
import math
import re
import anthropic

logger = logging.getLogger(__name__)

def _cfg_get(cfg: dict[str, Any], dotted: str) -> Any:
    """Look up a config value by `section.subsection.key`. Returns None if any
    segment is missing. Used by the manual-only rerouting path so dotted keys
    like `indicators.rsi.period` resolve to their current value.
    """
    cur: Any = cfg
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur

def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from Claude's response, handling fences, prose, and partial output."""
    # Strip markdown fences (```json ... ``` or ``` ... ```)
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the outermost { ... } in the text
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found in response", text, 0)

    # Walk forward tracking brace depth to find the matching close
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])

    # Braces didn't balance — try parsing from start anyway (truncated response)
    raise json.JSONDecodeError("Unbalanced braces in JSON response", text, start)

STRATEGY_SYSTEM_PROMPT = """\
You are the strategist for PolyBot, an automated trader for 5-min BTC Up/Down
binary contracts on Polymarket. Contracts resolve to $1 / $0 based on Chainlink BTC.

## Probability Model
  L1 — Student-t CDF (df=student_t_df, fat tails):
    z = (BTC - strike) / ((ATR / atr_sigma_ratio) * sqrt(minutes)) * sqrt(df/(df-2))
    P(Up) = t.cdf(z, df)
  L2-L5 are additive in log-odds, scaled by `logit_scale`:
    L2 regime — 1-lag autocorr × sign(last_return)
    L3  CLOB flow (book imbalance + trade flow)
    L3b spot flow (Binance CVD + taker ratio)
    L3e liquidation pressure (Bybit OI drop × direction)
    L5  prev-window margin (tanh-normalized by ATR)
    L4  indicator momentum (RSI/MACD/Stoch/OBV/VWAP — weakest signal)
  Then Platt calibration (re-fit each cycle on the train split) is the sole
  overconfidence correction.

  Edge = model_prob - market_price. Entry needs edge >= min_edge AND Kelly >= min_kelly.
  Kelly: f* = (p*b - q)/b * kelly_fraction, b = (1-price)/price.

## Backtestable Params (you can propose these in `changes`)
- atr_sigma_ratio (1.2-2.5, HIGHEST leverage; lower = sharper probs)
- logit_scale (2.0-5.0, master amplifier on L2-L5)
- student_t_df (3-8, lower = fatter tails)
- min_atr (8.0-25.0, ATR floor)
- flow_weight (0.02-0.12), spot_flow_weight (0.01-0.15), liquidation_weight (0.01-0.10)
- regime_weight (0.02-0.10), prev_margin_weight (0.01-0.05)
- momentum_weight (-0.10 to +0.10; NEGATIVE = fade indicators)
- kelly_fraction (0.05-0.18; leave unchanged unless strong risk evidence)
- min_edge (0.02-0.10), min_kelly (0.005-0.04), min_model_probability (0.52-0.70)
- weights (RSI/MACD/Stoch/OBV/VWAP dict, sum=1.0, each ≥0.05)
- exit_edge_threshold (-0.10 to -0.03; TIGHT range — directly changes realized P&L,
  more negative = hold longer, less negative = exit faster)
- normal_fraction (0.40-0.80, fraction of window with full Kelly)
- late_max_penalty (0.10-0.60, max Kelly cut at end of window)
- flip_edge_premium (0.005-0.05, extra edge required to re-enter after a scalp)

## Manual-Only Params (route to `manual_observations`, never `changes`)
- max_edge, adverse_selection_threshold
- loss_cut_fraction, loss_cut_time_s (stop-loss level and time gate — risk policy)
- trading_start/end_hour_et/minute, flip_enabled
- max_concurrent_positions, max_bankroll_deployed
- circuit_breaker.floor_pct, circuit_breaker.min_multiplier
- Indicator periods: indicators.{rsi,macd,stochastic,ema,obv,atr}.{period,...}
  Propose only when per_indicator accuracy is consistently poor at N≥50.
- SPRT: sprt.{alpha,beta,observation_interval_s,min_confidence}
  Propose only when execution-quality evidence (n≥50) suggests SPRT gates too eagerly or too loosely.

## Behavioral Rules
1. SPRT now gates entries (SKIP blocks; low confidence blocks after 2+ obs; favored-side mismatch blocks).
   SPRT state reflects how often the gate allowed trades, not win rate. Do not treat low ENTER fraction
   as a sign of model failure — it means SPRT filtered weak-directional setups.
2. "Trending regime WR low" is not a fix target — runtime already flips and amplifies
   momentum_weight in trending regimes. Don't move regime_weight on that alone.
3. Check the Recent Trends section. Metrics labeled IMPROVING are self-resolving — do
   not propose changes that target them. Only propose for STABLE-but-bad or DEGRADING.
4. Don't shuffle indicator weights unless an indicator shows >65% accuracy at N≥30.
   L4 is mostly disabled (momentum_weight ≈ -0.02). Focus on L1 + flow params.
5. Cover ≥3 parameter families per cycle. If last cycle a param showed negative delta
   in a direction, don't repeat it — try the opposite or a different param.
6. Hit the adoption_dynamic_floor shown in your context (it scales with backtest noise).
   A 0.92→0.90 tweak on compression dies to noise; try 0.92→0.85 or combine 2 params.
7. Direction: read exclusively from the Empirical Parameter Direction Table. If a row
   shows DECAYS or consistently negative BT delta over ≥3 tests, do not test it.
   Empty rows = no evidence; explore at low confidence.
8. Known interactions (combined backtest will back out the weaker change if combined
   delta < 0.7 × sum of individual deltas):
   - momentum_weight + regime_weight (runtime amplifier compounds them)
   - flow_weight + spot_flow_weight (shared CVD signal)
   - logit_scale + atr_sigma_ratio (both sharpen L1)

## Manual Observations
For exit/entry-timing/risk/schedule findings the data warrants but the pipeline can't
backtest. Each observation must cite: `evidence.n` ≥ 50, a specific `evidence.source`,
and direction must be unambiguous. Emit ZERO observations if the bar isn't met.
Max 3 per cycle (deduped by param).

Trigger mappings (manual-only — route to `manual_observations`):
- edge_calibration: high-edge bucket WR < low-edge bucket WR by 2× noise → max_edge LOWER.
- ghost_analysis.adverse_rate_30s with high pct_profitable + positive sim_pnl →
  adverse_selection_threshold HIGHER (gate over-filters). Negative sim_pnl → keep tight.

Note: exit_edge_threshold, normal_fraction, late_max_penalty, flip_edge_premium are now
pipeline-tunable — propose them in `changes` with predicted_delta_sharpe_7d, backed by
counterfactual_analysis / time_patterns / flip outcomes as you would any other tunable.

## Newly pipeline-tunable structural constants
regime_momentum_threshold, flow_combined_cap, final_logit_clamp, deep_loss_hold_threshold,
l5_regime_damp_cap, atr_regime_shift_threshold. These shape the signal stack; small
steps only. Each has a tight range — see CLAMP_RANGES in context.

## Derived feature weights (L6)
Eight `derived_<name>_weight` parameters (all default 0.0). Each turns on a feature
listed in polybot/core/derived_features.py. Raise off zero ONLY with bias-bucket
evidence the corresponding feature would have helped — never random walk. Hard
contribution cap of ±0.25 logits across the combined L6 layer means more than two
features active at >0.02 will saturate.

## Response (return ONLY valid JSON, no fences):
{
  "changes": [
    {"param": "atr_sigma_ratio", "value": 1.5, "reason": "one sentence",
     "predicted_delta_sharpe_7d": 0.025, "confidence_interval": [-0.005, 0.045]}
  ],
  "manual_observations": [
    {"param": "adverse_selection_threshold", "current": 0.65, "suggested": 0.72,
     "evidence": {"metric": "adverse_rate_30s", "value": 0.58, "n": 900, "source": "ghost_analysis"},
     "reason": "one sentence", "confidence": "high"}
  ],
  "key_findings": ["finding 1", "finding 2"],
  "risk_warnings": ["warning 1"],
  "reasoning": "2-3 sentence summary",
  "confidence": "high|medium|low"
}

- 0-5 changes (empty is valid, and correct when no finding exceeds 2× noise).
- N < 50 trades → return empty changes (variance is noise at that sample size).
- key_findings: max 5, each one short sentence (<100 chars), plain trader language.
- risk_warnings: max 3.
- reasoning: 2-3 sentences."""


class ClaudeClient:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        self.client: anthropic.AsyncAnthropic = anthropic.AsyncAnthropic(api_key=api_key)
        self.model: str = model

    async def analyze_strategy(self, context: dict[str, Any]) -> dict[str, Any]:
        """Send performance data to Claude for quant strategy analysis.

        Retries on transient server errors (529 Overloaded, 503 Service Unavailable,
        502 Bad Gateway) with exponential backoff: 30s → 60s → 120s. Non-transient
        errors (4xx auth/request) raise immediately.
        """
        user_message = _format_strategy_context(context)

        # Transient-error retry: one attempt after 30s for 529/503/502. Beyond that we
        # fall back to the local rule-based evolver rather than block the pipeline.
        RETRY_DELAYS_S = [30]
        TRANSIENT_STATUSES = {529, 503, 502}
        response = None
        last_err: Exception | None = None
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=8192,
                    system=STRATEGY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                    timeout=180.0,
                )
                break
            except anthropic.APIStatusError as e:
                status = getattr(e, "status_code", None)
                if status not in TRANSIENT_STATUSES or attempt == len(RETRY_DELAYS_S):
                    raise
                delay = RETRY_DELAYS_S[attempt]
                last_err = e
                logger.warning(
                    f"Claude API {status} (transient) on attempt {attempt + 1}; "
                    f"retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
        if response is None:
            # All retries exhausted — re-raise the last transient error so caller falls back.
            raise last_err if last_err else RuntimeError("Claude API: no response and no error captured")

        text = response.content[0].text.strip()
        logger.debug(f"Claude raw response (first 500 chars): {text[:500]}")

        try:
            data = _extract_json(text)
        except Exception as e:
            logger.error(f"Claude JSON parse failed: {e}\nRaw response: {text[:1000]}")
            raise
        current_config = context.get("current_config", {})
        current_weights = current_config.get("weights", {})
        total_trades = context.get("analysis", {}).get("overall", {}).get("total_trades", 0)
        return _validate_strategy_response(data, current_weights, total_trades, current_config)


def _validate_strategy_response(data: dict[str, Any], current_weights: dict[str, float] | None = None,
                                total_trades: int = 0,
                                current_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and clamp Claude's `changes` list recommendations.

    Returns a dict with:
      - `changes`: list of validated change dicts [{param, value, reason}, ...]
      - metadata: key_findings, risk_warnings, reasoning, confidence
    """
    indicators = ["rsi", "macd", "stochastic", "obv", "vwap"]
    cfg = current_config or {}

    # Normalize: ensure changes is a list
    if not isinstance(data.get("changes"), list):
        data["changes"] = []

    # Insufficient data guard — drop all changes
    if total_trades < 50:
        data["changes"] = []
        data.setdefault("risk_warnings", []).append(f"Only {total_trades} trades — insufficient data, no changes applied")

    # Entry gates are now pipeline-tunable: the backtest sample includes resolved
    # ghosts, so raising or lowering a gate filters baseline and candidate identically.
    READ_ONLY_PARAMS: set[str] = set()

    # All operator-owned (manual-only) params — Claude cannot adopt these via
    # `changes`; the rerouting block below moves them to `manual_observations`
    # for operator review. Sourced from param_registry so adding a manual param
    # touches one file (the registry), not two.
    MANUAL_ONLY_PARAMS = _MANUAL_ONLY_PARAMS

    # Per-param clamp ranges — imported from param_registry (single source of truth).
    CLAMP_RANGES = _CLAMP_RANGES

    validated_changes: list[dict[str, Any]] = []

    for change in data["changes"][:5]:  # enforce max 5
        if not isinstance(change, dict):
            continue
        param = change.get("param", "")
        value = change.get("value")
        reason = change.get("reason", "")

        if not param or value is None:
            continue

        # Drop read-only params silently (entry gates that corrupt the backtest)
        if param in READ_ONLY_PARAMS:
            logger.debug(f"Dropping read-only param change: {param}")
            continue

        # Drop manual-only params from changes — they aren't backtestable. But if Claude
        # provided enough metadata, reroute into manual_observations so the operator sees
        # the suggestion rather than silently losing it.
        if param in MANUAL_ONLY_PARAMS:
            logger.info(f"Rerouting manual-only param from changes -> manual_observations: {param}={value}")
            cur_val = _cfg_get(cfg, param)
            rerouted = {
                "param": param,
                "current": cur_val,
                "suggested": value,
                "reason": reason or "Claude proposed in `changes` (rerouted — manual-only param)",
                "confidence": "low",  # rerouted suggestions default to low — they bypassed the evidence schema
                "evidence": change.get("evidence") or {"source": "rerouted_from_changes", "note": "no explicit evidence block provided"},
                "source_channel": "rerouted",
            }
            data.setdefault("manual_observations", []).append(rerouted)
            continue

        # Handle indicator weights dict
        if param == "weights":
            if not isinstance(value, dict):
                continue
            w = dict(value)
            # Floor and renormalize
            for k in indicators:
                if w.get(k, 0.05) < 0.05:
                    w[k] = 0.05
            tot = sum(w.get(k, 0.20) for k in indicators)
            if tot > 0:
                w = {k: w.get(k, 0.20) / tot for k in indicators}
            for k in indicators:
                if w[k] < 0.05:
                    w[k] = 0.05
            largest = max(w, key=w.get)
            for k in indicators:
                if k != largest:
                    w[k] = round(w[k], 4)
            w[largest] = round(1.0 - sum(v for kk, v in w.items() if kk != largest), 4)
            # Enforce max 0.05 change per cycle
            if current_weights:
                changed = False
                for k in indicators:
                    old = current_weights.get(k, 0.20)
                    nw = w.get(k, old)
                    if abs(nw - old) > 0.05:
                        w[k] = old + 0.05 * (1 if nw > old else -1)
                        changed = True
                if changed:
                    tot = sum(w[k] for k in indicators)
                    if tot > 0:
                        w = {k: w[k] / tot for k in indicators}
                    largest = max(w, key=w.get)
                    for k in indicators:
                        if k != largest:
                            w[k] = round(w[k], 4)
                    w[largest] = round(1.0 - sum(v for kk, v in w.items() if kk != largest), 4)
            weight_entry: dict[str, Any] = {"param": "weights", "value": w, "reason": reason}
            for pred_key in ("predicted_delta_sharpe_7d", "confidence_interval"):
                if pred_key in change:
                    weight_entry[pred_key] = change[pred_key]
            validated_changes.append(weight_entry)
            continue

        # Scalar param: clamp to range
        if param in CLAMP_RANGES:
            lo, hi, cast = CLAMP_RANGES[param]
            try:
                raw_value = cast(value)
                clamped = cast(max(lo, min(hi, raw_value)))
            except (TypeError, ValueError):
                continue
            # Extra: momentum magnitude must stay below min_edge
            momentum_floor_applied = False
            if param == "momentum_weight":
                min_edge_live = cfg.get("min_edge", _d("min_edge"))
                if abs(clamped) >= min_edge_live:
                    clamped = float((min_edge_live - 0.001) * (1.0 if clamped >= 0 else -1.0))
                    momentum_floor_applied = True
            entry: dict[str, Any] = {"param": param, "value": clamped, "reason": reason}
            # Surface clamps so the directional table attributes results to the actual tested value.
            was_clamped = (raw_value != clamped)
            if was_clamped:
                entry["clamped"] = True
                entry["proposed_value"] = raw_value
                entry["clamp_range"] = [lo, hi]
                logger.warning(
                    "Claude proposed %s=%s outside [%s, %s] — clamped to %s "
                    "(momentum_floor=%s). Directional table will be attributed "
                    "to the clamped value.",
                    param, raw_value, lo, hi, clamped, momentum_floor_applied,
                )
            for pred_key in ("predicted_delta_sharpe_7d", "confidence_interval"):
                if pred_key in change:
                    entry[pred_key] = change[pred_key]
            validated_changes.append(entry)
        else:
            # Unknown param — skip
            logger.warning(f"Unknown param in changes list: {param!r}")

    data["changes"] = validated_changes

    # Validate manual_observations: strict evidence bar. Silently drop any that fail.
    # Each must have a manual-only param, an evidence dict with n >= 50, and a direction.
    # Rerouted entries (flagged above) are allowed through with confidence="low" so the
    # operator sees them, but they skip the n>=50 bar since they came without evidence.
    raw_obs = data.get("manual_observations")
    validated_obs: list[dict[str, Any]] = []
    if isinstance(raw_obs, list):
        # Validate ALL observations first, then dedupe by param, then truncate.
        # Truncating before validation would drop legitimate suggestions for the
        # less-frequently-observed manual levers (risk caps, circuit breaker, etc.).
        for obs in raw_obs:
            if not isinstance(obs, dict):
                continue
            p = obs.get("param", "")
            if p not in MANUAL_ONLY_PARAMS:
                logger.debug(f"Dropping manual_observation for non-manual param: {p}")
                continue
            reason = obs.get("reason", "")
            suggested = obs.get("suggested")
            if not reason or suggested is None:
                logger.debug(f"Dropping manual_observation missing reason/suggested: {p}")
                continue
            is_rerouted = obs.get("source_channel") == "rerouted"
            ev = obs.get("evidence") or {}
            if not is_rerouted:
                # Require a grounded evidence block: numeric n >= 50.
                n_ev = ev.get("n") if isinstance(ev, dict) else None
                try:
                    n_int = int(n_ev) if n_ev is not None else 0
                except (TypeError, ValueError):
                    n_int = 0
                if n_int < 50:
                    logger.info(f"Dropping under-evidenced manual_observation: {p} (n={n_int}, need >=50)")
                    continue
                if not ev.get("source"):
                    logger.info(f"Dropping manual_observation without evidence.source: {p}")
                    continue
            conf = obs.get("confidence", "low")
            if conf not in ("high", "medium", "low"):
                conf = "low"
            cur = obs.get("current", _cfg_get(cfg, p))
            validated_obs.append({
                "param": p,
                "current": cur,
                "suggested": suggested,
                "reason": reason,
                "evidence": ev,
                "confidence": conf,
                "source_channel": obs.get("source_channel", "direct"),
            })
    # Dedupe by param — keep the highest-confidence observation per param.
    # Then truncate to top 3 (by confidence) so the operator isn't flooded.
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    by_param: dict[str, dict[str, Any]] = {}
    for obs in validated_obs:
        p = obs["param"]
        prev = by_param.get(p)
        if prev is None or conf_rank.get(obs["confidence"], 0) > conf_rank.get(prev["confidence"], 0):
            by_param[p] = obs
    # Sort by confidence descending, take top 3
    sorted_obs = sorted(by_param.values(), key=lambda o: -conf_rank.get(o["confidence"], 0))
    data["manual_observations"] = sorted_obs[:3]
    return data


# ── Section helpers ──────────────────────────────────────────────────────────

def _section_config(cfg: dict[str, Any]) -> str:
    # Current config — organized by whether Claude can change each param.
    return (
        "## Current Configuration\n"
        "### YOU CAN CHANGE THESE (backtestable):\n"
        f"Indicator weights: {json.dumps(cfg.get('weights', _d('weights')))}\n"
        f"momentum_weight (Layer 4): {cfg.get('momentum_weight', _d('momentum_weight'))}\n"
        f"regime_weight (Layer 2): {cfg.get('regime_weight', _d('regime_weight'))}\n"
        f"flow_weight (Layer 3): {cfg.get('flow_weight', _d('flow_weight'))}\n"
        f"spot_flow_weight (L3b): {cfg.get('spot_flow_weight', _d('spot_flow_weight'))}\n"
        f"liquidation_weight (L3e): {cfg.get('liquidation_weight', _d('liquidation_weight'))}\n"
        f"prev_margin_weight (L5): {cfg.get('prev_margin_weight', _d('prev_margin_weight'))}\n"
        f"atr_sigma_ratio: {cfg.get('atr_sigma_ratio', _d('atr_sigma_ratio'))}\n"
        f"student_t_df (Layer 1): {cfg.get('student_t_df', _d('student_t_df'))}\n"
        f"logit_scale: {cfg.get('logit_scale', _d('logit_scale'))}\n"
        f"kelly_fraction: {cfg.get('kelly_fraction', _d('kelly_fraction'))}\n"
        f"min_atr: {cfg.get('min_atr', _d('min_atr'))}\n"
        f"min_model_probability: {cfg.get('min_model_probability', _d('min_model_probability'))}  (pipeline-tunable since ghosts joined backtest)\n"
        f"min_edge (entry_threshold): {cfg.get('min_edge', _d('min_edge'))}  (pipeline-tunable since ghosts joined backtest)\n"
        f"min_kelly (entry gate): {cfg.get('min_kelly', _d('min_kelly'))}  (pipeline-tunable since ghosts joined backtest)\n"
        "\n### MANUAL-ONLY (not in `changes` — propose via `manual_observations` if data warrants):\n"
        f"# Exit / scalp / loss-cut\n"
        f"exit_edge_threshold: {cfg.get('exit_edge_threshold', _d('exit_edge_threshold'))}\n"
        f"loss_cut_fraction: {cfg.get('loss_cut_fraction', _d('loss_cut_fraction'))}  "
        f"loss_cut_time_s: {cfg.get('loss_cut_time_s', _d('loss_cut_time_s'))}\n"
        f"# Entry filters (informed flow / stale price)\n"
        f"adverse_selection_threshold: {cfg.get('adverse_selection_threshold', _d('adverse_selection_threshold'))}\n"
        f"max_edge: {cfg.get('max_edge', _d('max_edge'))}\n"
        f"# Entry-timing Kelly envelope\n"
        f"normal_fraction: {cfg.get('normal_fraction', _d('normal_fraction'))}\n"
        f"late_max_penalty: {cfg.get('late_max_penalty', _d('late_max_penalty'))}\n"
        f"# Schedule\n"
        f"trading_start_hour_et: {cfg.get('trading_start_hour_et', 0)}, trading_start_minute: {cfg.get('trading_start_minute', 1)}\n"
        f"trading_end_hour_et: {cfg.get('trading_end_hour_et', 22)}, trading_end_minute: {cfg.get('trading_end_minute', 30)}\n"
        f"# Flip behavior\n"
        f"flip_enabled: {cfg.get('flip_enabled', _d('flip_enabled'))}, flip_edge_premium: {cfg.get('flip_edge_premium', _d('flip_edge_premium'))}\n"
        f"# Risk caps (operator-owned policy)\n"
        f"max_concurrent_positions: {cfg.get('max_concurrent_positions', _d('max_concurrent_positions'))}, max_bankroll_deployed: {cfg.get('max_bankroll_deployed', _d('max_bankroll_deployed'))}\n"
        f"# Circuit breaker\n"
        f"circuit_breaker.floor_pct: {cfg.get('circuit_breaker', {}).get('floor_pct', _d('circuit_breaker.floor_pct'))}, "
        f"circuit_breaker.min_multiplier: {cfg.get('circuit_breaker', {}).get('min_multiplier', _d('circuit_breaker.min_multiplier'))}\n"
        f"# Indicator periods (manual-only — shown in /config, not relevant to `changes`)\n"
        f"# SPRT (manual-only): alpha={_cfg_get(cfg, 'sprt.alpha')} "
        f"beta={_cfg_get(cfg, 'sprt.beta')} "
        f"interval={_cfg_get(cfg, 'sprt.observation_interval_s')}s "
        f"min_confidence={_cfg_get(cfg, 'sprt.min_confidence')}"
    )


def _section_overall(ana: dict[str, Any]) -> str:
    overall = ana.get("overall", {})
    if not overall:
        return ""
    return (
        "## Overall Performance\n"
        f"Total trades: {overall.get('total_trades', 0)}\n"
        f"Win rate: {overall.get('win_rate', 0):.1%}\n"
        f"Average edge at entry: {overall.get('avg_edge', 0):.1%}\n"
        f"Average gain pct: {overall.get('avg_gain_pct', 0):.4f}\n"
        f"Sharpe ratio: {overall.get('sharpe', 0):.3f}"
    )


def _section_noise(context: dict[str, Any]) -> str:
    # Statistical noise floors — findings must exceed 2× noise to be actionable.
    # Sharpe noise uses the ACTUAL baseline Sharpe (when available) rather than
    # an S=0.5 placeholder, so the figure shown to Claude matches the JK_SE the
    # adoption gate actually computes.
    ana = context.get("analysis", {})
    overall = ana.get("overall", {})
    n = overall.get("total_trades", 0)
    if n < 10:
        return ""
    actual_baseline = ana.get("baseline_kelly_sharpe")
    n_for_sharpe = ana.get("baseline_n_trades") or n
    wr_noise = round(math.sqrt(0.25 / max(n, 1)), 3)   # ±1σ at 50% WR
    if actual_baseline is not None and n_for_sharpe:
        # Same JK SE formula the gate uses (autocorr inflation is added at runtime)
        sharpe_noise = round(
            math.sqrt((1.0 + 0.5 * float(actual_baseline) ** 2) / max(int(n_for_sharpe), 1)),
            3,
        )
        sharpe_basis = f"JK SE at baseline Sharpe={actual_baseline:.3f}, N={n_for_sharpe}"
    else:
        sharpe_noise = round(math.sqrt((1.0 + 0.5 * 0.25) / max(n, 1)), 3)
        sharpe_basis = "JK SE at placeholder S=0.5 (baseline not yet computed)"
    per_ind_n = max(n // 5, 1)  # ~N/5 per indicator on average
    sig_noise = round(math.sqrt(0.25 / per_ind_n), 3)
    q_noise = round(math.sqrt(0.25 / max(n // 4, 1)), 3)
    return (
        f"## Statistical Noise Reference (at N={n} trades)\n"
        f"A finding must exceed 2× noise to be actionable — below that it's sampling variation.\n"
        f"- Win rate noise: ±{wr_noise:.1%} (1σ) → actionable only if difference > ±{2*wr_noise:.1%}\n"
        f"- Sharpe noise: ±{sharpe_noise:.3f} ({sharpe_basis}) → actionable only if Sharpe delta > {2*sharpe_noise:.3f}\n"
        f"- Per-signal accuracy noise (~{per_ind_n} samples/indicator): ±{sig_noise:.1%} → actionable if accuracy > {0.50 + 2*sig_noise:.1%}\n"
        f"- Edge realization quartile noise (~{n//4} samples/quartile): ±{q_noise:.1%}\n"
        f"Example: 'flow_weight accuracy 68% at N={per_ind_n}' = "
        f"{(0.68 - 0.50) / sig_noise:.1f}× noise — {'ACTIONABLE' if (0.68 - 0.50) / sig_noise > 2 else 'marginal'}"
    )


def _section_per_indicator(ana: dict[str, Any]) -> str:
    per_ind = ana.get("per_indicator", {})
    if not per_ind:
        return ""
    lines = ["## Per-Indicator Analysis"]
    for ind, stats in per_ind.items():
        lines.append(
            f"- **{ind}**: accuracy={stats.get('accuracy', 0):.1%} "
            f"(bullish={stats.get('bullish_accuracy', 0):.1%}, "
            f"bearish={stats.get('bearish_accuracy', 0):.1%}) "
            f"n={stats.get('sample_size', 0)}"
        )
    return "\n".join(lines)


def _section_side(ana: dict[str, Any]) -> str:
    side = ana.get("side_analysis", {})
    if not side:
        return ""
    lines = ["## Side Analysis (Up vs Down)"]
    for s, stats in side.items():
        lines.append(
            f"- **{s}**: win_rate={stats.get('win_rate', 0):.1%} "
            f"avg_ret={stats.get('avg_gain_pct', 0):.4f} n={stats.get('count', 0)}"
        )
    return "\n".join(lines)


def _section_edge_calibration(ana: dict[str, Any]) -> str:
    edge_cal = ana.get("edge_calibration", {})
    if not edge_cal:
        return ""
    lines = ["## Edge Calibration (does larger edge = more wins?)"]
    for bucket, stats in edge_cal.items():
        lines.append(f"- **{bucket}**: win_rate={stats.get('win_rate', 0):.1%} n={stats.get('count', 0)}")
    return "\n".join(lines)


def _section_time_patterns(ana: dict[str, Any]) -> str:
    time_p = ana.get("time_patterns", {})
    if not time_p:
        return ""
    lines = ["## Time Patterns (seconds remaining at entry)"]
    for bucket, stats in time_p.items():
        lines.append(f"- **{bucket}**: win_rate={stats.get('win_rate', 0):.1%} n={stats.get('count', 0)}")
    return "\n".join(lines)


def _section_volatility(ana: dict[str, Any]) -> str:
    vol_p = ana.get("volatility_patterns", {})
    if not vol_p:
        return ""
    lines = ["## Volatility Patterns (ATR regime)"]
    for bucket, stats in vol_p.items():
        lines.append(f"- **{bucket}**: win_rate={stats.get('win_rate', 0):.1%} n={stats.get('count', 0)}")
    return "\n".join(lines)


def _section_counterfactual(ana: dict[str, Any]) -> str:
    cf = ana.get("counterfactual_analysis", {})
    if not cf or cf.get("total_scalps_tracked", 0) == 0:
        return ""
    s_total = cf.get("total_scalps_tracked", 0)
    s_acc = cf.get("scalp_accuracy", 0)
    actual_pnl = cf.get("total_actual_scalp_pnl", 0)
    cf_pnl = cf.get("total_counterfactual_hold_pnl", 0)
    pnl_gap = cf.get("pnl_gap_from_early_scalps", 0)
    net_dir = cf.get("net_exit_direction", "calibrated")

    lines = [f"## Counterfactual Exit Analysis (scalps N={s_total})"]
    lines.append(
        f"Scalp accuracy: {s_acc:.1%} ({cf.get('optimal_scalps', 0)} correct, "
        f"{cf.get('suboptimal_scalps', 0)} suboptimal)"
    )
    lines.append(
        f"Scalp P&L: actual ${actual_pnl:+.2f} | if held to resolution ${cf_pnl:+.2f} "
        f"| gap ${pnl_gap:+.2f}"
    )
    if net_dir == "scalp_early":
        lines.append(
            f"→ SCALPING TOO EARLY (${pnl_gap:+.2f} left on table). "
            f"exit_edge_threshold is manual-only — the signal you CAN act on: "
            f"if the model is holding good positions that get scalped, it means "
            f"model probability is DROPPING during the window. Raise logit_scale "
            f"so the initial signal is stronger, or lower atr_sigma_ratio so "
            f"high-confidence entries are more confident."
        )
    elif net_dir == "hold_long":
        lines.append(
            f"→ HOLDING TOO LONG: scalp would have added value. "
            f"exit_edge_threshold is manual-only — the actionable finding is that "
            f"the entry-side model is OVERCONFIDENT (positions look good at entry "
            f"but decay). Raise atr_sigma_ratio (wider L1 sigma) — Platt will then "
            f"recalibrate next cycle."
        )
    else:
        lines.append("→ Exit threshold appears well-calibrated (informational only — manual param).")

    # Holding-edge accuracy buckets — DIAGNOSTIC ONLY (exit_edge_threshold is manual).
    hedge_acc = cf.get("holding_edge_accuracy", {})
    if hedge_acc:
        lines.append("\nScalp accuracy by holding_edge at exit (diagnostic — exit_edge_threshold is MANUAL-ONLY):")
        lines.append("  If accuracy <50% across buckets, the entry model is overconfident — Platt re-fit will compensate next cycle.")
        for bucket, stats in hedge_acc.items():
            lines.append(
                f"  {bucket:>16}: {stats.get('scalp_accuracy', 0):.0%} accuracy "
                f"n={stats.get('count', 0)} — {stats.get('signal', '')}"
            )

    time_acc = cf.get("time_accuracy", {})
    if time_acc:
        lines.append("Scalp accuracy by time remaining at exit:")
        for bucket, stats in time_acc.items():
            lines.append(f"  {bucket}: accuracy={stats.get('scalp_accuracy', 0):.1%} n={stats.get('count', 0)}")

    # Segment table: actionable exit patterns by (time × edge × regime)
    segments = cf.get("segments", [])
    actionable = [s for s in segments if s.get("signal") != "neutral"]
    if actionable:
        lines.append("\nExit pattern segments (N≥5, non-neutral only — maps to exit_edge_threshold / loss_cut params):")
        lines.append(f"  {'Time':<10} {'Edge':<18} {'Regime':<12} {'N':>4} {'Acc':>6} {'AvgΔ':>7}  Signal")
        for s in sorted(actionable, key=lambda x: x.get("n", 0), reverse=True)[:12]:
            lines.append(
                f"  {s['time']:<10} {s['edge']:<18} {s['regime']:<12} {s['n']:>4} "
                f"{s['scalp_accuracy']:>5.0%} {s['avg_pnl_delta']:>+7.4f}  {s['signal']}"
            )
        lines.append(
            "  scalping_too_early → exit_edge_threshold more negative (manual) or model params to slow exit.\n"
            "  scalping_correct   → well-calibrated; if loss_cut could be more aggressive, raise loss_cut_fraction."
        )

    # Hold counterfactual summary
    h_total = cf.get("total_holds_tracked", 0)
    if h_total > 0:
        hold_pnl = cf.get("total_actual_hold_pnl", 0)
        cf_scalp = cf.get("total_counterfactual_scalp_pnl", 0)
        hold_gap = cf.get("pnl_gap_from_holding", 0)
        lines.append(
            f"\nHolds (N={h_total}): actual ${hold_pnl:+.2f} | if scalped at worst ${cf_scalp:+.2f} "
            f"| holding was better by ${hold_gap:+.2f}"
        )
        lines.append(
            f"  Hold accuracy: {cf.get('hold_accuracy', 0):.1%} "
            f"({cf.get('optimal_holds', 0)} correct, {cf.get('suboptimal_holds', 0)} suboptimal)"
        )

    return "\n".join(lines)


def _section_by_regime(ana: dict[str, Any]) -> str:
    # Regime breakdown — key for regime-targeted changes
    by_regime = ana.get("by_regime", {})
    if not by_regime:
        return ""
    dominant = max(by_regime.items(), key=lambda x: x[1].get("n", 0), default=(None, {}))[0]
    lines = [f"## Performance by Regime (dominant: {dominant})"]
    lines.append("When proposing a change, identify which regime it targets. "
                 "A change that helps one regime must not degrade any other regime's Sharpe by >0.10.")
    for regime, stats in sorted(by_regime.items(), key=lambda x: -x[1].get("n", 0)):
        dom_mark = " ← dominant" if regime == dominant else ""
        lines.append(
            f"- **{regime}**{dom_mark}: n={stats.get('n', 0)} "
            f"WR={stats.get('win_rate', 0):.0%} "
            f"Sharpe={stats.get('sharpe', 0):+.3f} "
            f"avg_edge={stats.get('avg_edge', 0):.1%} "
            f"avg_gain={stats.get('avg_gain_pct', 0):.4f}"
        )
    return "\n".join(lines)


def _section_entry_phase(ana: dict[str, Any]) -> str:
    # Entry-phase breakdown — DIAGNOSTIC for manual-only timing levers
    # (normal_fraction, late_max_penalty). No backtestable proxy: the
    # backtest can't simulate different time-of-window behavior on stored
    # gain_pct, so actionable items go to manual_observations only.
    phase_data = ana.get("by_entry_phase", {})
    if not phase_data:
        return ""
    lines = ["## Performance by Entry Phase (DIAGNOSTIC — manual-only triggers)",
             "Maps to manual levers: normal_fraction (early/normal Kelly envelope), "
             "late_max_penalty (late-window Kelly cut). DO NOT propose these in "
             "`changes` — emit manual_observations only."]
    for phase, stats in sorted(phase_data.items(), key=lambda x: -x[1].get("n", 0)):
        n = stats.get("n", 0)
        if n == 0:
            continue
        lines.append(
            f"- **{phase}**: n={n} WR={stats.get('win_rate', 0):.0%} "
            f"Sharpe={stats.get('sharpe', 0):+.3f} avg_gain={stats.get('avg_gain_pct', 0):.4f}"
        )
    return "\n".join(lines)


def _section_flip(ana: dict[str, Any]) -> str:
    # Flip-trade breakdown — DIAGNOSTIC for manual-only flip_enabled / flip_edge_premium.
    flip_data = ana.get("flip_analysis", {})
    if not flip_data:
        return ""
    base = flip_data.get("base", {})
    flip = flip_data.get("flip", {})
    if base.get("n", 0) == 0 and flip.get("n", 0) == 0:
        return ""
    lines = ["## Flip-Trade Analysis (DIAGNOSTIC — manual-only triggers)",
             "Maps to manual levers: flip_enabled (boolean kill switch), flip_edge_premium "
             "(extra edge required for re-entry). DO NOT propose in `changes`."]
    for label, stats in [("base (no flip)", base), ("flip", flip)]:
        n = stats.get("n", 0)
        if n == 0:
            lines.append(f"- {label}: n=0")
            continue
        lines.append(
            f"- {label}: n={n} WR={stats.get('win_rate', 0):.0%} "
            f"Sharpe={stats.get('sharpe', 0):+.3f} avg_gain={stats.get('avg_gain_pct', 0):.4f}"
        )
    return "\n".join(lines)


def _section_edge_realization(ana: dict[str, Any]) -> str:
    # Edge realization quartiles (does larger predicted edge actually realize?)
    er_q = ana.get("edge_realization_quartiles", [])
    if not er_q:
        return ""
    labels = ["Q1 (lowest edge)", "Q2", "Q3", "Q4 (highest edge)"]
    lines = ["## Edge Realization by Predicted Edge Quartile",
             "(ratio = realized_gain / predicted_edge — 1.0 = perfect calibration)"]
    for label, ratio in zip(labels, er_q):
        lines.append(f"- {label}: {ratio:.2f}")
    return "\n".join(lines)


def _section_time_weighted(ana: dict[str, Any]) -> str:
    # Time-weighted stats (recent trades matter more)
    tw = ana.get("time_weighted", {})
    if not tw:
        return ""
    return (
        f"## Time-Weighted Stats (14-day half-life)\n"
        f"WR: {tw.get('win_rate', 0):.0%}  |  Sharpe: {tw.get('sharpe', 0):+.3f}"
    )


def _section_distribution_shifts(ana: dict[str, Any]) -> str:
    # Distribution shift warnings
    shifts = ana.get("distribution_shifts", {})
    if not shifts:
        return ""
    lines = ["## Distribution Shift Detected (recent vs historical)"]
    for feat, info in shifts.items():
        lines.append(f"- **{feat}**: KS={info['statistic']:.3f} p={info['p_value']:.3f} "
                    f"(mean {info.get('hist_mean', 0):.3f} -> {info.get('recent_mean', 0):.3f})")
    return "\n".join(lines)


def _section_execution_quality(ana: dict[str, Any]) -> str:
    # Execution quality (fill slippage, realized edge, breakdown by spread/time)
    eq = ana.get("execution_quality", {})
    if not eq:
        return ""
    lines = ["## Execution Quality"]
    avg_re = eq.get("avg_realized_edge", 0)
    avg_slip = eq.get("avg_fill_slippage", 0)
    pct_pos = eq.get("pct_positive_slippage", 0)
    sharpe_hit = eq.get("sharpe_impact_from_slippage")
    lines.append(f"Avg realized edge (signal_prob - fill_price): {avg_re:+.3f}")
    lines.append(f"Avg fill slippage (fill - signal_price): {avg_slip:+.4f}  |  {pct_pos:.0%} of fills paid above signal")
    if sharpe_hit is not None:
        lines.append(
            f"Sharpe impact from slippage: -{sharpe_hit:.3f} "
            f"(your Sharpe would be ~{sharpe_hit:.3f} higher with zero slippage)"
        )
    fok_rate = eq.get("fok_fill_rate")
    if fok_rate is not None:
        lines.append(f"FOK fill rate: {fok_rate:.0%} ({eq.get('fok_total_attempts', 0)} attempts)")

    # Slippage by spread bucket
    slip_spread = eq.get("slippage_by_spread", {})
    if slip_spread:
        lines.append("\nSlippage by market spread:")
        for bucket, stats in slip_spread.items():
            s = stats.get("avg_slippage")
            lines.append(f"  {bucket}: avg_slip={s:+.4f} n={stats.get('count', 0)}" if s is not None else f"  {bucket}: n={stats.get('count', 0)}")

    slip_time = eq.get("slippage_by_time", {})
    if slip_time:
        lines.append("\nSlippage by time remaining at entry:")
        for bucket, stats in slip_time.items():
            s = stats.get("avg_slippage")
            lines.append(f"  {bucket}: avg_slip={s:+.4f} n={stats.get('count', 0)}" if s is not None else f"  {bucket}: n={stats.get('count', 0)}")

    if avg_slip > 0.005:
        lines.append("WARNING: avg_fill_slippage > 0.005 — slippage is eating significant realized edge.")
    return "\n".join(lines)


def _section_gate_stats(ana: dict[str, Any]) -> str:
    # Gate skip stats (which entry gates are blocking trades)
    gate_stats = ana.get("gate_skip_stats", {})
    if not gate_stats:
        return ""
    counts = {k: v for k, v in gate_stats.items() if k != "total_skips" and isinstance(v, (int, float)) and v > 0}
    if not counts:
        return ""
    lines = [f"## Gate Skip Stats (total skips: {gate_stats.get('total_skips', 0)})"]
    lines.append("Which entry gates are blocking the most trades:")
    for gate, count in sorted(counts.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"- **{gate}**: {count}")
    return "\n".join(lines)


def _section_ghost(ana: dict[str, Any]) -> str:
    # Ghost trade analysis (downstream gate rejections that resolved profitably)
    ghost = ana.get("ghost_analysis", {})
    if not ghost:
        return ""
    lines = ["## Ghost Trade Analysis (trades blocked by gates, tracked to resolution)"]
    lines.append(
        f"Total resolved ghosts: {ghost.get('total_ghosts', 0)} | "
        f"Profitable if entered: {ghost.get('pct_profitable', 0):.0%}"
    )
    by_gate = ghost.get("by_gate", {})
    if by_gate:
        lines.append("Per-gate — sim_pnl = estimated dollar impact if gate were removed:")
        for gate, stats in sorted(by_gate.items(), key=lambda x: -x[1].get('count', 0))[:8]:
            pnl = stats.get("simulated_pnl")
            pnl_str = f" | sim_pnl={pnl:+.2f}" if pnl is not None else ""
            lines.append(
                f"- **{gate}**: {stats.get('count', 0)} blocked, "
                f"{stats.get('pct_profitable', 0):.0%} profitable{pnl_str} | "
                f"{stats.get('interpretation', '')}"
            )
    lines.append(
        "CRITICAL: adverse_rate_30s with LOW win-rate (< 50%) is WORKING — it filters "
        "informed-flow losers. Only loosen gates with >60% profitable AND positive sim_pnl."
    )
    return "\n".join(lines)


def _section_trends(ana: dict[str, Any]) -> str:
    # Recent trends — bucketed trajectory of WR / Sharpe / Q4 realization across
    # the last ~5 chronological slices of the trade history. Lets Claude see whether
    # a metric is self-resolving so it doesn't propose fixes for IMPROVING trends.
    trends_str = ana.get("trends", "")
    return trends_str if trends_str else ""


def _section_current_regime(ana: dict[str, Any]) -> str:
    # Current-regime snapshot (most recent 100 trades — detects regime shifts that
    # the train-split sample wouldn't reflect). If recent WR / Sharpe / mean_gain
    # diverges from the overall stats above, the market may have changed and
    # historical edge has decayed.
    cur_reg = ana.get("current_regime", {})
    if not cur_reg or cur_reg.get("n_trades", 0) < 30:
        return ""
    return (
        f"## Current Regime (last {cur_reg.get('n_trades')} trades — for regime-shift detection)\n"
        f"WR: {cur_reg.get('win_rate', 0):.1%}  |  "
        f"Total PnL: ${cur_reg.get('total_pnl', 0):+.2f}  |  "
        f"Mean gain_pct: {cur_reg.get('mean_gain_pct', 0):+.4f}\n"
        f"Compare to overall stats above — material divergence means "
        f"the market changed, historical edge may have decayed."
    )


def _section_sprt(ana: dict[str, Any]) -> str:
    sprt_agg = ana.get("sprt_aggregate", {})
    by_conf = ana.get("by_sprt_confidence", {})
    if not sprt_agg and not by_conf:
        return ""
    lines = [
        f"## SPRT Entry Gate (last 50 trades)",
        f"State: {sprt_agg.get('state', '?')} | "
        f"ENTER fraction: {sprt_agg.get('enter_pct', 0):.0%} | "
        f"Avg confidence: {sprt_agg.get('avg_confidence', 0):.2f}",
        "SPRT gates entries: SKIP blocks; confidence < min_confidence blocks after 2+ obs.",
    ]
    if by_conf:
        lines.append("WR by SPRT confidence at entry (does high-confidence predict wins?):")
        for bucket, stats in sorted(by_conf.items()):
            lines.append(
                f"  {bucket:>8}: n={stats['n']:>4} WR={stats['win_rate']:.0%} "
                f"Sharpe={stats['sharpe']:+.3f}")
    return "\n".join(lines)


def _section_adverse_selection(ana: dict[str, Any]) -> str:
    by_adv = ana.get("by_adverse_selection", {})
    if not by_adv:
        return ""
    lines = [
        "## Adverse Selection at Entry",
        "WR by adverse_selection_30s rate (low <0.40 / medium 0.40-0.60 / high >0.60):",
        "If high-rate entries underperform, adverse_selection_threshold should be tightened.",
    ]
    for bucket in ("low", "medium", "high"):
        stats = by_adv.get(bucket, {})
        if not stats:
            continue
        lines.append(
            f"  {bucket:>8}: n={stats['n']:>4} WR={stats['win_rate']:.0%} "
            f"Sharpe={stats['sharpe']:+.3f}")
    return "\n".join(lines)


def _section_trades(context: dict[str, Any]) -> str:
    # Recent trades — stratified sample across the full history so Claude sees
    # trades spread throughout the day, not just the final 3 hours.
    # Always anchors the last 15 for recency, evenly samples the rest for coverage.
    trades = context.get("trades", [])
    if not trades:
        return ""
    if len(trades) <= 100:
        sampled = trades
    else:
        recent = trades[-50:]                          # last 50 for recency
        rest = trades[:-50]
        step = max(1, len(rest) // 50)
        sampled = rest[::step][:50] + recent          # 50 spaced + 50 recent = 100
    lines = [f"## Recent Trades ({len(sampled)} sampled from {len(trades)} total)"]
    for i, t in enumerate(sampled, 1):
        ctx = t.get("indicator_snapshot", {}).get("trade_context", {})
        snap = t.get("indicator_snapshot", {})
        won = "WIN" if t.get("correct") else "LOSS"
        side = t.get("side", "?")
        entry = t.get("entry_price", 0)
        exit_ = t.get("exit_price", 0)
        lr = t.get("gain_pct", 0)
        prob = ctx.get("model_probability", t.get("signal_score", 0))
        edge = ctx.get("edge", 0)
        secs = ctx.get("seconds_remaining", 0)
        regime = ctx.get("regime_state", "?")
        flow = ctx.get("flow_score", 0)
        exit_reason = t.get("exit_reason", "resolution")
        lines.append(
            f"#{i} {won} {side} ({exit_reason}) | {entry:.3f}->{exit_:.3f} ret={lr:+.4f} | "
            f"prob={prob:.0%} edge={edge:+.0%} flow={flow:+.2f} {secs:.0f}s regime={regime}"
        )
    return "\n".join(lines)


def _section_active_adoptions(ana: dict[str, Any]) -> str:
    # Active adoptions — which of your past proposals are currently LIVE or ROLLED_BACK.
    # Reconsider direction for params in ROLLED_BACK.
    active_adoptions = ana.get("active_adoptions", "")
    if not active_adoptions:
        return ""
    return (
        "## Current Parameter State (your past proposals right now)\n"
        "Use this to avoid re-proposing the same direction on a rolled-back change.\n\n"
        + active_adoptions
    )


def _section_param_history(ana: dict[str, Any]) -> str:
    # Parameter change history — what worked and what didn't (shown before previous recs)
    param_history = ana.get("parameter_history", "")
    if not param_history:
        return ""
    return f"## Parameter Change History (what worked and what didn't)\n{param_history}"


def _section_prev_recs(context: dict[str, Any]) -> str:
    # Previous recommendations
    prev = context.get("previous_recommendations", "")
    if not prev:
        return ""
    return f"## Previous Recommendations (recent cycles)\n{prev}"


def _section_adoption_target(context: dict[str, Any]) -> str:
    # Baseline Kelly-Sharpe and adoption target — includes noise (JK_SE) so Claude
    # can size its proposals to actually clear the floor.
    ana = context.get("analysis", {})
    baseline_ks = ana.get("baseline_kelly_sharpe")
    adoption_target = ana.get("adoption_target")
    if baseline_ks is None:
        return ""
    jk_se = ana.get("baseline_jk_se")
    z_floor = ana.get("adoption_z_floor", 0.5)
    dyn_floor = ana.get("adoption_dynamic_floor")
    n_base = ana.get("baseline_n_trades")
    lines = [
        "## Adoption Target (the exact bar your change must clear)",
        f"Baseline Kelly-Sharpe: **{baseline_ks:.4f}** (N={n_base} trades)",
    ]
    if jk_se is not None and dyn_floor is not None:
        lines.append(
            f"Backtest noise (Jobson-Korkie SE, autocorr-adjusted): **±{jk_se:.4f}**"
        )
        lines.append(
            f"Required delta = z_floor × SE = {z_floor} × {jk_se:.4f} = **{dyn_floor:.4f}**  "
            f"(z-only gate; no static abs floor)"
        )
        lines.append(
            f"Target Sharpe: **{adoption_target:.4f}** = baseline + {dyn_floor:.4f}"
        )
        lines.append(
            f"Interpretation: a Δ of {jk_se:.3f} is 1 SD of noise. Changes with Δ < "
            f"{dyn_floor:.3f} are statistically indistinguishable from noise at this N. "
            f"Aim for Δ >= {2*dyn_floor:.3f} to have meaningful safety margin."
        )
        lines.append(
            f"**Also required:** candidate must improve in ≥2 of 4 walk-forward folds "
            f"AND pass regime-stratified check (≥2 of 3 regimes improve, OR dominant "
            f"regime improves without any regime degrading >0.10 Sharpe)."
        )
    else:
        lines.append(f"Target Sharpe: **{adoption_target:.4f}** (baseline + floor)")
    return "\n".join(lines)


def _section_cumulative_failures(ana: dict[str, Any]) -> str:
    # Cumulative failures — all parameter values tried across all cycles
    cum_failures = ana.get("cumulative_failures", {})
    if not cum_failures:
        return ""
    lines = ["## Cumulative Failed Attempts (do NOT repeat these)"]
    for param, attempts in cum_failures.items():
        lines.append(f"- **{param}**: tried {', '.join(attempts[:5])} — all failed")
    return "\n".join(lines)


def _section_per_change_results(ana: dict[str, Any]) -> str:
    # Per-change backtest results from last cycle — exact attribution of what worked/hurt
    per_change = ana.get("last_per_change_results", [])
    if not per_change:
        return ""
    lines = ["## Last Cycle Per-Parameter Results (CRITICAL — read before proposing anything)"]
    lines.append("These are the EXACT backtest results for each change you proposed last cycle:")
    for r in per_change:
        lines.append(f"- {r}")
    lines.append("If a change had NEGATIVE delta — it made Sharpe WORSE. Do NOT propose it again.")
    lines.append("If z was low but delta was positive — consider proposing a LARGER change to that parameter.")
    return "\n".join(lines)


def _section_platt_meta(ana: dict[str, Any]) -> str:
    # Platt meta-check: raw model vs current calibrator (surfaced only when close)
    platt_meta = ana.get("platt_meta_warning", "")
    if not platt_meta:
        return ""
    return (
        f"## Platt Calibration Meta-Warning\n{platt_meta}\n"
        f"If this persists across cycles, the operator may drop Platt entirely — "
        f"do not propose calibrator-dependent changes assuming Platt is load-bearing."
    )


def _section_pipeline_track_record(ana: dict[str, Any]) -> str:
    # Pipeline track record — did past adoptions actually help?
    track_record = ana.get("pipeline_track_record", "")
    return track_record if track_record else ""


def _section_decay_analysis(ana: dict[str, Any]) -> str:
    # Adoption decay analysis — are changes persisting or fading within 14 days?
    decay_analysis = ana.get("decay_analysis", "")
    return decay_analysis if decay_analysis else ""


def _section_prediction_accuracy(ana: dict[str, Any]) -> str:
    # Prediction accuracy — how well-calibrated are Claude's own delta predictions?
    pred_accuracy = ana.get("prediction_accuracy", "")
    return pred_accuracy if pred_accuracy else ""


def _section_directional_table(ana: dict[str, Any]) -> str:
    # Empirical directional table — replaces hardcoded "test HIGHER" rules
    dir_table = ana.get("directional_table", "")
    return dir_table if dir_table else ""


def _section_rerouting_notice(ana: dict[str, Any]) -> str:
    rerouted = ana.get("last_rerouted_params", []) or []
    if not rerouted:
        return ""
    unique = list(dict.fromkeys(rerouted))  # preserve order, dedupe
    return (
        "## Last Cycle Rerouting Notice (READ THIS)\n"
        f"Last cycle you put these MANUAL-ONLY params into `changes`: {', '.join(unique)}.\n"
        "They were rerouted to `manual_observations` with confidence=low and the slot in "
        "`changes` was wasted. These params are not backtestable — they will never adopt via "
        "`changes`. If the data still warrants a change to one of them, emit it directly in "
        "`manual_observations` with proper evidence (n>=50, source). Do NOT put them in "
        "`changes` again."
    )


def _format_strategy_context(context: dict[str, Any]) -> str:
    """Format context into a structured prompt for Claude."""
    cfg = context.get("current_config", {})
    ana = context.get("analysis", {})
    sections = [
        _section_config(cfg),
        _section_overall(ana),
        _section_noise(context),
        _section_per_indicator(ana),
        _section_side(ana),
        _section_edge_calibration(ana),
        _section_time_patterns(ana),
        _section_volatility(ana),
        _section_counterfactual(ana),
        _section_by_regime(ana),
        _section_entry_phase(ana),
        _section_flip(ana),
        _section_edge_realization(ana),
        _section_time_weighted(ana),
        _section_distribution_shifts(ana),
        _section_execution_quality(ana),
        _section_gate_stats(ana),
        _section_ghost(ana),
        _section_trends(ana),
        _section_current_regime(ana),
        _section_sprt(ana),
        _section_adverse_selection(ana),
        _section_trades(context),
        _section_active_adoptions(ana),
        _section_param_history(ana),
        _section_prev_recs(context),
        _section_adoption_target(context),
        _section_cumulative_failures(ana),
        _section_per_change_results(ana),
        _section_platt_meta(ana),
        _section_pipeline_track_record(ana),
        _section_decay_analysis(ana),
        _section_prediction_accuracy(ana),
        _section_directional_table(ana),
        _section_rerouting_notice(ana),
        (
            "## Your Task\n"
            "Analyze all the data above. Identify patterns, biases, and opportunities for improvement. "
            "If the pipeline track record shows your past recommendations hurt performance, "
            "explain what went wrong and adjust accordingly. "
            "If the decay analysis shows >50% of adoptions are decaying, prioritize an empty or very small changes list. "
            "Return your recommendations as JSON per the format in your instructions."
        ),
    ]
    return "\n\n".join(s for s in sections if s)
