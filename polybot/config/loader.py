from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import yaml
from dotenv import load_dotenv

_config: dict[str, Any] | None = None

def _get_nested(config: dict[str, Any], dotted_key: str) -> tuple[Any, bool]:
    keys = dotted_key.split(".")
    current = config
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return None, False
        current = current[k]
    return current, True

def validate_config(config: dict[str, Any]) -> None:
    errors: list[str] = []

    def _check_range(dotted_key: str, lo, hi, *, integer: bool = False):
        val, found = _get_nested(config, dotted_key)
        if not found:
            errors.append(f"{dotted_key}: missing from config")
            return
        if integer and not isinstance(val, int):
            errors.append(f"{dotted_key}: must be an integer, got {type(val).__name__}")
            return
        if not isinstance(val, (int, float)):
            errors.append(f"{dotted_key}: must be a number, got {type(val).__name__}")
            return
        if val < lo or val > hi:
            errors.append(f"{dotted_key}: {val} not in [{lo}, {hi}]")

    def _check_positive(dotted_key: str, *, integer: bool = False, strict: bool = True):
        val, found = _get_nested(config, dotted_key)
        if not found:
            errors.append(f"{dotted_key}: missing from config")
            return
        if integer and not isinstance(val, int):
            errors.append(f"{dotted_key}: must be an integer, got {type(val).__name__}")
            return
        if not isinstance(val, (int, float)):
            errors.append(f"{dotted_key}: must be a number, got {type(val).__name__}")
            return
        if strict and val <= 0:
            errors.append(f"{dotted_key}: must be > 0, got {val}")
        elif not strict and val < 0:
            errors.append(f"{dotted_key}: must be >= 0, got {val}")

    # Money-critical knobs: settings.yaml values must land inside these ranges at
    # load (a typo like kelly_fraction: 8.0 for 0.08 is rejected, not deployed).
    _check_range("signal.atr_sigma_ratio", 1.2, 2.5)
    _check_range("signal.student_t_df", 3, 8, integer=True)
    _check_range("signal.min_atr", 8.0, 25.0)
    _check_range("signal.atr_regime_shift_threshold", 0.40, 0.80)
    _check_range("math.kelly_fraction", 0.04, 0.18)
    _check_range("signal.min_edge", 0.02, 0.10)
    _check_range("signal.min_kelly", 0.005, 0.04)
    _check_range("signal.min_model_probability", 0.52, 0.70)
    _check_range("signal.exit_edge_threshold", -0.10, -0.03)

    _check_range("signal.max_edge", 0.15, 0.30)

    _check_positive("execution.max_concurrent_positions", integer=True)
    _check_range("execution.max_bankroll_deployed", 0.0, 1.0)
    _check_range("execution.max_book_fill_pct", 0.0, 1.0)
    _check_positive("execution.initial_bankroll")
    _check_range("execution.slippage_impact_pct", 0.0, 0.20)
    _check_positive("market.entry_window_seconds")
    _check_range("market.min_time_remaining_seconds", 0, 120)
    _check_range("market.max_spread", 0.0, 1.0)
    for cb_key in ("circuit_breaker.losses_to_reduce", "circuit_breaker.wins_to_restore"):
        _check_positive(cb_key, integer=True)

    if errors:
        header = f"Config validation failed with {len(errors)} error(s):"
        detail = "\n  - ".join([""] + errors)
        raise ValueError(header + detail)

def load_config(config_path: str | Path | None = None, env_path: str | Path | None = None) -> dict[str, Any]:
    global _config
    config_dir = Path(__file__).parent
    if env_path is None:
        env_path = config_dir / ".env"
    load_dotenv(env_path)
    if config_path is None:
        config_path = config_dir / "settings.yaml"
    with open(config_path, "r") as f:
        _config = yaml.safe_load(f)
    validate_config(_config)
    return _config

def get_config() -> dict[str, Any]:
    if _config is None:
        return load_config()
    return _config

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
