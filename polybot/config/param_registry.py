"""Single source of truth for parameter defaults and ranges.

All knobs are operator-owned — the nightly knob-tuning pipeline was deleted
with the entry-side prediction stack (entry forecasting has no edge over the
CLOB price; tasks/goal.md). Ranges are kept for loader validation only.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ParamSpec:
    name: str        # matches signal_engine attribute name
    yaml_key: str    # dotted path in settings.yaml
    lo: float | int
    hi: float | int
    cast: type       # int or float — applied during loader validation
    default: Any
    description: str

# Validated knobs: settings.yaml values must land inside these ranges at load.
VALIDATED_PARAMS: tuple[ParamSpec, ...] = (
    # ── L1 (the only model) ──────────────────────────────────────────────────
    ParamSpec("atr_sigma_ratio",         "signal.atr_sigma_ratio",         1.2,   2.5,   float, 1.3,   "L1 aggressiveness — lower = sharper probs"),
    ParamSpec("student_t_df",            "signal.student_t_df",            3,     8,     int,   5,     "L1 tail fatness — lower = fatter tails"),
    ParamSpec("min_atr",                 "signal.min_atr",                 8.0,   25.0,  float, 12.0,  "static ATR floor; runtime uses max(min_atr, 0.3 × rolling_20)"),
    ParamSpec("atr_regime_shift_threshold", "signal.atr_regime_shift_threshold", 0.40, 0.80, float, 0.60,
              "rolling/long-term ATR ratio below this widens the ATR floor (L1 vol-shift guard)"),
    # ── Sizing ──────────────────────────────────────────────────────────────
    ParamSpec("kelly_fraction",          "math.kelly_fraction",            0.04,  0.18,  float, 0.08,  "Kelly sizing fraction"),
    # ── Entry gates ─────────────────────────────────────────────────────────
    ParamSpec("min_edge",                "signal.min_edge",                0.02,  0.10,  float, 0.04,  "minimum model–market edge to enter"),
    ParamSpec("min_kelly",               "signal.min_kelly",               0.005, 0.04,  float, 0.01,  "minimum Kelly fraction to enter"),
    ParamSpec("min_model_probability",   "signal.min_model_probability",   0.52,  0.70,  float, 0.56,  "minimum model probability to enter"),
    # ── Exit / scalp threshold (the edge — Phase 3 replaces the hand curve) ─
    ParamSpec("exit_edge_threshold",     "signal.exit_edge_threshold",     -0.10, -0.03, float, -0.10, "holding_edge floor before scalping; blended with exit_boundary curve"),
)

# ── Derived lookups ──────────────────────────────────────────────────────────
BY_NAME: dict[str, ParamSpec] = {p.name: p for p in VALIDATED_PARAMS}

# ── Operator-owned defaults without ranges ───────────────────────────────────
_MANUAL_DEFAULTS: dict[str, Any] = {
    # Exit / hold policy
    "max_edge": 0.20,
    "loss_cut_fraction": 0.65,
    "loss_cut_time_s": 90.0,
    "adverse_selection_threshold": 0.80,
    "edge_decay_threshold": -0.05,
    "deep_loss_hold_threshold": -0.10,
    # Entry-timing envelope + flip hurdle
    "normal_fraction": 0.60,
    "late_max_penalty": 0.30,
    "flip_edge_premium": 0.015,
    # Risk caps
    "max_concurrent_positions": 2,
    "max_bankroll_deployed": 0.80,
    # Schedule (mirror settings.yaml so a missing key falls back coherently)
    "trading_start_hour_et": 0,
    "trading_start_minute": 1,
    "trading_end_hour_et": 23,
    "trading_end_minute": 30,
    # L1 vol-scale autocorr window
    "regime_lookback": 50,
    # Circuit breaker (dotted access)
    "circuit_breaker.floor_pct": 0.85,
    "circuit_breaker.min_multiplier": 0.40,
}

# Unified defaults map.
DEFAULTS: dict[str, Any] = {p.name: p.default for p in VALIDATED_PARAMS} | _MANUAL_DEFAULTS

def default_for(name: str) -> Any:
    """Canonical default for a parameter, by name. Single source of truth."""
    return DEFAULTS[name]
