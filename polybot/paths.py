"""Filesystem path constants — CWD-independent.

All persistent state under `polybot/memory/` is keyed off MEMORY_DIR so the
running directory of the bot/pipeline cannot leak a stray `polybot/polybot/memory/`
tree on disk. Anything else that needs a stable path off the package root
should compose from POLYBOT_DIR.
"""
from __future__ import annotations

from pathlib import Path

POLYBOT_DIR: Path = Path(__file__).resolve().parent
MEMORY_DIR: Path = POLYBOT_DIR / "memory"
