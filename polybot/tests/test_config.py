import os
import pytest
from polybot.config.loader import load_config, get_config, get_secret

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
