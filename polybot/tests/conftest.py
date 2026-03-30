import os
import tempfile
from pathlib import Path
import pytest
import yaml

SAMPLE_CONFIG = {
    "mode": "paper",
    "scanner": {"interval_seconds": 300, "max_markets_per_cycle": 100},
    "filters": {
        "min_volume_24h": 1000,
        "min_liquidity": 500,
        "min_days_to_expiry": 2,
        "max_days_to_expiry": 60,
        "max_spread": 0.05,
        "category_whitelist": [],
        "category_blacklist": [],
    },
    "brain": {
        "model": "claude-sonnet-4-6",
        "min_confidence": "high",
        "min_probability": 0.65,
        "active_prompt_version": "v001",
        "max_concurrent_calls": 10,
    },
    "math": {
        "ev_threshold": 0.05,
        "kelly_fraction": 0.25,
        "entry_discount": 0.85,
        "exit_target": 0.90,
        "stop_loss_pct": 0.15,
        "time_stop_hours": 24,
        "time_stop_min_gain": 0.02,
        "bayesian_price_trigger": 0.08,
        "bayesian_volume_trigger": 3.0,
    },
    "execution": {
        "max_slippage": 0.02,
        "max_bankroll_deployed": 0.80,
        "max_concurrent_positions": 5,
        "initial_bankroll": 100.0,
    },
    "agents": {
        "outcome_reviewer_interval_seconds": 3600,
        "daily_pipeline_hour": 2,
        "prompt_optimizer_min_improvement": 0.03,
        "prompt_optimizer_backtest_count": 50,
    },
    "discord": {
        "trade_channel_name": "polybot-trades",
        "control_channel_name": "polybot-control",
    },
    "database": {"path": ":memory:"},
}

@pytest.fixture
def sample_config(tmp_path):
    config_file = tmp_path / "settings.yaml"
    with open(config_file, "w") as f:
        yaml.dump(SAMPLE_CONFIG, f)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=test-key\n"
        "DISCORD_BOT_TOKEN=test-token\n"
        "POLYMARKET_API_KEY=test-pm-key\n"
        "POLYMARKET_SECRET=test-pm-secret\n"
        "POLYMARKET_PASSPHRASE=test-pm-pass\n"
        "ALCHEMY_RPC_URL=https://test-rpc\n"
        "PRIVATE_KEY=0xdeadbeef\n"
    )
    return {"config_path": str(config_file), "env_path": str(env_file)}

@pytest.fixture
def loaded_config(sample_config):
    from polybot.config.loader import load_config
    return load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
