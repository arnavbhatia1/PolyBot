import copy
import os
import pytest
from polybot.config.loader import load_config, get_config, get_secret, validate_config

# ---------------------------------------------------------------------------
# Existing loader tests
# ---------------------------------------------------------------------------

def test_load_config_returns_dict(sample_config):
    config = load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
    assert isinstance(config, dict)
    assert config["mode"] == "paper"

def test_load_config_has_all_sections(loaded_config):
    for section in ["math", "execution", "agents", "discord", "database"]:
        assert section in loaded_config

def test_get_config_returns_cached(loaded_config):
    config = get_config()
    assert config is loaded_config

def test_get_secret_returns_env_var(sample_config):
    load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
    assert get_secret("ANTHROPIC_API_KEY") == "test-key"

def test_get_secret_raises_on_missing():
    with pytest.raises(ValueError, match="Missing required secret"):
        get_secret("NONEXISTENT_SECRET_KEY_XYZ")


# ---------------------------------------------------------------------------
# Helper: deep-copy the conftest SAMPLE_CONFIG for mutation in tests
# ---------------------------------------------------------------------------

from polybot.tests.conftest import SAMPLE_CONFIG

def _valid_config() -> dict:
    """Return a fresh deep copy of the valid sample config."""
    return copy.deepcopy(SAMPLE_CONFIG)


def _set_nested(cfg: dict, dotted_key: str, value):
    """Set a value in a nested dict using 'section.key' notation."""
    keys = dotted_key.split(".")
    current = cfg
    for k in keys[:-1]:
        current = current[k]
    current[keys[-1]] = value


def _del_nested(cfg: dict, dotted_key: str):
    """Delete a key from a nested dict."""
    keys = dotted_key.split(".")
    current = cfg
    for k in keys[:-1]:
        current = current[k]
    del current[keys[-1]]


# ---------------------------------------------------------------------------
# Valid config passes
# ---------------------------------------------------------------------------

class TestValidateConfigPasses:
    def test_sample_config_valid(self):
        validate_config(_valid_config())  # should not raise

    def test_boundary_low_values(self):
        """All parameters at their minimum allowed values."""
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.05)
        _set_nested(cfg, "signal.entry_threshold", 0.02)
        _set_nested(cfg, "signal.min_kelly", 0.005)
        _set_nested(cfg, "signal.atr_sigma_ratio", 1.2)
        _set_nested(cfg, "signal.exit_edge_threshold", -0.25)
        _set_nested(cfg, "signal.min_model_probability", 0.52)
        _set_nested(cfg, "signal.momentum_weight", 0.02)
        _set_nested(cfg, "signal.regime_weight", 0.02)
        _set_nested(cfg, "signal.flow_weight", 0.02)
        _set_nested(cfg, "signal.student_t_df", 3)
        _set_nested(cfg, "execution.max_concurrent_positions", 1)
        _set_nested(cfg, "execution.max_bankroll_deployed", 0.0)
        _set_nested(cfg, "execution.max_book_fill_pct", 0.0)
        _set_nested(cfg, "execution.initial_bankroll", 0.01)
        _set_nested(cfg, "execution.slippage_impact_pct", 0.0)
        _set_nested(cfg, "market.entry_window_seconds", 1)
        _set_nested(cfg, "market.min_time_remaining_seconds", 0)
        _set_nested(cfg, "market.max_spread", 0.0)
        _set_nested(cfg, "circuit_breaker.losses_to_reduce", 1)
        _set_nested(cfg, "circuit_breaker.wins_to_restore", 1)
        validate_config(cfg)

    def test_boundary_high_values(self):
        """All parameters at their maximum allowed values."""
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.25)
        _set_nested(cfg, "signal.entry_threshold", 0.10)
        _set_nested(cfg, "signal.min_kelly", 0.04)
        _set_nested(cfg, "signal.atr_sigma_ratio", 2.5)
        _set_nested(cfg, "signal.exit_edge_threshold", 0.0)
        _set_nested(cfg, "signal.min_model_probability", 0.70)
        _set_nested(cfg, "signal.momentum_weight", 0.10)
        _set_nested(cfg, "signal.regime_weight", 0.10)
        _set_nested(cfg, "signal.flow_weight", 0.12)
        _set_nested(cfg, "signal.student_t_df", 8)
        _set_nested(cfg, "execution.max_bankroll_deployed", 1.0)
        _set_nested(cfg, "execution.max_book_fill_pct", 1.0)
        _set_nested(cfg, "execution.slippage_impact_pct", 0.20)
        _set_nested(cfg, "market.min_time_remaining_seconds", 120)
        _set_nested(cfg, "market.max_spread", 1.0)
        validate_config(cfg)


