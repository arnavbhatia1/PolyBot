import os
import tempfile
from pathlib import Path
import pytest
import yaml

SAMPLE_CONFIG = {
    "mode": "paper",
    "math": {
        "kelly_fraction": 0.15,
    },
    "execution": {
        "max_slippage": 0.02,
        "max_bankroll_deployed": 0.80,
        "max_concurrent_positions": 1,
        "initial_bankroll": 1000.0,
    },
    "agents": {
        "outcome_reviewer_interval_seconds": 3600,
        "daily_pipeline_hour": 2,
    },
    "discord": {
        "trade_channel_name": "polybot-trades",
        "control_channel_name": "polybot-control",
    },
    "database": {"path": ":memory:"},
    "signal": {
        "entry_threshold": 0.10,
        "exit_edge_threshold": -0.05,
        "momentum_weight": 0.08,
        "weights": {
            "rsi": 0.20,
            "macd": 0.25,
            "stochastic": 0.20,
            "obv": 0.15,
            "vwap": 0.20,
        },
        "active_weights_version": "weights_v001",
    },
    "market": {
        "contract_type": "btc_5min",
        "entry_window_seconds": 300,
        "min_time_remaining_seconds": 5,
        "scan_cache_seconds": 5,
    },
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
