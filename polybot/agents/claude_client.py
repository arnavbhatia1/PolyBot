"""ClaudeClient: Anthropic API wrapper for nightly strategy analysis.

Formats the BiasDetector analysis card and recent trade sample into a structured prompt,
calls Claude, validates and clamps the returned recommendations against safe parameter
ranges, and returns a recommendations dict for the WeightOptimizer.
"""
from __future__ import annotations

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
- exit_edge_threshold: -0.25 to 0.0 range (when to exit held positions)
- logit_scale: 2.0 to 6.0. Amplifies how much L2-L5 signal weights shift the final probability. Higher = signals have more impact. Lower = more conservative. If signals are noisy, lower it. If they're predictive but weak, raise it.
- probability_compression: 0.5 to 1.0. Shrinks final probability toward 0.5 after CDF. 1.0 = no compression. Use 0.7-0.85 if Q4 edge realization is poor (model overconfident at extremes).
- liquidation_weight: 0.01 to 0.06. L3e — Bybit OI drop signals liquidation cascades. Raise if large OI drops precede your wins.
- prev_margin_weight: 0.01 to 0.05. L5 — previous window momentum carry. Raise if consecutive windows trend together.
- adverse_selection_threshold: 0.45 to 0.75. Skip entries if 30s post-fill reversal rate exceeds this. Lower = stricter informed-flow filter.
- normal_fraction: 0.40 to 0.80. Fraction of 300s window with full Kelly. After this, late penalty applies to ATM trades.
- late_max_penalty: 0.20 to 0.80. Max Kelly reduction for ATM trades late in window.
- min_atr: 5.0 to 15.0. Floor on ATR (runtime: max(min_atr, 0.3 × rolling_20)). Raise in calm markets to avoid overtrading low-volatility windows.
- max_edge: 0.10 to 0.30. Safety cap — filters suspiciously high edges that may indicate stale prices.
- spot_flow_weight: 0.01 to 0.10. L3b — Binance CVD + taker ratio. Raise if CVD is predictive.
- min_model_probability, min_edge, min_kelly: READ-ONLY for Claude — these entry gates cannot be changed via pipeline recommendations as they corrupt the backtest (changing them alters which trades appear in both baseline and candidate runs). They are shown in Current Configuration for context only.
- Only recommend schedule changes if there's clear evidence from time-of-day patterns
- Be conservative — no single weight should change by more than 0.05 per cycle
- If fewer than 50 trades in the dataset, recommend NO CHANGES (insufficient data — win rate variance at N=25 is ±13 percentage points, which is noise)
- MAX 3 changes per cycle. Rank them by expected impact. If you have fewer than 3 high-confidence improvements, recommend fewer changes — do not pad.

## Parameter Impact Hierarchy (most to least leverage on the adoption metric)
1. **atr_sigma_ratio** — controls how aggressive L1 probability is. Lower = more aggressive (wider edge). If Q4 edge realization is poor (overconfident), raise it. HIGHEST leverage parameter.
2. **logit_scale** — amplifies ALL signal layers (L2-L5). Raising from 4.0 to 5.0 makes flow/regime/momentum signals 25% more impactful. Lower if signals are noisy, raise if they're predictive but weak.
3. **probability_compression** — shrinks probabilities toward 0.5. Use 0.75-0.85 if the model is overconfident at extremes (Q4 edge realization < 0.5).
4. **min_model_probability** — filters out weak trades. Raising eliminates low-confidence losers.
5. **exit_edge_threshold** — scalp vs hold timing. Counterfactual scalp accuracy <50% = tighten (less negative = hold longer). >65% = loosen.
6. **flow_weight / spot_flow_weight / liquidation_weight** — L3/L3b/L3e nudge the signal. Raise if those signals show consistent directional accuracy.
7. **adverse_selection_threshold** — informed flow filter. Lower if post-fill reversals are hurting you.
8. **regime_weight / prev_margin_weight** — L2/L5 momentum signals. Adjust based on regime and carry analysis.
9. **Indicator weights (rsi/macd/etc)** — LOWEST leverage. L4 is mostly disabled (momentum_weight=-0.02). Only adjust if an indicator shows >65% accuracy.

