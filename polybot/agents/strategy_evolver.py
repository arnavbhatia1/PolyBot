import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class StrategyRecommendation:
    param: str
    current_value: float
    recommended_value: float
    reason: str

class StrategyEvolver:
    def __init__(self, strategy_log_path: str):
        self.strategy_log_path = Path(strategy_log_path)

    def analyze_local(self, outcomes: list[dict], current_config: dict) -> dict:
        if not outcomes:
            return {"win_rate": 0, "avg_log_return": 0, "total_trades": 0}
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = [o.get("log_return", 0) for o in outcomes]
        return {"win_rate": wins / len(outcomes),
                "avg_log_return": sum(returns) / len(returns) if returns else 0,
                "total_trades": len(outcomes)}

    def generate_recommendations(self, analysis: dict, current_config: dict) -> list[StrategyRecommendation]:
        recs = []
        win_rate = analysis.get("win_rate", 0)
        avg_return = analysis.get("avg_log_return", 0)
        if win_rate < 0.50:
            new_ev = min(current_config["ev_threshold"] + 0.03, 0.20)
            recs.append(StrategyRecommendation(param="ev_threshold", current_value=current_config["ev_threshold"],
                recommended_value=round(new_ev, 2), reason=f"Win rate {win_rate:.0%} is below 50%. Raising EV threshold to be more selective."))
        if win_rate < 0.40:
            new_stop = max(current_config["stop_loss_pct"] - 0.03, 0.05)
            recs.append(StrategyRecommendation(param="stop_loss_pct", current_value=current_config["stop_loss_pct"],
                recommended_value=round(new_stop, 2), reason=f"Win rate {win_rate:.0%} very low. Tightening stop loss."))
        if win_rate > 0.70 and avg_return > 0:
            new_ev = max(current_config["ev_threshold"] - 0.01, 0.03)
            recs.append(StrategyRecommendation(param="ev_threshold", current_value=current_config["ev_threshold"],
                recommended_value=round(new_ev, 2), reason=f"Win rate {win_rate:.0%} strong. Lower EV threshold for more trades."))
        if avg_return < 0 and win_rate > 0.50:
            new_exit = max(current_config["exit_target"] - 0.05, 0.75)
            recs.append(StrategyRecommendation(param="exit_target", current_value=current_config["exit_target"],
                recommended_value=round(new_exit, 2), reason="Winning trades but negative returns. Lower exit target."))
        return recs

    def save_log(self, recommendations: list[StrategyRecommendation], analysis: dict):
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        entry = f"\n## {now}\n\n**Analysis:** {analysis}\n\n"
        if recommendations:
            entry += "**Recommendations:**\n"
            for rec in recommendations:
                entry += f"- `{rec.param}`: {rec.current_value} -> {rec.recommended_value} — {rec.reason}\n"
        else:
            entry += "**No recommendations — strategy performing well.**\n"
        existing = self.strategy_log_path.read_text() if self.strategy_log_path.exists() else "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry)
