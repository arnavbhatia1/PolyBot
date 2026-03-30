# polybot/config/loader.py
import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

_config = None

def load_config(config_path: str | None = None, env_path: str | None = None) -> dict:
    global _config
    config_dir = Path(__file__).parent
    if env_path is None:
        env_path = config_dir / ".env"
    load_dotenv(env_path)
    if config_path is None:
        config_path = config_dir / "settings.yaml"
    with open(config_path, "r") as f:
        _config = yaml.safe_load(f)
    return _config

def get_config() -> dict:
    if _config is None:
        return load_config()
    return _config

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
