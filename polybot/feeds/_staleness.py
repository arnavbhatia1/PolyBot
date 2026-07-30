"""Per-feed WS message inter-arrival sampling.

Persists gap percentiles so staleness gates get calibrated from measured
feed cadence, not guesses.
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

    n_total + connected state disambiguate a bare n=0 snapshot: a quiet but
    connected stream looks identical to a socket that never came up.
    reset() (on reconnect) clears only the gap anchor — n_total survives.
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

    @property
    def connected(self) -> bool | None:
        """Live connection state; None when the feed never reported either way."""
        return self._connected

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


def snapshot_feeds(trackers: Iterable[StalenessTracker]) -> list[dict[str, float | int | bool]]:
    """Read each tracker's gap deque into a plain snapshot list.

    Call ON the event loop only: snapshot() sorts the gap deque, which races
    the loop's observe() append from a worker thread ("deque mutated during
    iteration"). Hand only the result to write_feeds in a thread.
    """
    return [t.snapshot() for t in trackers]


def write_feeds(feeds: list[dict[str, float | int | bool]], path: Path) -> None:
    """Atomic JSON write of pre-gathered snapshots. Safe to run in a worker thread."""
    payload = {"updated_at": time.time(), "feeds": list(feeds)}
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
