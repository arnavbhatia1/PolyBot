"""Adverse selection monitor: detects if fills are systematically picked off.

All midprices — the fill baseline and every checkpoint — are the TRADED
token's own (bid+ask)/2, so drift is side-relative by construction: our
token's mid falling after we buy is adverse, for Up and Down alike. After
each fill, the mid is sampled at 5/10/15/30/60s; if it consistently drops,
someone is fading the bot with better information.

adverse_selection_rate = P(price moves against you | you just filled).
Live gate threshold is signal.adverse_selection_threshold (default 0.80). The
get_adverse_rate() result is Bayesian-shrunk to a neutral prior so the gate
stays active in low-volume hours and across restarts (state is also persisted
to JSON on each fill-record).
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path

from polybot.paths import ADVERSE_STATE_PATH

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = ADVERSE_STATE_PATH
_MAX_LOOKBACK_S = 2400.0
# Persisted-state schema. Bump whenever the midprice convention changes so a
# restart can't mix fills measured on incompatible axes.
_STATE_SCHEMA = 2

@dataclass
class FillEvent:
    """Records a fill for adverse selection tracking."""
    timestamp: float       # Unix seconds when filled
    side: str              # "Up" or "Down"
    fill_price: float      # Price we paid
    token_id: str          # Which token
    midprice_at_fill: float  # Traded token's own mid at fill time
    midprice_5s: float | None = None    # Traded token's mid 5s after fill (edge-decay)
    midprice_10s: float | None = None   # 10s after fill
    midprice_15s: float | None = None   # 15s after fill (edge-decay)
    midprice_30s: float | None = None   # 30s after fill
    midprice_60s: float | None = None   # 60s after fill
    resolved: bool = False              # All checkpoints measured
    position_id: int | None = None      # Link to the trade row; merged into outcome at close


class AdverseSelectionMonitor:
    """Track post-fill price movement to detect adverse selection.

    Lifecycle: `record_fill` on every fill, `update_prices` each tick; `get_adverse_rate`
    returns the (Bayesian-shrunk) fraction of fills the market faded — above
    `signal.adverse_selection_threshold` means we're being picked off.
    """

    def __init__(self, max_fills: int = 200, state_path: Path | None = None) -> None:
        self.max_fills = max_fills
        self._fills: deque[FillEvent] = deque(maxlen=max_fills)
        self._state_path: Path = state_path or _DEFAULT_STATE_PATH
        self._load()

    def _prune_stale(self) -> None:
        cutoff = time.time() - _MAX_LOOKBACK_S
        while self._fills and self._fills[0].timestamp < cutoff:
            self._fills.popleft()

    def record_fill(self, side: str, fill_price: float, token_id: str, midprice: float,
                    position_id: int | None = None) -> None:
        """Record a new fill event. ``position_id`` links to the DB row so the
        close-time outcome writer can stamp ``edge_decay``; optional — without it
        the per-trade lookup is lost but the adverse-rate gate still works.
        """
        self._prune_stale()
        self._fills.append(FillEvent(
            timestamp=time.time(),
            side=side,
            fill_price=fill_price,
            token_id=token_id,
            midprice_at_fill=midprice,
            position_id=position_id,
        ))
        self._schedule_save()

    def _schedule_save(self) -> None:
        """Defer the JSON write off the trade-open hot path. Fire-and-forget —
        if the process dies before it completes, the next fill triggers another
        save. Falls back to a sync write when called outside an event loop
        (tests, startup helpers)."""
        # Snapshot the fill deque ON the event loop. Iterating it inside the
        # to_thread worker would race the loop's record_fill/_prune_stale
        # append/popleft and can raise "deque mutated during iteration".
        fills_snapshot = [asdict(f) for f in self._fills]
        try:
            asyncio.get_running_loop().create_task(
                asyncio.to_thread(self._save, fills_snapshot))
        except RuntimeError:
            self._save(fills_snapshot)

    def _save(self, fills_snapshot: list[dict]) -> None:
        """Persist a pre-built fill snapshot to disk. Silent on I/O errors —
        don't crash trading."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": _STATE_SCHEMA,
                "saved_at": time.time(),
                "fills": fills_snapshot,
            }
            self._state_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning(f"AdverseSelectionMonitor save failed: {e}")

    def _load(self) -> None:
        """Restore the fill deque from disk. Discards snapshots older than 2h
        (beyond the gate lookback — stale mids would lie) or written under a
        different midprice convention (schema mismatch).
        """
        try:
            if not self._state_path.exists():
                return
            data = json.loads(self._state_path.read_text())
            if data.get("schema") != _STATE_SCHEMA:
                logger.info("AdverseSelectionMonitor: discarding snapshot with schema %s "
                            "(current %s)", data.get("schema"), _STATE_SCHEMA)
                return
            saved_at = float(data.get("saved_at", 0))
            age = time.time() - saved_at
            if age > 7200:
                logger.debug(f"AdverseSelectionMonitor: discarding stale snapshot (age {age:.0f}s)")
                return
            fills_data = data.get("fills", [])
            loaded = 0
            for fd in fills_data[-self.max_fills:]:
                self._fills.append(FillEvent(
                    timestamp=float(fd.get("timestamp", 0)),
                    side=fd.get("side", ""),
                    fill_price=float(fd.get("fill_price", 0)),
                    token_id=fd.get("token_id", ""),
                    midprice_at_fill=float(fd.get("midprice_at_fill", 0)),
                    midprice_5s=fd.get("midprice_5s"),
                    midprice_10s=fd.get("midprice_10s"),
                    midprice_15s=fd.get("midprice_15s"),
                    midprice_30s=fd.get("midprice_30s"),
                    midprice_60s=fd.get("midprice_60s"),
                    resolved=bool(fd.get("resolved", False)),
                    position_id=fd.get("position_id"),
                ))
                loaded += 1
            self._prune_stale()
            logger.debug(f"AdverseSelectionMonitor: restored {loaded} fills from disk")
        except Exception as e:
            logger.warning(f"AdverseSelectionMonitor load failed: {e} — starting fresh")

    def update_prices(self, get_midprice_fn) -> None:
        """Update pending fill events with current midprices.
        ``get_midprice_fn``: callable(token_id) -> float."""
        now = time.time()
        for fill in self._fills:
            if fill.resolved:
                continue
            elapsed = now - fill.timestamp
            mid = get_midprice_fn(fill.token_id)
            if mid <= 0:
                continue
            if fill.midprice_5s is None and elapsed >= 5.0:
                fill.midprice_5s = mid
            if fill.midprice_10s is None and elapsed >= 10.0:
                fill.midprice_10s = mid
            if fill.midprice_15s is None and elapsed >= 15.0:
                fill.midprice_15s = mid
            if fill.midprice_30s is None and elapsed >= 30.0:
                fill.midprice_30s = mid
            if fill.midprice_60s is None and elapsed >= 60.0:
                fill.midprice_60s = mid
                fill.resolved = True

    def get_decay_for_position(self, position_id: int) -> dict | None:
        """Edge-decay snapshot for a trade, or None if not found.

        Schema: ``{"midprice_at_fill": float, "deltas": {"5s"…"60s": float|None},
        "resolved_windows": int}``. Each delta is ``post - fill`` on the traded
        token's own mid — positive = in our favor, negative = market faded us;
        holds for Up and Down alike since both mids are the token we hold.
        """
        for fill in self._fills:
            if fill.position_id != position_id:
                continue
            def _d(post: float | None) -> float | None:
                if post is None:
                    return None
                return round(post - fill.midprice_at_fill, 6)
            deltas = {
                "5s":  _d(fill.midprice_5s),
                "10s": _d(fill.midprice_10s),
                "15s": _d(fill.midprice_15s),
                "30s": _d(fill.midprice_30s),
                "60s": _d(fill.midprice_60s),
            }
            return {
                "midprice_at_fill": fill.midprice_at_fill,
                "deltas": deltas,
                "resolved_windows": sum(1 for v in deltas.values() if v is not None),
            }
        return None

    def get_adverse_rate(self, window_s: float = 30.0, lookback_s: float = 1800.0) -> float:
        """Fraction of fills where price moved AGAINST us within window_s.

        Bayesian shrinkage toward a neutral prior (n=10, rate=0.5): with zero
        samples the rate is 0.5; with many samples the prior washes out. Keeps
        the guard active during low-volume hours.
        """
        now = time.time()
        adverse = 0
        total = 0
        for fill in self._fills:
            if now - fill.timestamp > lookback_s:
                continue  # stale — different market regime, drop from sample
            if window_s <= 10.0:
                post = fill.midprice_10s
            elif window_s <= 30.0:
                post = fill.midprice_30s
            else:
                post = fill.midprice_60s
            if post is None:
                continue
            total += 1
            if post < fill.midprice_at_fill:
                adverse += 1
        prior_n, prior_rate = 10, 0.5
        return (prior_n * prior_rate + adverse) / (prior_n + total)

    def get_recent_decay_mean(self, window_s: float = 15.0, lookback_s: float = 1800.0,
                              min_samples: int = 15) -> float | None:
        """Mean post-fill drift of the traded token's mid at ``window_s`` over
        ``lookback_s``. Positive = in our favor; negative = edge decay. ``None``
        below ``min_samples`` resolved checkpoints — caller treats that as "gate
        inactive", never substitutes a prior (a neutral 0 is on the same scale
        as a real signal).
        """
        now = time.time()
        deltas: list[float] = []
        for fill in self._fills:
            if now - fill.timestamp > lookback_s:
                continue
            if window_s <= 5.0:
                post = fill.midprice_5s
            elif window_s <= 10.0:
                post = fill.midprice_10s
            elif window_s <= 15.0:
                post = fill.midprice_15s
            elif window_s <= 30.0:
                post = fill.midprice_30s
            else:
                post = fill.midprice_60s
            if post is None:
                continue
            deltas.append(post - fill.midprice_at_fill)
        if len(deltas) < min_samples:
            return None
        return sum(deltas) / len(deltas)

    def get_stats(self) -> dict:
        """Return summary stats for logging/pipeline."""
        return {
            "total_tracked": len(self._fills),
            "resolved": sum(1 for f in self._fills if f.resolved),
            "adverse_rate_10s": self.get_adverse_rate(10.0),
            "adverse_rate_30s": self.get_adverse_rate(30.0),
            "adverse_rate_60s": self.get_adverse_rate(60.0),
        }
