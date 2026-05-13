"""Single source of truth for all pipeline-tunable parameters.

Every file that needs param ranges, defaults, or yaml paths reads from here.
Adding a new tunable param = add one ParamSpec row. Nothing else needs updating
(loader validation, CLAMP_RANGES, _config_for_helper, _backtest_single_change,
and the Claude system-prompt param list all derive from this table).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ParamSpec:
    name: str        # matches signal_engine attribute name and recommendation key
    yaml_key: str    # dotted path in settings.yaml (may differ from name, e.g. kelly_fraction)
    lo: float | int
    hi: float | int
    cast: type       # int or float — applied when clamping and in _config_for_helper
    default: Any     # fallback when signal_engine attribute is missing
    description: str # shown in Claude system prompt

PIPELINE_PARAMS: tuple[ParamSpec, ...] = (
    # ── Layer 1 ─────────────────────────────────────────────────────────────
    ParamSpec("atr_sigma_ratio",         "signal.atr_sigma_ratio",         1.2,   2.5,   float, 1.3,   "L1 aggressiveness — lower = sharper probs (HIGHEST leverage)"),
    ParamSpec("student_t_df",            "signal.student_t_df",            3,     8,     int,   5,     "L1 tail fatness — lower = fatter tails (BTC kurtosis target)"),
    ParamSpec("min_atr",                 "signal.min_atr",                 8.0,   25.0,  float, 12.0,  "static ATR floor; runtime uses max(min_atr, 0.3 × rolling_20)"),
    # ── Logit amplifier ─────────────────────────────────────────────────────
    ParamSpec("logit_scale",             "signal.logit_scale",             2.0,   5.0,   float, 4.0,   "master amplifier on all L2–L5 weights"),
    # ── Layer 2–5 weights ───────────────────────────────────────────────────
    ParamSpec("regime_weight",           "signal.regime_weight",           0.02,  0.10,  float, 0.03,  "L2 regime autocorr × direction"),
    ParamSpec("flow_weight",             "signal.flow_weight",             0.02,  0.12,  float, 0.04,  "L3 CLOB book imbalance + trade flow"),
    ParamSpec("spot_flow_weight",        "signal.spot_flow_weight",        0.01,  0.15,  float, 0.10,  "L3b Binance CVD + taker ratio"),
    ParamSpec("liquidation_weight",      "signal.liquidation_weight",      0.01,  0.10,  float, 0.03,  "L3e Bybit OI liquidation pressure"),
    ParamSpec("prev_margin_weight",      "signal.prev_margin_weight",      0.01,  0.05,  float, 0.02,  "L5 prev-window resolution margin carry"),
    ParamSpec("momentum_weight",         "signal.momentum_weight",         -0.10, 0.10,  float, -0.04, "L4 indicator momentum — NEGATIVE = fade (mean-revert)"),
    # ── Sizing ──────────────────────────────────────────────────────────────
    ParamSpec("kelly_fraction",          "math.kelly_fraction",            0.05,  0.18,  float, 0.08,  "Kelly sizing fraction — leave unchanged unless strong drawdown evidence"),
    # ── Entry gates (pipeline-tunable since ghosts are in the backtest) ─────
    ParamSpec("min_edge",                "signal.min_edge",                0.02,  0.10,  float, 0.04,  "minimum model–market edge to enter"),
    ParamSpec("min_kelly",               "signal.min_kelly",               0.005, 0.04,  float, 0.01,  "minimum Kelly fraction to enter"),
    ParamSpec("min_model_probability",   "signal.min_model_probability",   0.52,  0.70,  float, 0.56,  "minimum model probability to enter"),
    # ── Entry timing envelope ───────────────────────────────────────────────
    ParamSpec("normal_fraction",         "entry_timing.normal_fraction",   0.40,  0.80,  float, 0.60,  "fraction of window with full Kelly (no late-window penalty)"),
    ParamSpec("late_max_penalty",        "entry_timing.late_max_penalty",  0.10,  0.60,  float, 0.30,  "max Kelly penalty at the very end of the window (ATM trades)"),
    # ── Flip-trade behavior ─────────────────────────────────────────────────
    ParamSpec("flip_edge_premium",       "entry_timing.flip_edge_premium", 0.005, 0.05,  float, 0.015, "extra edge required to re-enter same window after a scalp"),
    # ── Exit / scalp threshold ──────────────────────────────────────────────
    # TIGHT bound — directly changes realized P&L. Lower (more negative) = hold
    # longer through noise; upper (less negative) = exit faster on any tick against.
    ParamSpec("exit_edge_threshold",     "signal.exit_edge_threshold",     -0.10, -0.03, float, -0.05, "holding_edge floor before scalping; blended with exit_boundary curve"),
)

# ── Derived lookups (everything else imports these, not PIPELINE_PARAMS directly) ──
BY_NAME: dict[str, ParamSpec] = {p.name: p for p in PIPELINE_PARAMS}

# Format used by claude_client and local_recommender: name → (lo, hi, cast)
CLAMP_RANGES: dict[str, tuple] = {p.name: (p.lo, p.hi, p.cast) for p in PIPELINE_PARAMS}

# Set of tunable param names for O(1) membership tests
TUNABLE_NAMES: frozenset[str] = frozenset(p.name for p in PIPELINE_PARAMS)

# ── Manual-only param defaults ────────────────────────────────────────────────
_MANUAL_DEFAULTS: dict[str, Any] = {
    # Exit / hold policy
    "max_edge": 0.20,
    "loss_cut_fraction": 0.65,
    "loss_cut_time_s": 120.0,
    "adverse_selection_threshold": 0.55,
    # Flip trading
    "flip_enabled": True,
    # Risk caps
    "max_concurrent_positions": 2,
    "max_bankroll_deployed": 0.80,
    # Signal/regime knobs (not pipeline-tunable)
    "regime_lookback": 50,
    "consensus_dead_zone": 0.05,
    # Indicator weight dict (shape only — runtime should always supply this from YAML)
    "weights": {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20, "obv": 0.15, "vwap": 0.20},
    # Circuit breaker (dotted access)
    "circuit_breaker.floor_pct": 0.85,
    "circuit_breaker.min_multiplier": 0.40,
}

# Unified defaults map: pipeline params + manual params.
DEFAULTS: dict[str, Any] = {p.name: p.default for p in PIPELINE_PARAMS} | _MANUAL_DEFAULTS

def default_for(name: str) -> Any:
    """Canonical default for a parameter, by name. Single source of truth."""
    return DEFAULTS[name]
