"""Orchestrates the nightly learning pipeline.

Runs BiasDetector, Isotonic calibration (with recency-weighted MLE), TA Evolver,
and WeightOptimizer in sequence. Adopts parameter changes only when they pass:
z = Δ_sharpe / JK_SE >= 0.3 (autocorr adjusted), n >= 100 candidate trades,
fold-consistency floor, regime-stratified Sharpe check, and last-7-day holdout
confirmation. After ≥2 adoptions: one combined backtest on the holdout pool —
if the joint set fails baseline + z-floor × holdout_SE, the whole batch is
backed out (re-evaluated individually next cycle).
"""
from __future__ import annotations

import asyncio
import json
import math
import logging
from datetime import datetime, timezone
from typing import Any
from polybot.config.loader import save_config
from polybot.config.param_registry import default_for as _d
from polybot.core.aux_layers import (compute_spot_flow_signal, regime_vol_factor,
                                      autocorr_vol_scale, combine_flow_family,
                                      student_t_cdf, MIN_STUDENT_T_DF)
from polybot.core.exit_boundary import effective_exit_threshold
from polybot.execution.base import DEFAULT_FEE_RATE
from polybot.paths import (
    CRISIS_STATE_PATH, GATE_STATS_CURRENT_PATH, FILL_STATS_PATH,
    COUNTERFACTUALS_DIR,
)

logger = logging.getLogger(__name__)

def _format_pipeline_summary(pipeline_info: dict[str, Any]) -> str:
    """Human-readable nightly pipeline result — logged and sent to Discord."""
    from polybot.agents.weight_optimizer import ADOPTION_Z_FLOOR
    wi: dict[str, Any] = pipeline_info.get("weights", {}) or {}
    calibration: dict[str, Any] = pipeline_info.get("calibration", {}) or {}
    source = pipeline_info.get("source", "?")
    _now = datetime.now(timezone.utc)
    ts = f"{_now.strftime('%b %d, %Y  %H:%M UTC')}"

    baseline = wi.get("old_sharpe", 0.0) or 0.0
    n_baseline = wi.get("n_baseline_trades", 0) or 0
    per_change = wi.get("per_change", []) or []
    # Mirror the actual gate (z=ADOPTION_Z_FLOOR × JK_SE)
    se_val: float | None = None
    if n_baseline >= 2:
        se_val = math.sqrt((1.0 + 0.5 * baseline * baseline) / n_baseline)
    dyn_floor = ADOPTION_Z_FLOOR * se_val if se_val is not None else 0.0

    manual_obs: list[dict[str, Any]] = pipeline_info.get("manual_observations", []) or []
    adopted = [c for c in per_change if c.get("decision") == "adopted"]
    rejected = [c for c in per_change if c.get("decision") != "adopted"]

    SEP_LIGHT = "─" * 56
    lines: list[str] = []
    lines.append(f"─── Pipeline result — {ts}  (Source: {str(source).upper()}) ───")
    lines.append("")

    # Outcome headline
    if adopted:
        outcome = f"{len(adopted)} of {len(per_change)} proposals"
    elif rejected:
        outcome = f"0 of {len(rejected)} proposals"
    else:
        outcome = "no proposals tested"
    lines.append(f"  Adopted:  {outcome}")
    if manual_obs:
        lines.append(f"            +{len(manual_obs)} manual-only suggestion(s) (see below)")
    lines.append("")

    # Baseline explanation — one line
    if n_baseline > 0:
        lines.append(f"  Baseline: Sharpe {baseline:+.3f} on {n_baseline:,} trades (60-days with recent 7-day holdout)")
    else:
        lines.append("  Baseline: not enough trades yet")
    lines.append(f"  Bar: Must beat baseline by ≥ {dyn_floor:+.3f} Sharpe")
    lines.append("")

    # Proposals
    if per_change:
        lines.append("  Tested proposals:")
        param_w = max((len(str(c.get("param", "?"))) for c in per_change), default=20)
        param_w = min(max(param_w, 18), 26)
        for c in per_change:
            param = str(c.get("param", "?"))
            old_val = c.get("old_value", "?")
            new_val = c.get("value", c.get("new_value", "?"))
            cand_sharpe = c.get("candidate_sharpe")
            delta = (cand_sharpe - baseline) if isinstance(cand_sharpe, (int, float)) else None
            arrow = f"{old_val} → {new_val}"
            is_adopted = c.get("decision") == "adopted"
            mark = "[+]" if is_adopted else "[-]"
            delta_str = f"Δ {delta:+.4f}" if delta is not None else "Δ   n/a   "
            if is_adopted:
                why = "ADOPTED"
            elif delta is not None and delta < 0:
                why = "worse than baseline"
            elif delta is not None and delta < dyn_floor:
                why = "too small to adopt"
            else:
                why = (c.get("reason", "didn't pass gates") or "didn't pass gates")[:36]
            lines.append(f"    {mark} {param:<{param_w}} {arrow:<20} {delta_str}  {why}")
    else:
        reason = wi.get("reason", "no candidates generated")
        lines.append(f"  Tested proposals: (none) — {reason}")
    lines.append("")
    lines.append(SEP_LIGHT)

    # Calibration line
    p_dec = calibration.get("decision", "skipped")
    n_knots = calibration.get("n_knots", 0) or 0
    cur_n_knots = calibration.get("current_n_knots", 0) or 0
    span_min = calibration.get("span_min")
    span_max = calibration.get("span_max")
    reason = (calibration.get("reason", "") or "").strip()

    if p_dec == "adopted" and n_knots > 0:
        if isinstance(span_min, (int, float)) and isinstance(span_max, (int, float)):
            cal_state = f"adopted new isotonic fit  ({n_knots} knots, output range [{span_min:.2f}, {span_max:.2f}])"
        else:
            cal_state = f"adopted new isotonic fit  ({n_knots} knots)"
        lines.append(f"  Calibration: {cal_state}")
    elif p_dec == "reverted":
        lines.append("  Calibration: reverted to identity")
        if reason:
            lines.append(f"               {reason}")
    elif cur_n_knots > 0:
        lines.append(f"  Calibration: unchanged — isotonic with {cur_n_knots} knots still active")
        if reason:
            lines.append(f"               new fit rejected: {reason}")
    elif reason:
        lines.append("  Calibration: identity (no transform)")
        lines.append(f"               new fit rejected: {reason}")
    else:
        lines.append("  Calibration: identity")

    # Holdout line — last 7 days held out from the optimizer as a fresh-data sanity check
    holdout_changes = [c for c in per_change if "holdout_candidate_sharpe" in c]
    holdout_n = pipeline_info.get("holdout_n_trades")
    holdout_active = isinstance(holdout_n, int) and holdout_n >= 30

    if holdout_changes:
        h_base = holdout_changes[0].get("holdout_baseline_sharpe", 0.0)
        passed = sum(1 for c in holdout_changes
                     if c.get("holdout_candidate_sharpe", 0) >= c.get("holdout_baseline_sharpe", 0))
        adopted_after_holdout = sum(1 for c in holdout_changes if c.get("decision") == "adopted")
        n_pool = holdout_n if isinstance(holdout_n, int) else "?"
        lines.append(f"  Holdout:     {passed}/{len(holdout_changes)} candidates cleared the "
                     f"last-7-day fresh-data check")
        tail = f"({adopted_after_holdout} adopted)" if adopted_after_holdout else "(none adopted)"
        lines.append(f"               pool: {n_pool} trades, baseline Sharpe {h_base:+.3f}  {tail}")
    elif holdout_active and rejected:
        lines.append(f"  Holdout:     skipped — all candidates rejected upstream (pool: {holdout_n} trades in last 7d)")
    elif holdout_active:
        lines.append(f"  Holdout:     ready ({holdout_n} trades) — no proposals tested tonight")
    elif holdout_n is not None:
        # "younger than" reason = holdout deliberately disabled (dataset younger than
        # the window), not a data outage — all trades went to training.
        _skip = pipeline_info.get("holdout_skipped_reason", "")
        if "younger than" in _skip:
            lines.append("  Holdout:     inactive — dataset younger than the 7-day window; "
                         "all trades used for training (no fresh-data check yet)")
        else:
            lines.append(f"  Holdout:     inactive — only {holdout_n} trades in last 7 days (need ≥30)")
    else:
        lines.append("  Holdout:     inactive (insufficient recent trades)")

    # Manual-only block (unchanged structure)
    if manual_obs:
        lines.append("")
        lines.append(f"  {'─' * 56}")
        lines.append("  OPERATOR ACTION SUGGESTIONS:")
        for ob in manual_obs:
            p = ob.get("param", "?")
            cur = ob.get("current", "?")
            sug = ob.get("suggested", "?")
            conf = ob.get("confidence", "low").upper()
            reason = (ob.get("reason", "") or "").strip()
            lines.append(f"    {p}  {cur} → {sug}  [{conf}]")
            if reason:
                words = reason.split()
                line_buf, wrapped = [], []
                for w in words:
                    if sum(len(x) + 1 for x in line_buf) + len(w) > 70:
                        wrapped.append("    " + " ".join(line_buf))
                        line_buf = [w]
                    else:
                        line_buf.append(w)
                if line_buf:
                    wrapped.append("    " + " ".join(line_buf))
                lines.extend(wrapped)

    return "\n".join(lines)

from polybot.agents.pipeline_analytics import (
    RECENCY_DECAY_PER_DAY,
    ghost_gain_pct,
    weighted_sharpe_from_returns as _weighted_sharpe,
    sharpe as _sharpe,
)
# Holdout = last HOLDOUT_DAYS. Two calibrators (see run_daily_pipeline + §11): the LIVE
# calibrator fits the freshest _CAL_WINDOW_DAYS for production; the GATE-REFERENCE
# calibrator (self._gate_calibrator) fits the SEPARATE window [HOLDOUT_DAYS,
# HOLDOUT_DAYS + _CAL_WINDOW_DAYS) back — disjoint from the holdout — and is the one the
# weight backtests score through, so the holdout-confirmation gate stays OOS even as the
# live calibrator tracks the freshest data. The gate calibrator is held fixed across
# baseline and candidate within each fold, so the adoption delta stays unbiased.
HOLDOUT_DAYS = 7
_CAL_WINDOW_DAYS = 7
HOLDOUT_MIN_TRADES = 30
# Min trades before the evolver/optimizer run, and the floor the pre-holdout pool must
# clear (below it, the holdout is disabled and the full pool is used).
MIN_TRADES_FOR_LEARNING = 200

def _outcome_ts(o: dict) -> float:
    """Outcome exit_timestamp (fallback: timestamp) as epoch seconds; unparseable -> 0.0.

    Shared by every window boundary (holdout split, 60d cutoff, gate-calibrator
    window, calibration window) so they all parse records identically.
    """
    s = o.get("exit_timestamp", o.get("timestamp", "")) or ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0

