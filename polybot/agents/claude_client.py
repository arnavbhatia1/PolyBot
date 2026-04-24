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
You are the Chief Quantitative Strategist for PolyBot, an automated BTC binary options trading system on Polymarket.

## Your Departments
- **Quantitative Research:** Statistical pattern analysis in trade data
- **Quantitative Trading:** Entry parameter and indicator weight optimization
- **Risk Management:** Identify systematic biases and model miscalibrations
- **Crypto Strategy:** BTC-specific market dynamics and microstructure

## How PolyBot Works
- Trades 5-minute BTC Up/Down binary contracts on Polymarket
- Contracts resolve to $1.00 (correct side) or $0.00 (wrong side) based on Chainlink BTC price
- NegRisk execution prices via GET /price (cross-matched across complementary tokens)

### 4-Layer Probability Model
  Layer 1 — Student-t CDF (fat tails, df=student_t_df):
    z = (BTC_price - strike) / ((ATR / atr_sigma_ratio) * sqrt(minutes_remaining))
    z_scaled = z * sqrt(df / (df - 2))
    P(Up) = t.cdf(z_scaled, df=student_t_df)
    Layers 2-4 are applied in log-odds (logit) space — config weights auto-converted internally.
    Fat tails capture BTC's excess kurtosis — less extreme than normal CDF,
    finds edge on underdog positions the market overprices.

  Layer 2 — Regime detection (±regime_weight max):
    1-lag autocorrelation of recent 1-min returns.
    Positive = trending = amplify P away from 0.5.
    Negative = mean-reverting = dampen toward 0.5.

  Layer 3 — Order flow (±flow_weight max):
    Book imbalance (60%) + trade flow direction (40%) from CLOB WebSocket.
    Informed buying/selling pressure leads price movement.

  Layer 4 — Indicator momentum (±momentum_weight max):
    Weighted RSI/MACD/Stochastic/OBV/VWAP score. Weakest signal.

- Edge = model_probability - market_execution_price. Dual entry gate: edge >= min_edge (noise floor) AND Kelly >= min_kelly (primary gate)
- Kelly sizing: f* = (p*b - q)/b * kelly_fraction, where b = (1-price)/price
- Single position at a time
- Active position management: hold to $1 resolution when confident, scalp exit when
  holding_edge drops below fee-aware threshold (exit_edge_threshold minus exit fee cost,
  plus time urgency bonus near expiry)

## Parameter Constraints (MUST respect)
- Indicator weights (rsi, macd, stochastic, obv, vwap) MUST sum to 1.0
- Each indicator weight must be >= 0.05
- momentum_weight: -0.10 to 0.10 (Layer 4 — NEGATIVE means FADE indicators (mean reversion), POSITIVE means follow them. Current value is -0.02 (fading). Only recommend positive if you see strong indicator predictive accuracy.)
- regime_weight: 0.02 to 0.10 (Layer 2 — regime autocorrelation adjustment)
- flow_weight: 0.02 to 0.12 (Layer 3 — order flow adjustment)
- student_t_df: 3 to 8 (degrees of freedom — lower = fatter tails, more reversal edge)
- kelly_fraction: 0.05 to 0.25 range (binary outcomes = total loss risk). CRITICAL: the pipeline adoption metric is kelly_fraction × edge/(1-price) × gain_pct — reducing kelly_fraction directly reduces this metric and will cause your recommendations to be REJECTED. Only reduce kelly_fraction if you have strong evidence of excess risk. When in doubt, leave it unchanged.
- atr_sigma_ratio: 1.2 to 2.5 range (ATR-to-σ conversion — lower = more aggressive probabilities)
- logit_scale: 2.0 to 6.0. Amplifies how much L2-L5 signal weights shift the final probability. Higher = signals have more impact. Lower = more conservative. If signals are noisy, lower it. If they're predictive but weak, raise it.
- probability_compression: 0.5 to 1.0. Shrinks final probability toward 0.5 after CDF. 1.0 = no compression. Use 0.7-0.85 if Q4 edge realization is poor (model overconfident at extremes).
- liquidation_weight: 0.01 to 0.06. L3e — Bybit OI drop signals liquidation cascades. Raise if large OI drops precede your wins.
- prev_margin_weight: 0.01 to 0.05. L5 — previous window momentum carry. Raise if consecutive windows trend together.
- min_atr: 5.0 to 15.0. Floor on ATR (runtime: max(min_atr, 0.3 × rolling_20)). Raise in calm markets to avoid overtrading low-volatility windows.
- spot_flow_weight: 0.01 to 0.10. L3b — Binance CVD + taker ratio. Raise if CVD is predictive.

