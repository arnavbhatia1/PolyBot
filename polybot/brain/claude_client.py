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
- kelly_fraction: 0.05 to 0.25 range (binary outcomes = total loss risk)
- min_edge (entry_threshold): 0.01 to 0.10 range (noise floor, not primary gate)
- min_kelly: 0.005 to 0.05 range (Kelly-based entry gate — minimum fraction of bankroll)
- atr_sigma_ratio: 1.2 to 2.5 range (ATR-to-σ conversion — lower = more aggressive probabilities)
- min_model_probability: 0.55 to 0.85 range (skip coin-flip trades)
- exit_edge_threshold: -0.25 to 0.0 range (when to exit held positions)
- min_time_remaining: 0 to 120 seconds (don't enter too late)
- trading_start_hour_et: 0 to 23 (ET hour to start trading)
- trading_end_hour_et: 0 to 23 (ET hour to stop trading)
- trading_end_minute: 0 to 59 (minute component of end time)
- Only recommend schedule changes if there's clear evidence from time-of-day patterns
- Be conservative — no single weight should change by more than 0.05 per cycle
- If fewer than 50 trades in the dataset, recommend NO CHANGES (insufficient data — win rate variance at N=25 is ±13 percentage points, which is noise)

## Response Format
Return ONLY valid JSON (no markdown fences, no commentary outside the JSON):
{
  "recommended_weights": {"rsi": 0.XX, "macd": 0.XX, "stochastic": 0.XX, "obv": 0.XX, "vwap": 0.XX},
  "recommended_momentum_weight": 0.XX,
  "recommended_regime_weight": 0.XX,
  "recommended_flow_weight": 0.XX,
  "recommended_student_t_df": X,
  "recommended_min_edge": 0.XX,
  "recommended_min_kelly": 0.XX,
  "recommended_atr_sigma_ratio": X.X,
  "recommended_kelly_fraction": 0.XX,
  "recommended_min_model_probability": 0.XX,
  "recommended_exit_edge_threshold": -0.XX,
  "recommended_min_time_remaining": XX,
  "recommended_trading_start_hour_et": XX,
  "recommended_trading_end_hour_et": XX,
  "recommended_trading_end_minute": XX,
  "key_findings": ["finding 1", "finding 2", ...],
  "risk_warnings": ["warning 1", ...],
  "reasoning": "2-3 sentence summary of your reasoning",
  "confidence": "high|medium|low"
}

IMPORTANT: key_findings and risk_warnings are shown in Discord.
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
            max_tokens=4096,
            system=STRATEGY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            timeout=120.0,
        )
        text = response.content[0].text.strip()

        data = _extract_json(text)
        current_config = context.get("current_config", {})
        current_weights = current_config.get("weights", {})
        total_trades = context.get("analysis", {}).get("overall", {}).get("total_trades", 0)
        return _validate_strategy_response(data, current_weights, total_trades, current_config)


