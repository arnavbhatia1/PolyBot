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
    "circuit_breaker": {
        "max_drawdown_pct": 0.15,
        "min_multiplier": 0.25,
        "losses_to_reduce": 3,
        "wins_to_restore": 2,
    },
    "execution": {
        "max_slippage": 0.02,
        "max_bankroll_deployed": 0.80,
        "max_concurrent_positions": 1,
        "max_book_fill_pct": 0.50,
        "slippage_impact_pct": 0.03,
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
        "momentum_weight": 0.08,
        "regime_weight": 0.03,
        "flow_weight": 0.04,
        "student_t_df": 4,
        "min_kelly": 0.015,
        "atr_sigma_ratio": 1.7,
        "min_atr": 8.0,
        "logit_scale": 4.0,
        "spot_flow_weight": 0.04,
        "liquidation_weight": 0.03,
        "prev_margin_weight": 0.02,
        "regime_momentum_threshold": 0.15,
        "flow_combined_cap": 0.35,
        "final_logit_clamp": 4.0,
        "deep_loss_hold_threshold": -0.10,
        "l5_regime_damp_cap": 0.7,
        "atr_regime_shift_threshold": 0.60,
        "weights": {
            "rsi": 0.20,
            "macd": 0.25,
            "stochastic": 0.20,
            "obv": 0.15,
            "vwap": 0.20,
        },
        # L6 derived weights (all 0.0 = layer inert)
        "derived": {
            "log_atr_ratio": 0.0,
            "autocorr_signed_mag": 0.0,
            "vol_regime_shift": 0.0,
            "flow_disagreement": 0.0,
            "distance_atr_ratio": 0.0,
            "time_remaining_logit": 0.0,
            "liq_signed_sqrt": 0.0,
            "prev_margin_sq": 0.0,
        },
    },
    "entry_timing": {
        "normal_fraction": 0.6,
        "late_max_penalty": 0.30,
        "flip_edge_premium": 0.015,
    },
    "market": {
        "contract_type": "btc_5min",
        "entry_window_seconds": 300,
        "min_time_remaining_seconds": 5,
        "scan_cache_seconds": 5,
        "max_spread": 0.10,
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
