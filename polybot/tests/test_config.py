import copy
import pytest
from polybot.config.loader import load_config, get_secret, validate_config
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
        _set_nested(cfg, "math.kelly_fraction", 0.04)
        _set_nested(cfg, "execution.max_concurrent_positions", 1)
        _set_nested(cfg, "execution.max_bankroll_deployed", 0.0)
        _set_nested(cfg, "execution.max_book_fill_pct", 0.0)
        _set_nested(cfg, "execution.initial_bankroll", 0.01)
        _set_nested(cfg, "execution.slippage_impact_pct", 0.0)
        _set_nested(cfg, "market.min_time_remaining_seconds", 0)
        _set_nested(cfg, "market.max_spread", 0.0)
        _set_nested(cfg, "circuit_breaker.losses_to_reduce", 1)
        _set_nested(cfg, "circuit_breaker.wins_to_restore", 1)
        validate_config(cfg)

    def test_boundary_high_values(self):
        """All parameters at their maximum allowed values."""
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.18)
        _set_nested(cfg, "execution.max_bankroll_deployed", 1.0)
        _set_nested(cfg, "execution.max_book_fill_pct", 1.0)
        _set_nested(cfg, "execution.slippage_impact_pct", 0.20)
        _set_nested(cfg, "market.min_time_remaining_seconds", 120)
        _set_nested(cfg, "market.max_spread", 1.0)
        validate_config(cfg)


# ---------------------------------------------------------------------------
# Validation — kelly_fraction floor (registry lo = 0.04). The loader must
# accept the floor exactly and reject anything below it.
# ---------------------------------------------------------------------------

class TestKellyFloor:
    def test_kelly_at_floor_loads(self):
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.04)
        validate_config(cfg)  # should not raise

    def test_kelly_below_floor_raises(self):
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", 0.039)
        with pytest.raises(ValueError, match="kelly_fraction"):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Validation — missing-field detection. Representative coverage across
# sections; per-key exhaustion was redundant churn for the validator framework.
# ---------------------------------------------------------------------------

class TestValidateSniperKnobs:
    """The sniper is the ONLY capital-deploying strategy — a fat-fingered knob
    must be rejected at load, not deployed."""

    @pytest.mark.parametrize("key,bad", [
        ("late_window.sniper_fok_slip", 0.10),   # 10x the calibrated pad
        ("late_window.twap_zone_s", 75.0),       # projection undefined before the 60s window
        ("late_window.twap_k_min_s", 20.0),      # would veto most of the dip zone
        ("late_window.sniper_min_edge", 0.001),  # fires on no defended edge
    ])
    def test_out_of_range_sniper_knob_rejected(self, key, bad):
        cfg = _valid_config()
        _set_nested(cfg, key, bad)
        with pytest.raises(ValueError, match=key.replace(".", r"\.")):
            validate_config(cfg)

    def test_production_values_pass(self):
        validate_config(_valid_config())  # conftest mirrors settings.yaml


class TestValidateConfigMissing:
    @pytest.mark.parametrize("key", [
        "math.kelly_fraction",       # registry-driven check
        "late_window.twap_zone_s",    # leg-zone check
        "execution.initial_bankroll", # execution section
        "late_window.sniper_fok_slip",  # sniper knobs are money-critical too
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
        ("late_window.sniper_min_edge", 0.001),              # float lower
        ("maker.post_close_hold_s", 500.0),                  # float upper
        ("circuit_breaker.losses_to_reduce", 0),             # positive int
        ("execution.initial_bankroll", -100),                # must be > 0
        ("execution.max_bankroll_deployed", 1.1),            # percent upper
    ])
    def test_out_of_range(self, key, bad_value):
        cfg = _valid_config()
        _set_nested(cfg, key, bad_value)
        with pytest.raises(ValueError, match=key):
            validate_config(cfg)


# ---------------------------------------------------------------------------
# Validation — types & multi-error
# ---------------------------------------------------------------------------

class TestValidateConfigTypes:
    def test_int_field_rejects_float(self):
        cfg = _valid_config()
        _set_nested(cfg, "execution.max_concurrent_positions", 4.5)
        with pytest.raises(ValueError, match="max_concurrent_positions.*integer"):
            validate_config(cfg)

    def test_numeric_field_rejects_string(self):
        cfg = _valid_config()
        _set_nested(cfg, "math.kelly_fraction", "high")
        with pytest.raises(ValueError, match="kelly_fraction.*must be a number"):
            validate_config(cfg)


def test_multiple_violations_all_listed():
    cfg = _valid_config()
    _set_nested(cfg, "math.kelly_fraction", 0.50)
    _set_nested(cfg, "late_window.sniper_min_edge", 0.001)
    _set_nested(cfg, "execution.initial_bankroll", -1)
    with pytest.raises(ValueError) as exc_info:
        validate_config(cfg)
    msg = str(exc_info.value)
    assert "kelly_fraction" in msg
    assert "sniper_min_edge" in msg
    assert "initial_bankroll" in msg
    assert "3 error(s)" in msg