def _validate_strategy_response(data: dict[str, Any], current_weights: dict[str, float] | None = None,
                                total_trades: int = 0,
                                current_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enforce parameter constraints on Claude's recommendations.

    Uses current_config as defaults so that params Claude omits are not silently
    overwritten with stale hardcoded values.
    """
    indicators = ["rsi", "macd", "stochastic", "obv", "vwap"]
    cfg = current_config or {}

    # Insufficient data — return no changes
    if total_trades < 50 and current_weights:
        data["recommended_weights"] = {k: current_weights.get(k, 0.20) for k in indicators}
        data.setdefault("risk_warnings", []).append(f"Only {total_trades} trades — insufficient data, no changes applied")

    weights = data.get("recommended_weights", {})

    # Floor, renormalize, then final floor (to handle rounding)
    for k in indicators:
        if weights.get(k, 0.05) < 0.05:
            weights[k] = 0.05
    total = sum(weights.get(k, 0.20) for k in indicators)
    if total > 0:
        weights = {k: weights.get(k, 0.20) / total for k in indicators}
    for k in indicators:
        if weights[k] < 0.05:
            weights[k] = 0.05
    largest = max(weights, key=weights.get)
    for k in indicators:
        if k != largest:
            weights[k] = round(weights[k], 4)
    weights[largest] = round(1.0 - sum(v for k, v in weights.items() if k != largest), 4)

    # Enforce max 0.05 change per cycle if we have current weights
    if current_weights:
        changed = False
        for k in indicators:
            old = current_weights.get(k, 0.20)
            new = weights.get(k, old)
            if abs(new - old) > 0.05:
                weights[k] = old + 0.05 * (1 if new > old else -1)
                changed = True
        if changed:
            total = sum(weights[k] for k in indicators)
            if total > 0:
                weights = {k: weights[k] / total for k in indicators}
            largest = max(weights, key=weights.get)
            for k in indicators:
                if k != largest:
                    weights[k] = round(weights[k], 4)
            weights[largest] = round(1.0 - sum(v for k, v in weights.items() if k != largest), 4)

    data["recommended_weights"] = weights

    # Clamp ranges — use current_config values as defaults so omitted params stay unchanged
    def _cur(key: str, fallback: float) -> float:
        return cfg.get(key, fallback)

    # Only write clamped value if Claude explicitly included it — otherwise leave absent
    # so scheduler's "if key in recommendations" guards don't fire for unchanged params.
    def _clamp_if_present(key: str, lo: float, hi: float, cur_val: float) -> None:
        if key in data:
            data[key] = max(lo, min(hi, float(data[key])))
        else:
            data[key] = cur_val  # preserve current value exactly

    _clamp_if_present("recommended_kelly_fraction",    0.05, 0.25, _cur("kelly_fraction", 0.15))
    _clamp_if_present("recommended_min_edge",          0.01, 0.10, _cur("min_edge", 0.04))
    _clamp_if_present("recommended_min_kelly",         0.005, 0.05, _cur("min_kelly", 0.015))
    _clamp_if_present("recommended_atr_sigma_ratio",   1.2,  2.5,  _cur("atr_sigma_ratio", 1.4))
    _clamp_if_present("recommended_momentum_weight",  -0.10, 0.10, _cur("momentum_weight", -0.02))
    _clamp_if_present("recommended_regime_weight",     0.02, 0.10, _cur("regime_weight", 0.03))
    _clamp_if_present("recommended_flow_weight",       0.02, 0.12, _cur("flow_weight", 0.04))
    _clamp_if_present("recommended_min_model_probability", 0.55, 0.85, _cur("min_model_probability", 0.58))
    _clamp_if_present("recommended_exit_edge_threshold", -0.25, 0.0, _cur("exit_edge_threshold", -0.05))
    _clamp_if_present("recommended_min_time_remaining", 0, 120, _cur("min_time_remaining", 0))

    data["recommended_student_t_df"] = max(3, min(8,
        int(data["recommended_student_t_df"]) if "recommended_student_t_df" in data
        else int(_cur("student_t_df", 5))))

    if "recommended_trading_start_hour_et" in data:
        data["recommended_trading_start_hour_et"] = max(0, min(23, int(data["recommended_trading_start_hour_et"])))
    if "recommended_trading_end_hour_et" in data:
        data["recommended_trading_end_hour_et"] = max(0, min(23, int(data["recommended_trading_end_hour_et"])))
    if "recommended_trading_end_minute" in data:
        data["recommended_trading_end_minute"] = max(0, min(59, int(data["recommended_trading_end_minute"])))

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

    # SPRT aggregate evidence
    sprt_agg = analysis.get("sprt_aggregate", {})
    if sprt_agg:
        sections.append(
            f"## SPRT Edge Evidence (last 50 trades)\n"
            f"State: {sprt_agg.get('state', '?')}  |  "
            f"ENTER pct: {sprt_agg.get('enter_pct', 0):.0%}  |  "
            f"Avg confidence: {sprt_agg.get('avg_confidence', 0):.2f}"
        )

    # Recent trades — compact, last 25 only (analysis card above is the real signal)
    trades = context.get("trades", [])
    if trades:
        lines = [f"## Recent Trades (last 25 of {len(trades)} total)"]
        for i, t in enumerate(trades[-25:], 1):
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

    # Previous recommendations
    prev = context.get("previous_recommendations", "")
    if prev:
        sections.append(f"## Previous Recommendations (recent cycles)\n{prev}")

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
