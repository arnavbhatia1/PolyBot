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
    """Records WS message inter-arrival gaps for a single feed.

    Also tracks a lifetime message count and (for feeds that report it via
    mark_connected/mark_disconnected) live connection state. Without these, a
    persisted ``n=0`` snapshot is ambiguous: a connected feed that received no
    messages (a genuinely quiet stream) looks identical to a
    socket that never came up. ``reset()`` (called on reconnect) clears only the
    inter-arrival anchor, so ``n_total`` survives across reconnects.
    """

    __slots__ = ("name", "_gaps", "_last_ts", "_n_total", "_connected")

    def __init__(self, name: str, maxlen: int = 2000) -> None:
        self.name = name
        self._gaps: deque[float] = deque(maxlen=maxlen)
        self._last_ts: float = 0.0
        self._n_total: int = 0
        self._connected: bool | None = None

    def observe(self, now: float | None = None) -> None:
        t = now if now is not None else time.time()
        if self._last_ts > 0:
            self._gaps.append(t - self._last_ts)
        self._last_ts = t
        self._n_total += 1

    def reset(self) -> None:
        self._last_ts = 0.0

    def mark_connected(self) -> None:
        self._connected = True

    def mark_disconnected(self) -> None:
        self._connected = False

    def snapshot(self) -> dict[str, float | int | bool]:
        snap: dict[str, float | int | bool] = {
            "name": self.name, "n": len(self._gaps), "n_total": self._n_total,
        }
        if self._connected is not None:
            snap["connected"] = self._connected
        if self._gaps:
            s = sorted(self._gaps)
            n = len(s)
            snap.update({
                "p50": round(s[n // 2], 3),
                "p95": round(s[min(n - 1, int(n * 0.95))], 3),
                "p99": round(s[min(n - 1, int(n * 0.99))], 3),
                "max": round(s[-1], 3),
            })
        return snap


_lock = Lock()


def persist(trackers: Iterable[StalenessTracker], path: Path) -> None:
    """Atomic write of all tracker snapshots to a single JSON file."""
    payload = {"updated_at": time.time(), "feeds": [t.snapshot() for t in trackers]}
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
