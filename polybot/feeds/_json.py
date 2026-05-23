"""Shared JSON parser for all WS feeds. Uses orjson when available."""
from __future__ import annotations

import json

try:
    import orjson as _orjson
    loads = _orjson.loads
except ImportError:
    loads = json.loads
