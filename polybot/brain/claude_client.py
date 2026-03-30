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
