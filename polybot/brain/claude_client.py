import json
import logging
from dataclasses import dataclass
import anthropic

logger = logging.getLogger(__name__)

CONFIDENCE_LEVELS = {"low": 0, "medium": 1, "high": 2}

@dataclass
class MarketAnalysis:
    probability: float
    confidence: str
    reasoning: str
    key_factors: list[str]
    base_rate_considered: bool

    @classmethod
    def from_dict(cls, data: dict) -> "MarketAnalysis":
        prob = data["probability"]
        if not (0.0 <= prob <= 1.0):
            raise ValueError(f"probability must be 0-1, got {prob}")
        conf = data["confidence"]
        if conf not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {list(CONFIDENCE_LEVELS)}, got {conf}")
        return cls(probability=prob, confidence=conf, reasoning=data.get("reasoning", ""),
                   key_factors=data.get("key_factors", []), base_rate_considered=data.get("base_rate_considered", False))

    def passes_gate(self, min_confidence: str, min_probability: float) -> bool:
        conf_level = CONFIDENCE_LEVELS.get(self.confidence, 0)
        min_level = CONFIDENCE_LEVELS.get(min_confidence, 2)
        return conf_level >= min_level and self.probability >= min_probability


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
- Probability model (Brownian motion): z = (BTC_price - strike) / (ATR * sqrt(minutes_remaining))
- P(Up) = 1 / (1 + exp(-1.7 * z))
- 5 momentum indicators (RSI, MACD, Stochastic, OBV, VWAP) nudge the probability:
  P(Up) += weighted_indicator_score * momentum_weight
- Edge = model_probability - market_price. Trade only when edge >= min_edge
- Kelly sizing: f* = (p*b - q)/b * kelly_fraction, where b = (1-price)/price
- Single position at a time. Hold to resolution (no scalping — binary outcome)

## Parameter Constraints (MUST respect)
- Indicator weights (rsi, macd, stochastic, obv, vwap) MUST sum to 1.0
- Each indicator weight must be >= 0.05
- momentum_weight MUST be < min_edge (so indicators alone cannot trigger trades)
- kelly_fraction: 0.05 to 0.25 range (binary outcomes = total loss risk)
- min_edge: 0.05 to 0.25 range
- Be conservative — no single weight should change by more than 0.05 per cycle
- If fewer than 20 trades in the dataset, recommend NO CHANGES (insufficient data)

## Response Format
Return ONLY valid JSON (no markdown fences, no commentary outside the JSON):
{
  "recommended_weights": {"rsi": 0.XX, "macd": 0.XX, "stochastic": 0.XX, "obv": 0.XX, "vwap": 0.XX},
  "recommended_momentum_weight": 0.XX,
  "recommended_min_edge": 0.XX,
  "recommended_kelly_fraction": 0.XX,
  "key_findings": ["finding 1", "finding 2", ...],
  "risk_warnings": ["warning 1", ...],
  "reasoning": "Detailed multi-paragraph analysis of what the data shows and why you recommend these changes...",
  "confidence": "high|medium|low"
}"""


class ClaudeClient:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def analyze_market(self, question: str, price: float, volume: float, liquidity: float,
                             spread: float, days_to_expiry: int, prompt: str) -> MarketAnalysis:
        user_message = (
            f"{prompt}\n\n"
            f"Market Question: {question}\nCurrent YES Price: {price}\n"
            f"24h Volume: ${volume:,.0f}\nLiquidity: ${liquidity:,.0f}\n"
            f"Spread: {spread:.2%}\nDays to Expiry: {days_to_expiry}\n\n"
            "Respond with ONLY valid JSON in this exact format:\n"
            '{"probability": 0.XX, "confidence": "high/medium/low", '
            '"reasoning": "...", "key_factors": ["..."], "base_rate_considered": true/false}')
        response = await self.client.messages.create(
            model=self.model, max_tokens=500,
            messages=[{"role": "user", "content": user_message}])
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return MarketAnalysis.from_dict(data)

    async def analyze_strategy(self, context: dict) -> dict:
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
            max_tokens=1500,
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


def _validate_strategy_response(data: dict, current_weights: dict | None = None,
                                total_trades: int = 0) -> dict:
    """Enforce parameter constraints on Claude's recommendations."""
    indicators = ["rsi", "macd", "stochastic", "obv", "vwap"]

    # Insufficient data — return no changes
    if total_trades < 20 and current_weights:
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
    data["recommended_min_edge"] = max(0.05, min(0.25,
        data.get("recommended_min_edge", 0.10)))
    data["recommended_momentum_weight"] = max(0.02, min(0.20,
        data.get("recommended_momentum_weight", 0.08)))

    # Enforce momentum_weight < min_edge
    if data["recommended_momentum_weight"] >= data["recommended_min_edge"]:
        data["recommended_momentum_weight"] = round(data["recommended_min_edge"] - 0.02, 2)

    return data


def _format_strategy_context(context: dict) -> str:
    """Format context into a structured prompt for Claude."""
    sections = []

    # Current config
    cfg = context.get("current_config", {})
    sections.append(
        "## Current Configuration\n"
        f"Indicator weights: {json.dumps(cfg.get('weights', {}))}\n"
        f"momentum_weight: {cfg.get('momentum_weight', 0.08)}\n"
        f"min_edge (entry_threshold): {cfg.get('min_edge', 0.10)}\n"
        f"kelly_fraction: {cfg.get('kelly_fraction', 0.15)}"
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
                f"Average log return: {overall.get('avg_log_return', 0):.4f}\n"
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
                    f"avg_ret={stats.get('avg_log_return', 0):.4f} n={stats.get('count', 0)}"
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
            lr = t.get("log_return", 0)
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

            lines.append(
                f"#{i} {won} {side} | {entry:.3f}->{exit_:.3f} ret={lr:+.4f} | "
                f"prob={prob:.0%} edge={edge:+.0%} | "
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
