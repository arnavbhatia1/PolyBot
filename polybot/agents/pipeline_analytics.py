"""ET date helper shared by the daily rollup writers (outcomes, ghosts, counterfactuals)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def slug_to_window(slug: str) -> str:
    """'btc-updown-5m-1785700000' → '12:35-12:40 ET' for operator-facing lines."""
    try:
        from datetime import timedelta
        ts = int(slug.rsplit("-", 1)[-1])
        start = datetime.fromtimestamp(ts, tz=_ET)
        end = start + timedelta(minutes=5)
        return f"{start.strftime('%I:%M').lstrip('0')}-{end.strftime('%I:%M ET').lstrip('0')}"
    except Exception:
        return slug


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
