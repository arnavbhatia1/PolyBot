# polybot/config/loader.py
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_config: dict[str, Any] | None = None


def _get_nested(config: dict[str, Any], dotted_key: str) -> tuple[Any, bool]:
    """Retrieve a value from a nested dict using 'section.key' notation.
    Returns (value, True) if found, (None, False) if any segment is missing.
    """
    keys = dotted_key.split(".")
    current = config
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return None, False
        current = current[k]
    return current, True


def validate_config(config: dict[str, Any]) -> None:
    """Validate settings values are within acceptable ranges.

    Raises ValueError listing ALL violations if any are found.
    """
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

    # --- math ---
    _check_range("math.kelly_fraction", 0.05, 0.25)

    # --- signal ---
    _check_range("signal.entry_threshold", 0.01, 0.10)
    _check_range("signal.max_edge", 0.10, 0.30)
    _check_range("signal.exit_edge_threshold", -0.25, 0.0)
    _check_range("signal.min_model_probability", 0.55, 0.85)
    _check_range("signal.momentum_weight", -0.10, 0.10)  # negative = fade (mean reversion)
    _check_range("signal.regime_weight", 0.02, 0.10)
    _check_range("signal.flow_weight", 0.02, 0.12)
    _check_range("signal.student_t_df", 3, 8, integer=True)
    _check_range("signal.min_kelly", 0.005, 0.05)
    _check_range("signal.atr_sigma_ratio", 1.2, 2.5)
    _check_range("signal.min_atr", 1.0, 30.0)

    # --- signal.weights ---
    weights_val, weights_found = _get_nested(config, "signal.weights")
    if not weights_found:
        errors.append("signal.weights: missing from config")
    elif not isinstance(weights_val, dict):
        errors.append(f"signal.weights: must be a dict, got {type(weights_val).__name__}")
    else:
        for name, w in weights_val.items():
            if not isinstance(w, (int, float)):
                errors.append(f"signal.weights.{name}: must be a number, got {type(w).__name__}")
            elif w < 0.05:
                errors.append(f"signal.weights.{name}: {w} < 0.05 minimum")
        numeric_weights = [v for v in weights_val.values() if isinstance(v, (int, float))]
        if numeric_weights:
            total = sum(numeric_weights)
            if abs(total - 1.0) > 0.001:
                errors.append(
                    f"signal.weights: sum is {total:.4f}, must be 1.0 (within 0.001 tolerance)"
                )

    # --- execution ---
    _check_positive("execution.max_concurrent_positions", integer=True)
    _check_range("execution.max_bankroll_deployed", 0.0, 1.0)
    _check_range("execution.max_single_position_pct", 0.05, 0.30)
    _check_range("execution.max_book_fill_pct", 0.0, 1.0)
    _check_positive("execution.initial_bankroll")
    _check_range("execution.slippage_impact_pct", 0.0, 0.20)

    # --- market ---
    _check_positive("market.entry_window_seconds")
    _check_range("market.min_time_remaining_seconds", 0, 120)
    _check_range("market.max_spread", 0.0, 1.0)

    # --- circuit_breaker ---
    for cb_key in ("circuit_breaker.losses_to_reduce", "circuit_breaker.wins_to_restore"):
        _check_positive(cb_key, integer=True)

    # Signal layer weights (optional — only validate if present)
    for key, lo, hi in [
        ("signal.spot_flow_weight", 0.0, 0.15),
        ("signal.prev_margin_weight", 0.0, 0.05),
        ("signal.liquidation_weight", 0.0, 0.10),
        ("signal.logit_scale", 1.0, 10.0),
        ("signal.probability_compression", 0.1, 1.0),
        ("signal.consensus_dead_zone", 0.0, 0.20),
    ]:
        val, found = _get_nested(config, key)
        if found:
            _check_range(key, lo, hi)

    # IV ratio bounds (optional)
    for key, lo, hi in [
        ("deribit.iv_ratio_min", 0.1, 1.0),
        ("deribit.iv_ratio_max", 1.0, 10.0),
    ]:
        val, found = _get_nested(config, key)
        if found:
            _check_range(key, lo, hi)

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


def save_config(config: dict[str, Any], config_path: str | Path | None = None) -> None:
    """Write the config dict back to settings.yaml so pipeline-tuned values survive restarts.

    Uses atomic write (temp file + rename) to prevent corrupt config on crash.
    """
    config_dir = Path(__file__).parent
    if config_path is None:
        config_path = config_dir / "settings.yaml"
    config_path = Path(config_path)
    # Atomic write: write to temp file, then rename over the target
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, str(config_path))
    except Exception:
        # Clean up temp file if rename failed
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