# ---------------------------------------------------------------------------
# Missing fields
# ---------------------------------------------------------------------------

class TestValidateConfigMissing:
    @pytest.mark.parametrize("key", [
        "math.kelly_fraction",
        "signal.entry_threshold",
        "signal.exit_edge_threshold",
        "signal.min_model_probability",
        "signal.momentum_weight",
        "signal.regime_weight",
        "signal.flow_weight",
        "signal.student_t_df",
        "signal.min_kelly",
        "signal.atr_sigma_ratio",
        "signal.weights",
        "execution.max_concurrent_positions",
        "execution.max_bankroll_deployed",
        "execution.max_book_fill_pct",
        "execution.initial_bankroll",
        "execution.slippage_impact_pct",
        "market.entry_window_seconds",
        "market.min_time_remaining_seconds",
        "market.max_spread",
        "circuit_breaker.losses_to_reduce",
        "circuit_breaker.wins_to_restore",
    ])
    def test_missing_field_is_reported(self, key):
        cfg = _valid_config()
        _del_nested(cfg, key)
        with pytest.raises(ValueError, match=f"{key}: missing from config"):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Out-of-range values
# ---------------------------------------------------------------------------

class TestValidateConfigOutOfRange:
    @pytest.mark.parametrize("key, bad_value, expected_fragment", [
        # math
        ("math.kelly_fraction", 0.01, "not in [0.05, 0.25]"),
        ("math.kelly_fraction", 0.50, "not in [0.05, 0.25]"),
        # signal scalars
        ("signal.entry_threshold", 0.001, "not in [0.02, 0.1]"),
        ("signal.entry_threshold", 0.20, "not in [0.02, 0.1]"),
        ("signal.exit_edge_threshold", -0.30, "not in [-0.25, 0.0]"),
        ("signal.exit_edge_threshold", 0.01, "not in [-0.25, 0.0]"),
        ("signal.min_model_probability", 0.50, "not in [0.52, 0.7]"),
        ("signal.min_model_probability", 0.90, "not in [0.52, 0.7]"),
        ("signal.momentum_weight", -0.15, "not in [-0.1, 0.1]"),
        ("signal.momentum_weight", 0.15, "not in [-0.1, 0.1]"),
        ("signal.regime_weight", 0.01, "not in [0.02, 0.1]"),
        ("signal.regime_weight", 0.15, "not in [0.02, 0.1]"),
        ("signal.flow_weight", 0.01, "not in [0.02, 0.12]"),
        ("signal.flow_weight", 0.15, "not in [0.02, 0.12]"),
        ("signal.student_t_df", 2, "not in [3, 8]"),
        ("signal.student_t_df", 9, "not in [3, 8]"),
        ("signal.min_kelly", 0.001, "not in [0.005, 0.04]"),
        ("signal.min_kelly", 0.10, "not in [0.005, 0.04]"),
        ("signal.atr_sigma_ratio", 1.0, "not in [1.2, 2.5]"),
        ("signal.atr_sigma_ratio", 3.0, "not in [1.2, 2.5]"),
        # execution
        ("execution.max_bankroll_deployed", -0.1, "not in [0.0, 1.0]"),
        ("execution.max_bankroll_deployed", 1.1, "not in [0.0, 1.0]"),
        ("execution.max_book_fill_pct", -0.1, "not in [0.0, 1.0]"),
        ("execution.max_book_fill_pct", 1.1, "not in [0.0, 1.0]"),
        ("execution.initial_bankroll", 0, "must be > 0"),
        ("execution.initial_bankroll", -100, "must be > 0"),
        ("execution.slippage_impact_pct", -0.01, "not in [0.0, 0.2]"),
        ("execution.slippage_impact_pct", 0.25, "not in [0.0, 0.2]"),
        # market
        ("market.entry_window_seconds", 0, "must be > 0"),
        ("market.entry_window_seconds", -1, "must be > 0"),
        ("market.min_time_remaining_seconds", -1, "not in [0, 120]"),
        ("market.min_time_remaining_seconds", 200, "not in [0, 120]"),
        ("market.max_spread", -0.1, "not in [0.0, 1.0]"),
        ("market.max_spread", 1.5, "not in [0.0, 1.0]"),
        # circuit breaker
        ("circuit_breaker.losses_to_reduce", 0, "must be > 0"),
        ("circuit_breaker.losses_to_reduce", -1, "must be > 0"),
        ("circuit_breaker.wins_to_restore", 0, "must be > 0"),
    ])
    def test_out_of_range(self, key, bad_value, expected_fragment):
        cfg = _valid_config()
        _set_nested(cfg, key, bad_value)
        with pytest.raises(ValueError, match=key):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Type checks
