"""ET date helper shared by the daily rollup writers (outcomes, ghosts,
counterfactuals)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def utc_ts_to_et_date(ts: str) -> str:
    """Convert a UTC ISO timestamp string to an ET date string YYYY-MM-DD.

    Falls back to the leading 10 chars on parse failure so a malformed
    timestamp never crashes a daily rollup.
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if ts else ""
