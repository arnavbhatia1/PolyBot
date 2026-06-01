from __future__ import annotations

import os
import tempfile
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

def _set_nested_ruamel(doc: Any, dotted_key: str, value: Any) -> None:
    """Set a value in a ruamel CommentedMap by dotted path.

    Creates intermediate maps if missing so a fresh ParamSpec that references a
    new nested section (e.g. ``signal.derived.x``) doesn't crash save_config
    when the section hasn't been written yet.
    """
    from ruamel.yaml.comments import CommentedMap
    keys = dotted_key.split(".")
    d = doc
    for k in keys[:-1]:
        if k not in d:
            d[k] = CommentedMap()
        d = d[k]
    d[keys[-1]] = value

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

    from polybot.config.param_registry import PIPELINE_PARAMS
    for _spec in PIPELINE_PARAMS:
        _check_range(_spec.yaml_key, _spec.lo, _spec.hi, integer=(_spec.cast is int))

    _check_range("signal.max_edge", 0.15, 0.30)
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
                errors.append(f"signal.weights: sum is {total:.4f}, must be 1.0")

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

    val, found = _get_nested(config, "signal.consensus_dead_zone")
    if found:
        _check_range("signal.consensus_dead_zone", 0.0, 0.20)

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
    """Write pipeline-adopted values back into settings.yaml, preserving all comments.

    Uses ruamel.yaml round-trip mode: loads the file as a CommentedMap (which
    stores inline comments, block comments, and formatting), patches only the
    changed values, then writes back. Your hand-written comments survive.
    """
    from ruamel.yaml import YAML
    from polybot.paths import is_pipeline_frozen

    # Freeze gate: when the sentinel is present, every pipeline-adopted param write
    # (weight optimizer, regime/revert adoptions, crisis-mode kelly) is suppressed so
    # the live strategy stays fixed for a clean multi-day measurement. See paths.py.
    if is_pipeline_frozen():
        import logging
        logging.getLogger("polybot").warning(
            "PIPELINE FROZEN — save_config() suppressed; settings.yaml unchanged "
            "(delete memory/state/PIPELINE_FROZEN to resume adoption)"
        )
        return

    config_dir = Path(__file__).parent
    if config_path is None:
        config_path = config_dir / "settings.yaml"
    config_path = Path(config_path)

    ryaml = YAML()
    ryaml.preserve_quotes = True
    ryaml.width = 120

    with open(config_path, "r") as f:
        doc = ryaml.load(f)

    # Patch pipeline-tunable params
    from polybot.config.param_registry import PIPELINE_PARAMS
    for spec in PIPELINE_PARAMS:
        val, found = _get_nested(config, spec.yaml_key)
        if found:
            _set_nested_ruamel(doc, spec.yaml_key, spec.cast(val))

    # Patch signal.weights
    weights, found = _get_nested(config, "signal.weights")
    if found and isinstance(weights, dict):
        for k, v in weights.items():
            doc["signal"]["weights"][k] = round(float(v), 4)

    # Patch math.kelly_fraction (crisis mode can change it)
    kf, found = _get_nested(config, "math.kelly_fraction")
    if found:
        doc["math"]["kelly_fraction"] = round(float(kf), 4)

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            ryaml.dump(doc, f)
        os.replace(tmp_path, str(config_path))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
