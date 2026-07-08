"""Single source of truth for parameter defaults and ranges.

All knobs are operator-owned (no auto-tuning — entry forecasting has no edge
over the CLOB price; tasks/todo.md). Ranges are kept for loader validation only.
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
    ParamSpec("atr_sigma_ratio",         "signal.atr_sigma_ratio",         1.2,   2.5,   float, 1.3,   "L1 vol scale (vol_scaled = ATR/this) — higher = sharper/more confident probs"),
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
    # ── Exit / scalp threshold ───────────────────────────────────────────────
    ParamSpec("exit_edge_threshold",     "signal.exit_edge_threshold",     -0.10, -0.03, float, -0.10, "holding_edge floor before scalping; blended with exit_boundary curve"),
)

# ── Operator-owned defaults without ranges ───────────────────────────────────
_MANUAL_DEFAULTS: dict[str, Any] = {
    # Exit / hold policy
    "loss_cut_fraction": 0.65,
    "loss_cut_time_s": 90.0,
    "adverse_selection_threshold": 0.80,
    "deep_loss_hold_threshold": -0.10,
    # Entry-timing envelope + flip hurdle
    "normal_fraction": 0.60,
    "late_max_penalty": 0.30,
    "flip_edge_premium": 0.015,
    # Late-window sniper (gated; default OFF until its kill bar passes at a reachable
    # RTT — the one bot-formable late-window edge; see tasks/todo.md + CLAUDE.md §2).
    "sniper_enabled": False,        # MASTER KILL — must stay False until the kill bar passes
    "sniper_only": False,           # live-deploy switch: suppress base-entry BUYs (ghosted, so the
                                    # base strategy keeps accruing evidence) — capital deploys only
                                    # on sniper fires. The base strategy has no proven edge; never
                                    # run it with real capital (tasks/todo.md go-live gate).
    "sniper_late_start_s": 45.0,    # only fire in the final N seconds of the window
    "sniper_move_window_s": 2.0,    # Coinbase move lookback (s)
    "sniper_cb_move": 8.0,          # $ Coinbase move over the lookback to fire
    "sniper_ask_cap": 0.92,         # only buy if the chosen side's ask is still <= this
    "sniper_min_edge": 0.04,        # stale-cheap floor (= min_edge so the downstream net-edge/
                                    # pre-submit gates don't silently raise it; one consistent floor)
    "sniper_max_edge": 0.50,        # sniper's own sanity cap, replacing the bypassed 0.20
    "sniper_fok_slip": 0.05,        # FOK limit pad above the decision ask — the kill bar's
                                    # lenient leg (fills up to +0.05) passed at +9.3¢/sh
    # Risk caps
    "max_concurrent_positions": 2,
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
