import os
import tempfile
import pytest
import yaml

# Isolate the whole suite from the real polybot/memory/ tree. paths.py reads
# POLYBOT_MEMORY_DIR at import, so set it HERE — before any polybot module imports
# paths — to a throwaway dir. Without this, tests using default state paths
# (live_trader fill/orphan stats, adverse-selection state, gate stats, …) read and
# clobber live production state, and re-importing a module would defeat per-attr patches.
if "POLYBOT_MEMORY_DIR" not in os.environ:
    os.environ["POLYBOT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="polybot-test-mem-")

SAMPLE_CONFIG = {
    "mode": "paper",
    "math": {
        "kelly_fraction": 0.15,
    },
    "circuit_breaker": {
        "max_drawdown_pct": 0.15,
        "floor_pct": 0.85,
        "min_multiplier": 0.25,
        "losses_to_reduce": 3,
        "wins_to_restore": 2,
    },
    "execution": {
        "max_bankroll_deployed": 0.80,
        "max_concurrent_positions": 1,
        "max_book_fill_pct": 0.50,
        "slippage_impact_pct": 0.03,
        "fok_spread_cross_floor": 0.08,
        "initial_bankroll": 1000.0,
    },
    "agents": {
        "outcome_reviewer_interval_seconds": 3600,
        "daily_pipeline_hour": 0,
    },
    "discord": {
        "trade_channel_name": "polybot-trades",
        "control_channel_name": "polybot-control",
    },
    "database": {"path": ":memory:"},
    "signal": {
        "min_edge": 0.03,
        "max_edge": 0.20,
        "exit_edge_threshold": -0.05,
        "min_model_probability": 0.65,
        "student_t_df": 4,
        "min_kelly": 0.015,
        "atr_sigma_ratio": 1.7,
        "min_atr": 8.0,
        "deep_loss_hold_threshold": -0.10,
        "atr_regime_shift_threshold": 0.60,
        "loss_cut_fraction": 0.65,
        "loss_cut_time_s": 90.0,
        "regime_lookback": 50,
        "adverse_selection_threshold": 0.80,
        "adverse_penalty_floor": 0.45,
        "adverse_penalty_slope": 1.5,
        "adverse_penalty_min": 0.30,
        "edge_decay_threshold": -0.05,
    },
    "entry_timing": {
        "normal_fraction": 0.6,
        "late_max_penalty": 0.30,
        "flip_edge_premium": 0.015,
    },
    "market": {
        "entry_window_seconds": 300,
        "min_time_remaining_seconds": 5,
        "scan_cache_seconds": 5,
        "max_spread": 0.10,
    },
    "late_window": {
        "sniper_enabled": False,
        "twap_zone_s": 30.0,
        "twap_k_min_s": 0.8,
        "sniper_min_edge": 0.04,
        "sniper_max_edge": 0.50,
        "sniper_fok_slip": 0.01,
    },
    "open_window": {
        "open_edge_enabled": False,
        "open_zone_s": 20.0,
        "open_min_edge": 0.06,
    },
}

@pytest.fixture
def sample_config(tmp_path):
    config_file = tmp_path / "settings.yaml"
    with open(config_file, "w") as f:
        yaml.dump(SAMPLE_CONFIG, f)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POLYMARKET_API_KEY=test-pm-key\n"
        "POLYMARKET_SECRET=test-pm-secret\n"
        "ANTHROPIC_API_KEY=test-key\n"
        "DISCORD_BOT_TOKEN=test-token\n"
    )
    return {"config_path": str(config_file), "env_path": str(env_file)}

@pytest.fixture
def loaded_config(sample_config):
    from polybot.config.loader import load_config
    return load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