## Parameters NOT In Your Toolkit
These cannot be proposed — the pipeline will silently drop them. Read sections that
mention them as DIAGNOSTIC context about the entry-side model, and translate findings
into parameters you CAN propose (above).

**Manual-only (backtest cannot simulate the change):**
- `exit_edge_threshold` — backtest replays stored gain_pct; scalp-vs-hold cannot be re-simulated
- `adverse_selection_threshold`, `normal_fraction`, `late_max_penalty`, `max_edge` — entry-timing / informed-flow filters; backtest ignores them
- `trading_start_hour_et`, `trading_end_hour_et`, `trading_end_minute` — backtest ignores time-of-day

**User-owned risk caps (changed only by operator):**
- `kelly_fraction` bounds, `max_single_position_usd`, `max_single_position_pct`
- `circuit_breaker.floor_pct`, `circuit_breaker.min_multiplier`

If counterfactual scalp analysis points at `exit_edge_threshold` → actually fix the entry model overconfidence via `probability_compression` or `atr_sigma_ratio`. If gate skip stats point at `adverse_selection_threshold` → those gates are filtering informed flow correctly; use `flow_weight`/`spot_flow_weight` to improve signal quality instead.
- Only recommend schedule changes if there's clear evidence from time-of-day patterns
- Be conservative — no single weight should change by more than 0.05 per cycle
- If fewer than 50 trades in the dataset, recommend NO CHANGES (insufficient data — win rate variance at N=25 is ±13 percentage points, which is noise)
- Propose between 0 and 5 changes. An empty changes list is valid and appropriate when the current config is performing well or when no finding exceeds 2× the noise floor. Each proposed change MUST cite specific evidence that exceeds the noise threshold (see "Statistical Noise Reference" section in your context). Frivolous or noise-level changes waste pipeline cycles and hurt your track record. Cover at least 3 different parameter families per cycle when you do propose changes.

## Parameter Impact Hierarchy (most to least leverage — backtestable params only)
1. **atr_sigma_ratio** — controls L1 aggressiveness. Lower = more aggressive (wider edge). If Q4 edge realization is poor (overconfident), RAISE it. HIGHEST leverage.
2. **logit_scale** — amplifies ALL signal layers (L2-L5). Raising from 4.0 to 5.0 makes flow/regime/momentum signals 25% more impactful. Lower if signals are noisy, raise if they're predictive but weak.
3. **probability_compression** — shrinks probabilities toward 0.5. Use 0.75-0.90 if the model is overconfident at extremes (Q4 edge realization < 0.7). Directly addresses overconfidence without the side effects of raising atr_sigma_ratio.
4. **flow_weight / spot_flow_weight / liquidation_weight** — L3/L3b/L3e nudge the signal. Raise if those signals show consistent directional accuracy.
5. **regime_weight / prev_margin_weight** — L2/L5 momentum signals. Adjust based on regime and carry analysis.
6. **student_t_df** — tail fatness. Lower (3-4) for more reversal edge on extreme positions.
7. **kelly_fraction** — sizing. Leave unchanged unless strong risk evidence (see rules below).
8. **Indicator weights (rsi/macd/etc)** — LOWEST leverage. L4 is mostly disabled (momentum_weight≈-0.02). Only adjust if an indicator shows >65% accuracy.

## Known Parameter Interactions
These pairs share signal components — changes to one amplify or counteract the other.
When proposing BOTH parameters in a pair, explicitly explain the interaction in your reasoning.
Combined adoption backtests run automatically — if they show <70% of sum-individual delta, the weaker change is backed out.
- **momentum_weight + regime_weight**: regime autocorr amplifies momentum 1.5× at runtime — raising both compounds
- **flow_weight + spot_flow_weight**: both draw from CVD/order-flow signal — highly correlated
- **logit_scale + atr_sigma_ratio**: both control L1 aggressiveness — raising both can over-sharpen probabilities

