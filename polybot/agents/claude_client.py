"""ClaudeClient: Anthropic API wrapper for nightly strategy analysis.

Formats the BiasDetector analysis card and recent trade sample into a structured prompt,
calls Claude, validates and clamps the returned recommendations against safe parameter
ranges, and returns a recommendations dict for the WeightOptimizer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

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
  Then `probability_compression` shrinks toward 0.5; then Platt calibration.

  Edge = model_prob - market_price. Entry needs edge >= min_edge AND Kelly >= min_kelly.
  Kelly: f* = (p*b - q)/b * kelly_fraction, b = (1-price)/price.

## Backtestable Params (you can propose these in `changes`)
- atr_sigma_ratio (1.2-2.5, HIGHEST leverage; lower = sharper probs)
- logit_scale (2.0-6.0, master amplifier on L2-L5)
- probability_compression (0.5-1.0, shrink toward 0.5)
- student_t_df (3-8, lower = fatter tails)
- min_atr (4.0-25.0, ATR floor)
- flow_weight (0.02-0.12), spot_flow_weight (0.01-0.15), liquidation_weight (0.01-0.10)
- regime_weight (0.02-0.10), prev_margin_weight (0.01-0.05)
- momentum_weight (-0.10 to +0.10; NEGATIVE = fade indicators)
- kelly_fraction (0.05-0.25; leave unchanged unless strong risk evidence)
- min_edge (0.02-0.10), min_kelly (0.005-0.04), min_model_probability (0.52-0.70)
- weights (RSI/MACD/Stoch/OBV/VWAP dict, sum=1.0, each ≥0.05)

## Manual-Only Params (route to `manual_observations`, never `changes`)
- exit_edge_threshold, max_edge, adverse_selection_threshold
- final_min_probability, normal_fraction, late_max_penalty
- trading_start_hour_et / trading_end_hour_et / trading_end_minute
- flip_enabled, flip_edge_premium
- max_single_position_*, max_concurrent_positions, max_bankroll_deployed
- circuit_breaker.floor_pct, circuit_breaker.min_multiplier
- Indicator periods (use full dotted path, e.g. `indicators.rsi.period`):
  rsi.period/overbought/oversold; macd.fast_period/slow_period/signal_period;
  stochastic.k_period/d_smoothing/overbought/oversold;
  ema.fast_period/slow_period/chop_threshold; obv.slope_period;
  vwap.session_minutes; atr.period/low_percentile/high_percentile/history_periods.
  Backtest can't replay alternate periods (snapshot stores the live-period score
  only). Propose only when an indicator's per_indicator accuracy is consistently
  poor at N≥50, with the period change as the operator-reviewable hypothesis.
- SPRT: `sprt.alpha`, `sprt.beta`, `sprt.observation_interval_s`. Backtest
  replays stored gain_pct from a fixed entry instant — it can't simulate
  alternate intra-window entry timings. Propose only when execution-quality
  evidence (n≥50) suggests SPRT is firing too eagerly or too cautiously.

## Behavioral Rules
1. SPRT state = signal aggressiveness of recent trades, NOT win rate or entry quality.
   Do not write findings like "0% edge-positive entries" based on SPRT state.
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

Trigger mappings:
- counterfactual_analysis.net_exit_direction = "scalp_early" → exit_edge_threshold MORE
  negative. = "hold_long" → exit_edge_threshold LESS negative.
- edge_calibration: high-edge bucket WR < low-edge bucket WR by 2× noise → max_edge LOWER.
- ghost_analysis.adverse_rate_30s with high pct_profitable + positive sim_pnl →
  adverse_selection_threshold HIGHER (gate over-filters). Negative sim_pnl → keep tight.
- time_patterns: last-30s WR < 55% at N≥50 → final_min_probability HIGHER.
- time_patterns: late-window WR much lower than early → late_max_penalty LOWER.

## Response (return ONLY valid JSON, no fences):
{
  "changes": [
    {"param": "atr_sigma_ratio", "value": 1.5, "reason": "one sentence",
     "predicted_delta_sharpe_7d": 0.025, "confidence_interval": [-0.005, 0.045]}
  ],
  "manual_observations": [
    {"param": "exit_edge_threshold", "current": -0.05, "suggested": -0.12,
     "evidence": {"metric": "scalp_accuracy", "value": 0.46, "n": 900, "source": "counterfactual_analysis"},
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

    # All manual-only params — Claude cannot adopt these via `changes` (they get
    # rerouted to manual_observations for the operator to review). Includes:
    # - Exit/timing/schedule (backtest can't simulate)
    # - Risk caps (operator-owned policy)
    # - Circuit breaker (bankroll protection)
    MANUAL_ONLY_PARAMS = {
        # Exit / scalp behavior
        "exit_edge_threshold",
        # Entry-time filters (informed flow, stale price, late window)
        "adverse_selection_threshold",
        "max_edge",
        "final_min_probability",
        # Entry-timing Kelly envelope
        "normal_fraction",
        "late_max_penalty",
        # Schedule
        "trading_start_hour_et",
        "trading_start_minute",
        "trading_end_hour_et",
        "trading_end_minute",
        # Flip-trade behavior
        "flip_enabled",
        "flip_edge_premium",
        # Risk caps (operator-owned)
        "max_concurrent_positions",
        "max_bankroll_deployed",
        # Circuit breaker
        "circuit_breaker.floor_pct",
        "circuit_breaker.min_multiplier",
        # Indicator periods — manual-only because the backtest replays stored
        # norm_scores (computed live with the active period) and can't recompute
        # alternate periods without raw 1-min candle history per snapshot.
        "indicators.rsi.period",
        "indicators.rsi.overbought",
        "indicators.rsi.oversold",
        "indicators.macd.fast_period",
        "indicators.macd.slow_period",
        "indicators.macd.signal_period",
        "indicators.stochastic.k_period",
        "indicators.stochastic.d_smoothing",
        "indicators.stochastic.overbought",
        "indicators.stochastic.oversold",
        "indicators.ema.fast_period",
        "indicators.ema.slow_period",
        "indicators.ema.chop_threshold",
        "indicators.obv.slope_period",
        "indicators.vwap.session_minutes",
        "indicators.atr.period",
        "indicators.atr.low_percentile",
        "indicators.atr.high_percentile",
        "indicators.atr.history_periods",
        # SPRT — manual-only because SPRT decides intra-window entry timing,
        # and the backtest replays stored gain_pct from a fixed entry instant
        # (alternate timings would produce different fills, which aren't stored).
        "sprt.alpha",
        "sprt.beta",
        "sprt.observation_interval_s",
    }

    # Per-param clamp ranges — only backtestable params appear here.
    CLAMP_RANGES: dict[str, tuple] = {
        "atr_sigma_ratio":              (1.2,   2.5,   float),
        "logit_scale":                  (2.0,   6.0,   float),
        "probability_compression":      (0.5,   1.0,   float),
        "liquidation_weight":           (0.01,  0.10,  float),
        "prev_margin_weight":           (0.01,  0.05,  float),
        "spot_flow_weight":             (0.01,  0.15,  float),
        "flow_weight":                  (0.02,  0.12,  float),
        "regime_weight":                (0.02,  0.10,  float),
        "momentum_weight":             (-0.10,  0.10,  float),
        "student_t_df":                 (3,     8,     int),
        "kelly_fraction":               (0.05,  0.25,  float),
        "min_atr":                      (4.0,   25.0,  float),
        # Entry gates — pipeline-tunable now that ghosts are in the backtest sample.
        # Ranges are conservative; Claude should move them in small steps.
        "min_edge":                     (0.02,  0.10,  float),
        "min_kelly":                    (0.005, 0.04,  float),
        "min_model_probability":        (0.52,  0.70,  float),
    }

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
                clamped = cast(max(lo, min(hi, cast(value))))
            except (TypeError, ValueError):
                continue
            # Extra: momentum magnitude must stay below min_edge
            if param == "momentum_weight":
                min_edge_live = cfg.get("min_edge", 0.04)
                if abs(clamped) >= min_edge_live:
                    clamped = float((min_edge_live - 0.001) * (1.0 if clamped >= 0 else -1.0))
            entry: dict[str, Any] = {"param": param, "value": clamped, "reason": reason}
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


def _format_strategy_context(context: dict[str, Any]) -> str:
    """Format context into a structured prompt for Claude."""
    sections = []

    # Current config — organized by whether Claude can change each param.
    cfg = context.get("current_config", {})
    sections.append(
        "## Current Configuration\n"
        "### YOU CAN CHANGE THESE (backtestable):\n"
        f"Indicator weights: {json.dumps(cfg.get('weights', {}))}\n"
        f"momentum_weight (Layer 4): {cfg.get('momentum_weight', 0.04)}\n"
        f"regime_weight (Layer 2): {cfg.get('regime_weight', 0.05)}\n"
        f"flow_weight (Layer 3): {cfg.get('flow_weight', 0.06)}\n"
        f"spot_flow_weight (L3b): {cfg.get('spot_flow_weight', 0.04)}\n"
        f"liquidation_weight (L3e): {cfg.get('liquidation_weight', 0.03)}\n"
        f"prev_margin_weight (L5): {cfg.get('prev_margin_weight', 0.02)}\n"
        f"atr_sigma_ratio: {cfg.get('atr_sigma_ratio', 1.7)}\n"
        f"student_t_df (Layer 1): {cfg.get('student_t_df', 4)}\n"
        f"logit_scale: {cfg.get('logit_scale', 4.0)}\n"
        f"probability_compression: {cfg.get('probability_compression', 1.0)}\n"
        f"kelly_fraction: {cfg.get('kelly_fraction', 0.15)}\n"
        f"min_atr: {cfg.get('min_atr', 8.0)}\n"
        f"min_model_probability: {cfg.get('min_model_probability', 0.58)}  (pipeline-tunable since ghosts joined backtest)\n"
        f"min_edge (entry_threshold): {cfg.get('min_edge', 0.04)}  (pipeline-tunable since ghosts joined backtest)\n"
        f"min_kelly (entry gate): {cfg.get('min_kelly', 0.015)}  (pipeline-tunable since ghosts joined backtest)\n"
        "\n### MANUAL-ONLY (not in `changes` — propose via `manual_observations` if data warrants):\n"
        f"# Exit / scalp\n"
        f"exit_edge_threshold: {cfg.get('exit_edge_threshold', -0.05)}\n"
        f"# Entry filters (informed flow / stale price / late window)\n"
        f"adverse_selection_threshold: {cfg.get('adverse_selection_threshold', 0.55)}\n"
        f"max_edge: {cfg.get('max_edge', 0.20)}\n"
        f"final_min_probability: {cfg.get('final_min_probability', 0.90)}\n"
        f"# Entry-timing Kelly envelope\n"
        f"normal_fraction: {cfg.get('normal_fraction', 0.60)}\n"
        f"late_max_penalty: {cfg.get('late_max_penalty', 0.60)}\n"
        f"# Schedule\n"
        f"trading_start_hour_et: {cfg.get('trading_start_hour_et', 0)}, trading_start_minute: {cfg.get('trading_start_minute', 1)}\n"
        f"trading_end_hour_et: {cfg.get('trading_end_hour_et', 22)}, trading_end_minute: {cfg.get('trading_end_minute', 30)}\n"
        f"# Flip behavior\n"
        f"flip_enabled: {cfg.get('flip_enabled', True)}, flip_edge_premium: {cfg.get('flip_edge_premium', 0.015)}\n"
        f"# Risk caps (operator-owned policy)\n"
        f"max_concurrent_positions: {cfg.get('max_concurrent_positions', 2)}, max_bankroll_deployed: {cfg.get('max_bankroll_deployed', 0.80)}\n"
        f"# Circuit breaker\n"
        f"circuit_breaker.floor_pct: {cfg.get('circuit_breaker', {}).get('floor_pct', 0.85)}, "
        f"circuit_breaker.min_multiplier: {cfg.get('circuit_breaker', {}).get('min_multiplier', 0.40)}\n"
        f"# Indicator periods (manual-only — backtest can't recompute from raw candles)\n"
        + "\n".join(
            f"{dotted}: {_cfg_get(cfg, dotted)}"
            for dotted in (
                "indicators.rsi.period", "indicators.rsi.overbought", "indicators.rsi.oversold",
                "indicators.macd.fast_period", "indicators.macd.slow_period", "indicators.macd.signal_period",
                "indicators.stochastic.k_period", "indicators.stochastic.d_smoothing",
                "indicators.stochastic.overbought", "indicators.stochastic.oversold",
                "indicators.ema.fast_period", "indicators.ema.slow_period", "indicators.ema.chop_threshold",
                "indicators.obv.slope_period", "indicators.vwap.session_minutes",
                "indicators.atr.period", "indicators.atr.low_percentile",
                "indicators.atr.high_percentile", "indicators.atr.history_periods",
            )
            if _cfg_get(cfg, dotted) is not None
        )
        + "\n# SPRT (manual-only — backtest can't simulate alternate entry timings)\n"
        f"sprt.alpha: {_cfg_get(cfg, 'sprt.alpha')}, "
        f"sprt.beta: {_cfg_get(cfg, 'sprt.beta')}, "
        f"sprt.observation_interval_s: {_cfg_get(cfg, 'sprt.observation_interval_s')}"
    )

    # Performance analysis from BiasDetector
    analysis = context.get("analysis", {})
    if analysis:
        overall = analysis.get("overall", {})
        if overall:
            sections.append(
                "## Overall Performance\n"
                f"Total trades: {overall.get('total_trades', 0)}\n"
                f"Win rate: {overall.get('win_rate', 0):.1%}\n"
                f"Average edge at entry: {overall.get('avg_edge', 0):.1%}\n"
                f"Average gain pct: {overall.get('avg_gain_pct', 0):.4f}\n"
                f"Sharpe ratio: {overall.get('sharpe', 0):.3f}"
            )

        # Statistical noise floors — findings must exceed 2× noise to be actionable.
        # Sharpe noise uses the ACTUAL baseline Sharpe (when available) rather than
        # an S=0.5 placeholder, so the figure shown to Claude matches the JK_SE the
        # adoption gate actually computes.
        n = overall.get("total_trades", 0)
        if n >= 10:
            import math as _math
            ana = context.get("analysis", {})
            actual_baseline = ana.get("baseline_kelly_sharpe")
            n_for_sharpe = ana.get("baseline_n_trades") or n
            wr_noise = round(_math.sqrt(0.25 / max(n, 1)), 3)   # ±1σ at 50% WR
            if actual_baseline is not None and n_for_sharpe:
                # Same JK SE formula the gate uses (autocorr inflation is added at runtime)
                sharpe_noise = round(
                    _math.sqrt((1.0 + 0.5 * float(actual_baseline) ** 2) / max(int(n_for_sharpe), 1)),
                    3,
                )
                sharpe_basis = f"JK SE at baseline Sharpe={actual_baseline:.3f}, N={n_for_sharpe}"
            else:
                sharpe_noise = round(_math.sqrt((1.0 + 0.5 * 0.25) / max(n, 1)), 3)
                sharpe_basis = "JK SE at placeholder S=0.5 (baseline not yet computed)"
            per_ind_n = max(n // 5, 1)  # ~N/5 per indicator on average
            sig_noise = round(_math.sqrt(0.25 / per_ind_n), 3)
            q_noise = round(_math.sqrt(0.25 / max(n // 4, 1)), 3)
            sections.append(
                f"## Statistical Noise Reference (at N={n} trades)\n"
                f"A finding must exceed 2× noise to be actionable — below that it's sampling variation.\n"
                f"- Win rate noise: ±{wr_noise:.1%} (1σ) → actionable only if difference > ±{2*wr_noise:.1%}\n"
                f"- Sharpe noise: ±{sharpe_noise:.3f} ({sharpe_basis}) → actionable only if Sharpe delta > {2*sharpe_noise:.3f}\n"
                f"- Per-signal accuracy noise (~{per_ind_n} samples/indicator): ±{sig_noise:.1%} → actionable if accuracy > {0.50 + 2*sig_noise:.1%}\n"
                f"- Edge realization quartile noise (~{n//4} samples/quartile): ±{q_noise:.1%}\n"
                f"Example: 'flow_weight accuracy 68% at N={per_ind_n}' = "
                f"{(0.68 - 0.50) / sig_noise:.1f}× noise — {'ACTIONABLE' if (0.68 - 0.50) / sig_noise > 2 else 'marginal'}"
            )

        per_ind = analysis.get("per_indicator", {})
        if per_ind:
            lines = ["## Per-Indicator Analysis"]
            for ind, stats in per_ind.items():
                lines.append(
                    f"- **{ind}**: accuracy={stats.get('accuracy', 0):.1%} "
                    f"(bullish={stats.get('bullish_accuracy', 0):.1%}, "
                    f"bearish={stats.get('bearish_accuracy', 0):.1%}) "
                    f"n={stats.get('sample_size', 0)}"
                )
            sections.append("\n".join(lines))

        side = analysis.get("side_analysis", {})
        if side:
            lines = ["## Side Analysis (Up vs Down)"]
            for s, stats in side.items():
                lines.append(
                    f"- **{s}**: win_rate={stats.get('win_rate', 0):.1%} "
                    f"avg_ret={stats.get('avg_gain_pct', 0):.4f} n={stats.get('count', 0)}"
                )
            sections.append("\n".join(lines))

        edge_cal = analysis.get("edge_calibration", {})
        if edge_cal:
            lines = ["## Edge Calibration (does larger edge = more wins?)"]
            for bucket, stats in edge_cal.items():
                lines.append(f"- **{bucket}**: win_rate={stats.get('win_rate', 0):.1%} n={stats.get('count', 0)}")
            sections.append("\n".join(lines))

        time_p = analysis.get("time_patterns", {})
        if time_p:
            lines = ["## Time Patterns (seconds remaining at entry)"]
            for bucket, stats in time_p.items():
                lines.append(f"- **{bucket}**: win_rate={stats.get('win_rate', 0):.1%} n={stats.get('count', 0)}")
            sections.append("\n".join(lines))

        vol_p = analysis.get("volatility_patterns", {})
        if vol_p:
            lines = ["## Volatility Patterns (ATR regime)"]
            for bucket, stats in vol_p.items():
                lines.append(f"- **{bucket}**: win_rate={stats.get('win_rate', 0):.1%} n={stats.get('count', 0)}")
            sections.append("\n".join(lines))

        cf = analysis.get("counterfactual_analysis", {})
        if cf and cf.get("total_scalps_tracked", 0) > 0:
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
                    f"but decay). Raise probability_compression (pull toward 0.5) or "
                    f"atr_sigma_ratio (wider L1 sigma)."
                )
            else:
                lines.append("→ Exit threshold appears well-calibrated (informational only — manual param).")

            # Holding-edge accuracy buckets — DIAGNOSTIC ONLY (exit_edge_threshold is manual).
            hedge_acc = cf.get("holding_edge_accuracy", {})
            if hedge_acc:
                lines.append("\nScalp accuracy by holding_edge at exit (diagnostic — exit_edge_threshold is MANUAL-ONLY):")
                lines.append("  If accuracy <50% across buckets, the entry model is overconfident — fix via probability_compression.")
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

            sections.append("\n".join(lines))

    # Regime breakdown — key for regime-targeted changes
    by_regime = analysis.get("by_regime", {})
    if by_regime:
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
        sections.append("\n".join(lines))

    # Entry-phase breakdown — DIAGNOSTIC for manual-only timing levers
    # (normal_fraction, late_max_penalty, final_min_probability).
    # No backtestable proxy: the backtest can't simulate different time-of-window
    # behavior on stored gain_pct, so anything actionable here goes to
    # manual_observations, never to `changes`.
    phase_data = analysis.get("by_entry_phase", {})
    if phase_data:
        lines = ["## Performance by Entry Phase (DIAGNOSTIC — manual-only triggers)",
                 "Maps to manual levers: normal_fraction (early/normal Kelly envelope), "
                 "late_max_penalty (late-window Kelly cut), final_min_probability (last-30s gate). "
                 "DO NOT propose these in `changes` — emit manual_observations only."]
        for phase, stats in sorted(phase_data.items(), key=lambda x: -x[1].get("n", 0)):
            n = stats.get("n", 0)
            if n == 0:
                continue
            lines.append(
                f"- **{phase}**: n={n} WR={stats.get('win_rate', 0):.0%} "
                f"Sharpe={stats.get('sharpe', 0):+.3f} avg_gain={stats.get('avg_gain_pct', 0):.4f}"
            )
        sections.append("\n".join(lines))

    # Flip-trade breakdown — DIAGNOSTIC for manual-only flip_enabled / flip_edge_premium.
    flip_data = analysis.get("flip_analysis", {})
    if flip_data:
        base = flip_data.get("base", {})
        flip = flip_data.get("flip", {})
        if base.get("n", 0) > 0 or flip.get("n", 0) > 0:
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
            sections.append("\n".join(lines))

    # Edge realization quartiles (does larger predicted edge actually realize?)
    er_q = analysis.get("edge_realization_quartiles", [])
    if er_q:
        labels = ["Q1 (lowest edge)", "Q2", "Q3", "Q4 (highest edge)"]
        lines = ["## Edge Realization by Predicted Edge Quartile",
                 "(ratio = realized_gain / predicted_edge — 1.0 = perfect calibration)"]
        for label, ratio in zip(labels, er_q):
            lines.append(f"- {label}: {ratio:.2f}")
        sections.append("\n".join(lines))

    # Adaptive calibration: live state from the rolling 100-trade buffer.
    # TWO orthogonal learning loops, each producing its own multiplier:
    #   confidence  — drift in the model's prediction range (moderate/high/extreme)
    #   disagreement — drift conditional on |model - market| (agree/medium/strong)
    # Runtime applies min(conf_mult, disagree_mult) to each new prediction.
    # If only the strong-disagreement bucket has drift, runtime already handles
    # the +50% edge problem — DON'T propose static probability_compression to
    # "fix" something the runtime already compresses.
    cal_state = analysis.get("adaptive_calibration_buckets", {})
    if cal_state:
        lines = ["## Adaptive Calibration State (live, last 100 trades)",
                 "Drift > 5pp at n>=15 means that bucket is miscalibrated. "
                 "Runtime applies the more conservative of confidence and disagreement multipliers."]

        def _render_table(title: str, ranges: list[tuple[str, str]], data: dict) -> list[str]:
            out = [f"\n### {title}"]
            header = f"{'Bucket':<10} {'Range':<12} {'N':>4} {'Predicted':>10} {'Actual':>8} {'Drift':>7} {'Mult':>6}"
            out.append(header)
            out.append("-" * len(header))
            for name, range_str in ranges:
                b = data.get(name, {})
                n = b.get("n", 0)
                if n == 0:
                    out.append(f"{name:<10} {range_str:<12} {0:>4} {'—':>10} {'—':>8} {'—':>7} {b.get('multiplier', 1.0):>6.2f}")
                    continue
                out.append(
                    f"{name:<10} {range_str:<12} {n:>4} "
                    f"{b.get('mean_predicted', 0):>10.3f} {b.get('mean_actual', 0):>8.3f} "
                    f"{b.get('drift', 0):>7.3f} {b.get('multiplier', 1.0):>6.2f}"
                )
            return out

        lines.extend(_render_table(
            "Confidence buckets (max(p, 1-p))",
            [("moderate", "0.58-0.70"), ("high", "0.70-0.85"), ("extreme", "0.85-1.00")],
            cal_state.get("confidence", {}),
        ))
        lines.extend(_render_table(
            "Disagreement buckets (|model - market|)",
            [("agree", "0.00-0.10"), ("medium", "0.10-0.25"), ("strong", "0.25+")],
            cal_state.get("disagreement", {}),
        ))
        sections.append("\n".join(lines))

    # Time-weighted stats (recent trades matter more)
    tw = analysis.get("time_weighted", {})
    if tw:
        sections.append(
            f"## Time-Weighted Stats (14-day half-life)\n"
            f"WR: {tw.get('win_rate', 0):.0%}  |  Sharpe: {tw.get('sharpe', 0):+.3f}"
        )

    # Distribution shift warnings
    shifts = analysis.get("distribution_shifts", {})
    if shifts:
        lines = ["## Distribution Shift Detected (recent vs historical)"]
        for feat, info in shifts.items():
            lines.append(f"- **{feat}**: KS={info['statistic']:.3f} p={info['p_value']:.3f} "
                        f"(mean {info.get('hist_mean', 0):.3f} -> {info.get('recent_mean', 0):.3f})")
        sections.append("\n".join(lines))

    # Execution quality (fill slippage, realized edge, breakdown by spread/time)
    eq = analysis.get("execution_quality", {})
    if eq:
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
            lines.append("\nSlippage by market spread (wide spread = stale/illiquid market):")
            for bucket, stats in slip_spread.items():
                s = stats.get("avg_slippage")
                lines.append(f"  {bucket}: avg_slip={s:+.4f} n={stats.get('count', 0)}" if s is not None else f"  {bucket}: n={stats.get('count', 0)}")
            lines.append(
                "  → Wide-spread high-slippage pattern is diagnostic — max_edge is manual-only. "
                "If slippage is eating edge, the fix is to improve signal quality (logit_scale, flow_weight) "
                "so higher-probability entries wait for tighter spreads naturally."
            )

        # Slippage by time-in-window
        slip_time = eq.get("slippage_by_time", {})
        if slip_time:
            lines.append("\nSlippage by time remaining at entry:")
            for bucket, stats in slip_time.items():
                s = stats.get("avg_slippage")
                lines.append(f"  {bucket}: avg_slip={s:+.4f} n={stats.get('count', 0)}" if s is not None else f"  {bucket}: n={stats.get('count', 0)}")
            lines.append(
                "  → Late-window slippage is highest (thin book near expiry). "
                "late_max_penalty / normal_fraction are manual-only — flag high late-window "
                "slippage in key_findings for the operator, and tighten the entry model "
                "(logit_scale, probability_compression) so marginal late entries self-filter."
            )

        lines.append(
            "\nActionable (backtestable) params for slippage: "
            "logit_scale (amplified signals fake-edge if slippage eats it), "
            "kelly_fraction (slippage is hidden cost reducing true Kelly). "
            "max_edge is manual-only."
        )
        if avg_slip > 0.005:
            lines.append("WARNING: avg_fill_slippage > 0.005 — slippage is eating significant realized edge.")
        sections.append("\n".join(lines))

    # Gate skip stats (which entry gates are blocking trades)
    gate_stats = analysis.get("gate_skip_stats", {})
    if gate_stats:
        counts = {k: v for k, v in gate_stats.items() if k != "total_skips" and isinstance(v, (int, float)) and v > 0}
        if counts:
            lines = [f"## Gate Skip Stats (total skips: {gate_stats.get('total_skips', 0)})"]
            lines.append("Which entry gates are blocking the most trades:")
            for gate, count in sorted(counts.items(), key=lambda x: -x[1])[:10]:
                lines.append(f"- **{gate}**: {count}")
            lines.append(
                "NOTE: adverse_selection_threshold is MANUAL-ONLY — if adverse_selection "
                "dominates skips, flag in key_findings for the operator. High layer_disagreement "
                "skips can be reduced via logit_scale (harmonize L2-L5 strength) or "
                "probability_compression (pull extreme L1 toward center)."
            )
            sections.append("\n".join(lines))

    # Ghost trade analysis (downstream gate rejections that resolved profitably)
    ghost = analysis.get("ghost_analysis", {})
    if ghost:
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
        sections.append("\n".join(lines))

    # Recent trends — bucketed trajectory of WR / Sharpe / Q4 realization across
    # the last ~5 chronological slices of the trade history. Lets Claude see whether
    # a metric is self-resolving so it doesn't propose fixes for IMPROVING trends.
    trends_str = analysis.get("trends", "")
    if trends_str:
        sections.append(trends_str)

    # Current-regime snapshot (most recent 100 trades — detects regime shifts that
    # the train-split sample wouldn't reflect). If recent WR / Sharpe / mean_gain
    # diverges from the overall stats above, the market may have changed and
    # historical edge has decayed.
    cur_reg = analysis.get("current_regime", {})
    if cur_reg and cur_reg.get("n_trades", 0) >= 30:
        sections.append(
            f"## Current Regime (last {cur_reg.get('n_trades')} trades — for regime-shift detection)\n"
            f"WR: {cur_reg.get('win_rate', 0):.1%}  |  "
            f"Total PnL: ${cur_reg.get('total_pnl', 0):+.2f}  |  "
            f"Mean gain_pct: {cur_reg.get('mean_gain_pct', 0):+.4f}\n"
            f"Compare to overall stats above — material divergence means "
            f"the market changed, historical edge may have decayed."
        )

    # SPRT aggregate evidence — diagnostic of recent SIGNAL AGGRESSIVENESS only.
    # Does NOT measure win rate or entry quality. See behavioral rule #1.
    sprt_agg = analysis.get("sprt_aggregate", {})
    if sprt_agg:
        sections.append(
            f"## SPRT Signal Aggressiveness (last 50 trades — diagnostic only)\n"
            f"State: {sprt_agg.get('state', '?')} (passive = few ENTERs recently, aggressive = many) | "
            f"ENTER fraction: {sprt_agg.get('enter_pct', 0):.0%} (how often per-trade SPRT said ENTER) | "
            f"Avg confidence: {sprt_agg.get('avg_confidence', 0):.2f}\n"
            f"NOTE: This is NOT win rate or edge realization. Trades that passed entry gates were edge-positive by definition."
        )

    # Recent trades — stratified sample across the full history so Claude sees
    # trades spread throughout the day, not just the final 3 hours.
    # Always anchors the last 15 for recency, evenly samples the rest for coverage.
    trades = context.get("trades", [])
    if trades:
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
        sections.append("\n".join(lines))

    # Active adoptions — which of your past proposals are currently LIVE, IN_COOLDOWN,
    # or ROLLED_BACK. Do NOT propose params listed as IN_COOLDOWN; reconsider direction
    # for params in ROLLED_BACK.
    active_adoptions = context.get("analysis", {}).get("active_adoptions", "")
    if active_adoptions:
        sections.append(
            "## Current Parameter State (your past proposals right now)\n"
            "Use this to avoid re-proposing cooldowned params or re-proposing the same direction "
            "on a rolled-back change.\n\n" + active_adoptions
        )

    # Parameter change history — what worked and what didn't (shown before previous recs)
    param_history = context.get("analysis", {}).get("parameter_history", "")
    if param_history:
        sections.append(f"## Parameter Change History (what worked and what didn't)\n{param_history}")

    # Previous recommendations
    prev = context.get("previous_recommendations", "")
    if prev:
        sections.append(f"## Previous Recommendations (recent cycles)\n{prev}")

    # Baseline Kelly-Sharpe and adoption target — includes noise (JK_SE) so Claude
    # can size its proposals to actually clear the floor.
    baseline_ks = context.get("analysis", {}).get("baseline_kelly_sharpe")
    adoption_target = context.get("analysis", {}).get("adoption_target")
    if baseline_ks is not None:
        ana = context.get("analysis", {})
        jk_se = ana.get("baseline_jk_se")
        abs_floor = ana.get("adoption_abs_floor")
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
                f"Required delta = max(abs_floor={abs_floor:.3f}, 0.25 × SE={0.25*jk_se:.4f}) = **{dyn_floor:.4f}**"
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
        sections.append("\n".join(lines))

    # Cumulative failures — all parameter values tried across all cycles
    cum_failures = context.get("analysis", {}).get("cumulative_failures", {})
    if cum_failures:
        lines = ["## Cumulative Failed Attempts (do NOT repeat these)"]
        for param, attempts in cum_failures.items():
            lines.append(f"- **{param}**: tried {', '.join(attempts[:5])} — all failed")
        sections.append("\n".join(lines))

    # Per-change backtest results from last cycle — exact attribution of what worked/hurt
    per_change = context.get("analysis", {}).get("last_per_change_results", [])
    if per_change:
        lines = ["## Last Cycle Per-Parameter Results (CRITICAL — read before proposing anything)"]
        lines.append("These are the EXACT backtest results for each change you proposed last cycle:")
        for r in per_change:
            lines.append(f"- {r}")
        lines.append("If a change had NEGATIVE delta — it made Sharpe WORSE. Do NOT propose it again.")
        lines.append("If z was low but delta was positive — consider proposing a LARGER change to that parameter.")
        sections.append("\n".join(lines))

    # Platt meta-check: raw model vs current calibrator (surfaced only when close)
    platt_meta = context.get("analysis", {}).get("platt_meta_warning", "")
    if platt_meta:
        sections.append(
            f"## Platt Calibration Meta-Warning\n{platt_meta}\n"
            f"If this persists across cycles, the operator may drop Platt entirely — "
            f"do not propose calibrator-dependent changes assuming Platt is load-bearing."
        )

    # Pipeline track record — did past adoptions actually help?
    track_record = context.get("analysis", {}).get("pipeline_track_record", "")
    if track_record:
        sections.append(track_record)

    # Adoption decay analysis — are changes persisting or fading within 14 days?
    decay_analysis = context.get("analysis", {}).get("decay_analysis", "")
    if decay_analysis:
        sections.append(decay_analysis)

    # Prediction accuracy — how well-calibrated are Claude's own delta predictions?
    pred_accuracy = context.get("analysis", {}).get("prediction_accuracy", "")
    if pred_accuracy:
        sections.append(pred_accuracy)

    # Empirical directional table — replaces hardcoded "test HIGHER" rules
    dir_table = context.get("analysis", {}).get("directional_table", "")
    if dir_table:
        sections.append(dir_table)

    rerouted = context.get("analysis", {}).get("last_rerouted_params", []) or []
    if rerouted:
        unique = list(dict.fromkeys(rerouted))  # preserve order, dedupe
        sections.append(
            "## Last Cycle Rerouting Notice (READ THIS)\n"
            f"Last cycle you put these MANUAL-ONLY params into `changes`: {', '.join(unique)}.\n"
            "They were rerouted to `manual_observations` with confidence=low and the slot in "
            "`changes` was wasted. These params are not backtestable — they will never adopt via "
            "`changes`. If the data still warrants a change to one of them, emit it directly in "
            "`manual_observations` with proper evidence (n>=50, source). Do NOT put them in "
            "`changes` again."
        )

    sections.append(
        "## Your Task\n"
        "Analyze all the data above. Identify patterns, biases, and opportunities for improvement. "
        "If the pipeline track record shows your past recommendations hurt performance, "
        "explain what went wrong and adjust accordingly. "
        "If the decay analysis shows >50% of adoptions are decaying, prioritize an empty or very small changes list. "
        "Return your recommendations as JSON per the format in your instructions."
    )

    return "\n\n".join(sections)