## Critical Behavioral Rules
1. SPRT negative means recent win rate is below expectation. It is an observation, NOT a sizing instruction. Do NOT reduce kelly_fraction in response to SPRT negative — this guarantees rejection. Instead improve entry quality: tighten min_model_probability or raise atr_sigma_ratio.
2. "Trending regime wins only 49%" is NOT a problem to fix. The bot already handles trending regimes at runtime by flipping momentum_weight sign and amplifying 1.5×. Do not recommend regime_weight changes based on trending win rate alone.
3. If the Last Pipeline Rejection section appears, your previous proposal was rejected for that reason. Address it directly.
4. Do NOT shuffle indicator weights unless you have a specific indicator showing >65% accuracy. Changing RSI from 0.18 to 0.15 has near-zero effect on performance. Focus on parameters 1-4 above.
5. CRITICAL — Do NOT raise min_model_probability, min_edge, or min_kelly above their current live values. The validation set was collected under the current gates — raising them filters historical trades out of the backtest, leaving too few candidate trades and causing "only N candidate trades" rejection. These can only safely be LOWERED. If you think entry quality needs improving, use atr_sigma_ratio, logit_scale, or probability_compression instead.

## Response Format
Return ONLY valid JSON (no markdown fences, no commentary outside the JSON):
{
  "changes": [
    {"param": "atr_sigma_ratio", "value": 1.5, "reason": "one sentence why"},
    {"param": "logit_scale", "value": 4.5, "reason": "one sentence why"},
    {"param": "weights", "value": {"rsi": 0.20, "macd": 0.20, "stochastic": 0.20, "obv": 0.20, "vwap": 0.20}, "reason": "one sentence why"}
  ],
  "key_findings": ["finding 1", "finding 2", ...],
  "risk_warnings": ["warning 1", ...],
  "reasoning": "2-3 sentence summary of your reasoning",
  "confidence": "high|medium|low"
}