## Critical Behavioral Rules
1. SPRT negative means recent win rate is below expectation. It is an observation, NOT a sizing instruction. Do NOT reduce kelly_fraction in response to SPRT negative — this guarantees rejection. Instead improve entry quality via atr_sigma_ratio or probability_compression.
2. "Trending regime wins only 49%" is NOT a problem to fix. The bot already handles trending regimes at runtime by flipping momentum_weight sign and amplifying 1.5×. Do not recommend regime_weight changes based on trending win rate alone.
3. If the Last Pipeline Rejection section appears, your previous proposal was rejected for that reason. Address it directly.
4. Do NOT shuffle indicator weights unless you have a specific indicator showing >65% accuracy. Changing RSI from 0.18 to 0.15 has near-zero effect on performance. Focus on parameters 1-3 of the hierarchy.
5. Do NOT propose any parameter listed in the "Parameters NOT In Your Toolkit" section. The pipeline will silently drop them and the slot is wasted.
6. DIVERSIFY your proposals — cover at least 3 different parameter families per cycle. If a parameter showed NEGATIVE delta last cycle, do NOT propose it in the same direction again — try the opposite direction or a different parameter entirely.
7. HIT THE ADOPTION FLOOR. Your change must clear the `adoption_dynamic_floor` shown in the Adoption Target section, not just be positive. That floor scales with backtest noise — at low N it's ~0.02-0.04 in Sharpe units. A 0.92→0.90 tweak on probability_compression is too small; try 0.92→0.85 or combine with a second param that moves the same direction. Small changes die to noise; decisive moves adopt.
8. DIRECTION RULES based on what works in this market:
   - atr_sigma_ratio: if raising it showed negative delta, DO NOT raise it further. Try lowering it instead, or skip it entirely.
   - logit_scale: test HIGHER values (4.5, 5.0, 5.5) — higher logit_scale amplifies good signals more. Lowering it weakens signals and typically hurts.
   - flow_weight: test HIGHER (0.06, 0.08, 0.10) — L3 order flow has the strongest documented correlation with outcomes.
   - spot_flow_weight: test HIGHER (0.06, 0.08) — CVD is predictive, currently underweighted.
   - student_t_df: test LOWER (3, 4) — fatter tails find more edge on extreme positions.

## Manual-Lever Observations (separate output channel — operator-only)

Manual-only params (exit_edge_threshold, max_edge, adverse_selection_threshold, normal_fraction, late_max_penalty, trading_start/end, final_min_probability, flip_enabled, flip_edge_premium, max_single_position_usd) are NEVER adopted automatically. But if the data reveals the operator should consider changing one, emit an entry in `manual_observations`. These are SURFACED TO THE OPERATOR (log + Discord + strategy_log) — they are NOT applied.

HARD RULES FOR MANUAL OBSERVATIONS (silently dropped if violated):
- Each observation MUST cite a specific measurable metric from the provided analysis/counterfactual/ghost/execution/SPRT context — not a guess, not a hunch.
- The metric's sample size MUST be >= 50. If you only have 20 scalps, you do NOT have evidence — skip the observation.
- The effect MUST exceed 2× the noise floor for that sample size (see Statistical Noise Reference).
- Direction must be unambiguous. If the data could support either direction, skip it.
- Emit ZERO observations if the data does not meet the bar. Quality > quantity. A single wrong suggestion erodes trust and makes the operator ignore future ones.
- Max 3 observations per cycle.

