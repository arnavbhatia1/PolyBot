from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)


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
- momentum_weight: 0.02 to 0.10 (Layer 4 — weakest signal, MUST be < min_edge)
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
  "reasoning": "Detailed multi-paragraph analysis of what the data shows and why you recommend these changes...",
  "confidence": "high|medium|low"
}"""


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
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(text)
        current_weights = context.get("current_config", {}).get("weights", {})
        total_trades = context.get("analysis", {}).get("overall", {}).get("total_trades", 0)
        return _validate_strategy_response(data, current_weights, total_trades)


def _validate_strategy_response(data: dict[str, Any], current_weights: dict[str, float] | None = None,
                                total_trades: int = 0) -> dict[str, Any]:
    """Enforce parameter constraints on Claude's recommendations."""
    indicators = ["rsi", "macd", "stochastic", "obv", "vwap"]

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
    # Final floor + redistribute rounding dust to largest weight
    for k in indicators:
        if weights[k] < 0.05:
            weights[k] = 0.05
    largest = max(weights, key=weights.get)
    # Round all except the largest, then set largest to exact remainder
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

    # Clamp ranges
    data["recommended_kelly_fraction"] = max(0.05, min(0.25,
        data.get("recommended_kelly_fraction", 0.15)))
    data["recommended_min_edge"] = max(0.01, min(0.10,
        data.get("recommended_min_edge", 0.03)))
    data["recommended_min_kelly"] = max(0.005, min(0.05,
        data.get("recommended_min_kelly", 0.015)))
    data["recommended_atr_sigma_ratio"] = max(1.2, min(2.5,
        float(data.get("recommended_atr_sigma_ratio", 1.7))))
    data["recommended_momentum_weight"] = max(0.02, min(0.10,
        data.get("recommended_momentum_weight", 0.04)))
    data["recommended_regime_weight"] = max(0.02, min(0.10,
        data.get("recommended_regime_weight", 0.05)))
    data["recommended_flow_weight"] = max(0.02, min(0.12,
        data.get("recommended_flow_weight", 0.06)))
    data["recommended_student_t_df"] = max(3, min(8,
        int(data.get("recommended_student_t_df", 4))))
    data["recommended_min_model_probability"] = max(0.55, min(0.85,
        data.get("recommended_min_model_probability", 0.65)))
    data["recommended_exit_edge_threshold"] = max(-0.25, min(0.0,
        data.get("recommended_exit_edge_threshold", -0.10)))
    data["recommended_min_time_remaining"] = max(0, min(120,
        int(data.get("recommended_min_time_remaining", 0))))
    if "recommended_trading_start_hour_et" in data:
        data["recommended_trading_start_hour_et"] = max(0, min(23,
            int(data.get("recommended_trading_start_hour_et", 0))))
    if "recommended_trading_end_hour_et" in data:
        data["recommended_trading_end_hour_et"] = max(0, min(23,
            int(data.get("recommended_trading_end_hour_et", 23))))
    if "recommended_trading_end_minute" in data:
        data["recommended_trading_end_minute"] = max(0, min(59,
            int(data.get("recommended_trading_end_minute", 59))))

    # Enforce momentum_weight < min_edge
    if data["recommended_momentum_weight"] >= data["recommended_min_edge"]:
        data["recommended_momentum_weight"] = round(data["recommended_min_edge"] - 0.02, 2)

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

    # Recent trades (compact format)
    trades = context.get("trades", [])
    if trades:
        lines = [f"## Recent Trades ({len(trades)} total)"]
        for i, t in enumerate(trades[-75:], 1):
            ctx = t.get("indicator_snapshot", {}).get("trade_context", {})
            snap = t.get("indicator_snapshot", {})
            won = "WIN" if t.get("correct") else "LOSS"
            side = t.get("side", "?")
            entry = t.get("entry_price", 0)
            exit_ = t.get("exit_price", 0)
            lr = t.get("gain_pct", 0)
            prob = ctx.get("model_probability", t.get("signal_score", 0))
            edge = ctx.get("edge", 0)
            btc = ctx.get("btc_price", 0)
            strike = ctx.get("strike_price", 0)
            secs = ctx.get("seconds_remaining", 0)
            atr = ctx.get("atr", snap.get("atr", {}).get("atr", 0))
            rsi = snap.get("rsi", {}).get("score", 0)
            macd_s = snap.get("macd", {}).get("score", 0)
            stoch = snap.get("stochastic", {}).get("score", 0)
            obv_s = snap.get("obv", {}).get("score", 0)
            vwap_s = snap.get("vwap", {}).get("score", 0)

            flow = ctx.get("flow_score", 0)
            exit_reason = t.get("exit_reason", "resolution")
            lines.append(
                f"#{i} {won} {side} ({exit_reason}) | {entry:.3f}->{exit_:.3f} ret={lr:+.4f} | "
                f"prob={prob:.0%} edge={edge:+.0%} flow={flow:+.2f} | "
                f"BTC={btc:,.0f} str={strike:,.0f} {secs:.0f}s atr={atr:.1f} | "
                f"rsi={rsi:+.2f} macd={macd_s:+.2f} stoch={stoch:+.2f} obv={obv_s:+.2f} vwap={vwap_s:+.2f}"
            )
        sections.append("\n".join(lines))

    # Previous recommendations
    prev = context.get("previous_recommendations", "")
    if prev:
        sections.append(f"## Previous Recommendations (recent cycles)\n{prev}")

    sections.append(
        "## Your Task\n"
        "Analyze all the data above. Identify patterns, biases, and opportunities for improvement. "
        "Return your recommendations as JSON per the format in your instructions."
    )

    return "\n\n".join(sections)