class AgentScheduler:
    def __init__(self, outcome_reviewer: Any, bias_detector: Any, ta_evolver: Any, weight_optimizer: Any,
                 indicator_engine: Any = None, signal_engine: Any = None, alert_manager: Any = None,
                 outcome_interval_seconds: int = 3600, daily_pipeline_hour: int = 2,
                 daily_pipeline_minute: int = 0,
                 claude_client: Any = None, market_scanner: Any = None,
                 config: dict[str, Any] | None = None, counterfactual_tracker: Any = None,
                 pipeline_tracker: Any = None) -> None:
        self.outcome_reviewer: Any = outcome_reviewer
        self.bias_detector: Any = bias_detector
        self.ta_evolver: Any = ta_evolver
        self.weight_optimizer: Any = weight_optimizer
        self.indicator_engine: Any = indicator_engine
        self.signal_engine: Any = signal_engine
        self.alert_manager: Any = alert_manager
        self.outcome_interval_seconds: int = outcome_interval_seconds
        self.daily_pipeline_hour: int = daily_pipeline_hour
        self.daily_pipeline_minute: int = daily_pipeline_minute
        self.claude_client: Any = claude_client  # stored for future use + passed to ta_evolver
        self.market_scanner: Any = market_scanner
        self._config: dict[str, Any] | None = config  # Full config dict — written back to settings.yaml after pipeline adoption
        self.counterfactual_tracker: Any = counterfactual_tracker
        self.pipeline_tracker: Any = pipeline_tracker
        self.ghost_tracker: Any = None  # injected by main.py after construction
        self._exit_edge_threshold: float | None = None  # Set by main.py, updated by pipeline
        self._min_time_remaining: int | None = None   # Set by main.py, updated by pipeline
        self._trading_start: tuple[int, int] | None = None        # (hour, minute) ET — updated by pipeline
        self._trading_end: tuple[int, int] | None = None          # (hour, minute) ET — updated by pipeline
        self._running: bool = False
        self._auto_shutdown: bool = False
        self._last_per_change_results: list[str] = []  # per-parameter backtest results for Claude
        self._baseline_kelly_sharpe: float = 0.0  # current baseline Kelly-Sharpe for Claude context
        self._gate_calibrator: Any = None  # OOS reference calibrator for weight backtests (set per cycle)
        self._last_rerouted_params: list[str] = []  # manual-only params Claude tried to put in `changes` last cycle
        self._shutdown_requested: bool = False

        # Inject claude_client into ta_evolver if not already set
        if claude_client and not getattr(self.ta_evolver, 'claude_client', None):
            self.ta_evolver.claude_client = claude_client

    def _invalidate_baseline_cache(self) -> None:
        """Drop the cached baseline Sharpe / JK_SE / N; `_run_weight_optimizer`
        sees the None and recomputes before the per-change z-tests."""
        self._baseline_kelly_sharpe = None
        self._baseline_n_trades = None
        self._baseline_jk_se = None

    async def _run_bias_detector(self, outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if outcomes is None:
            outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes to analyze for biases")
            return {}
        analysis = self.bias_detector.detect(outcomes)
        return analysis

    @staticmethod
    def _resolved_at_to_iso(v: Any) -> str:
        """Convert a ghost's epoch-float ``resolved_at`` to ISO-8601 so it parses like
        every other ``exit_timestamp``. Already-ISO strings pass through unchanged."""
        if v is None or v == "":
            return ""
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(float(v), timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                return ""
        return str(v)

    def _ghost_to_outcome(self, g: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize a resolved ghost (gate-rejected trade) into the outcomes/ shape.

        Ghosts in the backtest pool make entry gates tunable: raising a gate filters
        baseline and candidate identically; lowering one includes ghosts with known
        resolutions. Gain_pct is re-derived fee-aware from market_price_<side>
        (``ghost_gain_pct``) to match real outcomes' net-of-fee ``pnl/size`` — the
        on-disk ghost gain (vs signal_prob, gross) would flatter marginal trades.
        """
        if not g.get("resolved"):
            return None
        side = (g.get("side") or "").lower()
        if side not in ("up", "down"):
            return None
        snap = dict(g.get("indicator_snapshot", {}) or {})
        ctx = dict(snap.get("trade_context", {}) or {})
        if "model_probability_raw" not in ctx and g.get("signal_prob"):
            ctx["model_probability_raw"] = float(g["signal_prob"])
        snap["trade_context"] = ctx
        mp = ctx.get("market_price_up", 0) if side == "up" else ctx.get("market_price_down", 0)
        if not mp or mp <= 0 or mp >= 1:
            logger.debug(
                "ghost dropped: market_id=%s gate=%s mp=%r (pre-Pillar-1 schema)",
                g.get("market_id"), g.get("gate_name"), mp,
            )
            return None
        correct = bool(g.get("ghost_correct"))
        gain_pct = ghost_gain_pct(mp, correct)
        return {
            "side": side,
            "correct": correct,
            "gain_pct": round(gain_pct, 4),
            "indicator_snapshot": snap,
            "entry_price": mp,
            "exit_price": 1.0 if correct else 0.0,
            "exit_timestamp": self._resolved_at_to_iso(g.get("resolved_at")) or str(g.get("timestamp") or ""),
            "timestamp": str(g.get("timestamp") or ""),
            "is_ghost": True,
        }

    def _load_combined_outcomes(self) -> list[dict[str, Any]]:
        """Real outcomes + normalized resolved ghosts, sorted by exit_timestamp."""
        real = self.outcome_reviewer.load_all_outcomes() if self.outcome_reviewer else []
        real = real or []
        ghost_outcomes: list[dict[str, Any]] = []
        gt = getattr(self, 'ghost_tracker', None)
        if gt is not None:
            try:
                for g in gt.load_all():
                    norm = self._ghost_to_outcome(g)
                    if norm is not None:
                        ghost_outcomes.append(norm)
            except Exception as e:
                logger.debug(f"Ghost load failed (non-critical): {e}")
        combined = real + ghost_outcomes
        def _sort_key(o: dict) -> float:
            # Parse to a float timestamp so mixed ISO-8601 formats (e.g., one
            # record missing a trailing Z) still sort chronologically. Failed
            # parses sort to the front (treated as oldest).
            s = str(o.get("exit_timestamp") or o.get("timestamp") or "")
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return 0.0
        combined.sort(key=_sort_key)
        return combined

    @staticmethod
    def _split_holdout(outcomes: list[dict[str, Any]],
                       now_ts: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split outcomes by exit_timestamp: (optimizer_pool, last-HOLDOUT_DAYS pool).

        ``now_ts`` lets the pipeline pass one cycle-wide timestamp so this cutoff
        and the gate-calibrator window share the exact same boundary (no seam where
        a trade lands in both)."""
        if not outcomes:
            return [], []
        if now_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = now_ts - HOLDOUT_DAYS * 86400.0
        opt, hold = [], []
        for o in outcomes:
            (hold if _outcome_ts(o) >= cutoff else opt).append(o)
        return opt, hold

    @staticmethod
    def _calibration_xy(pool: list[dict[str, Any]], now_ts: float | None = None):
        """(P(up), up_won, recency_weight) per resolved trade — the domain the
        calibrator is APPLIED to (signal_engine + replay calibrate P(up)). The bot
        records only the chosen side's prob (``model_probability_raw`` ≥ 0.56), so
        reconstruct raw P(up) from it + ``side`` and the up-outcome from ``side`` +
        ``correct``, making the fit domain match the serve domain across the full
        [0,1] range (Down trades populate P(up) < 0.5).
        """
        if now_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()
        probs: list[float] = []
        outs: list[int] = []
        ws: list[float] = []
        for o in pool:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            # `or` chain: an explicit null in either field falls through instead of
            # crashing the numeric compare below.
            mp = ctx.get("model_probability_raw") or ctx.get("model_probability") or 0.0
            side = str(o.get("side") or "").capitalize()
            if mp <= 0 or side not in ("Up", "Down"):
                continue
            p_up = mp if side == "Up" else 1.0 - mp
            won = bool(o.get("correct", False))
            up_won = 1 if ((side == "Up") == won) else 0
            probs.append(p_up)
            outs.append(up_won)
            s = o.get("exit_timestamp", o.get("timestamp", "")) or ""
            try:
                t = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else now_ts
                ws.append(RECENCY_DECAY_PER_DAY ** max(0.0, (now_ts - t) / 86400.0))
            except Exception:
                ws.append(1.0)
        return probs, outs, ws

    def _fit_calibrator_on(self, pool: list[dict[str, Any]], *, min_samples: int = 75):
        """Fit an IsotonicCalibrator on a pool of resolved outcomes in the P(up)
        serve domain (see ``_calibration_xy``), recency-weighted. Returns the fitted
        calibrator iff ``fit()``'s bootstrap-CI gate passes, else ``None`` (identity).
        Used to build the gate-reference calibrator, which scores weight candidates on
        a window disjoint from the holdout while the live calibrator fits the freshest
        data.
        """
        from polybot.core.calibrator import IsotonicCalibrator
        probs, outs, ws = self._calibration_xy(pool)
        if len(probs) < min_samples:
            return None
        cal = IsotonicCalibrator()
        return cal if cal.fit(probs, outs, min_samples=min_samples, sample_weights=ws) else None

    def _precompute_baseline(self, all_outcomes: list[dict[str, Any]]) -> None:
        """Compute baseline Kelly-Sharpe + JK_SE + N and cache on self BEFORE Claude runs.

        Without this, first-cycle-after-restart Claude context shows `baseline_n_trades=None`
        because the values are only set inside `_run_weight_optimizer` which runs AFTER
        the TA evolver (and thus after context is built). Called once per pipeline cycle.
        """
        from polybot.agents.weight_optimizer import _lag1_autocorr as _ac
        if not all_outcomes or len(all_outcomes) < 10:
            return
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        all_returns: list[float] = []
        all_weights: list[float] = []
        for i in range(len(fold_boundaries) - 1):
            start_idx = int(n * fold_boundaries[i])
            end_idx = int(n * fold_boundaries[i + 1])
            fold_test = all_outcomes[start_idx:end_idx]
            if len(fold_test) < 3:
                continue
            r, w = self._backtest_recommendations({}, fold_test)
            all_returns.extend(r)
            all_weights.extend(w)
        if not all_returns:
            return
        current_sharpe = _weighted_sharpe(all_returns, all_weights)
        self._baseline_kelly_sharpe = round(current_sharpe, 4)
        n_base = len(all_returns)
        # JK_SE uses the weighted Sharpe; autocorr factor operates on the
        # unweighted realized returns (autocorr is what we're correcting for,
        # not the recency weighting).
        base_se = math.sqrt((1.0 + 0.5 * current_sharpe ** 2) / n_base)
        if n_base >= 3:
            rho = _ac(all_returns)
            base_se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
        self._baseline_jk_se = round(base_se, 4)
        self._baseline_n_trades = n_base

    def _directional_old_value(self, param: str) -> Any:
        """Live value of `param` for the directional log's old_value. L6 weights
        live in `signal_engine.derived_weights` and `exit_edge_threshold` is
        scheduler-owned — a plain getattr(signal_engine, param) returns None for
        both, blanking the directional table's first row for those params.
        For `weights` returns the live L4 dict (pre-mutation), which the adoption
        record needs so a flagged weights change can actually be reverted."""
        if not self.signal_engine:
            return None
        if param == "weights":
            if not self.indicator_engine:
                return None
            return {k: v for k, v in self.indicator_engine.get_weights().items()
                    if k in ("rsi", "macd", "stochastic", "obv", "vwap")}
        if param.startswith("derived_") and param.endswith("_weight"):
            return self.signal_engine.derived_weights.get(param[len("derived_"):-len("_weight")])
        if param == "exit_edge_threshold":
            return (self._exit_edge_threshold if self._exit_edge_threshold is not None
                    else _d("exit_edge_threshold"))
        return getattr(self.signal_engine, param, None)

    def _build_current_config(self) -> dict[str, Any]:
        """Snapshot live engine/scheduler param values the recommender dedups
        against. Includes the four L6 derived weights — without them the
        recommender's cfg lookup returns None for an already-on feature, so it
        re-proposes a no-op structural probe (e.g. flow_disagreement 0.005->0.005)
        that wastes a slot under the proposal cap and can crowd out a still-
        unprobed L6 feature."""
        current_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        current_config: dict[str, Any] = {
            "weights": {k: v for k, v in current_weights.items()
                        if k in ["rsi", "macd", "stochastic", "obv", "vwap"]},
            "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
            "regime_weight": getattr(self.signal_engine, 'regime_weight', _d("regime_weight")),
            "flow_weight": getattr(self.signal_engine, 'flow_weight', _d("flow_weight")),
            "student_t_df": getattr(self.signal_engine, 'student_t_df', _d("student_t_df")),
            "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
            "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
            "min_model_probability": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
            "exit_edge_threshold": getattr(self, '_exit_edge_threshold', _d("exit_edge_threshold")),
            "min_time_remaining": getattr(self, '_min_time_remaining', 0),
            "trading_start_hour_et": self._trading_start[0] if self._trading_start else _d("trading_start_hour_et"),
            "trading_end_hour_et": self._trading_end[0] if self._trading_end else _d("trading_end_hour_et"),
            "trading_end_minute": self._trading_end[1] if self._trading_end else _d("trading_end_minute"),
            "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
            "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
            "spot_flow_weight": getattr(self.signal_engine, 'spot_flow_weight', _d("spot_flow_weight")),
            "prev_margin_weight": getattr(self.signal_engine, 'prev_margin_weight', _d("prev_margin_weight")),
            "logit_scale": getattr(self.signal_engine, 'logit_scale', _d("logit_scale")),
            "adverse_selection_threshold": (self._config or {}).get("signal", {}).get("adverse_selection_threshold", _d("adverse_selection_threshold")),
            "normal_fraction": (self._config or {}).get("entry_timing", {}).get("normal_fraction", _d("normal_fraction")),
            "late_max_penalty": (self._config or {}).get("entry_timing", {}).get("late_max_penalty", _d("late_max_penalty")),
            "min_atr": getattr(self.signal_engine, 'min_atr', _d("min_atr")),
            "max_edge": getattr(self.signal_engine, 'max_edge', _d("max_edge")),
        }
        for _name, _w in (getattr(self.signal_engine, "derived_weights", None) or {}).items():
            current_config[f"derived_{_name}_weight"] = _w
        return current_config

    async def _run_ta_evolver(self, analysis: dict[str, Any], outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if outcomes is None:
            outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            return {}

        current_config = self._build_current_config()

        if hasattr(self, '_last_per_change_results') and self._last_per_change_results:
            analysis["last_per_change_results"] = self._last_per_change_results
        if hasattr(self, '_last_rerouted_params') and self._last_rerouted_params:
            analysis["last_rerouted_params"] = list(self._last_rerouted_params)

        # Adoption gate is a pure z-test (delta_sharpe / JK_SE >= ADOPTION_Z_FLOOR).
        # Surface baseline Sharpe and SE so Claude can size proposals to clear it.
        if hasattr(self, '_baseline_kelly_sharpe'):
            from polybot.agents.weight_optimizer import ADOPTION_Z_FLOOR
            analysis["baseline_kelly_sharpe"] = self._baseline_kelly_sharpe
            jk_se = getattr(self, '_baseline_jk_se', None)
            if jk_se is not None:
                dyn_floor = ADOPTION_Z_FLOOR * jk_se
                analysis["baseline_jk_se"] = jk_se
                analysis["baseline_n_trades"] = getattr(self, '_baseline_n_trades', None)
                analysis["adoption_z_floor"] = ADOPTION_Z_FLOOR
                analysis["adoption_dynamic_floor"] = round(dyn_floor, 4)
                analysis["adoption_target"] = round(self._baseline_kelly_sharpe + dyn_floor, 4)
        # Cumulative failures derived from pipeline_run_log.json (restart-safe, no duplicate state)
        if self.pipeline_tracker:
            try:
                cum = self.pipeline_tracker.get_cumulative_failures()
                if cum:
                    analysis["cumulative_failures"] = cum
            except Exception as e:
                logger.debug(f"Failed to derive cumulative failures: {e}")

        # Inject prediction accuracy, empirical directional table, and decay analysis
        if self.pipeline_tracker:
            try:
                pred_accuracy = self.pipeline_tracker.format_prediction_accuracy()
                if pred_accuracy:
                    analysis["prediction_accuracy"] = pred_accuracy
                dir_table = self.pipeline_tracker.format_directional_table()
                if dir_table:
                    analysis["directional_table"] = dir_table
                decay_analysis = self.pipeline_tracker.format_decay_analysis()
                if decay_analysis:
                    analysis["decay_analysis"] = decay_analysis
                recently_tested = self.pipeline_tracker.get_recently_tested_params(n_cycles=3)
                if recently_tested:
                    analysis["recently_tested_params"] = list(recently_tested)
            except Exception as e:
                logger.debug(f"Failed to build prediction/directional/decay context: {e}")

        # Build parameter change history for Claude
        if self.pipeline_tracker:
            try:
                history_lines = []
                records = self.pipeline_tracker.get_track_record()
                # Last 5 adoptions with Sharpe outcome
                adoptions = [r for r in records if r.get("changes")]
                for rec in adoptions[-5:]:
                    date = rec.get("date", "?")[:10]
                    baseline = rec.get("baseline_sharpe", 0)
                    predicted = rec.get("predicted_sharpe", 0)
                    changes_dict = rec.get("changes", {})
                    r7 = rec.get("review_7d")
                    actual_str = f"7d actual Sharpe={r7['sharpe']:.3f}" if r7 else "7d: pending"
                    change_strs = [f"{k}: {v[0]}->{v[1]}" for k, v in list(changes_dict.items())[:4]]
                    history_lines.append(
                        f"ADOPTED {date}: {', '.join(change_strs)} | "
                        f"predicted {baseline:.3f}->{predicted:.3f} | {actual_str}"
                    )

                # Rollback-recommended adoptions (7d review showed the change hurt)
                rollback_recs = [r for r in records if r.get("rollback_recommended")]
                for rec in rollback_recs[-3:]:
                    date = rec.get("date", "?")[:10]
                    r7 = rec.get("review_7d", {})
                    baseline = rec.get("baseline_sharpe", 0)
                    history_lines.append(
                        f"ROLLBACK {date}: 7d Sharpe={r7.get('sharpe', 0):.3f} trailed baseline {baseline:.3f} — "
                        f"changes: {', '.join(f'{k}: {v[0]}->{v[1]}' for k, v in list(rec.get('changes', {}).items())[:4])}"
                    )

                if history_lines:
                    analysis["parameter_history"] = "\n".join(history_lines)
            except Exception as e:
                logger.debug(f"Failed to build parameter history: {e}")

        # Active adoptions table — which past proposals are currently LIVE or ROLLED_BACK.
        # Prevents Claude wasting proposal slots on reversed params because it didn't know.
        if self.pipeline_tracker:
            try:
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
                active_lines: list[str] = []
                rolled_lines: list[str] = []
                records = self.pipeline_tracker.get_track_record()
                for rec in records:
                    try:
                        rec_dt = _dt.fromisoformat(rec.get("date", ""))
                    except (ValueError, TypeError):
                        continue
                    age_days = (now - rec_dt).total_seconds() / 86400.0
                    if age_days > 30:
                        continue
                    for param, (old_val, new_val) in (rec.get("changes") or {}).items():
                        # Look up the current live value from the built current_config
                        if param == "weights":
                            continue  # multi-value; skip from this compact table
                        cur = current_config.get(param)
                        r7 = rec.get("review_7d") or {}
                        pred = rec.get("predicted_sharpe", 0)
                        baseline = rec.get("baseline_sharpe", 0)
                        pred_delta = round(pred - baseline, 3)
                        actual_delta = round(r7.get("sharpe", 0) - baseline, 3) if r7 else None
                        actual_str = f"7d_actual={actual_delta:+.3f}" if actual_delta is not None else "7d=pending"
                        # Rolled back = current live value no longer matches what was adopted
                        try:
                            drifted = (cur is None) or (abs(float(cur) - float(new_val)) > 1e-6)
                        except (TypeError, ValueError):
                            drifted = (cur != new_val)
                        if drifted:
                            rolled_lines.append(
                                f"  {param}: was adopted {old_val}->{new_val} {age_days:.0f}d ago, "
                                f"now at {cur} — {actual_str} vs pred={pred_delta:+.3f}"
                            )
                        else:
                            active_lines.append(
                                f"  {param}: {old_val}->{new_val} (adopted {age_days:.0f}d ago) "
                                f"{actual_str} vs pred={pred_delta:+.3f} — LIVE"
                            )
                sections_out: list[str] = []
                if active_lines:
                    sections_out.append("ACTIVE ADOPTIONS (last 30 days, currently LIVE):\n" + "\n".join(active_lines))
                if rolled_lines:
                    sections_out.append("ROLLED BACK (adopted value no longer live):\n" + "\n".join(rolled_lines))
                if sections_out:
                    analysis["active_adoptions"] = "\n\n".join(sections_out)
            except Exception as e:
                logger.debug(f"Failed to build active adoptions table: {e}")

        recommendations = await self.ta_evolver.evolve(outcomes, analysis, current_config)
        # Capture which manual-only params Claude tried to propose in `changes` this cycle
        # (the validator rerouted them to manual_observations). Surfaced next cycle so
        # Claude sees the misclassification and stops repeating it.
        rerouted = [
            o.get("param", "") for o in (recommendations.get("manual_observations") or [])
            if isinstance(o, dict) and o.get("source_channel") == "rerouted"
        ]
        self._last_rerouted_params = [p for p in rerouted if p]
        return recommendations

    def _kelly_bankroll_returns(
        self,
        outcomes: list[dict[str, Any]],
        recommended_weights: dict[str, float],
        momentum_weight: float,
        atr_sigma_ratio: float,
        student_t_df: int,
        min_edge: float,
        calibrator: Any,
        kelly_fraction: float,
        min_kelly: float,
        min_prob: float,
        regime_weight: float | None = None,
        flow_weight: float | None = None,
        spot_flow_weight: float | None = None,
        prev_margin_weight: float | None = None,
        logit_scale: float | None = None,
        min_atr: float | None = None,
        # Promoted structural constants — mirror live so baseline/candidate Sharpe stays aligned.
        regime_momentum_threshold: float | None = None,
        final_logit_clamp: float | None = None,
        l5_regime_damp_cap: float | None = None,
        atr_regime_shift_threshold: float | None = None,
        # L6 weights — default 0.0 keeps backtest inert until pipeline raises one (matches live).
        derived_weights: dict[str, float] | None = None,
        # When set, the replay re-decides recorded exits in BOTH directions
        # (judged against the blended effective threshold live fires on, never the
        # raw value): scalps the candidate would NOT have fired reprice to their
        # hold-to-resolution counterfactual (loss-cut closes excluded), and holds
        # the candidate WOULD have fired at the recorded worst moment reprice to
        # their hypothetical-scalp counterfactual (whipsaw-cushion and
        # deep-loss-hold branches respected — they hold regardless of threshold).
        exit_threshold_override: float | None = None,
        counterfactual_index: dict[int, dict[str, Any]] | None = None,
        hold_counterfactual_index: dict[int, dict[str, Any]] | None = None,
    ) -> tuple[list[float], list[float]]:
        """Replay the full logit composition used in production for a candidate
        config and return the Kelly-sized per-trade returns. Sharpe of the result
        is the candidate's adoption metric.
        """
        # Optional defaults resolve from the registry — lockstep with param_registry.
        if regime_weight is None: regime_weight = _d("regime_weight")
        if flow_weight is None: flow_weight = _d("flow_weight")
        if spot_flow_weight is None: spot_flow_weight = _d("spot_flow_weight")
        if prev_margin_weight is None: prev_margin_weight = _d("prev_margin_weight")
        if logit_scale is None: logit_scale = _d("logit_scale")
        if min_atr is None: min_atr = _d("min_atr")
        if regime_momentum_threshold is None: regime_momentum_threshold = _d("regime_momentum_threshold")
        if final_logit_clamp is None: final_logit_clamp = _d("final_logit_clamp")
        if l5_regime_damp_cap is None: l5_regime_damp_cap = _d("l5_regime_damp_cap")
        if atr_regime_shift_threshold is None: atr_regime_shift_threshold = _d("atr_regime_shift_threshold")
        from polybot.core.derived_features import DERIVED_FEATURES, FeatureContext, L6_LOGIT_CAP
        # Constants shared with live via single import — if signal_engine.py changes
        # these, the backtest moves too.
        from polybot.core.signal_engine import (
            _ATR_HISTORY_MIN_SAMPLES as _ATR_MIN_SHORT,
            _ATR_LONG_TERM_MIN_SAMPLES as _ATR_MIN_LONG,
            _ATR_FLOOR_FRACTION as _ATR_FLOOR_FRAC,
            _REGIME_MOMENTUM_DAMPEN as _L4_DAMPEN,
            _REGIME_MOMENTUM_AMPLIFY as _L4_AMPLIFY,
        )
        _dw_in = derived_weights or {}
        l6_weights: dict[str, float] = {
            name: float(_dw_in.get(name, _d(f"derived_{name}_weight")))
            for name in DERIVED_FEATURES.keys()
        }
        l6_active = any(w != 0.0 for w in l6_weights.values())
        # Rolling ATR history mirrors signal_engine._record_atr (sized 20/200).
        # Outcomes arrive sorted by exit_timestamp (see _get_outcomes_for_pipeline),
        # so this rolling state is causal at each tick — and is used both by L1's
        # dynamic floor (mirroring _effective_atr_floor) and by L6 features.
        from collections import deque as _deque
        _atr_short = _deque(maxlen=20)
        _atr_long = _deque(maxlen=200)
        _atr_short_sum = 0.0
        _atr_long_sum = 0.0

        realism_factor = 1.0
        if self._config:
            realism_factor = float(self._config.get("execution", {}).get("backtest_realism_factor", 1.0))
        # Recency weights returned alongside returns so callers compute a proper
        # weighted Sharpe — never multiplied into the returns (would bias variance).
        now_ts = datetime.now(timezone.utc).timestamp()
        returns: list[float] = []
        sample_weights: list[float] = []

        for o in outcomes:
            snap = o.get("indicator_snapshot", {})
            if not snap:
                continue
            ctx = snap.get("trade_context", {})

            stored_raw = ctx.get("model_probability_raw") or ctx.get("model_probability") or 0.0
            if stored_raw <= 0 or stored_raw >= 1:
                continue

            side = (o.get("side") or "").lower()
            if side not in ("up", "down"):
                continue

            market_price_side = ctx.get("market_price_up", 0) if side == "up" else ctx.get("market_price_down", 0)
            if market_price_side <= 0 or market_price_side >= 1:
                continue

            # L1 — re-derive raw prob_up from the CDF. Rows missing L1 inputs or with
            # a dead ATR are skipped in BOTH arms: `stored_raw` embeds live's full
            # CHOSEN-SIDE stack (replaying from it would invert Down rows and
            # double-count L2-L6), and the non-tunable ATR gate blocks such trades live.
            btc = ctx.get("btc_price") or 0
            strike = ctx.get("strike_price") or 0
            atr_raw = ctx.get("atr") or 0
            secs = ctx.get("seconds_remaining") or 0
            if btc <= 0 or strike <= 0 or secs <= 0 or atr_raw <= 0:
                continue

            # Update rolling ATR state BEFORE the floor is read — mirrors
            # SignalEngine._record_atr → _effective_atr_floor ordering exactly.
            # Used by L1's dynamic floor and (if active) L6 features below.
            if atr_raw > 0:
                if len(_atr_short) == _atr_short.maxlen:
                    _atr_short_sum -= _atr_short[0]
                _atr_short.append(atr_raw)
                _atr_short_sum += atr_raw
                if len(_atr_long) == _atr_long.maxlen:
                    _atr_long_sum -= _atr_long[0]
                _atr_long.append(atr_raw)
                _atr_long_sum += atr_raw

            # Dynamic ATR floor mirrors signal_engine._effective_atr_floor:
            # base = max(min_atr, FLOOR_FRAC × rolling_20); widened when rolling/
            # long-term ratio falls below atr_regime_shift_threshold.
            _n_short = len(_atr_short)
            if _n_short >= _ATR_MIN_SHORT:
                _rolling_short = _atr_short_sum / _n_short
                _base_floor = max(min_atr, _ATR_FLOOR_FRAC * _rolling_short)
                _n_long = len(_atr_long)
                if _n_long >= _ATR_MIN_LONG:
                    _rolling_long = _atr_long_sum / _n_long
                    if (_rolling_long > 0
                            and _rolling_short / _rolling_long < atr_regime_shift_threshold):
                        _regime_floor = _rolling_long * atr_regime_shift_threshold * _ATR_FLOOR_FRAC
                        _base_floor = max(_base_floor, _regime_floor)
                atr_effective = max(atr_raw, _base_floor)
            else:
                # Pre-warmup: fall back to static floor (same as live).
                atr_effective = max(atr_raw, min_atr)
            atr = atr_effective

            # Regime (lag-1 autocorr) feeds L1 vol scaling + L2/L4/L5. Stored float
            # when available (exact); regime_state string approximation for old rows.
            stored_autocorr = ctx.get("regime_autocorr")
            if stored_autocorr is not None:
                regime_factor = float(stored_autocorr)
            else:
                regime_str = (ctx.get("regime_state") or "").lower()
                if regime_str.startswith("trending"):
                    regime_factor = 0.20
                elif regime_str.startswith("mean"):
                    regime_factor = -0.20
                else:
                    regime_factor = 0.0

            # df clamped ≥3 via the shared MIN_STUDENT_T_DF — replay identical to live.
            df_eff = max(MIN_STUDENT_T_DF, student_t_df)
            minutes = max(secs / 60.0, 0.01)  # same floor as live
            vol = (atr / atr_sigma_ratio) * math.sqrt(minutes) * autocorr_vol_scale(regime_factor)
            z = ((btc - strike) / vol) * math.sqrt(df_eff / (df_eff - 2))
            raw_prob_up = max(1e-6, min(1 - 1e-6, student_t_cdf(z, df_eff)))
            logit_p = math.log(raw_prob_up / (1.0 - raw_prob_up))

            # L2 — regime × direction (regime_factor computed above for L1 vol scaling).
            prev_margin = ctx.get("prev_resolution_margin", 0.0)
            # Direction: stamped last-1min-return sign when present; noisy
            # sign(prev_resolution_margin) proxy for rows recorded before the field.
            stored_direction = ctx.get("regime_direction")
            if stored_direction is not None:
                direction = float(stored_direction)
            else:
                direction = 1.0 if prev_margin > 0 else (-1.0 if prev_margin < 0 else 0.0)
            logit_p += regime_factor * direction * (regime_weight * logit_scale)

            # L3 + L3b — recompute from stamped aux signals with the same
            # vol/price-relative normalization + redundancy combine live uses (shared
            # via aux_layers); fall back to stored values for rows lacking raw aux.
            _vol_factor = regime_vol_factor(atr_raw, ctx.get("atr_long_term_mean"))
            # flow_score/spot_flow_signal are recorded None when the feed was cold;
            # coerce explicitly to 0.0 exactly as live does before feeding the logit.
            # (.get(..., 0.0) returns the present None and combine_flow_family would
            # crash on None*weight.)
            _fs = ctx.get("flow_score")
            flow_signal = 0.0 if _fs is None else _fs
            if ctx.get("coinbase_cvd_60s") is not None:
                spot_flow = compute_spot_flow_signal(
                    ctx.get("coinbase_cvd_60s"),
                    ctx.get("coinbase_taker_60s"),
                    ctx.get("coinbase_taker_n", 0),
                    vol_factor=_vol_factor,
                )
            else:
                _sf = ctx.get("spot_flow_signal")
                spot_flow = 0.0 if _sf is None else _sf
            logit_p += combine_flow_family(
                flow_signal * (flow_weight * logit_scale),
                spot_flow * (spot_flow_weight * logit_scale),
            )

            # L5 — previous-window margin carry (tanh-normalized by ATR).
            # Live applies a (1 - min(l5_regime_damp_cap, |regime|)) dampener to
            # orthogonalize with L2 early in a window; backtest must mirror or it
            # will over-credit prev_margin_weight in strong-regime samples.
            if prev_margin != 0.0 and atr_raw > 0:
                normalized = prev_margin / max(atr_raw, 1.0)
                l5_damp = 1.0 - min(l5_regime_damp_cap, abs(regime_factor))
                logit_p += math.tanh(normalized) * (prev_margin_weight * logit_scale) * l5_damp

            # L4 — indicator committee. Mirrors live `compute_momentum` +
            # `effective_momentum_weight` exactly: smooth tanh(autocorr/threshold)
            # regime conditioning, direction-aware mean-revert flip in trend regime,
            # and smooth magnitude scaling between DAMPEN (0.5×) and AMPLIFY (1.5×).
            def _ind_score(name: str) -> float:
                return snap.get(name, {}).get("score", 0)
            mean_revert_score = (
                _ind_score("rsi") * recommended_weights.get("rsi", 0)
                + _ind_score("stochastic") * recommended_weights.get("stochastic", 0)
                + _ind_score("vwap") * recommended_weights.get("vwap", 0)
            )
            trend_confirm_score = (
                _ind_score("macd") * recommended_weights.get("macd", 0)
                + _ind_score("obv") * recommended_weights.get("obv", 0)
            )
            _t = math.tanh(regime_factor / regime_momentum_threshold) if regime_momentum_threshold > 0 else 0.0
            _t_pos = max(0.0, _t)
            _contrarian_mult = (1.0 - _t) * _L4_DAMPEN
            _tc_mult = _L4_DAMPEN + (1.0 - _L4_DAMPEN) * _t_pos
            momentum_score = (mean_revert_score * _contrarian_mult
                              + abs(mean_revert_score) * direction * _t_pos
                              + _tc_mult * trend_confirm_score)
            momentum_score = max(-1.0, min(1.0, momentum_score))
            _t_abs = abs(_t)
            eff_mw = abs(momentum_weight) * (_L4_DAMPEN + (_L4_AMPLIFY - _L4_DAMPEN) * _t_abs)
            logit_p += momentum_score * eff_mw * logit_scale

            # L6 — derived features. ATR rolling state was updated at L1 above,
            # so short/long means here are causal-through-this-tick (matches live).
            if l6_active and atr_raw > 0:
                atr_short_mean = ctx.get("atr_rolling_20")
                if atr_short_mean is None:
                    atr_short_mean = _atr_short_sum / len(_atr_short) if _atr_short else 0.0
                atr_long_mean = ctx.get("atr_long_term_mean")
                if atr_long_mean is None:
                    atr_long_mean = _atr_long_sum / len(_atr_long) if _atr_long else 0.0
                # `last_return` matches live: prefer the stamped `btc_price`
                # (Coinbase WS) over `closes_tail[-1]` (Binance partial kline) so
                # the L6 autocorr_signed_mag replay mirrors signal_engine exactly.
                _closes_tail = snap.get("closes_tail") or ctx.get("closes_tail")
                _ref_price = ctx.get("btc_price")
                if _ref_price is None and _closes_tail:
                    _ref_price = _closes_tail[-1]
                if _ref_price is not None and _closes_tail and len(_closes_tail) >= 2 and float(_closes_tail[-2]) != 0.0:
                    _last_return = (float(_ref_price) - float(_closes_tail[-2])) / float(_closes_tail[-2])
                else:
                    _last_return = 0.0
                fctx = FeatureContext(
                    atr=atr_raw,
                    atr_rolling_20=atr_short_mean,
                    atr_long_term_mean=atr_long_mean,
                    regime=regime_factor,
                    last_return=_last_return,
                    flow_signal=flow_signal,
                    spot_flow_signal=spot_flow,
                    prev_resolution_margin=prev_margin,
                    seconds_remaining=secs,
                    distance=(btc - strike),
                )
                l6_total = 0.0
                for _name, _fn in DERIVED_FEATURES.items():
                    _w = l6_weights[_name]
                    if _w == 0.0:
                        continue
                    l6_total += _fn(fctx) * (_w * logit_scale)
                if l6_total > L6_LOGIT_CAP:
                    l6_total = L6_LOGIT_CAP
                elif l6_total < -L6_LOGIT_CAP:
                    l6_total = -L6_LOGIT_CAP
                logit_p += l6_total

            # Final clamp — mirrors live (signal_engine: max(-clamp, min(clamp, logit_p))).
            logit_p = max(-final_logit_clamp, min(final_logit_clamp, logit_p))

            prob_up_adj = 1.0 / (1.0 + math.exp(-logit_p))
            if calibrator is not None and hasattr(calibrator, "calibrate"):
                calibrated_up = calibrator.calibrate(prob_up_adj)
            else:
                calibrated_up = prob_up_adj

            if side == "up":
                prob_side = calibrated_up
            else:
                prob_side = 1.0 - calibrated_up
            edge = prob_side - market_price_side

            if edge < min_edge:
                continue
            if prob_side < min_prob:
                continue
            # Fee-aware Kelly — mirrors live SignalEngine._kelly EXACTLY (net_b =
            # b*(1-fee)) so the backtest sizes the same trades live would;
            # DEFAULT_FEE_RATE matches the live fetch_fee_rate value plumbed into _kelly.
            if market_price_side <= 0.01 or market_price_side >= 0.99:
                continue
            _b = (1.0 - market_price_side) / market_price_side
            _net_b = _b * max(1e-6, 1.0 - DEFAULT_FEE_RATE)
            _raw = (prob_side * _net_b - (1.0 - prob_side)) / _net_b
            kelly_frac = max(0.0, _raw * kelly_fraction)
            if kelly_frac < min_kelly:
                continue

            # Recency weight: parallel to returns, applied by weighted_sharpe.
            ts_str = o.get("exit_timestamp", o.get("timestamp", ""))
            try:
                trade_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() if ts_str else now_ts
                days_ago = max(0.0, (now_ts - trade_ts) / 86400.0)
            except Exception:
                days_ago = 0.0
            recency_w = RECENCY_DECAY_PER_DAY ** days_ago

            # Counterfactual-aware gain_pct for exit_edge_threshold candidates: a
            # recorded scalp re-prices to its hold-to-resolution counterfactual only
            # when the candidate would NOT have fired it — judged against the same
            # BLENDED effective threshold live fires on, never the raw candidate.
            # Loss-cut closes fire independently of the threshold: never re-priced
            # (records without the loss_cut flag replay as ordinary scalps).
            outcome_gain_pct = o.get("gain_pct", 0.0)
            if (exit_threshold_override is not None
                    and counterfactual_index
                    and o.get("exit_reason") == "scalp"):
                pid = o.get("position_id")
                cf = counterfactual_index.get(pid) if pid is not None else None
                if cf:
                    cf_ctx = cf.get("context_at_scalp", {})
                    he_at_scalp = cf_ctx.get("holding_edge")
                    cf_gain = cf.get("counterfactual", {}).get("gain_pct")
                    if (he_at_scalp is not None
                            and cf_gain is not None
                            and not cf_ctx.get("loss_cut", False)):
                        mp_at_scalp = float(cf_ctx.get("market_price") or 0.0)
                        if mp_at_scalp > 0:
                            eff = effective_exit_threshold(
                                float(exit_threshold_override),
                                float(cf_ctx.get("seconds_remaining") or 0.0),
                                mp_at_scalp,
                                fee_rate=float(cf_ctx.get("fee_rate") or DEFAULT_FEE_RATE),
                            )
                        else:
                            eff = float(exit_threshold_override)
                        if float(he_at_scalp) > eff:
                            outcome_gain_pct = float(cf_gain)
            # Mirror image — without it the replay is blind to less-patient
            # candidates (they fire strictly more often, so the scalp-side path
            # alone yields delta == 0 and the z-gate auto-rejects). A held trade
            # re-prices to its worst-moment hypothetical scalp when the candidate
            # WOULD have fired there, unless a threshold-independent live branch
            # (whipsaw cushion, deep-loss-hold) would have held it anyway.
            # Approximation: the snapshot is the worst-holding_edge moment, not a
            # tick series — a candidate could fire at some other moment where the
            # boundary sat higher; same accepted class as the scalp-side replay.
            elif (exit_threshold_override is not None
                    and hold_counterfactual_index
                    and o.get("exit_reason") == "resolution"):
                pid = o.get("position_id")
                cf = hold_counterfactual_index.get(pid) if pid is not None else None
                if cf:
                    cf_ctx = cf.get("context_at_worst_moment", {})
                    he_worst = cf_ctx.get("holding_edge")
                    cf_gain = cf.get("counterfactual", {}).get("gain_pct")
                    mp_worst = float(cf_ctx.get("market_price") or 0.0)
                    if he_worst is not None and cf_gain is not None and mp_worst > 0:
                        dist = cf_ctx.get("btc_distance_atr")  # (btc - strike)/ATR
                        o_side = o.get("side", "")
                        wrong_side = (dist is not None
                                      and ((o_side == "Up" and dist < 0)
                                           or (o_side == "Down" and dist > 0)))
                        whipsaw = wrong_side and abs(float(dist)) <= 0.5
                        entry_price = float(o.get("entry_price") or 0.0)
                        deep_loss_hold = (
                            float(he_worst) < _d("deep_loss_hold_threshold")
                            and entry_price > 0 and mp_worst < entry_price)
                        if not whipsaw and not deep_loss_hold:
                            eff = effective_exit_threshold(
                                float(exit_threshold_override),
                                float(cf_ctx.get("seconds_remaining") or 0.0),
                                mp_worst,
                                fee_rate=float(cf_ctx.get("fee_rate") or DEFAULT_FEE_RATE),
                            )
                            if float(he_worst) <= eff:
                                outcome_gain_pct = float(cf_gain)
            returns.append(kelly_frac * outcome_gain_pct * realism_factor)
            sample_weights.append(recency_w)

        return returns, sample_weights

    def _load_counterfactual_index(self) -> dict[int, dict[str, Any]]:
        """{position_id: scalp-type counterfactual} for scalp outcomes that resolved.

        See _scan_counterfactuals for the shared file scan; keys consumed by the
        replay: `context_at_scalp.{holding_edge, market_price, seconds_remaining,
        fee_rate, loss_cut}`, `counterfactual.gain_pct`.
        """
        return self._scan_counterfactuals()[0]

    def _load_hold_counterfactual_index(self) -> dict[int, dict[str, Any]]:
        """{position_id: hold-type counterfactual} for positions held to resolution.

        Keys consumed by the replay: `context_at_worst_moment.{holding_edge,
        market_price, seconds_remaining, btc_distance_atr}`,
        `counterfactual.gain_pct` (the hypothetical scalp at the worst moment).
        """
        return self._scan_counterfactuals()[1]

    def _scan_counterfactuals(self) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        """One pass over counterfactuals/ -> (scalp_index, hold_index) by position_id.

        Reads per-trade dicts AND rollup arrays (the nightly rollup runs before the
        optimizer stage, so most history lives in rollups). Scalp-type records key
        on `context_at_scalp`, hold-type on `context_at_worst_moment` — separate
        indexes, so a pid carrying both can never be shadowed by glob order.
        Cached on the scheduler instance so a multi-fold backtest run pays the
        I/O cost once.
        """
        cached = getattr(self, "_counterfactual_index_cached", None)
        if cached is not None:
            return cached
        import glob
        scalp_idx: dict[int, dict[str, Any]] = {}
        hold_idx: dict[int, dict[str, Any]] = {}
        try:
            files = glob.glob(str(COUNTERFACTUALS_DIR / "*.json"))
        except Exception:
            files = []
        for f in files:
            try:
                with open(f, "r") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            records = payload if isinstance(payload, list) else [payload]
            for d in records:
                if not isinstance(d, dict):
                    continue
                pid = d.get("position_id")
                cf = d.get("counterfactual", {})
                if pid is None or not isinstance(cf, dict) or cf.get("gain_pct") is None:
                    continue
                if "context_at_scalp" in d:
                    scalp_idx[int(pid)] = d
                elif "context_at_worst_moment" in d:
                    hold_idx[int(pid)] = d
        self._counterfactual_index_cached = (scalp_idx, hold_idx)
        return scalp_idx, hold_idx

    def _config_for_helper(self, recommendations: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve config for `_kelly_bankroll_returns` — recommendation first, live engine fallback.

        Entry gates (min_model_probability, min_edge, min_kelly) are now pipeline-tunable:
        the backtest sample includes resolved ghosts (trades rejected at live gates), so
        raising or lowering any gate filters both baseline and candidate identically and
        the comparison stays clean.
        """
        from polybot.config.param_registry import PIPELINE_PARAMS
        from polybot.core.derived_features import DERIVED_FEATURES
        rec = recommendations or {}
        live_weights = self.indicator_engine.get_weights() if self.indicator_engine else {}
        cfg: dict[str, Any] = {
            "weights": rec.get("recommended_weights") or {
                k: live_weights.get(k, 0.0) for k in ("rsi", "macd", "stochastic", "obv", "vwap")
            },
        }
        for _spec in PIPELINE_PARAMS:
            # SignalEngine stores L6 weights in self.derived_weights[name], not in
            # a `derived_<name>_weight` attribute — handle that lookup specially.
            if _spec.name.startswith("derived_") and _spec.name.endswith("_weight"):
                _fname = _spec.name[len("derived_"):-len("_weight")]
                live_val = (self.signal_engine.derived_weights.get(_fname, _spec.default)
                            if self.signal_engine is not None else _spec.default)
            else:
                live_val = getattr(self.signal_engine, _spec.name, _spec.default)
            cfg[_spec.name] = _spec.cast(rec.get(f"recommended_{_spec.name}", live_val))
        # Assemble the L6 weights dict for the backtest call site.
        cfg["derived_weights"] = {
            name: cfg.get(f"derived_{name}_weight", 0.0) for name in DERIVED_FEATURES.keys()
        }
        return cfg

    @staticmethod
    def _backtest_kwargs(cfg: dict[str, Any]) -> dict[str, Any]:
        """Common `_kelly_bankroll_returns` kwargs from a `_config_for_helper` cfg.
        Per-site extras (outcomes, calibrator, exit override) are passed explicitly
        at each call site."""
        return dict(
            recommended_weights=cfg["weights"],
            momentum_weight=cfg["momentum_weight"],
            atr_sigma_ratio=cfg["atr_sigma_ratio"],
            student_t_df=cfg["student_t_df"],
            min_edge=cfg["min_edge"],
            kelly_fraction=cfg["kelly_fraction"],
            min_kelly=cfg["min_kelly"],
            min_prob=cfg["min_model_probability"],
            regime_weight=cfg["regime_weight"],
            flow_weight=cfg["flow_weight"],
            spot_flow_weight=cfg["spot_flow_weight"],
            prev_margin_weight=cfg["prev_margin_weight"],
            logit_scale=cfg["logit_scale"],
            min_atr=cfg["min_atr"],
            regime_momentum_threshold=cfg["regime_momentum_threshold"],
            final_logit_clamp=cfg["final_logit_clamp"],
            l5_regime_damp_cap=cfg["l5_regime_damp_cap"],
            atr_regime_shift_threshold=cfg["atr_regime_shift_threshold"],
            derived_weights=cfg["derived_weights"],
        )

    def _backtest_recommendations(self, recommendations: dict[str, Any],
                                    outcomes: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
        """Kelly-sized portfolio returns + parallel recency weights.

        Returns ``(returns, sample_weights)`` — callers compute weighted Sharpe via
        ``weighted_sharpe_from_returns(returns, weights)``. Scores through
        ``self._gate_calibrator`` — the OOS gate-reference calibrator fit on the window
        disjoint from the holdout (set once per cycle in `run_daily_pipeline`), NOT the
        live ``signal_engine.calibrator``. Held fixed across baseline and candidate
        within a backtest (one variable at a time), so its mapping is common-mode and
        cancels in the adoption delta to first order. ``None`` → identity.
        """
        cfg = self._config_for_helper(recommendations)
        calibrator = self._gate_calibrator

        return self._kelly_bankroll_returns(
            outcomes=outcomes,
            calibrator=calibrator,
            **self._backtest_kwargs(cfg),
        )

    def _backtest_single_change(self, change: dict[str, Any],
                                outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Run a Kelly-backtest with exactly ONE change applied on top of live values.

        Builds a synthetic recommendations dict that only contains the single change,
        so _config_for_helper applies that change while all other params remain at
        their live engine values. Returns {"returns": [...], "weights": [...],
        "sharpe": float, "candidate_trades": int}. Sharpe is the proper weighted
        Sharpe from ``weighted_sharpe_from_returns``.
        """
        param = change.get("param", "")
        value = change.get("value")

        # Clamp BEFORE backtest so we test the exact value that would be applied.
        if param != "weights" and value is not None:
            from polybot.config.param_registry import CLAMP_RANGES
            if param in CLAMP_RANGES:
                lo, hi, cast = CLAMP_RANGES[param]
                try:
                    value = cast(max(lo, min(hi, cast(value))))
                except (TypeError, ValueError):
                    pass

        # Build a thin recommendations dict for _config_for_helper. Empty
        # ``change`` (no param) is the explicit baseline-backtest path used by
        # _check_regime_adoption — silent fall-through, no warning.
        single_rec: dict[str, Any] = {}
        from polybot.config.param_registry import TUNABLE_NAMES
        if param == "weights":
            single_rec["recommended_weights"] = value
        elif param in TUNABLE_NAMES:
            single_rec[f"recommended_{param}"] = value
        elif param:
            # Non-empty unknown param — real misconfiguration worth warning about.
            logger.warning(
                f"Backtest for '{param}' falls back to baseline config (param not in TUNABLE_NAMES). "
                f"It cannot show improvement and will always be rejected by the z-test."
            )

        cfg = self._config_for_helper(single_rec)
        calibrator = self._gate_calibrator

        # Counterfactual-aware replay only when the candidate is exit_edge_threshold —
        # recorded fill history can't tell us "what if we held instead?", the counterfactual
        # tracker does. Other params see the same data either way.
        _exit_thr_override = None
        _cf_index: dict[int, dict[str, Any]] | None = None
        _hold_cf_index: dict[int, dict[str, Any]] | None = None
        if param == "exit_edge_threshold" and value is not None:
            _exit_thr_override = float(value)
            _cf_index = self._load_counterfactual_index()
            _hold_cf_index = self._load_hold_counterfactual_index()

        returns, weights = self._kelly_bankroll_returns(
            outcomes=outcomes,
            calibrator=calibrator,
            exit_threshold_override=_exit_thr_override,
            counterfactual_index=_cf_index,
            hold_counterfactual_index=_hold_cf_index,
            **self._backtest_kwargs(cfg),
        )
        return {
            "returns": returns,
            "weights": weights,
            "sharpe": _weighted_sharpe(returns, weights),
            "candidate_trades": len(returns),
        }

    def _check_regime_adoption(
        self,
        change: dict[str, Any],
        all_outcomes: list[dict[str, Any]],
        baseline_sharpe: float,
    ) -> tuple[bool, str]:
        """Regime-stratified adoption gate.

        Segments outcomes into trending / reverting / neutral buckets. Accepts
        when either (a) ≥2 of populated regimes improved, OR (b) the dominant
        regime improved — both branches require no regime to degrade by >0.10
        Sharpe.

        Skipped (returns True) when fewer than 2 regimes have ≥ MIN_REGIME_N (8)
        qualifying trades.
        """
        # Segment outcomes by regime
        regime_buckets: dict[str, list] = {"trending": [], "reverting": [], "neutral": []}
        for o in all_outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            r = (ctx.get("regime_state") or "neutral").lower()
            if r.startswith("trending"):
                regime_buckets["trending"].append(o)
            elif r in ("reverting", "mean_reverting"):
                regime_buckets["reverting"].append(o)
            else:
                regime_buckets["neutral"].append(o)

        # 8 requires a meaningful sample while still letting trending/mean-reverting
        # buckets participate (≥20 left the stratified check effectively dormant).
        MIN_REGIME_N = 8
        populated = {k: v for k, v in regime_buckets.items() if len(v) >= MIN_REGIME_N}
        if len(populated) < 2:
            return True, "regime check skipped (insufficient per-regime sample)"

        dominant = max(populated, key=lambda k: len(populated[k]))

        baseline_by_regime: dict[str, float] = {}
        candidate_by_regime: dict[str, float] = {}
        for regime, outcomes_r in populated.items():
            base_result = self._backtest_single_change({}, outcomes_r)  # empty = baseline
            cand_result = self._backtest_single_change(change, outcomes_r)
            baseline_by_regime[regime] = base_result["sharpe"]
            candidate_by_regime[regime] = cand_result["sharpe"]

        regressed_hard = [
            r for r in populated
            if baseline_by_regime[r] - candidate_by_regime[r] > 0.10
        ]
        # Acceptance: (a) ≥2 of populated regimes improved, OR (b) dominant regime improved.
        # Both branches share the "no regime degrades >0.10 Sharpe" floor.
        # "Improved" requires clearing a small margin (not a strict >) so a
        # float-noise win of ~1e-6 in a single bucket — one repriced trade — can't
        # satisfy the gate. 0.02 mirrors the holdout-confirmation margin floor.
        _REGIME_IMPROVE_MARGIN = 0.02
        dom_improved = (candidate_by_regime[dominant]
                        > baseline_by_regime[dominant] + _REGIME_IMPROVE_MARGIN)
        n_improved = sum(1 for r in populated
                         if candidate_by_regime[r] > baseline_by_regime[r] + _REGIME_IMPROVE_MARGIN)
        detail = " | ".join(
            f"{r}: {baseline_by_regime[r]:+.3f}->{candidate_by_regime[r]:+.3f}"
            for r in sorted(populated)
        )
        if regressed_hard:
            return False, f"regime check failed: {regressed_hard} regressed >0.10 Sharpe [{detail}]"
        if dom_improved:
            return True, f"regime check passed (branch b: dominant {dominant} improved) [{detail}]"
        if n_improved >= 2:
            return True, f"regime check passed (branch a: {n_improved}/{len(populated)} regimes improved) [{detail}]"
        return False, f"regime check failed: dominant {dominant} flat AND only {n_improved}/{len(populated)} regime(s) improved [{detail}]"

    async def _run_weight_optimizer(self, recommendations: dict[str, Any],
                                    all_outcomes: list[dict[str, Any]] | None = None,
                                    pipeline_source: str = "local",
                                    holdout_outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Run per-parameter walk-forward backtests and adopt each change independently.

        Walk-forward folds (each fold's test set is genuinely out-of-sample):
          Fold 1: Test [60%:70%]
          Fold 2: Test [70%:80%]
          Fold 3: Test [80%:90%]
          Fold 4: Test [90%:100%]

        Each proposed change is backtested in isolation against the baseline so the
        signal-to-noise of any single parameter is measured cleanly. Changes that pass
        the adoption gates are applied and persisted; the rest are logged as rejected.

        Returns info dict with per-change decision details.
        """
        info: dict[str, Any] = {"decision": "skipped", "reason": "", "per_change": []}
        if all_outcomes is None:
            all_outcomes = self.outcome_reviewer.load_all_outcomes()
        if not all_outcomes or len(all_outcomes) < 10:
            info["reason"] = f"only {len(all_outcomes) if all_outcomes else 0} outcomes (need 10)"
            return info

        _crisis_kelly_locked: bool = False
        try:
            _cs_path = CRISIS_STATE_PATH
            if _cs_path.exists():
                _cs = json.loads(_cs_path.read_text())
                _crisis_kelly_locked = bool(_cs.get("kelly_reduced"))
        except Exception:
            _crisis_kelly_locked = False

        changes_list: list[dict[str, Any]] = recommendations.get("changes", [])

        if not changes_list:
            info["reason"] = "no changes proposed by evolver"
            _cn = getattr(self, '_baseline_n_trades', None)
            _cs = getattr(self, '_baseline_kelly_sharpe', None)
            if _cn and _cs is not None:
                info["old_sharpe"] = round(float(_cs), 4)
                info["n_baseline_trades"] = _cn
            return info

        # --- Baseline: reuse the cached values from `_precompute_baseline` if available
        # (computed once per cycle to feed Claude's prompt). Recomputing here would just
        # repeat the same 4-fold backtest with the same data and same calibrator.
        n = len(all_outcomes)
        fold_boundaries = [0.60, 0.70, 0.80, 0.90, 1.0]
        cached_n = getattr(self, '_baseline_n_trades', None)
        cached_sharpe = getattr(self, '_baseline_kelly_sharpe', None)
        if cached_n and cached_n > 0 and cached_sharpe is not None:
            current_sharpe = float(cached_sharpe)
            info["old_sharpe"] = round(current_sharpe, 4)
            info["n_baseline_trades"] = cached_n
            # Candidate-trade returns aren't cached, but win rate isn't needed for
            # adoption — only for `record_score` telemetry. Skip it here; the directional
            # win-rate per candidate is captured per-change below.
        else:
            all_current_returns: list[float] = []
            all_current_weights: list[float] = []
            baseline_request: dict[str, Any] = {}
            for i in range(len(fold_boundaries) - 1):
                start_idx = int(n * fold_boundaries[i])
                end_idx = int(n * fold_boundaries[i + 1])
                fold_test = all_outcomes[start_idx:end_idx]
                if len(fold_test) < 3:
                    continue
                fr, fw = self._backtest_recommendations(baseline_request, fold_test)
                all_current_returns.extend(fr)
                all_current_weights.extend(fw)
            current_sharpe = _weighted_sharpe(all_current_returns, all_current_weights) if all_current_returns else 0.0
            info["old_sharpe"] = round(current_sharpe, 4)
            info["n_baseline_trades"] = len(all_current_returns)

            # Cache JK SE for Claude's adoption-target context (next cycle).
            from polybot.agents.weight_optimizer import _lag1_autocorr as _ac
            n_base = len(all_current_returns)
            if n_base >= 2:
                base_se = math.sqrt((1.0 + 0.5 * current_sharpe ** 2) / n_base)
                if n_base >= 3:
                    rho = _ac(all_current_returns)
                    base_se *= math.sqrt(1.0 + 2.0 * max(0.0, rho))
                self._baseline_jk_se = round(base_se, 4)
                self._baseline_n_trades = n_base
                self._baseline_kelly_sharpe = round(current_sharpe, 4)

        # --- Per-change walk-forward backtests ---
        adopted_changes: list[dict[str, Any]] = []
        any_adopted = False

        for change in changes_list[:5]:
            param = change.get("param", "")
            value = change.get("value")
            change_info: dict[str, Any] = {"param": param, "value": value}

            if _crisis_kelly_locked and param == "kelly_fraction":
                msg = (
                    "deferred: crisis-mode kelly halving is active. The optimizer's "
                    "claim is valid but cannot override the safety floor mid-crisis. "
                    "Will re-evaluate on the first non-crisis cycle."
                )
                change_info.update({"decision": "deferred_crisis", "reason": msg})
                info["per_change"].append(change_info)
                continue

            # Capture old value for directional tracking via _directional_old_value
            # (handles L6 weights + the scheduler-owned exit_edge_threshold, neither
            # of which is a signal_engine attribute).
            old_val = self._directional_old_value(param)
            if old_val is not None:
                change_info["old_value"] = old_val

            # Pass through Claude's per-change predictions
            for pred_key in ("predicted_delta_sharpe_7d", "confidence_interval"):
                if pred_key in change:
                    change_info[pred_key] = change[pred_key]

            fold_sharpes: list[float] = []
            all_candidate_returns: list[float] = []
            all_candidate_weights: list[float] = []

            for i in range(len(fold_boundaries) - 1):
                start_idx = int(n * fold_boundaries[i])
                end_idx = int(n * fold_boundaries[i + 1])
                fold_test = all_outcomes[start_idx:end_idx]
                if len(fold_test) < 3:
                    continue
                fold_result = self._backtest_single_change(change, fold_test)
                fold_returns = fold_result["returns"]
                fold_weights = fold_result["weights"]
                # Pool every return unconditionally — the baseline pools all of its
                # fold returns, so the z-test must compare like with like. The
                # per-fold Sharpe (consistency diagnostic only) still needs ≥3
                # returns to be meaningful.
                all_candidate_returns.extend(fold_returns)
                all_candidate_weights.extend(fold_weights)
                if len(fold_returns) >= 3:
                    fold_sharpes.append(_weighted_sharpe(fold_returns, fold_weights))

            # Always record diagnostic fields up front so rejection branches
            # below preserve enough detail in pipeline_run_log.json to tell
            # "rejected for being terrible" from "rejected for thin pool".
            if all_candidate_returns:
                candidate_sharpe = _weighted_sharpe(all_candidate_returns, all_candidate_weights)
                candidate_win_rate = sum(1 for r in all_candidate_returns if r > 0) / len(all_candidate_returns)
            else:
                candidate_sharpe = 0.0
                candidate_win_rate = 0.0
            change_info.update({
                "candidate_sharpe": round(candidate_sharpe, 4),
                "candidate_win_rate": round(candidate_win_rate, 4),
                "fold_sharpes": [round(s, 4) for s in fold_sharpes],
                "n_candidate_trades": len(all_candidate_returns),
            })

            if len(all_candidate_returns) < 10:
                msg = f"only {len(all_candidate_returns)} hypothetical trades (need 10)"
                change_info.update({"decision": "rejected", "reason": msg})
                logger.debug(f"REJECTED {param}: {msg}")
                info["per_change"].append(change_info)
                continue

            # Fold consistency: reject if the worst fold collapses below -0.10 Sharpe.
            worst_fold = min(fold_sharpes) if fold_sharpes else 0.0
            if len(fold_sharpes) >= 2 and worst_fold < -0.10:
                msg = (f"fold inconsistency: worst fold Sharpe {worst_fold:+.3f} < -0.10 "
                       f"({[f'{s:+.3f}' for s in fold_sharpes]})")
                change_info.update({"decision": "rejected", "reason": msg})
                logger.debug(f"REJECTED {param}: {msg}")
                info["per_change"].append(change_info)
                continue

            adopt, adopt_reason, z_score = self.weight_optimizer.should_adopt(
                current_sharpe, candidate_sharpe,
                n_trades=len(all_candidate_returns),
                candidate_returns=all_candidate_returns,
            )
            change_info["z_score"] = round(z_score, 3)

            # Regime-stratified check: a change that passes aggregate stats
            # but hurts a specific regime is likely overfitting to the dominant sample.
            # Use only the validation fold (40%) — same data the z-test used.
            validation_outcomes = all_outcomes[int(len(all_outcomes) * 0.60):]
            if adopt and validation_outcomes:
                regime_ok, regime_reason = self._check_regime_adoption(change, validation_outcomes, current_sharpe)
                if not regime_ok:
                    adopt = False
                    adopt_reason = f"regime gate: {regime_reason}"
                else:
                    adopt_reason += f" | {regime_reason}"

            # Holdout confirmation: the last HOLDOUT_DAYS of trades were excluded from
            # all folds above. Margin scales by holdout JK_SE so the gate has the
            # same z=0.3 confidence regardless of holdout sample size — at n=30 the
            # margin is ~0.06, at n=300 it shrinks to ~0.02.
            if adopt and holdout_outcomes and len(holdout_outcomes) >= HOLDOUT_MIN_TRADES:
                base_h, base_h_w = self._backtest_recommendations({}, holdout_outcomes)
                cand_result_h = self._backtest_single_change(change, holdout_outcomes)
                cand_h = cand_result_h["returns"]
                cand_h_w = cand_result_h["weights"]
                base_sh = _weighted_sharpe(base_h, base_h_w) if base_h else 0.0
                cand_sh = _weighted_sharpe(cand_h, cand_h_w) if cand_h else 0.0
                from polybot.agents.weight_optimizer import _jk_se as _h_jk_se, ADOPTION_Z_FLOOR as _ZF
                _holdout_se = _h_jk_se(base_sh, len(base_h), base_h) if base_h else 0.0
                HOLDOUT_ADOPTION_MARGIN = max(0.02, _ZF * _holdout_se)
                change_info["holdout_baseline_sharpe"] = round(base_sh, 4)
                change_info["holdout_candidate_sharpe"] = round(cand_sh, 4)
                change_info["holdout_margin"] = round(HOLDOUT_ADOPTION_MARGIN, 4)
                if cand_sh < base_sh + HOLDOUT_ADOPTION_MARGIN:
                    adopt = False
                    adopt_reason = (f"holdout gate: candidate {cand_sh:+.3f} < baseline "
                                    f"{base_sh:+.3f} + {HOLDOUT_ADOPTION_MARGIN:.2f} "
                                    f"on last {HOLDOUT_DAYS}d (n={len(cand_h)})")
                else:
                    adopt_reason += f" | holdout {cand_sh:+.3f} ≥ {base_sh + HOLDOUT_ADOPTION_MARGIN:+.3f}"

            if adopt:
                change_info.update({"decision": "adopted", "reason": adopt_reason})
                adopted_changes.append(change)
                any_adopted = True
                old_val_str = ""
                if param != "weights":
                    # Pre-mutation, L6-aware old value captured above into change_info.
                    old_val = change_info.get("old_value")
                    if old_val is not None:
                        old_val_str = f"{old_val}->"
                n_trades = len(all_candidate_returns)
                # Detailed line demoted to DEBUG — user-facing summary renders once at pipeline end.
                logger.debug(f"ADOPTED {param}: {old_val_str}{value} ({adopt_reason}, n={n_trades} candidates, baseline={current_sharpe:.3f}, candidate={candidate_sharpe:.3f})")
            else:
                change_info.update({"decision": "rejected", "reason": adopt_reason})
                n_trades = len(all_candidate_returns)
                logger.debug(f"REJECTED {param}: {value} — {adopt_reason} (n={n_trades} candidates, baseline={current_sharpe:.3f}, candidate={candidate_sharpe:.3f})")

            info["per_change"].append(change_info)

        # Store per-change results for Claude's next cycle
        self._last_per_change_results = [
            f"{c['param']}={c.get('value', '?')}: {c['decision'].upper()} — {c['reason']} "
            f"(baseline={current_sharpe:.3f}, candidate={c.get('candidate_sharpe', 'N/A')}, "
            f"n={c.get('n_candidate_trades', '?')})"
            for c in info["per_change"]
        ]

        # Record all changes tested this cycle (adopted + rejected) for directional table
        if self.pipeline_tracker:
            try:
                self.pipeline_tracker.record_pipeline_run(
                    source=pipeline_source,
                    baseline_sharpe=current_sharpe,
                    per_change_results=info["per_change"],
                )
            except Exception as e:
                logger.debug(f"Failed to record pipeline run: {e}")

        if not any_adopted:
            info["decision"] = "no_change"
            info["reason"] = "no changes passed adoption gates"
            return info

        # --- Combined holdout interaction check ---
        # ≥2 independently-adopted changes can interfere (shared logit budget, joint
        # clamps). One combined backtest on the holdout: if the joint set misses
        # baseline + the per-change z-floor margin, back out the WHOLE batch. No
        # iteration — next cycle re-proposes individually with the table updated.
        if len(adopted_changes) >= 2 and holdout_outcomes and len(holdout_outcomes) >= HOLDOUT_MIN_TRADES:
            try:
                from polybot.config.param_registry import TUNABLE_NAMES as _TN
                from polybot.agents.weight_optimizer import _jk_se as _h_jk_se, ADOPTION_Z_FLOOR as _ZF

                combined_rec: dict[str, Any] = {}
                for c in adopted_changes:
                    param = c["param"]
                    value = c["value"]
                    if param == "weights":
                        combined_rec["recommended_weights"] = value
                    elif param in _TN:
                        combined_rec[f"recommended_{param}"] = value

                # exit_edge_threshold acts on the backtest only via the counterfactual
                # override — without it the "joint set" would silently exclude that
                # change. Clamp to the registry range, same as the per-change backtest.
                _exit_c = next((c for c in adopted_changes
                                if c["param"] == "exit_edge_threshold"
                                and c.get("value") is not None), None)
                _exit_thr_combined: float | None = None
                _cf_index_combined: dict[int, dict[str, Any]] | None = None
                _hold_cf_combined: dict[int, dict[str, Any]] | None = None
                if _exit_c is not None:
                    from polybot.config.param_registry import CLAMP_RANGES as _CR
                    _lo, _hi, _cast = _CR["exit_edge_threshold"]
                    _exit_thr_combined = _cast(max(_lo, min(_hi, _cast(_exit_c["value"]))))
                    _cf_index_combined = self._load_counterfactual_index()
                    _hold_cf_combined = self._load_hold_counterfactual_index()

                cfg_combined = self._config_for_helper(combined_rec)
                calibrator = self._gate_calibrator
                base_h_rets, base_h_w = self._backtest_recommendations({}, holdout_outcomes)
                combined_rets, combined_w = self._kelly_bankroll_returns(
                    outcomes=holdout_outcomes,
                    calibrator=calibrator,
                    exit_threshold_override=_exit_thr_combined,
                    counterfactual_index=_cf_index_combined,
                    hold_counterfactual_index=_hold_cf_combined,
                    **self._backtest_kwargs(cfg_combined),
                )
                base_h_sharpe = _weighted_sharpe(base_h_rets, base_h_w) if base_h_rets else 0.0
                combined_h_sharpe = _weighted_sharpe(combined_rets, combined_w) if combined_rets else 0.0
                combined_delta = combined_h_sharpe - base_h_sharpe
                holdout_se = _h_jk_se(base_h_sharpe, len(base_h_rets), base_h_rets) if base_h_rets else 0.0
                combined_margin = max(0.02, _ZF * holdout_se)

                info["combined_holdout_baseline_sharpe"] = round(base_h_sharpe, 4)
                info["combined_holdout_candidate_sharpe"] = round(combined_h_sharpe, 4)
                info["combined_holdout_delta"] = round(combined_delta, 4)
                info["combined_holdout_margin"] = round(combined_margin, 4)

                if combined_h_sharpe < base_h_sharpe + combined_margin:
                    # Whole-batch back-out. Each per-change gate cleared on its own
                    # data; the combined set failed on the same holdout. Re-evaluate
                    # next cycle once the directional table reflects this evidence.
                    backed_out_params = [c["param"] for c in adopted_changes]
                    for c in info["per_change"]:
                        if c.get("decision") == "adopted":
                            c["decision"] = "backed_out"
                            c["reason"] = (
                                f"combined-holdout back-out: joint set Sharpe "
                                f"{combined_h_sharpe:+.3f} < baseline {base_h_sharpe:+.3f} + "
                                f"margin {combined_margin:.3f} on {len(combined_rets)} holdout trades"
                            )
                    adopted_changes = []
                    info["interaction_detected"] = True
                    info["backed_out_params"] = backed_out_params
                    logger.info(
                        f"Combined-holdout back-out: combined {combined_h_sharpe:+.3f} < "
                        f"baseline {base_h_sharpe:+.3f} + {combined_margin:.3f}. "
                        f"Dropped: {backed_out_params}"
                    )

            except Exception as e:
                # Fail CLOSED. The combined-holdout check is the last safety gate
                # before a ≥2-change batch goes live; an exception here is exactly
                # when we want to be conservative, not adopt the joint set blind.
                backed_out_params = [c["param"] for c in adopted_changes]
                for c in info["per_change"]:
                    if c.get("decision") == "adopted":
                        c["decision"] = "backed_out"
                        c["reason"] = f"combined-holdout check errored — backed out (fail-closed): {e}"
                adopted_changes = []
                info["interaction_detected"] = True
                info["backed_out_params"] = backed_out_params
                info["combined_holdout_error"] = str(e)
                logger.warning(f"Combined holdout backtest errored — backing out batch {backed_out_params}: {e}")

        if not adopted_changes:
            info["decision"] = "no_change"
            info["reason"] = "all changes backed out (interactions)"
            return info

        # --- Apply and persist all adopted changes ---
        info["decision"] = "adopted"
        info["adopted_params"] = [c["param"] for c in adopted_changes]

        weights_change = next((c for c in adopted_changes if c["param"] == "weights"), None)
        new_weights: dict[str, Any] = dict(weights_change["value"]) if weights_change else {}
        if weights_change and self.indicator_engine:
            self.indicator_engine.set_weights({
                k: v for k, v in new_weights.items()
                if k in ("rsi", "macd", "stochastic", "obv", "vwap")
            })

        # Apply adopted changes to signal_engine. Registry is the single source of
        # truth for clamp ranges — no inline literals that diverge from param_registry.
        if self.signal_engine:
            from polybot.config.param_registry import BY_NAME as _BY_NAME
            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param == "weights":
                    self.signal_engine.weights = {k: v for k, v in new_weights.items()
                                                   if k in ["rsi", "macd", "stochastic", "obv", "vwap"]}
                elif param == "exit_edge_threshold":
                    spec = _BY_NAME[param]
                    self._exit_edge_threshold = spec.cast(max(spec.lo, min(spec.hi, spec.cast(value))))
                elif param in _BY_NAME:
                    spec = _BY_NAME[param]
                    clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(value))))
                    if param.startswith("derived_") and param.endswith("_weight"):
                        # L6 weights live in self.derived_weights[feature_name].
                        _fname = param[len("derived_"):-len("_weight")]
                        self.signal_engine.derived_weights[_fname] = clamped
                    elif hasattr(self.signal_engine, param):
                        setattr(self.signal_engine, param, clamped)

        # Persist to settings.yaml — registry yaml_key drives section routing.
        # Only registry tunables (+ the weights dict) can reach adopted_changes:
        # the validator reroutes manual-only params and unknown params backtest
        # as baseline (z=0 → rejected).
        if self._config:
            from polybot.config.param_registry import BY_NAME as _BY_NAME
            if weights_change:
                self._config.setdefault("signal", {})["weights"] = {
                    k: v for k, v in new_weights.items()
                    if k in ("rsi", "macd", "stochastic", "obv", "vwap")}

            for change in adopted_changes:
                param = change["param"]
                value = change["value"]
                if param == "weights":
                    pass  # handled above
                elif param in _BY_NAME:
                    spec = _BY_NAME[param]
                    clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(value))))
                    # Walk the dotted yaml_key path so nested keys (e.g. signal.derived.x)
                    # produce real nested dicts, not a literal "derived.x" key under signal.
                    _parts = spec.yaml_key.split(".")
                    _node = self._config
                    for _p in _parts[:-1]:
                        _node = _node.setdefault(_p, {})
                    _node[_parts[-1]] = clamped

            try:
                config_to_save = dict(self._config)
                save_config(config_to_save)
                logger.info("Pipeline parameters persisted to settings.yaml")
            except Exception as e:
                logger.error(f"Failed to persist config: {e}")

        # Track adoption in pipeline_tracker (one record per run) for the auto-revert path.
        self._record_run_adoption(adopted_changes, info, current_sharpe, pipeline_source)

        return info

    def _record_run_adoption(self, adopted_changes: list[dict[str, Any]], info: dict[str, Any],
                             current_sharpe: float, pipeline_source: str) -> None:
        """Record a run's adopted changes into the PipelineTracker for the auto-revert path.

        Old values MUST come from the pre-mutation `old_value` in info["per_change"]
        (via `_directional_old_value`) — the mutation loop has already run, so a
        getattr re-read would return the NEW value (revert → no-op) and None for L6
        weights (revert silently skipped; they live in `derived_weights`).
        """
        if not (self.pipeline_tracker and adopted_changes):
            return
        old_by_param = {ci["param"]: ci.get("old_value")
                        for ci in info.get("per_change", [])
                        if ci.get("decision") == "adopted"}
        tracker_changes: dict[str, tuple] = {}
        for change in adopted_changes:
            param = change["param"]
            if param == "weights":
                # Real dicts (pre-mutation old via _directional_old_value) so the
                # auto-revert path can restore the prior L4 committee.
                tracker_changes["weights"] = (old_by_param.get("weights"),
                                              dict(change["value"] or {}))
            else:
                tracker_changes[param] = (old_by_param.get(param), change["value"])

        best_candidate_sharpe = max(
            (ci.get("candidate_sharpe", current_sharpe) for ci in info["per_change"]
             if ci.get("decision") == "adopted"),
            default=current_sharpe,
        )
        # Sum of Claude's per-change predicted deltas for adopted changes
        adopted_preds = [
            c["predicted_delta_sharpe_7d"]
            for c in info["per_change"]
            if c.get("decision") == "adopted" and c.get("predicted_delta_sharpe_7d") is not None
        ]
        run_predicted_delta = round(sum(adopted_preds), 4) if adopted_preds else None
        self.pipeline_tracker.record_adoption(
            source=pipeline_source,
            version="params",
            baseline_sharpe=current_sharpe,
            predicted_sharpe=best_candidate_sharpe,
            changes=tracker_changes,
            reason=f"{len(adopted_changes)} change(s) adopted",
            run_predicted_delta=run_predicted_delta,
        )

    def _apply_revert_adoptions(self) -> None:
        """Auto-revert adoptions flagged as rollback_recommended by pipeline_tracker.

        Works newest-first. For each flagged-but-not-yet-reverted record, reverts
        params to their pre-adoption values unless a newer adoption already changed
        the same param (in which case the newer adoption takes precedence).
        Updates both signal_engine and settings.yaml so the revert is live immediately.
        A record touching kelly_fraction is deferred (retried next cycle) while the
        crisis halving is active — reverting would strip the crisis floor, and the
        later restore would clobber the revert with a stale value.
        """
        if not self.pipeline_tracker:
            return
        records = self.pipeline_tracker._load()
        if not records:
            return

        _kelly_locked = False
        try:
            if CRISIS_STATE_PATH.exists():
                _kelly_locked = bool(json.loads(CRISIS_STATE_PATH.read_text()).get("kelly_reduced"))
        except Exception:
            _kelly_locked = False

        already_handled: set[str] = set()  # params touched by records processed so far
        reverted_any = False

        for rec in reversed(records):  # newest first
            changes_raw = rec.get("changes", {})  # {param: [old_val, new_val]}

            if not rec.get("rollback_recommended") or rec.get("reverted"):
                # Not flagged or already reverted — mark its params as handled
                already_handled.update(changes_raw.keys())
                continue

            if _kelly_locked and "kelly_fraction" in changes_raw:
                # Deferred, not reverted — retried on the first non-halved cycle.
                # Its params still shield older records (newest adoption wins).
                logger.info("[AUTO-REVERT] deferred: record touches kelly_fraction "
                            "while crisis halving is active")
                already_handled.update(changes_raw.keys())
                continue

            # Build revert list using old (pre-adoption) values. `weights` reverts
            # only when the record carries a real dict — placeholder-string records
            # hold nothing restorable.
            revert_changes: list[dict[str, Any]] = []
            for param, vals in changes_raw.items():
                if param in already_handled:
                    continue
                old_val = vals[0] if isinstance(vals, (list, tuple)) and len(vals) >= 2 else None
                if old_val is None:
                    continue
                if param == "weights" and not isinstance(old_val, dict):
                    continue
                revert_changes.append({"param": param, "value": old_val})

            if not revert_changes:
                rec["reverted"] = True
                reverted_any = True
                already_handled.update(changes_raw.keys())
                continue

            # Apply to signal_engine (takes effect in the 45-min window before restart)
            if self.signal_engine:
                from polybot.config.param_registry import BY_NAME as _BY_NAME
                for rc in revert_changes:
                    p, v = rc["param"], rc["value"]
                    if p == "weights":
                        _w5 = {k: float(x) for k, x in v.items()
                               if k in ("rsi", "macd", "stochastic", "obv", "vwap")}
                        if _w5:
                            if self.indicator_engine:
                                self.indicator_engine.set_weights(_w5)
                            self.signal_engine.weights = dict(_w5)
                    elif p == "exit_edge_threshold":
                        spec = _BY_NAME[p]
                        self._exit_edge_threshold = spec.cast(max(spec.lo, min(spec.hi, spec.cast(v))))
                    elif p in _BY_NAME:
                        spec = _BY_NAME[p]
                        _clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(v))))
                        if p.startswith("derived_") and p.endswith("_weight"):
                            _fname = p[len("derived_"):-len("_weight")]
                            self.signal_engine.derived_weights[_fname] = _clamped
                        elif hasattr(self.signal_engine, p):
                            setattr(self.signal_engine, p, _clamped)

            # Apply to config dict and persist to settings.yaml
            if self._config:
                from polybot.config.param_registry import BY_NAME as _BY_NAME
                for rc in revert_changes:
                    p, v = rc["param"], rc["value"]
                    if p == "weights":
                        _w5 = {k: float(x) for k, x in v.items()
                               if k in ("rsi", "macd", "stochastic", "obv", "vwap")}
                        if _w5:
                            self._config.setdefault("signal", {})["weights"] = _w5
                    elif p in _BY_NAME:
                        spec = _BY_NAME[p]
                        clamped = spec.cast(max(spec.lo, min(spec.hi, spec.cast(v))))
                        _parts = spec.yaml_key.split(".")
                        _node = self._config
                        for _p in _parts[:-1]:
                            _node = _node.setdefault(_p, {})
                        _node[_parts[-1]] = clamped
                try:
                    save_config(dict(self._config))
                except Exception as e:
                    # In-memory revert already applied; the record stays flagged so
                    # the persist retries next cycle. Its params still shield older
                    # records from re-reverting to even older values in this pass.
                    logger.error(f"Auto-revert: failed to persist settings.yaml: {e}")
                    already_handled.update(changes_raw.keys())
                    continue

            rec["reverted"] = True
            rec["reverted_at"] = datetime.now(timezone.utc).isoformat()
            reverted_any = True
            already_handled.update(changes_raw.keys())

            summary = ", ".join(f"{rc['param']}→{rc['value']}" for rc in revert_changes)
            logger.warning(
                "[AUTO-REVERT] %s rolled back: %s | reason: %s",
                rec.get("version", "?"), summary,
                rec.get("rollback_reason", "performance regression"),
            )

        if reverted_any:
            self.pipeline_tracker._save(records)
            self._invalidate_baseline_cache()

    async def run_daily_pipeline(self) -> None:
        _now_utc = datetime.now(timezone.utc)
        now_et_str = f"{_now_utc.strftime('%b')} {_now_utc.day}, {_now_utc.strftime('%Y  %I:%M %p UTC')}"
        logger.info(f"─── Pipeline starting — {now_et_str} ───")

        pipeline_info: dict[str, Any] = {}

        # Frozen = analysis-only cycle: data/bias/ghost cards still build, but the
        # calibrator is neither refit nor swapped, no weight change is tested or
        # adopted, and no auto-revert fires. (loader.save_config independently
        # refuses to persist while frozen — this gate keeps the in-memory engines
        # untouched too.)
        from polybot.paths import is_pipeline_frozen
        _frozen = is_pipeline_frozen()
        if _frozen:
            logger.warning(
                "PIPELINE FROZEN — analysis-only cycle "
                "(delete memory/state/PIPELINE_FROZEN to resume adoption)"
            )
            pipeline_info["frozen"] = True
        # One timestamp for every window boundary this cycle (60d cutoff, holdout
        # split, gate-calibrator window, evolver-context filter, calibration windows)
        # so no record can straddle two boundaries computed at different instants.
        _cycle_now_ts = _now_utc.timestamp()
        # Counterfactuals accumulate between cycles — rebuild the index per cycle.
        self._counterfactual_index_cached = None

        # Snapshot current config before changes
        old_config = {}
        if self.signal_engine:
            old_config = {
                "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
                "min_model_probability": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_time_remaining": self._min_time_remaining,
                "trading_start": self._trading_start,
                "trading_end": self._trading_end,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
            }

        # Rollups are best-effort — a disk/permission error must not crash the
        # pipeline, but it MUST surface so the operator can fix the cause.
        def _safe_rollup(name: str, fn):
            try:
                return fn()
            except Exception as e:
                logger.error(f"Rollup '{name}' failed: {e}")
                pipeline_info.setdefault("rollup_errors", []).append(f"{name}: {e}")
                return 0
        rolled = _safe_rollup("outcomes", self.outcome_reviewer.rollup_old_outcomes)
        ghost_rolled = _safe_rollup("ghosts", self.ghost_tracker.rollup_old_ghosts) if self.ghost_tracker else 0
        cf_rolled = _safe_rollup("counterfactuals", self.counterfactual_tracker.rollup_old_counterfactuals) if self.counterfactual_tracker else 0

        _raw_outcomes = self._load_combined_outcomes()
        # Bound active dataset to the last PIPELINE_WINDOW_DAYS so weight
        # candidates aren't judged against probability machines that no longer
        # exist. Walk-forward 60/40 is preserved INSIDE the window.
        PIPELINE_WINDOW_DAYS = 60
        _cutoff_ts = _cycle_now_ts - PIPELINE_WINDOW_DAYS * 86400.0
        _windowed = [o for o in _raw_outcomes if _outcome_ts(o) >= _cutoff_ts]
        if len(_windowed) >= 500:  # need ≥500 for the 4-fold expanding test
            all_outcomes = _windowed
            _window_note = f"  |  bounded to last {PIPELINE_WINDOW_DAYS}d (was {len(_raw_outcomes):,})"
        else:
            all_outcomes = _raw_outcomes
            _window_note = (f"  |  window {PIPELINE_WINDOW_DAYS}d had only "
                            f"{len(_windowed)} trades — using full history {len(_raw_outcomes):,}")
        split_idx = max(1, int(len(all_outcomes) * 0.6))
        train_outcomes = all_outcomes[:split_idx]
        validation_outcomes = all_outcomes[split_idx:]

        logger.info(
            f"  Data loaded  |  {len(all_outcomes):,} trades "
            f"({len(train_outcomes):,} train / {len(validation_outcomes):,} val)"
            + _window_note
            + (f"  |  rolled up: {rolled} outcomes, {cf_rolled} scalps, {ghost_rolled} ghosts"
               if rolled or cf_rolled or ghost_rolled else "")
        )
        pipeline_info["total_outcomes"] = len(all_outcomes)
        pipeline_info["train_count"] = len(train_outcomes)
        pipeline_info["validation_count"] = len(all_outcomes) - len(train_outcomes)

        # Ghosts (rejected trades) belong only in the optimizer's backtest pool (§3).
        # real_all is the real-trades-only view used by every performance and
        # model-training consumer: adoption review, the calibrators, the all-time card.
        real_all = [o for o in all_outcomes if not o.get("is_ghost")]

        # Fill in adoptions' realized 7d/14d/30d Sharpe (review spans the holdout — it
        # wants the freshest data) and auto-revert any that decayed. Reverts mutate
        # the engines + settings.yaml, so they're suspended while frozen.
        if self.pipeline_tracker:
            self.pipeline_tracker.review_past_adoptions(real_all)
            if not _frozen:
                self._apply_revert_adoptions()

        # Holdout = last HOLDOUT_DAYS, reserved out-of-sample for adoption confirmation.
        # Disable it (fall back to the full pool) when the holdout is too thin to confirm
        # on, or the pre-holdout pool is below the learning floor. Must run before the
        # analysis is built: the recommender keys off analysis["overall"]["total_trades"],
        # so an empty opt pool would zero all learning even when all_outcomes is large.
        opt_outcomes, holdout_outcomes = self._split_holdout(all_outcomes, now_ts=_cycle_now_ts)
        if len(holdout_outcomes) < HOLDOUT_MIN_TRADES:
            _holdout_off_reason = (f"only {len(holdout_outcomes)} trades in last "
                                   f"{HOLDOUT_DAYS}d (need {HOLDOUT_MIN_TRADES})")
        elif len(opt_outcomes) < MIN_TRADES_FOR_LEARNING:
            _holdout_off_reason = (f"opt-pool {len(opt_outcomes)} < {MIN_TRADES_FOR_LEARNING} "
                                   f"(dataset younger than the {HOLDOUT_DAYS}d holdout window)")
        else:
            _holdout_off_reason = ""
        if _holdout_off_reason:
            logger.info(
                f"  Holdout INACTIVE: {_holdout_off_reason}. Falling back to full pool for "
                f"analysis + evolver; no post-gate confirmation this cycle."
            )
            pipeline_info["holdout_active"] = False
            pipeline_info["holdout_skipped_reason"] = _holdout_off_reason
            opt_outcomes, holdout_outcomes = all_outcomes, []
        else:
            pipeline_info["holdout_active"] = True

        # Gate-reference calibrator (two-calibrator split, §11): fit on the window behind
        # the holdout (days [HOLDOUT_DAYS, HOLDOUT_DAYS + _CAL_WINDOW_DAYS) back), disjoint
        # from the holdout the adoption gate confirms on, so weight backtests score through
        # it (not the live calibrator) and stay OOS. None → backtests run at identity.
        _g_lo = _cycle_now_ts - (HOLDOUT_DAYS + _CAL_WINDOW_DAYS) * 86400.0
        _g_hi = _cycle_now_ts - HOLDOUT_DAYS * 86400.0
        _gate_pool = [o for o in real_all if _g_lo <= _outcome_ts(o) < _g_hi]
        self._gate_calibrator = self._fit_calibrator_on(_gate_pool)
        logger.info(
            f"  Gate calibrator: {'fitted' if self._gate_calibrator else 'identity'} "
            f"on {len(_gate_pool)} trades (days {HOLDOUT_DAYS}-{HOLDOUT_DAYS + _CAL_WINDOW_DAYS} "
            f"back, disjoint from holdout)"
        )

        # Real + holdout-excluded view for the bias detector and analysis aggregates.
        opt_real = [o for o in opt_outcomes if not o.get("is_ghost")]
        analysis = await self._run_bias_detector(opt_real)

        # Gate skip stats: how often did each entry gate fire?
        # Tells Claude which gates are over-filtering and whether adverse selection /
        # pre-submit drift / late-window guards are actually affecting trade count.
        _gate_stats_path = GATE_STATS_CURRENT_PATH
        if _gate_stats_path.exists():
            try:
                gate_stats = json.loads(_gate_stats_path.read_text())
                # Flatten nested {"counts": {...}, "total_skips": N} → {"gate": N, "total_skips": N}
                # so claude_client can iterate flat k/v pairs without knowing the schema.
                counts = gate_stats.get("counts", gate_stats)
                total = gate_stats.get("total_skips", sum(v for v in counts.values() if isinstance(v, (int, float))))
                analysis["gate_skip_stats"] = {**counts, "total_skips": total}
                pipeline_info["gate_total_skips"] = total
            except Exception:
                pass

        # Realized edge / fill slippage / fill rate — real-trade aggregates for the evolver.
        realized_edges = [o.get("realized_edge", 0) for o in opt_real if o.get("realized_edge") is not None]
        fill_slippages = [o.get("fill_slippage", 0) for o in opt_real if o.get("fill_slippage") is not None]
        exec_quality: dict[str, Any] = {}
        if realized_edges:
            exec_quality.update({
                "avg_realized_edge": round(sum(realized_edges) / len(realized_edges), 4),
                "avg_fill_slippage": round(sum(fill_slippages) / len(fill_slippages), 4) if fill_slippages else 0,
                "n_trades_with_data": len(realized_edges),
                "pct_positive_slippage": round(sum(1 for s in fill_slippages if s > 0.001) / len(fill_slippages), 3) if fill_slippages else 0,
            })
        _fill_stats_path = FILL_STATS_PATH
        if _fill_stats_path.exists():
            try:
                fill_stats = json.loads(_fill_stats_path.read_text())
                exec_quality["fok_fill_rate"] = fill_stats.get("fill_rate", None)
                exec_quality["fok_total_attempts"] = fill_stats.get("total_attempts", 0)
                exec_quality["fok_buy_fill_rate"] = round(
                    fill_stats.get("buy_fills", 0) / max(fill_stats.get("buy_attempts", 1), 1), 4)
            except Exception:
                pass
        # Slippage breakdown by spread and time-in-window (actionable for max_edge, logit_scale, kelly_fraction)
        try:
            exec_detail = self.bias_detector.analyze_execution_quality_detailed(opt_real)
            if exec_detail:
                exec_quality.update(exec_detail)
        except Exception as e:
            logger.debug(f"Execution quality detail failed: {e}")

        if exec_quality:
            analysis["execution_quality"] = exec_quality

        # Counterfactual + ghost analysis feed the evolver context. When the holdout
        # is active, exclude the last HOLDOUT_DAYS so the holdout-confirmation gate
        # stays OOS for the proposals these aggregates inform. (No filter when
        # inactive — there is no confirmation pool to protect.)
        _evo_cutoff = _cycle_now_ts - HOLDOUT_DAYS * 86400.0
        _holdout_on = bool(pipeline_info.get("holdout_active"))
        def _before_holdout(rec: dict[str, Any], *ts_keys: str) -> bool:
            if not _holdout_on:
                return True
            for _k in ts_keys:
                _s = rec.get(_k)
                if _s in (None, ""):
                    continue
                # Ghost `resolved_at` is an epoch float; everything else is ISO-8601.
                if isinstance(_s, (int, float)):
                    return float(_s) < _evo_cutoff
                try:
                    return datetime.fromisoformat(str(_s).replace("Z", "+00:00")).timestamp() < _evo_cutoff
                except Exception:
                    return True  # unparseable ts → keep (matches load_all's lenient sort)
            return True  # no ts field → keep

        # Counterfactual analysis: how accurate are our scalp exits?
        cf_info: dict[str, Any] = {}
        if self.counterfactual_tracker:
            counterfactuals = [c for c in self.counterfactual_tracker.load_all()
                               if _before_holdout(c, "timestamp")]
            if counterfactuals:
                cf_analysis = self.bias_detector.analyze_counterfactuals(counterfactuals)
                analysis["counterfactual_analysis"] = cf_analysis
                cf_info = {
                    "total": cf_analysis.get("total_scalps_tracked", 0),
                    "accuracy": cf_analysis.get("scalp_accuracy", 0),
                }
        pipeline_info["counterfactual"] = cf_info

        # Ghost trade analysis: which downstream gates are blocking profitable trades?
        ghost_tracker = getattr(self, 'ghost_tracker', None)
        if ghost_tracker:
            ghosts = ghost_tracker.load_all()
            resolved_ghosts = [g for g in ghosts
                               if g.get("resolved", False)
                               and _before_holdout(g, "resolved_at", "timestamp")]
            if resolved_ghosts:
                analysis["ghost_analysis"] = self.bias_detector.analyze_ghosts(resolved_ghosts)

        # Live/production isotonic re-fit on the FRESHEST _CAL_WINDOW_DAYS (the OOS
        # gate-reference calibrator was fit on the disjoint pre-holdout window above;
        # weight backtests use that one). Adoption gate on top of fit()'s bootstrap
        # CI: the new fit must beat the CURRENT calibrator on full-pool weighted
        # log-loss by ≥ LOG_LOSS_FLOOR AND not reduce Kelly-Sharpe on cal_val.
        cal_info: dict[str, Any] = {"decision": "skipped"}
        # Current serving calibrator, stamped up front so every exit path (skip,
        # reject, frozen) renders truthfully in the summary instead of a bare
        # "identity" while a fitted isotonic is live.
        cal_info["current_n_knots"] = (
            getattr(getattr(self.signal_engine, "calibrator", None), "n_knots", 0) or 0
        ) if self.signal_engine else 0
        from polybot.core.calibrator import IsotonicCalibrator, _weighted_log_loss as _wll
        MIN_CAL_VALIDATION_TRADES = 50
        _pending_cal_save: IsotonicCalibrator | None = None
        _cal_cutoff_new = _cycle_now_ts                                  # freshest edge (day 0)
        _cal_cutoff_old = _cycle_now_ts - _CAL_WINDOW_DAYS * 86400.0      # _CAL_WINDOW_DAYS back
        # real_all only: the calibrator changes LIVE trading probabilities, so it must fit
        # on trades the bot actually took, not rejected ghosts.
        _cal_pool = [o for o in real_all if _cal_cutoff_old <= _outcome_ts(o) < _cal_cutoff_new]
        if _frozen:
            cal_train = []
            cal_val = []
            cal_info["reason"] = "pipeline frozen — calibration unchanged"
        elif len(_cal_pool) >= 125:
            _split = max(1, int(len(_cal_pool) * 0.6))
            cal_train = _cal_pool[:_split]
            cal_val = _cal_pool[_split:]
            logger.info(
                f"  Live calibrator window: freshest {_CAL_WINDOW_DAYS}d "
                f"({len(cal_train)} train / {len(cal_val)} val)"
            )
        else:
            cal_train = []
            cal_val = []
            cal_info["reason"] = (
                f"only {len(_cal_pool)} trades in the freshest {_CAL_WINDOW_DAYS}d "
                f"(need 125) — skipping calibration"
            )
            logger.info(
                f"  Live calibrator window: only {len(_cal_pool)} trades in the freshest "
                f"{_CAL_WINDOW_DAYS}d — skipping calibration"
            )

        if len(cal_train) >= 75 and self.signal_engine:
            cal_now_ts = _cycle_now_ts
            cal_probs, cal_outcomes, cal_weights = self._calibration_xy(cal_train, cal_now_ts)

            if len(cal_probs) < 75:
                cal_info["decision"] = "rejected"
                cal_info["reason"] = (f"only {len(cal_probs)} usable calibration samples "
                                      f"after filtering (need 75)")
            else:
                cal = IsotonicCalibrator()
                _fit_ok = cal.fit(cal_probs, cal_outcomes, min_samples=75, sample_weights=cal_weights)
                cal_info["fit_diagnostics"] = dict(cal.last_fit_diagnostics)
                if not _fit_ok:
                    cal_info["decision"] = "rejected"
                    _fit_dec = (cal.last_fit_diagnostics or {}).get("decision")
                    cal_info["reason"] = (
                        "bootstrap-CI gate failed (OOB lower bound ≤ 0)"
                        if _fit_dec == "rejected_ci"
                        else "fit rejected before bootstrap (range check / weights / fit error)"
                    )
                if _fit_ok:
                    # Log-loss on full 7-day pool (more data = more reliable calibration signal).
                    # Hierarchy: identity (no-cal) → current (live) → new (today's fit).
                    # Each tier should beat the one below it.
                    # Recency-weighted, matching the calibrator-internal bootstrap CI weighting.
                    import numpy as _np
                    all_pool_probs, all_pool_outs, all_pool_w = self._calibration_xy(_cal_pool, cal_now_ts)

                    if all_pool_probs:
                        _p = _np.asarray(all_pool_probs, dtype=float)
                        _o = _np.asarray(all_pool_outs, dtype=float)
                        _w = _np.asarray(all_pool_w, dtype=float)
                        identity_loss = _wll(_p, _o, _w)
                        new_loss_full = _wll(_np.asarray([cal.calibrate(p) for p in all_pool_probs]), _o, _w)
                    else:
                        identity_loss = float("nan")
                        new_loss_full = float("nan")
                    cur_cal = self.signal_engine.calibrator
                    if cur_cal and not getattr(cur_cal, 'is_identity', False) and all_pool_probs:
                        current_loss = _wll(_np.asarray([cur_cal.calibrate(p) for p in all_pool_probs]), _o, _w)
                    else:
                        current_loss = identity_loss

                    # Kelly-Sharpe on cal_val (sizing sanity check).
                    cfg = self._config_for_helper()
                    helper_kwargs = dict(outcomes=cal_val, **self._backtest_kwargs(cfg))
                    identity_returns, identity_weights = self._kelly_bankroll_returns(calibrator=None, **helper_kwargs)
                    new_returns, new_weights = self._kelly_bankroll_returns(calibrator=cal, **helper_kwargs)
                    current_returns, current_weights = self._kelly_bankroll_returns(calibrator=cur_cal, **helper_kwargs)
                    identity_sharpe = _weighted_sharpe(identity_returns, identity_weights)
                    new_sharpe = _weighted_sharpe(new_returns, new_weights)
                    current_sharpe = _weighted_sharpe(current_returns, current_weights)

                    # Min nats the new fit must beat the current calibrator by, on the
                    # full cal pool's recency-weighted log-loss, before adoption.
                    LOG_LOSS_FLOOR = 0.005
                    # update(), not reassign — fit_diagnostics (and current_n_knots)
                    # must survive into the adopted/reverted/rejected record.
                    cal_info.update({
                        "identity_loss": round(identity_loss, 4) if all_pool_probs else None,
                        "current_loss": round(current_loss, 4) if all_pool_probs else None,
                        "new_loss": round(new_loss_full, 4) if all_pool_probs else None,
                        "identity_sharpe": round(identity_sharpe, 4),
                        "current_sharpe": round(current_sharpe, 4),
                        "new_sharpe": round(new_sharpe, 4),
                        "n_pool": len(all_pool_probs),
                        "n_val": len(new_returns),
                        "n_knots": cal.n_knots,
                        "log_loss_improvement": round(cal.log_loss_improvement, 4),
                        "current_n_knots": getattr(cur_cal, "n_knots", 0) if cur_cal else 0,
                        "span_min": (round(float(cal._iso.y_thresholds_[0]), 4)
                                     if getattr(cal, "_iso", None) is not None else None),
                        "span_max": (round(float(cal._iso.y_thresholds_[-1]), 4)
                                     if getattr(cal, "_iso", None) is not None else None),
                    })

                    insufficient = len(new_returns) < MIN_CAL_VALIDATION_TRADES
                    # Gate 1: new fit must beat current on log-loss by ≥ LOG_LOSS_FLOOR (0.005)
                    new_beats_current = (not (math.isnan(new_loss_full) or math.isnan(current_loss))
                                         and new_loss_full < current_loss - LOG_LOSS_FLOOR)
                    # Gate 2: new fit must not hurt sizing vs current (parallel structure to log-loss gate)
                    sizing_ok = new_sharpe >= current_sharpe
                    # Revert check: if current calibrator is worse than identity, revert
                    current_worse_than_identity = (not (math.isnan(current_loss) or math.isnan(identity_loss))
                                                    and current_loss > identity_loss
                                                    and cur_cal and not getattr(cur_cal, 'is_identity', False))

                    if insufficient:
                        cal_info["decision"] = "rejected"
                        cal_info["reason"] = f"only {len(new_returns)} validation trades (need {MIN_CAL_VALIDATION_TRADES})"
                    elif current_worse_than_identity:
                        # Current calibrator hurts accuracy vs identity. Try the new fit first —
                        # it may already beat identity directly, skipping the revert step.
                        new_beats_identity_loss = (not (math.isnan(new_loss_full) or math.isnan(identity_loss))
                                                   and new_loss_full < identity_loss - LOG_LOSS_FLOOR)
                        new_beats_identity_sharpe = new_sharpe >= identity_sharpe
                        if new_beats_identity_loss and new_beats_identity_sharpe:
                            cal_info["decision"] = "adopted"
                            cal_info["reason"] = (f"current calibrator (loss={current_loss:.3f}) worse than identity "
                                                    f"(loss={identity_loss:.3f}); new fit beats identity — upgrading directly")
                            _pending_cal_save = cal
                            self.signal_engine.calibrator = cal
                            self._invalidate_baseline_cache()
                            logger.info(f"Isotonic adopted (bypassing bad current): loss {identity_loss:.4f} → {new_loss_full:.4f}, "
                                        f"sharpe {identity_sharpe:.4f} → {new_sharpe:.4f}")
                        else:
                            identity_cal = IsotonicCalibrator()  # unfitted == identity
                            _pending_cal_save = identity_cal
                            self.signal_engine.calibrator = identity_cal
                            cal_info["decision"] = "reverted"
                            cal_info["reason"] = (f"current calibrator (loss={current_loss:.3f}) worse than "
                                                    f"identity (loss={identity_loss:.3f}); new fit also doesn't beat identity — reverting")
                            logger.warning(f"Isotonic reverted to identity: current loss {current_loss:.4f} > identity {identity_loss:.4f}")
                    elif new_beats_current and sizing_ok:
                        # New fit beats current on accuracy AND doesn't hurt sizing — adopt
                        cal_info["decision"] = "adopted"
                        _pending_cal_save = cal
                        self.signal_engine.calibrator = cal
                        self._invalidate_baseline_cache()
                        logger.debug(f"Isotonic adopted: loss {current_loss:.4f} → {new_loss_full:.4f} "
                                     f"(identity {identity_loss:.4f}), sharpe {identity_sharpe:.4f} → {new_sharpe:.4f}")
                    elif new_beats_current and not sizing_ok:
                        cal_info["decision"] = "rejected"
                        cal_info["reason"] = (f"new fit improves accuracy (loss {current_loss:.3f}→{new_loss_full:.3f}) "
                                                f"but hurts sizing vs current ({new_sharpe:.3f} < {current_sharpe:.3f})")
                    else:
                        cal_info["decision"] = "rejected"
                        gap = current_loss - new_loss_full if not math.isnan(new_loss_full) else 0
                        cal_info["reason"] = (f"new fit doesn't beat current by enough "
                                                f"(loss gap {gap:+.4f}, need -{LOG_LOSS_FLOOR})")
        pipeline_info["calibration"] = cal_info
        # Expose the raw-vs-calibrated meta-check so Claude sees the diagnostic
        if cal_info.get("meta_warning"):
            analysis["cal_meta_warning"] = cal_info["meta_warning"]

        # Trend buckets for the evolver — real trades only.
        from polybot.agents.pipeline_analytics import format_trends
        trends_str = format_trends(opt_real, n_buckets=5, min_per_bucket=50)
        if trends_str:
            analysis["trends"] = trends_str

        # Current-regime snapshot for the evolver: most recent 100 real trades.
        recent_window = opt_real[-100:] if len(opt_real) >= 100 else opt_real
        if recent_window:
            rw_gains = [o.get("gain_pct", 0) for o in recent_window]
            rw_wr = sum(1 for o in recent_window if o.get("correct", False)) / len(recent_window)
            rw_pnl = sum(o.get("pnl", 0) for o in recent_window)
            analysis["current_regime"] = {
                "n_trades": len(recent_window),
                "win_rate": round(rw_wr, 4),
                "total_pnl": round(rw_pnl, 4),
                "mean_gain_pct": round(sum(rw_gains) / len(rw_gains), 6) if rw_gains else 0,
                "note": ("Most recent 100 real trades in the optimizer pool "
                         "(holdout-excluded — ends ~7d ago when the holdout is active)"),
            }

        # Emit analysis summary now that bias/calibration are done
        _cf_acc = cf_info.get("accuracy", 0) if cf_info else None
        _cf_total = cf_info.get("total", 0) if cf_info else 0
        _gate_skips = pipeline_info.get("gate_total_skips", 0)
        _real_trades = [o for o in all_outcomes if not o.get("is_ghost")]
        _res_acc = (sum(1 for o in _real_trades if o.get("correct")) / len(_real_trades)) if _real_trades else None
        _analysis_parts = []
        if _res_acc is not None:
            _analysis_parts.append(f"resolution accuracy {_res_acc:.0%}")
        if _cf_total:
            _analysis_parts.append(f"scalp accuracy {_cf_acc:.0%} on {_cf_total:,}" if _cf_acc is not None else f"{_cf_total:,} scalps tracked")
        if _gate_skips:
            _analysis_parts.append(f"{_gate_skips:,} gate skips")
        logger.info("  Analysis done" + (f"  |  {' | '.join(_analysis_parts)}" if _analysis_parts else ""))

        # Need at least MIN_TRADES_FOR_LEARNING trades before running the evolver/optimizer.
        weight_info: dict[str, Any] = {"decision": "skipped"}
        if _frozen:
            recommendations = {}
            weight_info["reason"] = "pipeline frozen (analysis-only cycle)"
        elif len(all_outcomes) < MIN_TRADES_FOR_LEARNING:
            logger.info(f"Skipping learning pipeline: only {len(all_outcomes)} trades, need {MIN_TRADES_FOR_LEARNING}")
            recommendations = {}
            weight_info["reason"] = f"only {len(all_outcomes)} trades (need {MIN_TRADES_FOR_LEARNING})"
        else:
            pipeline_info["holdout_n_trades"] = len(holdout_outcomes)

            # Precompute baseline Sharpe/SE/N so Claude's context shows real numbers
            # instead of None on the first cycle after restart.
            self._precompute_baseline(opt_outcomes)

            # Build Claude context including pipeline track record
            if self.pipeline_tracker:
                track_record = self.pipeline_tracker.format_for_claude()
                if track_record:
                    analysis["pipeline_track_record"] = track_record

            # Pass opt_outcomes so the evolver and adoption gates share the same data.
            # Holdout trades are reserved for the post-gate confirmation backtest below.
            recommendations = await self._run_ta_evolver(analysis, opt_outcomes)
            source = recommendations.get("_pipeline_source", "local")
            pipeline_info["source"] = source
            # Manual-lever observations — evidence-backed suggestions for operator-only
            # params. These are never auto-applied; just surfaced in the summary table,
            # Discord alert, and strategy_log.md for the operator to review.
            pipeline_info["manual_observations"] = recommendations.get("manual_observations", []) or []

            # Crisis mode: baseline Sharpe < 0.10 AND (recent_50 WR < 48% OR loss/win
            # ratio > 2.0 OR trailing-3-day Sharpe < 0). The 3-day branch catches
            # sustained collapses that recent-50 smoothing masks — a multi-day
            # bleed where the freshest fills are still mixed in the rolling 50.
            _recent_real = [o for o in all_outcomes if not o.get("is_ghost")]
            _recent_50 = _recent_real[-50:] if len(_recent_real) >= 50 else _recent_real
            _recent_wr = sum(1 for o in _recent_50 if o.get("correct", False)) / max(len(_recent_50), 1)
            _recent_gains = [o.get("gain_pct", 0) for o in _recent_50]
            _wins = [g for g in _recent_gains if g > 0]
            _losses = [-g for g in _recent_gains if g < 0]
            _avg_win = (sum(_wins) / len(_wins)) if _wins else 0.0
            _avg_loss = (sum(_losses) / len(_losses)) if _losses else 0.0
            _loss_ratio = (_avg_loss / _avg_win) if _avg_win > 0 else 0.0
            # Trailing 3-day Sharpe — independent multi-day signal. Parse timestamps
            # (not a lexicographic string compare) so a non-UTC/offset-suffixed record
            # can't silently fall on the wrong side of the cutoff.
            from datetime import timedelta as _td
            _three_d_cutoff = datetime.now(timezone.utc) - _td(days=3)
            def _after_cutoff(o: dict) -> bool:
                s = o.get("exit_timestamp") or o.get("timestamp") or ""
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                except (ValueError, TypeError, AttributeError):
                    return False
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt >= _three_d_cutoff
            _trailing_gains = [
                float(o.get("gain_pct", 0)) for o in _recent_real if _after_cutoff(o)
            ]
            _trailing_3d_sharpe = (
                _sharpe(_trailing_gains) if len(_trailing_gains) >= 20 else 0.0
            )
            _in_crisis = (
                (self._baseline_kelly_sharpe or 0.0) < 0.10
                and (_recent_wr < 0.48 or _loss_ratio > 2.0)
            ) or (len(_trailing_gains) >= 20 and _trailing_3d_sharpe < 0.0)

            # Sustained crisis (≥3 cycles) → halve kelly_fraction, restore on first non-crisis.
            _crisis_state_path = CRISIS_STATE_PATH
            _crisis_state = {"streak": 0, "kelly_reduced": False, "original_kelly": None}
            try:
                if _crisis_state_path.exists():
                    _crisis_state.update(json.loads(_crisis_state_path.read_text()))
            except Exception:
                pass

            if _in_crisis:
                _crisis_state["streak"] = int(_crisis_state.get("streak", 0)) + 1
                pipeline_info["crisis_mode"] = True
                pipeline_info["crisis_streak"] = _crisis_state["streak"]
                logger.info(
                    f"Pipeline CRISIS MODE (streak={_crisis_state['streak']}): "
                    f"recent WR={_recent_wr:.1%}, Sharpe={self._baseline_kelly_sharpe:.3f}"
                )

                # Sustained crisis (3+ consecutive runs) → auto-reduce kelly_fraction.
                # CRISIS_KELLY_FLOOR sits below the optimizer-tunable range so crisis
                # sizes more defensively than any adoptable state; the loader accepts
                # it (same constant) so the persisted value survives the next boot.
                if _crisis_state["streak"] >= 3 and not _crisis_state.get("kelly_reduced") \
                        and self.signal_engine and self._config:
                    from polybot.config.param_registry import CRISIS_KELLY_FLOOR
                    _orig = float(self.signal_engine.kelly_fraction)
                    _reduced = max(CRISIS_KELLY_FLOOR, _orig * 0.5)
                    # Persist kelly_reduced BEFORE applying the cut so a crash
                    # mid-pipeline can't compound the halving on restart.
                    _crisis_state["original_kelly"] = _orig
                    _crisis_state["kelly_reduced"] = True
                    try:
                        _crisis_state_path.parent.mkdir(parents=True, exist_ok=True)
                        _crisis_state_path.write_text(json.dumps(_crisis_state, indent=2))
                    except Exception as e:
                        logger.error(f"Auto Kelly reduction: failed to persist crisis_state: {e}")
                    self.signal_engine.kelly_fraction = _reduced
                    self._config.setdefault("math", {})["kelly_fraction"] = _reduced
                    try:
                        save_config(dict(self._config))
                    except Exception as e:
                        logger.error(f"Auto Kelly reduction: failed to persist: {e}")
                    logger.warning(
                        f"[AUTO KELLY REDUCTION] kelly_fraction {_orig:.3f} → {_reduced:.3f} "
                        f"after {_crisis_state['streak']} consecutive crisis cycles. "
                        f"Will restore on first non-crisis cycle."
                    )
                    pipeline_info["kelly_auto_reduced"] = True
            else:
                pipeline_info["crisis_mode"] = False

                # Recovery: if we previously auto-reduced kelly, restore it. Crisis
                # state resets only after the restore persists — a failed save keeps
                # the state so the restore retries next cycle instead of leaving the
                # halved value on disk beside a clean crisis file.
                _restore_persist_failed = False
                if _crisis_state.get("kelly_reduced") and _crisis_state.get("original_kelly") is not None \
                        and self.signal_engine and self._config:
                    _orig = float(_crisis_state["original_kelly"])
                    self.signal_engine.kelly_fraction = _orig
                    self._config.setdefault("math", {})["kelly_fraction"] = _orig
                    try:
                        save_config(dict(self._config))
                        logger.info(
                            f"[AUTO KELLY RESTORE] kelly_fraction restored to {_orig:.3f} "
                            f"after crisis ended (was reduced for {_crisis_state.get('streak', 0)} cycles)."
                        )
                        pipeline_info["kelly_auto_restored"] = True
                    except Exception as e:
                        _restore_persist_failed = True
                        logger.error(
                            f"Auto Kelly restore: failed to persist — crisis state kept, "
                            f"restore retries next cycle: {e}"
                        )

                if not _restore_persist_failed:
                    _crisis_state = {"streak": 0, "kelly_reduced": False, "original_kelly": None}

            try:
                _crisis_state_path.parent.mkdir(parents=True, exist_ok=True)
                _crisis_state_path.write_text(json.dumps(_crisis_state, indent=2))
            except Exception as e:
                logger.debug(f"Failed to persist crisis_state: {e}")

            weight_info = await self._run_weight_optimizer(recommendations, opt_outcomes, pipeline_source=source, holdout_outcomes=holdout_outcomes)
        pipeline_info["weights"] = weight_info

        # Deferred save: when the optimizer ran, its save_config has already returned
        # by here. A crash before the next line leaves new weights + the previous-
        # session calibrator on disk — slightly mismatched but each a valid, coherent
        # artifact on its own. Saving the calibrator first risked a brand-new
        # calibrator paired with stale weights, the worse half. Sits OUTSIDE the
        # learning-floor gate so an adoption/revert on a young dataset (125-199
        # trades) still persists. While frozen, stage 3 never sets a pending save.
        if _pending_cal_save is not None:
            try:
                _pending_cal_save.save()
            except Exception as e:
                logger.error(f"Failed to persist isotonic calibrator: {e}")

        # All-time stats — real trades only (ghosts have a gain_pct but no pnl, so they'd
        # show negative Sharpe beside positive P&L).
        all_gains = [o.get("gain_pct", 0) for o in real_all]
        all_pnl = sum(o.get("pnl", 0) for o in real_all)
        all_wins = sum(1 for o in real_all if o.get("correct", False))
        if all_gains:
            avg_g = sum(all_gains) / len(all_gains)
            var_g = sum((r - avg_g) ** 2 for r in all_gains) / len(all_gains) if len(all_gains) > 1 else 1
            std_g = math.sqrt(var_g) if var_g > 0 else 1
            all_sharpe = avg_g / std_g if std_g > 0 else 0
        else:
            all_sharpe = 0
        pipeline_info["all_time"] = {
            "total_trades": len(real_all),
            "win_rate": round(all_wins / len(real_all), 4) if real_all else 0,
            "sharpe": round(all_sharpe, 4),
            "total_pnl": round(all_pnl, 2),
        }

        # Current config snapshot (post-pipeline values)
        if self.signal_engine:
            pipeline_info["current_config"] = {
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
                "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
                "min_model_prob": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
                "regime_weight": getattr(self.signal_engine, 'regime_weight', _d("regime_weight")),
                "flow_weight": getattr(self.signal_engine, 'flow_weight', _d("flow_weight")),
                "spot_flow_weight": getattr(self.signal_engine, 'spot_flow_weight', _d("spot_flow_weight")),
                "student_t_df": getattr(self.signal_engine, 'student_t_df', _d("student_t_df")),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
            }

        # Compute config diff
        config_changes = {}
        if self.signal_engine and old_config:
            new_vals = {
                "min_edge": getattr(self.signal_engine, 'min_edge', _d("min_edge")),
                "kelly_fraction": getattr(self.signal_engine, 'kelly_fraction', _d("kelly_fraction")),
                "momentum_weight": getattr(self.signal_engine, 'momentum_weight', _d("momentum_weight")),
                "min_model_probability": getattr(self.signal_engine, 'min_model_probability', _d("min_model_probability")),
                "exit_edge_threshold": self._exit_edge_threshold,
                "min_time_remaining": self._min_time_remaining,
                "trading_start": self._trading_start,
                "trading_end": self._trading_end,
                "min_kelly": getattr(self.signal_engine, 'min_kelly', _d("min_kelly")),
                "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', _d("atr_sigma_ratio")),
            }
            for k, old_v in old_config.items():
                new_v = new_vals.get(k)
                if old_v != new_v:
                    config_changes[k] = {"old": old_v, "new": new_v}

        # Human-readable summary — logged AND used by Discord report.
        pipeline_info["summary_block"] = _format_pipeline_summary(pipeline_info)
        for line in pipeline_info["summary_block"].splitlines():
            logger.info(line)

        # Send daily report
        if self.alert_manager:
            try:
                await self.alert_manager.send_daily_report(
                    all_outcomes, analysis, recommendations, config_changes, pipeline_info)
            except Exception as e:
                logger.error(f"Failed to send daily report: {e}")

    async def run_outcome_loop(self) -> None:
        """Periodic outcome review — outcomes are recorded inline by the trading loop.
        This loop exists for future periodic analysis tasks."""
        while self._running:
            await asyncio.sleep(self.outcome_interval_seconds)

    async def run_daily_loop(self) -> None:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        while self._running:
            now = datetime.now(ET)
            if now.hour == self.daily_pipeline_hour and self.daily_pipeline_minute <= now.minute < self.daily_pipeline_minute + 5:
                try:
                    await self.run_daily_pipeline()
                except Exception as e:
                    logger.error(f"Daily pipeline error: {e}")
                    if self.alert_manager:
                        await self.alert_manager.send_error(f"Daily pipeline failed: {e}")
                if self._auto_shutdown:
                    logger.info("Pipeline complete")
                    self._shutdown_requested = True
                    return
                await asyncio.sleep(3600)
            await asyncio.sleep(60)

    async def start(self) -> None:
        self._running = True
        logger.debug("Agent scheduler started")

    async def stop(self) -> None:
        self._running = False
        logger.debug("Agent scheduler stopped")
