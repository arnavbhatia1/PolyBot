"""Per-feed inter-arrival staleness sampling.

Lightweight rolling deque of gaps between successive WS messages, plus periodic
persistence so the operator can calibrate staleness gates against P50/P95/P99
of actual feed cadence rather than guess.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Iterable


class StalenessTracker:
    """Records WS message inter-arrival gaps for a single feed."""

    __slots__ = ("name", "_gaps", "_last_ts")

    def __init__(self, name: str, maxlen: int = 2000) -> None:
        self.name = name
        self._gaps: deque[float] = deque(maxlen=maxlen)
        self._last_ts: float = 0.0

    def observe(self, now: float | None = None) -> None:
        t = now if now is not None else time.time()
        if self._last_ts > 0:
            self._gaps.append(t - self._last_ts)
        self._last_ts = t

    def reset(self) -> None:
        self._last_ts = 0.0

    def snapshot(self) -> dict[str, float | int]:
        if not self._gaps:
            return {"name": self.name, "n": 0}
        s = sorted(self._gaps)
        n = len(s)
        return {
            "name": self.name,
            "n": n,
            "p50": round(s[n // 2], 3),
            "p95": round(s[min(n - 1, int(n * 0.95))], 3),
            "p99": round(s[min(n - 1, int(n * 0.99))], 3),
            "max": round(s[-1], 3),
        }


_lock = Lock()


def persist(trackers: Iterable[StalenessTracker], path: Path) -> None:
    """Atomic write of all tracker snapshots to a single JSON file."""
    payload = {"updated_at": time.time(), "feeds": [t.snapshot() for t in trackers]}
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
