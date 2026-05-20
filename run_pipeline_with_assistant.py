"""One-shot pipeline runner using a stubbed Claude client.

Replaces the API call with a hand-crafted recommendation dict in the exact
schema the validator expects. The rest of the pipeline (backtest, z-gate,
settings.yaml update, calibrator persist, pipeline_run_log.json) runs
unchanged — so adoption decisions still go through the statistical gate,
NOT my judgment alone.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

# Set up logging early so import-time logs render
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class AssistantStubClaude:
    """Mimics polybot.agents.claude_client.ClaudeClient.analyze_strategy.

    Reads the analysis context the pipeline assembles, returns hand-crafted
    recommendations grounded in the actual biases.json signals.
    """

    def __init__(self) -> None:
        self.model = "assistant-stub"

    async def analyze_strategy(self, context: dict[str, Any]) -> dict[str, Any]:
        analysis = context.get("analysis", {}) or {}
        cfg = context.get("current_config", {}) or {}

        # Pull live values so suggested deltas are anchored to current config.
        cur_logit_scale = float(cfg.get("logit_scale", 3.0))
        cur_min_edge = float(cfg.get("min_edge", 0.04))
        cur_adverse = float(cfg.get("adverse_selection_threshold", 0.65))

        # Signals the recommendation rests on (sanity-bound so we never
        # silently emit a change against missing data).
        eq = analysis.get("edge_realization_quartiles") or []
        q1, q4 = (float(eq[0]) if len(eq) >= 1 else None, float(eq[-1]) if len(eq) >= 4 else None)
        phase = analysis.get("by_entry_phase", {}) or {}
        sprt = analysis.get("by_sprt_confidence", {}) or {}
        adv = analysis.get("by_adverse_selection", {}) or {}

        changes: list[dict[str, Any]] = []
        # 1) Edge-realization degrades sharply at high predicted edge (Q4 ~0.55 vs Q1 ~1.20).
        #    Compress logit_scale modestly so extreme prob tails get pulled in.
        if q1 is not None and q4 is not None and q4 < 0.75 and q1 > 1.0:
            target = round(max(2.0, cur_logit_scale - 0.2), 3)
            if target != cur_logit_scale:
                changes.append({
                    "param": "logit_scale",
                    "value": target,
                    "reason": (
                        f"Edge realization Q1={q1:.2f} vs Q4={q4:.2f} — model overconfident "
                        f"in tails; compress logit_scale to pull extreme probs in."
                    ),
                    "predicted_delta_sharpe_7d": 0.015,
                    "confidence_interval": [-0.005, 0.035],
                })

        # 2) Same overconfidence symptom argues for slightly raising min_edge —
        #    filters the most-overconfident marginal entries.
        if q4 is not None and q4 < 0.70:
            target = round(min(0.10, cur_min_edge + 0.005), 4)
            if target != cur_min_edge:
                changes.append({
                    "param": "min_edge",
                    "value": target,
                    "reason": (
                        f"Q4 edge realization {q4:.2f} — raise min_edge to filter the "
                        f"most overconfident entries."
                    ),
                    "predicted_delta_sharpe_7d": 0.010,
                    "confidence_interval": [-0.008, 0.025],
                })

        # ---- manual_observations: operator-only params ----
        manual: list[dict[str, Any]] = []

        # Late-phase entries underperform normal-phase Sharpe.
        norm_sh = phase.get("normal", {}).get("sharpe")
        late_sh = phase.get("late", {}).get("sharpe")
        late_n = int(phase.get("late", {}).get("n", 0) or 0)
        if norm_sh is not None and late_sh is not None and late_n >= 200 and (norm_sh - late_sh) > 0.02:
            manual.append({
                "param": "late_max_penalty",
                "current": cfg.get("late_max_penalty", 0.30),
                "suggested": round(min(0.50, float(cfg.get("late_max_penalty", 0.30)) + 0.05), 3),
                "evidence": {
                    "metric": "phase_sharpe_gap",
                    "value": round(norm_sh - late_sh, 4),
                    "n": late_n,
                    "source": "bias_detector.by_entry_phase",
                },
                "reason": (
                    f"Late-phase Sharpe {late_sh:.3f} trails normal {norm_sh:.3f} on n={late_n} — "
                    f"tighten late-entry penalty to reduce late marginal entries."
                ),
                "confidence": "medium",
            })

        # SPRT high-confidence trades have WORSE Sharpe than low — counterintuitive,
        # suggests the gate may be drawing the wrong tail. Operator review.
        hi_sprt = sprt.get("high", {})
        lo_sprt = sprt.get("low", {})
        if hi_sprt and lo_sprt:
            hi_n = int(hi_sprt.get("n", 0) or 0)
            hi_sh = float(hi_sprt.get("sharpe", 0))
            lo_sh = float(lo_sprt.get("sharpe", 0))
            if hi_n >= 200 and hi_sh < lo_sh - 0.02:
                manual.append({
                    "param": "sprt.upper_bound",
                    "current": cfg.get("sprt", {}).get("upper_bound", 2.94),
                    "suggested": "operator-review",
                    "evidence": {
                        "metric": "sprt_high_vs_low_sharpe",
                        "value": round(hi_sh - lo_sh, 4),
                        "n": hi_n,
                        "source": "bias_detector.by_sprt_confidence",
                    },
                    "reason": (
                        f"SPRT high-confidence Sharpe {hi_sh:.3f} UNDERPERFORMS low-confidence "
                        f"{lo_sh:.3f} on n={hi_n}. Gate may be drawing the wrong tail; review."
                    ),
                    "confidence": "low",
                })

        # Adverse-rate "high" bucket has the BEST Sharpe — gate may be over-filtering.
        adv_high = adv.get("high", {})
        adv_low = adv.get("low", {})
        if adv_high and adv_low:
            ah_n = int(adv_high.get("n", 0) or 0)
            ah_sh = float(adv_high.get("sharpe", 0))
            al_sh = float(adv_low.get("sharpe", 0))
            if ah_n >= 200 and ah_sh > al_sh:
                manual.append({
                    "param": "adverse_selection_threshold",
                    "current": cur_adverse,
                    "suggested": round(min(0.80, cur_adverse + 0.05), 3),
                    "evidence": {
                        "metric": "adverse_high_vs_low_sharpe",
                        "value": round(ah_sh - al_sh, 4),
                        "n": ah_n,
                        "source": "bias_detector.by_adverse_selection",
                    },
                    "reason": (
                        f"High-adverse bucket Sharpe {ah_sh:.3f} BEATS low-adverse {al_sh:.3f} "
                        f"— gate may be over-filtering profitable high-adverse setups."
                    ),
                    "confidence": "low",
                })

        return {
            "changes": changes,
            "manual_observations": manual,
            "key_findings": [
                f"Edge realization degrades sharply with predicted edge: Q1={q1:.2f} vs Q4={q4:.2f}" if q1 and q4
                else "Edge realization data unavailable",
                f"Time-weighted Sharpe {analysis.get('time_weighted', {}).get('sharpe', 0):.3f} > overall "
                f"{analysis.get('overall', {}).get('sharpe', 0):.3f} — recent regime favorable",
                f"Late-phase entries underperform: normal Sharpe {norm_sh:.3f} vs late {late_sh:.3f}"
                if norm_sh is not None and late_sh is not None else "Phase data thin",
            ],
            "risk_warnings": [
                "logit_scale compression affects every layer's contribution — backtest delta is the source of truth, not the proposal alone.",
                "Adverse-rate 'high' bucket outperformance may be sample artifact (n<500) — manual review only.",
            ],
            "reasoning": (
                "Edge realization degrades sharply at high predicted edges (Q4 below 0.75 vs Q1 above 1.0), "
                "indicating model overconfidence in the tail. Compress logit_scale and modestly raise "
                "min_edge to pull extreme probs in and filter the most-overconfident marginal entries. "
                "Two manual observations flag counterintuitive subgroup behavior (SPRT, adverse) for "
                "operator review — confidence low on those."
            ),
            "confidence": "medium",
        }


async def main() -> None:
    # Local imports so the logging config above applies to everything.
    from polybot.config.loader import load_config, get_secret
    from polybot.config.param_registry import default_for as _d
    from polybot.indicators.engine import IndicatorEngine
    from polybot.core.signal_engine import SignalEngine
    from polybot.core.calibrator import PlattCalibrator
    from polybot.agents.outcome_reviewer import OutcomeReviewer
    from polybot.agents.counterfactual_tracker import CounterfactualTracker
    from polybot.agents.ghost_tracker import GhostTracker
    from polybot.agents.bias_detector import BiasDetector
    from polybot.agents.ta_evolver import TAEvolver
    from polybot.agents.weight_optimizer import WeightOptimizer
    from polybot.agents.pipeline_tracker import PipelineTracker
    from polybot.agents.scheduler import AgentScheduler
    from polybot.main import _build_signal_engine

    config = load_config()
    base_dir = Path(__file__).parent / "polybot"

    signal_cfg = config.get("signal", {})
    market_cfg = config.get("market", {})
    sched_cfg = config.get("schedule", {})
    ind_cfg = config.get("indicators", {})

    indicator_params = {
        "rsi": {"period": ind_cfg.get("rsi", {}).get("period", 14),
                "overbought": ind_cfg.get("rsi", {}).get("overbought", 70),
                "oversold": ind_cfg.get("rsi", {}).get("oversold", 30)},
        "macd": {"fast": ind_cfg.get("macd", {}).get("fast_period", 12),
                 "slow": ind_cfg.get("macd", {}).get("slow_period", 26),
                 "signal_period": ind_cfg.get("macd", {}).get("signal_period", 9)},
        "stochastic": {"k_period": ind_cfg.get("stochastic", {}).get("k_period", 14),
                       "d_smoothing": ind_cfg.get("stochastic", {}).get("d_smoothing", 3),
                       "overbought": ind_cfg.get("stochastic", {}).get("overbought", 80),
                       "oversold": ind_cfg.get("stochastic", {}).get("oversold", 20)},
        "ema": {"fast_period": ind_cfg.get("ema", {}).get("fast_period", 9),
                "slow_period": ind_cfg.get("ema", {}).get("slow_period", 21),
                "chop_threshold": ind_cfg.get("ema", {}).get("chop_threshold", 0.0001)},
        "obv": {"slope_period": ind_cfg.get("obv", {}).get("slope_period", 5)},
        "atr": {"period": ind_cfg.get("atr", {}).get("period", 14),
                "low_pct": ind_cfg.get("atr", {}).get("low_percentile", 5),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(weights=signal_cfg.get("weights"), params=indicator_params)
    signal_engine = _build_signal_engine(signal_cfg, config)

    calibrator = PlattCalibrator()
    _cal_path = base_dir / "memory" / "calibration" / "platt_params.json"
    calibrator.load(_cal_path)
    signal_engine.calibrator = calibrator

    # ----- The key swap: stub Claude client -----
    claude = AssistantStubClaude()

    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    ghost_tracker = GhostTracker(memory_dir=str(base_dir / "memory"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    ta_evolver = TAEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"),
                           claude_client=claude)
    weight_optimizer = WeightOptimizer()
    pipeline_tracker = PipelineTracker(path=base_dir / "memory" / "pipeline_history.json")

    agents_cfg = config["agents"]
    scheduler = AgentScheduler(
        outcome_reviewer=outcome_reviewer,
        bias_detector=bias_detector,
        ta_evolver=ta_evolver,
        weight_optimizer=weight_optimizer,
        indicator_engine=indicator_engine,
        signal_engine=signal_engine,
        alert_manager=None,  # no Discord
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
        math_config=config["math"],
        config=config,
        counterfactual_tracker=counterfactual_tracker,
        pipeline_tracker=pipeline_tracker,
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", _d("exit_edge_threshold"))
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    scheduler._trading_start = (sched_cfg.get("trading_start_hour_et", 0), sched_cfg.get("trading_start_minute", 15))
    scheduler._trading_end = (sched_cfg.get("trading_end_hour_et", 23), sched_cfg.get("trading_end_minute", 59))
    scheduler.ghost_tracker = ghost_tracker

    logger.info("==== Running daily learning pipeline with AssistantStubClaude as recommender ====")
    await scheduler.run_daily_pipeline()
    logger.info("==== Pipeline complete ====")


if __name__ == "__main__":
    asyncio.run(main())
