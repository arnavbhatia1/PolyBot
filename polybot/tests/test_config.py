import copy
import pytest
from polybot.config.loader import load_config, get_config, get_secret, validate_config
from polybot.tests.conftest import SAMPLE_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_config() -> dict:
    return copy.deepcopy(SAMPLE_CONFIG)


def _set_nested(cfg: dict, dotted_key: str, value):
    keys = dotted_key.split(".")
    current = cfg
    for k in keys[:-1]:
        current = current[k]
    current[keys[-1]] = value


def _del_nested(cfg: dict, dotted_key: str):
    keys = dotted_key.split(".")
    current = cfg
    for k in keys[:-1]:
        current = current[k]
    del current[keys[-1]]


# ---------------------------------------------------------------------------
# Loader
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
    assert get_config() is loaded_config


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
# Validation — happy path
# ---------------------------------------------------------------------------

class TestValidateConfigPasses:
    def test_sample_config_valid(self):
        validate_config(_valid_config())  # should not raise

    def test_boundary_low_values(self):
        """All parameters at their minimum allowed values."""
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.05)
        _set_nested(cfg, "signal.min_edge", 0.02)
        _set_nested(cfg, "signal.min_kelly", 0.005)
        _set_nested(cfg, "signal.atr_sigma_ratio", 1.2)
        _set_nested(cfg, "signal.exit_edge_threshold", -0.10)
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
        _set_nested(cfg, "math.kelly_fraction", 0.18)
        _set_nested(cfg, "signal.min_edge", 0.10)
        _set_nested(cfg, "signal.min_kelly", 0.04)
        _set_nested(cfg, "signal.atr_sigma_ratio", 2.5)
        _set_nested(cfg, "signal.exit_edge_threshold", -0.03)
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
# Validation — missing-field detection. Representative coverage across
# sections; per-key exhaustion was redundant churn for the validator framework.
# ---------------------------------------------------------------------------

class TestValidateConfigMissing:
    @pytest.mark.parametrize("key", [
        "math.kelly_fraction",       # top-level math section
        "signal.weights",             # nested dict required field
        "execution.initial_bankroll", # execution section
    ])
    def test_missing_field_is_reported(self, key):
        cfg = _valid_config()
        _del_nested(cfg, key)
        with pytest.raises(ValueError, match=f"{key}: missing from config"):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Validation — out-of-range. One case per distinct validator-rule shape
# (float range, int range, signed range, must-be-positive, percent range).
# ---------------------------------------------------------------------------

class TestValidateConfigOutOfRange:
    @pytest.mark.parametrize("key, bad_value", [
        ("math.kelly_fraction", 0.50),                       # float upper
        ("signal.min_edge", 0.001),                          # float lower
        ("signal.exit_edge_threshold", 0.01),                # signed (must be negative)
        ("signal.student_t_df", 9),                          # int upper
        ("signal.momentum_weight", 0.15),                    # symmetric signed range
        ("execution.initial_bankroll", -100),                # must be > 0
        ("execution.max_bankroll_deployed", 1.1),            # percent upper
        ("market.entry_window_seconds", 0),                  # must be > 0 (int)
    ])
    def test_out_of_range(self, key, bad_value):
        cfg = _valid_config()
        _set_nested(cfg, key, bad_value)
        with pytest.raises(ValueError, match=key):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Validation — types & weights & multi-error
# ---------------------------------------------------------------------------

class TestValidateConfigTypes:
    def test_int_field_rejects_float(self):
        cfg = _valid_config()
        _set_nested(cfg, "signal.student_t_df", 4.5)
        with pytest.raises(ValueError, match="student_t_df.*integer"):
            validate_config(cfg)

    def test_numeric_field_rejects_string(self):
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", "high")
        with pytest.raises(ValueError, match="kelly_fraction.*must be a number"):
            validate_config(cfg)


class TestValidateConfigWeights:
    def test_weight_below_minimum(self):
        cfg = _valid_config()
        cfg["signal"]["weights"]["rsi"] = 0.01
        cfg["signal"]["weights"]["macd"] = 0.44  # fix sum so only individual error fires
        with pytest.raises(ValueError, match="signal.weights.rsi.*0.01.*0.05"):
            validate_config(cfg)

    def test_weights_do_not_sum_to_one(self):
        cfg = _valid_config()
        cfg["signal"]["weights"]["rsi"] = 0.30  # was 0.20, total becomes 1.10
        with pytest.raises(ValueError, match="signal.weights.*sum"):
            validate_config(cfg)

    def test_weights_sum_within_tolerance(self):
        cfg = _valid_config()
        cfg["signal"]["weights"]["rsi"] = 0.2005
        cfg["signal"]["weights"]["macd"] = 0.2495
        validate_config(cfg)  # should pass

    def test_weights_not_a_dict(self):
        cfg = _valid_config()
        cfg["signal"]["weights"] = [0.2, 0.25, 0.2, 0.15, 0.2]
        with pytest.raises(ValueError, match="signal.weights.*must be a dict"):
            validate_config(cfg)


def test_multiple_violations_all_listed():
    cfg = _valid_config()
    _set_nested(cfg, "math.kelly_fraction", 0.50)
    _set_nested(cfg, "signal.min_edge", 0.001)
    _set_nested(cfg, "execution.initial_bankroll", -1)
    with pytest.raises(ValueError) as exc_info:
        validate_config(cfg)
    msg = str(exc_info.value)
    assert "kelly_fraction" in msg
    assert "min_edge" in msg
    assert "initial_bankroll" in msg
    assert "3 error(s)" in msg
