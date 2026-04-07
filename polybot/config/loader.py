# polybot/config/loader.py
import os
import tempfile
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


def save_config(config: dict, config_path: str | None = None) -> None:
    """Write the config dict back to settings.yaml so pipeline-tuned values survive restarts.

    Uses atomic write (temp file + rename) to prevent corrupt config on crash.
    """
    config_dir = Path(__file__).parent
    if config_path is None:
        config_path = config_dir / "settings.yaml"
    config_path = Path(config_path)
    # Atomic write: write to temp file, then rename over the target
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, str(config_path))
    except Exception:
        # Clean up temp file if rename failed
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