IMPORTANT:
- `changes` is a ranked list (most impactful first), max 3 entries.
- Valid param names: atr_sigma_ratio, logit_scale, probability_compression, liquidation_weight, prev_margin_weight, spot_flow_weight, flow_weight, regime_weight, momentum_weight, student_t_df, exit_edge_threshold, kelly_fraction, normal_fraction, late_max_penalty, min_atr, max_edge, adverse_selection_threshold, weights (indicator weights dict).
- DO NOT include min_model_probability, min_edge, or min_kelly in changes — they are read-only.
- If you have no high-confidence improvements, return an empty changes list: "changes": []
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

        Args:
            context: Dict with keys: current_config, analysis, trades, previous_recommendations

        Returns:
            Dict with recommended_weights, recommended_momentum_weight,
            recommended_min_edge, recommended_kelly_fraction, key_findings,
            risk_warnings, reasoning, confidence.
        """
        user_message = _format_strategy_context(context)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=STRATEGY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            timeout=180.0,
        )
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
    """Validate and clamp Claude's recommendations.

    Accepts the new `changes` list format. For backward compatibility, also accepts the
    old flat `recommended_X` format and converts it to the new format automatically.

    Returns a dict with:
      - `changes`: list of validated change dicts [{param, value, reason}, ...]
      - `recommended_weights`: extracted from any weights change (for backward compat)
      - metadata: key_findings, risk_warnings, reasoning, confidence
    """
    indicators = ["rsi", "macd", "stochastic", "obv", "vwap"]
    cfg = current_config or {}

    def _cur(key: str, fallback: float) -> float:
        return cfg.get(key, fallback)

    # --- Backward compatibility: convert old flat format to new changes list ---
    if "changes" not in data and any(k.startswith("recommended_") for k in data):
        old_changes = []
        # Map old recommended_X keys to new param names
        param_map = {
            "recommended_atr_sigma_ratio": "atr_sigma_ratio",
            "recommended_logit_scale": "logit_scale",
            "recommended_probability_compression": "probability_compression",
            "recommended_liquidation_weight": "liquidation_weight",
            "recommended_prev_margin_weight": "prev_margin_weight",
            "recommended_spot_flow_weight": "spot_flow_weight",
            "recommended_flow_weight": "flow_weight",
            "recommended_regime_weight": "regime_weight",
            "recommended_momentum_weight": "momentum_weight",
            "recommended_student_t_df": "student_t_df",
            "recommended_exit_edge_threshold": "exit_edge_threshold",
            "recommended_kelly_fraction": "kelly_fraction",
            "recommended_normal_fraction": "normal_fraction",
            "recommended_late_max_penalty": "late_max_penalty",
            "recommended_min_atr": "min_atr",
            "recommended_max_edge": "max_edge",
            "recommended_adverse_selection_threshold": "adverse_selection_threshold",
        }
        for old_key, param in param_map.items():
            if old_key in data:
                old_changes.append({"param": param, "value": data[old_key], "reason": "legacy format"})
        if data.get("recommended_weights"):
            old_changes.append({"param": "weights", "value": data["recommended_weights"], "reason": "legacy format"})
        data["changes"] = old_changes[:3]  # cap at 3

    # Normalize: ensure changes is a list
    if not isinstance(data.get("changes"), list):
        data["changes"] = []

    # Insufficient data guard — drop all changes
    if total_trades < 50:
        data["changes"] = []
        data.setdefault("risk_warnings", []).append(f"Only {total_trades} trades — insufficient data, no changes applied")

    # Read-only gate params: silently drop any Claude attempt to change them
    READ_ONLY_PARAMS = {"min_model_probability", "min_edge", "min_kelly"}

    # Per-param clamp ranges
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
        "exit_edge_threshold":         (-0.25,  0.0,   float),
        "kelly_fraction":               (0.05,  0.25,  float),
        "normal_fraction":              (0.40,  0.80,  float),
        "late_max_penalty":             (0.20,  0.80,  float),
        "min_atr":                      (5.0,   15.0,  float),
        "max_edge":                     (0.10,  0.30,  float),
        "adverse_selection_threshold":  (0.45,  0.75,  float),
    }

    validated_changes: list[dict[str, Any]] = []
    extracted_weights: dict[str, float] = {}

    for change in data["changes"][:3]:  # enforce max 3
        if not isinstance(change, dict):
            continue
        param = change.get("param", "")
        value = change.get("value")
        reason = change.get("reason", "")

        if not param or value is None:
            continue

        # Drop read-only params silently
        if param in READ_ONLY_PARAMS:
            logger.debug(f"Dropping read-only param change: {param}")
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
            extracted_weights = w
            validated_changes.append({"param": "weights", "value": w, "reason": reason})
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
            validated_changes.append({"param": param, "value": clamped, "reason": reason})
        elif param in ("trading_start_hour_et", "trading_end_hour_et"):
            try:
                validated_changes.append({"param": param, "value": max(0, min(23, int(value))), "reason": reason})
            except (TypeError, ValueError):
                continue
        elif param == "trading_end_minute":
            try:
                validated_changes.append({"param": param, "value": max(0, min(59, int(value))), "reason": reason})
            except (TypeError, ValueError):
                continue
        else:
            # Unknown param — skip
            logger.warning(f"Unknown param in changes list: {param!r}")

    data["changes"] = validated_changes

    # Extract recommended_weights for backward compat (scheduler still reads this key)
    if extracted_weights:
        data["recommended_weights"] = extracted_weights
    elif not data.get("recommended_weights") and current_weights:
        # No weights change recommended — leave recommended_weights absent so
        # scheduler's weight adoption gate fires correctly (no weights = skip weight opt)
        pass

    return data


def _format_strategy_context(context: dict[str, Any]) -> str:
    """Format context into a structured prompt for Claude."""
    sections = []

    # Current config
    cfg = context.get("current_config", {})
    sections.append(
        "## Current Configuration\n"
        f"Indicator weights: {json.dumps(cfg.get('weights', {}))}\n"
        f"momentum_weight (Layer 4): {cfg.get('momentum_weight', 0.04)}\n"
        f"regime_weight (Layer 2): {cfg.get('regime_weight', 0.05)}\n"
        f"flow_weight (Layer 3): {cfg.get('flow_weight', 0.06)}\n"
        f"student_t_df (Layer 1): {cfg.get('student_t_df', 4)}\n"
        f"min_edge (entry_threshold): {cfg.get('min_edge', 0.20)}\n"
        f"kelly_fraction: {cfg.get('kelly_fraction', 0.15)}\n"
        f"min_model_probability: {cfg.get('min_model_probability', 0.65)}\n"
        f"exit_edge_threshold: {cfg.get('exit_edge_threshold', -0.10)}\n"
        f"min_kelly (entry gate): {cfg.get('min_kelly', 0.015)}\n"
        f"atr_sigma_ratio: {cfg.get('atr_sigma_ratio', 1.7)}\n"
        f"trading_start_hour (ET): {cfg.get('trading_start_hour_et', 0)}\n"
        f"trading_end_hour (ET): {cfg.get('trading_end_hour_et', 23)}\n"
        f"trading_end_minute: {cfg.get('trading_end_minute', 59)}\n"
        f"logit_scale: {cfg.get('logit_scale', 4.0)}\n"
        f"probability_compression: {cfg.get('probability_compression', 1.0)}\n"
        f"liquidation_weight (L3e): {cfg.get('liquidation_weight', 0.03)}\n"
        f"prev_margin_weight (L5): {cfg.get('prev_margin_weight', 0.02)}\n"
        f"spot_flow_weight (L3b): {cfg.get('spot_flow_weight', 0.04)}\n"
        f"adverse_selection_threshold: {cfg.get('adverse_selection_threshold', 0.65)}\n"
        f"normal_fraction (entry timing): {cfg.get('normal_fraction', 0.60)}\n"
        f"late_max_penalty (entry timing): {cfg.get('late_max_penalty', 0.60)}\n"
        f"min_atr: {cfg.get('min_atr', 8.0)}\n"
        f"max_edge: {cfg.get('max_edge', 0.20)}"
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
            lines = [
                "## Counterfactual Analysis (scalps that resolved)",
                f"Tracks what would have happened if scalped positions were held to resolution.",
                f"Total scalps tracked: {cf.get('total_scalps_tracked', 0)}",
                f"Scalp accuracy: {cf.get('scalp_accuracy', 0):.1%} (% where exiting was actually better than holding)",
                f"Optimal scalps: {cf.get('optimal_scalps', 0)} | Suboptimal: {cf.get('suboptimal_scalps', 0)}",
                f"Avg missed PnL on suboptimal scalps: ${cf.get('avg_missed_pnl', 0):+.2f}",
                f"Avg missed gain_pct on suboptimal scalps: {cf.get('avg_missed_gain_pct', 0):+.4f}",
                f"Avg holding_edge at scalp (optimal): {cf.get('avg_holding_edge_optimal', 0):+.4f}",
                f"Avg holding_edge at scalp (suboptimal): {cf.get('avg_holding_edge_suboptimal', 0):+.4f}",
                f"Avg seconds_remaining (optimal): {cf.get('avg_seconds_remaining_optimal', 0):.0f}s",
                f"Avg seconds_remaining (suboptimal): {cf.get('avg_seconds_remaining_suboptimal', 0):.0f}s",
            ]
            time_acc = cf.get("time_accuracy", {})
            if time_acc:
                lines.append("Scalp accuracy by time remaining:")
                for bucket, stats in time_acc.items():
                    lines.append(f"  - **{bucket}**: accuracy={stats.get('scalp_accuracy', 0):.1%} n={stats.get('count', 0)}")
            lines.append(
                "NOTE: If scalp accuracy is low (<60%), consider tightening exit_edge_threshold "
                "(less negative = hold longer). If high (>80%), current threshold is well-calibrated."
            )
            sections.append("\n".join(lines))

    # Regime breakdown (distilled — more useful than raw trades)
    by_regime = analysis.get("by_regime", {})
    if by_regime:
        lines = ["## Performance by Regime"]
        for regime, stats in by_regime.items():
            lines.append(f"- **{regime}**: n={stats.get('n', 0)} WR={stats.get('win_rate', 0):.0%} "
                        f"avg_edge={stats.get('avg_edge', 0):.1%} avg_gain={stats.get('avg_gain_pct', 0):.4f}")
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

    # Execution quality (fill slippage, realized edge)
    eq = analysis.get("execution_quality", {})
    if eq:
        sections.append(
            f"## Execution Quality\n"
            f"Avg realized edge (model_prob - fill_price): {eq.get('avg_realized_edge', 0):+.3f}\n"
            f"Avg fill slippage (fill - signal_price): {eq.get('avg_fill_slippage', 0):+.4f}\n"
            f"Pct trades with positive slippage (paid more than signal): {eq.get('pct_positive_slippage', 0):.0%}\n"
            f"NOTE: If avg_fill_slippage > 0.005, raise min_edge to compensate for execution costs."
        )

    # Gate skip stats (which entry gates are blocking trades)
    gate_stats = analysis.get("gate_skip_stats", {})
    if gate_stats:
        counts = {k: v for k, v in gate_stats.items() if k != "total_skips" and isinstance(v, (int, float)) and v > 0}
        if counts:
            lines = [f"## Gate Skip Stats (total skips: {gate_stats.get('total_skips', 0)})"]
            lines.append("Which entry gates are blocking the most trades:")
            for gate, count in sorted(counts.items(), key=lambda x: -x[1])[:10]:
                lines.append(f"- **{gate}**: {count}")
            lines.append("NOTE: High counts on adverse_selection or layer_disagreement may mean those gates are too strict.")
            sections.append("\n".join(lines))

    # Ghost trade analysis (downstream gate rejections that resolved profitably)
    ghost = analysis.get("ghost_analysis", {})
    if ghost:
        lines = ["## Ghost Trade Analysis (trades blocked by gates, tracked to resolution)"]
        lines.append(f"Total resolved ghosts: {ghost.get('total_ghosts', 0)} | Profitable if entered: {ghost.get('pct_profitable', 0):.0%}")
        by_gate = ghost.get("by_gate", {})
        if by_gate:
            lines.append("Profitability by gate that blocked the trade:")
            for gate, stats in sorted(by_gate.items(), key=lambda x: -x[1].get('count', 0))[:8]:
                lines.append(f"- **{gate}**: {stats.get('count', 0)} blocked, {stats.get('pct_profitable', 0):.0%} would have been profitable")
        lines.append("NOTE: If a gate blocks >60% profitable trades, it may be over-filtering. Consider loosening that parameter.")
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

    # Parameter change history — what worked and what didn't (shown before previous recs)
    param_history = context.get("analysis", {}).get("parameter_history", "")
    if param_history:
        sections.append(f"## Parameter Change History (what worked and what didn't)\n{param_history}")

    # Previous recommendations
    prev = context.get("previous_recommendations", "")
    if prev:
        sections.append(f"## Previous Recommendations (recent cycles)\n{prev}")

    # Last rejection reason — tell Claude why its previous proposal was rejected
    last_rejection = context.get("analysis", {}).get("last_rejection_reason", "")
    if last_rejection:
        sections.append(
            f"## Last Pipeline Rejection\n"
            f"Your previous recommendations were NOT adopted. Reason: **{last_rejection}**\n"
            f"Adjust your recommendations to address this. If the reason is a negative delta, "
            f"your proposed changes made the backtest WORSE — reconsider those parameters."
        )

    # Pipeline track record — did past adoptions actually help?
    track_record = context.get("analysis", {}).get("pipeline_track_record", "")
    if track_record:
        sections.append(track_record)

    sections.append(
        "## Your Task\n"
        "Analyze all the data above. Identify patterns, biases, and opportunities for improvement. "
        "If the pipeline track record shows your past recommendations hurt performance, "
        "explain what went wrong and adjust accordingly. "
        "Return your recommendations as JSON per the format in your instructions."
    )

    return "\n\n".join(sections)
