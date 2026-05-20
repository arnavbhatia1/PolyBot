"""Adverse selection monitor: detects if fills are systematically picked off.

After each fill, tracks the midprice 10s, 30s, and 60s later. If the price
consistently moves against the bot's position after entry, someone is fading
the bot with better information.

adverse_selection_rate = P(price moves against you | you just filled)
If rolling rate > 0.55, the bot is being picked off.

State is persisted to JSON on fill-record so it survives restart — without that,
the first ~10 post-restart fills run with a neutral 0.5 rate (gate effectively off).
"""
from __future__ import annotations

import json
import time
import logging
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = Path("polybot/memory/adverse_state.json")


@dataclass
class FillEvent:
    """Records a fill for adverse selection tracking."""
    timestamp: float       # Unix seconds when filled
    side: str              # "Up" or "Down"
    fill_price: float      # Price we paid
    token_id: str          # Which token
    midprice_at_fill: float  # Market midprice at fill time
    midprice_10s: float | None = None   # Midprice 10s after fill
    midprice_30s: float | None = None   # Midprice 30s after fill
    midprice_60s: float | None = None   # Midprice 60s after fill
    resolved: bool = False              # All checkpoints measured


class AdverseSelectionMonitor:
    """Track post-fill price movement to detect adverse selection.

    Usage:
        monitor = AdverseSelectionMonitor()
        # After each fill:
        monitor.record_fill(side="Up", fill_price=0.60, token_id="abc", midprice=0.60)
        # On each tick:
        monitor.update_prices(clob_ws, time.time())
        # Check health:
        rate = monitor.get_adverse_rate()
        if rate > 0.55: # being picked off
    """

    def __init__(self, max_fills: int = 20, check_windows: tuple[float, ...] = (10.0, 30.0, 60.0),
                 state_path: Path | None = None) -> None:
        self.max_fills = max_fills
        self.check_windows = check_windows
        self._fills: deque[FillEvent] = deque(maxlen=max_fills)
        self._state_path: Path = state_path or _DEFAULT_STATE_PATH
        self._load()

    def record_fill(self, side: str, fill_price: float, token_id: str, midprice: float) -> None:
        """Record a new fill event for tracking."""
        self._fills.append(FillEvent(
            timestamp=time.time(),
            side=side,
            fill_price=fill_price,
            token_id=token_id,
            midprice_at_fill=midprice,
        ))
        self._save()

    def _save(self) -> None:
        """Persist current fill deque to disk. Silent on I/O errors — don't crash trading."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": time.time(),
                "fills": [asdict(f) for f in self._fills],
            }
            self._state_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning(f"AdverseSelectionMonitor save failed: {e}")

    def _load(self) -> None:
        """Restore fill deque from disk if a fresh-enough snapshot exists.

        Discards stale snapshots (>1 hour old) since fill outcomes older than the
        60-second checkpoint window are irrelevant and stale mid-prices would lie.
        """
        try:
            if not self._state_path.exists():
                return
            data = json.loads(self._state_path.read_text())
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
                    midprice_10s=fd.get("midprice_10s"),
                    midprice_30s=fd.get("midprice_30s"),
                    midprice_60s=fd.get("midprice_60s"),
                    resolved=bool(fd.get("resolved", False)),
                ))
                loaded += 1
            logger.info(f"AdverseSelectionMonitor: restored {loaded} fills from disk")
        except Exception as e:
            logger.warning(f"AdverseSelectionMonitor load failed: {e} — starting fresh")

    def update_prices(self, get_midprice_fn) -> None:
        """Update pending fill events with current midprices.

        Args:
            get_midprice_fn: callable(token_id) -> float, returns current midprice
        """
        now = time.time()
        for fill in self._fills:
            if fill.resolved:
                continue
            elapsed = now - fill.timestamp
            mid = get_midprice_fn(fill.token_id)
            if mid <= 0:
                continue
            if fill.midprice_10s is None and elapsed >= 10.0:
                fill.midprice_10s = mid
            if fill.midprice_30s is None and elapsed >= 30.0:
                fill.midprice_30s = mid
            if fill.midprice_60s is None and elapsed >= 60.0:
                fill.midprice_60s = mid
                fill.resolved = True

    def get_adverse_rate(self, window_s: float = 30.0, lookback_s: float = 1800.0) -> float:
        """Fraction of fills where price moved AGAINST us within window_s.

        Bayesian shrinkage toward a neutral prior (n=10, rate=0.5): with zero
        samples the rate is 0.5; with many samples the prior washes out. This
        keeps the guard active during low-volume hours where the prior cliff
        previously disabled it.
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
            if fill.side == "Up" and post < fill.midprice_at_fill:
                adverse += 1
            elif fill.side == "Down" and post > fill.midprice_at_fill:
                adverse += 1
        prior_n, prior_rate = 10, 0.5
        return (prior_n * prior_rate + adverse) / (prior_n + total)

    def get_stats(self) -> dict:
        """Return summary stats for logging/pipeline."""
        return {
            "total_tracked": len(self._fills),
            "resolved": sum(1 for f in self._fills if f.resolved),
            "adverse_rate_10s": self.get_adverse_rate(10.0),
            "adverse_rate_30s": self.get_adverse_rate(30.0),
            "adverse_rate_60s": self.get_adverse_rate(60.0),
        }
