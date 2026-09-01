from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
import yaml
from dotenv import load_dotenv

_config: dict[str, Any] | None = None


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """Reject duplicate YAML mapping keys at parse.

    PyYAML silently keeps the LAST duplicate — a duplicated top-level section
    wipes every knob in the first copy without a trace; fail at boot instead.
    """


def _no_dup_construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.YAMLError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1} — "
                "the second mapping would silently clobber the first")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_NoDuplicateKeysLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_construct_mapping)

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
    _check_range("math.kelly_fraction", 0.04, 0.18)
    _check_range("circuit_breaker.floor_pct", 0.50, 0.95)
    _check_range("circuit_breaker.min_multiplier", 0.10, 1.0)
    _check_range("execution.fok_spread_cross_floor", 0.0, 0.20)

    # Sniper knobs — the ONLY capital-deploying strategy, so a typo here deploys.
    # twap_zone_s hard-caps at 60: the projection is undefined before the
    # resolving 60s averaging window even starts.
    _check_range("late_window.twap_zone_s", 5.0, 60.0)
    _check_range("late_window.twap_k_min_s", 0.0, 15.0)
    _check_range("late_window.sniper_min_edge", 0.02, 0.10)
    ladder, found = _get_nested(config, "maker.maker_ladder")
    if not found or not (isinstance(ladder, list) and 1 <= len(ladder) <= 7
                         and all(isinstance(r, list) and len(r) == 3
                                 and 0.10 <= float(r[0]) <= 0.95
                                 and 0.0 < float(r[1]) <= 1.0
                                 and 0.05 <= float(r[2]) <= 3.0 for r in ladder)):
        errors.append("maker.maker_ladder: must be 1-7 rungs of "
                      "[price 0.10-0.95, frac 0-1, need 0.05-3]")
    # taker_enabled is the dormant taker's arm switch; absent means dormant.
    val, found = _get_nested(config, "late_window.taker_enabled")
    if found and not isinstance(val, bool):
        errors.append("late_window.taker_enabled: must be a boolean when present")
    _check_range("maker.maker_k_place_max", 5.0, 29.0)
    _check_range("maker.maker_k_place_min", 1.0, 15.0)
    _check_range("maker.maker_bankroll_frac", 0.0, 0.50)
    _check_range("maker.post_close_hold_s", 0.0, 120.0)
    val, found = _get_nested(config, "maker.maker_bid_enabled")
    if not found or not isinstance(val, bool):
        errors.append("maker.maker_bid_enabled: missing or not a boolean")
    _check_range("late_window.sniper_max_edge", 0.20, 0.60)
    _check_range("late_window.sniper_fok_slip", 0.0, 0.05)
    val, found = _get_nested(config, "late_window.require_max_tier")
    if not found or not isinstance(val, bool):
        errors.append("late_window.require_max_tier: missing or not a boolean")
    # trading_enabled is THE master brake. sniper_enabled is its retired name
    # (the burst sniper is long deleted) — aliased with a warning so an old
    # settings file still halts/runs correctly instead of KeyError-ing at boot.
    lw = config.get("late_window")
    if isinstance(lw, dict) and "trading_enabled" not in lw and "sniper_enabled" in lw:
        lw["trading_enabled"] = lw["sniper_enabled"]
        logging.getLogger("polybot").warning(
            "settings: late_window.sniper_enabled is deprecated — rename it to "
            "trading_enabled (aliased for this run)")
    val, found = _get_nested(config, "late_window.trading_enabled")
    if not found or not isinstance(val, bool):
        errors.append("late_window.trading_enabled: missing or not a boolean")
    epoch, found = _get_nested(config, "late_window.validation_epoch")
    if found and epoch is not None:
        from datetime import datetime as _dt
        try:
            parsed = _dt.fromisoformat(str(epoch).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            if str(epoch).endswith("Z"):
                # readers string-compare against '+00:00' ISO timestamps; a 'Z'
                # suffix sorts above them and silently excludes every fill
                raise ValueError
        except ValueError:
            errors.append("late_window.validation_epoch: must be tz-aware ISO with "
                          "+00:00 offset (not 'Z')")

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
    with open(config_path, "r", encoding="utf-8") as f:
        _config = yaml.load(f, Loader=_NoDuplicateKeysLoader)
    validate_config(_config)
    return _config

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