# ---------------------------------------------------------------------------

class TestValidateConfigTypes:
    def test_student_t_df_must_be_int(self):
        cfg = _valid_config()
        _set_nested(cfg, "signal.student_t_df", 4.5)
        with pytest.raises(ValueError, match="student_t_df.*integer"):
            validate_config(cfg)

    def test_max_concurrent_positions_must_be_int(self):
        cfg = _valid_config()
        _set_nested(cfg, "execution.max_concurrent_positions", 1.5)
        with pytest.raises(ValueError, match="max_concurrent_positions.*integer"):
            validate_config(cfg)

    def test_circuit_breaker_must_be_int(self):
        cfg = _valid_config()
        _set_nested(cfg, "circuit_breaker.losses_to_reduce", 3.0)
        with pytest.raises(ValueError, match="losses_to_reduce.*integer"):
            validate_config(cfg)

    def test_string_value_rejected(self):
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", "high")
        with pytest.raises(ValueError, match="kelly_fraction.*must be a number"):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# signal.weights validation
# ---------------------------------------------------------------------------

class TestValidateConfigWeights:
    def test_weight_below_minimum(self):
        cfg = _valid_config()
        cfg["signal"]["weights"]["rsi"] = 0.01
        # Fix sum so we only trigger the individual-weight error
        cfg["signal"]["weights"]["macd"] = 0.44
        with pytest.raises(ValueError, match="signal.weights.rsi.*0.01.*0.05"):
            validate_config(cfg)

    def test_weights_do_not_sum_to_one(self):
        cfg = _valid_config()
        cfg["signal"]["weights"]["rsi"] = 0.30  # was 0.20, total becomes 1.10
        with pytest.raises(ValueError, match="signal.weights.*sum"):
            validate_config(cfg)

    def test_weights_sum_within_tolerance(self):
        cfg = _valid_config()
        # Nudge slightly but stay within 0.001
        cfg["signal"]["weights"]["rsi"] = 0.2005
        cfg["signal"]["weights"]["macd"] = 0.2495
        validate_config(cfg)  # should pass

    def test_weights_not_a_dict(self):
        cfg = _valid_config()
        cfg["signal"]["weights"] = [0.2, 0.25, 0.2, 0.15, 0.2]
        with pytest.raises(ValueError, match="signal.weights.*must be a dict"):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Multiple errors reported at once
# ---------------------------------------------------------------------------

class TestValidateConfigMultipleErrors:
    def test_multiple_violations_all_listed(self):
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.50)
        _set_nested(cfg, "signal.entry_threshold", 0.001)
        _set_nested(cfg, "execution.initial_bankroll", -1)
        with pytest.raises(ValueError) as exc_info:
            validate_config(cfg)
        msg = str(exc_info.value)
        assert "kelly_fraction" in msg
        assert "entry_threshold" in msg
        assert "initial_bankroll" in msg
        assert "3 error(s)" in msg