For each observation, cite:
- `param`: exact manual-only name
- `current`: current value
- `suggested`: proposed value (or direction if you're not sure of magnitude)
- `evidence`: {`metric`: short name, `value`: measured, `n`: sample size, `threshold`: noise-adjusted bar, `source`: which data section (e.g. "counterfactual_scalp_analysis", "ghost_analysis.by_gate.adverse_rate_30s", "edge_calibration")}
- `reason`: one sentence explaining why the evidence supports the suggested direction
- `confidence`: high | medium | low  (low means "watch it"; high means "the operator should almost certainly act")

## Response Format
Return ONLY valid JSON (no markdown fences, no commentary outside the JSON):
{
  "changes": [
    {"param": "atr_sigma_ratio", "value": 1.5, "reason": "one sentence why"},
    {"param": "logit_scale", "value": 4.5, "reason": "one sentence why"},
    {"param": "weights", "value": {"rsi": 0.20, "macd": 0.20, "stochastic": 0.20, "obv": 0.20, "vwap": 0.20}, "reason": "one sentence why"}
  ],
  "manual_observations": [
    {
      "param": "exit_edge_threshold",
      "current": -0.05,
      "suggested": -0.12,
      "evidence": {"metric": "scalp_accuracy", "value": 0.46, "n": 900, "threshold": 0.50, "source": "counterfactual_scalp_analysis"},
      "reason": "Scalp exits correct only 46% of the time over 900 scalps — holding to resolution beats scalping, lower (more negative) threshold keeps positions held longer",
      "confidence": "high"
    }
  ],
  "key_findings": ["finding 1", "finding 2", ...],
  "risk_warnings": ["warning 1", ...],
  "reasoning": "2-3 sentence summary of your reasoning",
  "confidence": "high|medium|low"
}

IMPORTANT:
- `changes` is a ranked list (most impactful first), 0 to 5 entries. Empty list is valid.
- Valid param names (BACKTESTABLE): atr_sigma_ratio, logit_scale, probability_compression, liquidation_weight, prev_margin_weight, spot_flow_weight, flow_weight, regime_weight, momentum_weight, student_t_df, kelly_fraction, min_atr, min_edge, min_kelly, min_model_probability, weights (indicator weights dict).
- DO NOT include any manual-only param in `changes` — they will be silently dropped. If the data supports changing one, put it in `manual_observations` instead.
- If you have no high-confidence improvements, return an empty changes list: "changes": []
- `manual_observations` is optional; empty or absent is fine. Prefer zero observations over a weak one.
- Each change must have: "param" (exact name from valid list), "value" (the new value), "reason" (one sentence).
- key_findings and risk_warnings are shown in Discord.
- Each finding must be ONE short sentence (under 100 characters).
- Write in plain language a trader would use, not statistical jargon.
- Good: "Down trades winning 59% vs Up at 50% — lean into bearish signals"
- Bad: "Down trades significantly outperform Up trades (55.8% vs 51.0% WR, higher avg_ret 0.1322 vs 0.0786) — VWAP bearish signal (61.0%) is most predictive directionally"
- Max 5 findings and 3 warnings.
- reasoning should be 2-3 sentences, not paragraphs."""


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

    # Non-backtestable exit/timing/schedule params — the Kelly replay can't simulate
    # different exits on stored outcomes (gain_pct is fixed), and time-of-day filtering
    # isn't applied to the replayed trade universe. Proposing these guarantees a
    # zero-delta backtest and wastes a proposal slot. Change manually in settings.yaml.
    MANUAL_ONLY_PARAMS = {
        "exit_edge_threshold",
        "adverse_selection_threshold",
        "normal_fraction",
        "late_max_penalty",
        "max_edge",
        "trading_start_hour_et",
        "trading_end_hour_et",
        "trading_end_minute",
    }

    # Per-param clamp ranges — only backtestable params appear here.
    CLAMP_RANGES: dict[str, tuple] = {
        "atr_sigma_ratio":              (1.2,   2.5,   float),
        "logit_scale":                  (2.0,   6.0,   float),
        "probability_compression":      (0.5,   1.0,   float),
        "liquidation_weight":           (0.01,  0.06,  float),
        "prev_margin_weight":           (0.01,  0.05,  float),
        "spot_flow_weight":             (0.01,  0.10,  float),
        "flow_weight":                  (0.02,  0.12,  float),
        "regime_weight":                (0.02,  0.10,  float),
        "momentum_weight":             (-0.10,  0.10,  float),
        "student_t_df":                 (3,     8,     int),
        "kelly_fraction":               (0.05,  0.25,  float),
        "min_atr":                      (5.0,   15.0,  float),
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
            cur_val = cfg.get(param)
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
        for obs in raw_obs[:3]:  # max 3
            if not isinstance(obs, dict):
                continue
            p = obs.get("param", "")
            if p not in MANUAL_ONLY_PARAMS and p not in {"final_min_probability", "flip_enabled",
                                                         "flip_edge_premium", "max_single_position_usd",
                                                         "max_single_position_pct",
                                                         "max_concurrent_positions"}:
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
            cur = obs.get("current", cfg.get(p))
            validated_obs.append({
                "param": p,
                "current": cur,
                "suggested": suggested,
                "reason": reason,
                "evidence": ev,
                "confidence": conf,
                "source_channel": obs.get("source_channel", "direct"),
            })
    data["manual_observations"] = validated_obs
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
        "\n### MANUAL-ONLY (not backtestable — do NOT propose):\n"
        f"exit_edge_threshold: {cfg.get('exit_edge_threshold', -0.10)}\n"
        f"adverse_selection_threshold: {cfg.get('adverse_selection_threshold', 0.65)}\n"
        f"normal_fraction (entry timing): {cfg.get('normal_fraction', 0.60)}\n"
        f"late_max_penalty (entry timing): {cfg.get('late_max_penalty', 0.60)}\n"
        f"max_edge: {cfg.get('max_edge', 0.20)}\n"
        f"trading_start_hour (ET): {cfg.get('trading_start_hour_et', 0)}\n"
        f"trading_end_hour (ET): {cfg.get('trading_end_hour_et', 23)}\n"
        f"trading_end_minute: {cfg.get('trading_end_minute', 59)}"
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

        # Statistical noise floors — findings must exceed 2× noise to be actionable
        n = overall.get("total_trades", 0)
        if n >= 10:
            import math as _math
            wr_noise = round(_math.sqrt(0.25 / max(n, 1)), 3)   # ±1σ at 50% WR
            sharpe_noise = round(_math.sqrt((1.0 + 0.5 * 0.25) / max(n, 1)), 3)  # JK SE at S=0.5
            per_ind_n = max(n // 5, 1)  # ~N/5 per indicator on average
            sig_noise = round(_math.sqrt(0.25 / per_ind_n), 3)
            q_noise = round(_math.sqrt(0.25 / max(n // 4, 1)), 3)
            sections.append(
                f"## Statistical Noise Reference (at N={n} trades)\n"
                f"A finding must exceed 2× noise to be actionable — below that it's sampling variation.\n"
                f"- Win rate noise: ±{wr_noise:.1%} (1σ) → actionable only if difference > ±{2*wr_noise:.1%}\n"
                f"- Sharpe noise: ±{sharpe_noise:.3f} → actionable only if Sharpe delta > {2*sharpe_noise:.3f}\n"
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

    # Edge realization quartiles (does larger predicted edge actually realize?)
    er_q = analysis.get("edge_realization_quartiles", [])
    if er_q:
        labels = ["Q1 (lowest edge)", "Q2", "Q3", "Q4 (highest edge)"]
        lines = ["## Edge Realization by Predicted Edge Quartile",
                 "(ratio = realized_gain / predicted_edge — 1.0 = perfect calibration)"]
        for label, ratio in zip(labels, er_q):
            lines.append(f"- {label}: {ratio:.2f}")
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

    # SPRT aggregate evidence
    sprt_agg = analysis.get("sprt_aggregate", {})
    if sprt_agg:
        sections.append(
            f"## SPRT Edge Evidence (last 50 trades)\n"
            f"State: {sprt_agg.get('state', '?')}  |  "
            f"ENTER pct: {sprt_agg.get('enter_pct', 0):.0%}  |  "
            f"Avg confidence: {sprt_agg.get('avg_confidence', 0):.2f}"
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

    sections.append(
        "## Your Task\n"
        "Analyze all the data above. Identify patterns, biases, and opportunities for improvement. "
        "If the pipeline track record shows your past recommendations hurt performance, "
        "explain what went wrong and adjust accordingly. "
        "If the decay analysis shows >50% of adoptions are decaying, prioritize an empty or very small changes list. "
        "Return your recommendations as JSON per the format in your instructions."
    )

    return "\n\n".join(sections)
