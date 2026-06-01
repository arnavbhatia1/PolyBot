"""Passive microstructure telemetry for edge discovery.

This module records, observation-only, the relationship between the Coinbase spot
price and the Polymarket CLOB book over time. It NEVER influences a trading
decision — it exists so that after a frozen multi-day paper run we can test the
only three places a real edge could plausibly live in this market (the model's own
probability is known to be worse than the market price, so forecasting edge is
dead):

  A. Quote-staleness / latency arb — when spot moves, how long does the CLOB book
     lag before repricing, and is the stale quote actually fillable? Measured from
     book-update timestamps (`bkts_*`) vs spot moves across consecutive rows.
  B. Resolution-lag mispricing — in the final seconds, the Chainlink-implied
     outcome is near-certain; is the near-certain side offered at a discount on the
     book? Measured from `cl`/`strike`/`secs` vs the executable price (`ask_*`).
  C. Fill toxicity — already captured per-fill as `edge_decay.deltas` on outcomes;
     analysed alongside this data by the offline tool, not re-logged here.

Sampling is event-driven so the file stays small AND dense where it matters:
quiet periods emit a low-rate heartbeat; spot moves and the last-90s endgame are
sampled densely. All writes are guarded — a telemetry failure can never propagate
into the trading loop.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from polybot.paths import MICROSTRUCTURE_DIR

logger = logging.getLogger("polybot")

_ET = ZoneInfo("America/New_York")

# Sampling policy (per market_id).
_MIN_INTERVAL_S = 0.5      # hard floor between rows for one market — bounds volume
_HEARTBEAT_S = 10.0        # baseline cadence in quiet periods
_MOVE_BPS = 0.0003         # |spot move| since last row that forces a sample (~3 bps)
_ENDGAME_S = 90.0          # within this many secs to expiry, sample every ~1s (Exp. B)
_ENDGAME_INTERVAL_S = 1.0


class MicrostructureRecorder:
    """Append-only JSONL recorder. One file per ET day. Disabled via
    ``POLYBOT_DISABLE_MICRO_LOG=1``.
    """

    def __init__(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = os.environ.get("POLYBOT_DISABLE_MICRO_LOG", "") not in ("1", "true", "True")
        self.enabled = enabled
        # Per-market sampling state for the event-driven trigger.
        self._last_ts: dict[str, float] = {}
        self._last_spot: dict[str, float] = {}
        self._rows_written = 0

    def _path_for_today(self):
        day = datetime.now(_ET).strftime("%Y%m%d")
        return MICROSTRUCTURE_DIR / f"micro_{day}.jsonl"

    def _should_sample(self, mid: str, now: float, spot: float, secs: float) -> bool:
        last = self._last_ts.get(mid, 0.0)
        dt = now - last
        if dt < _MIN_INTERVAL_S:
            return False
        if secs <= _ENDGAME_S:
            return dt >= _ENDGAME_INTERVAL_S
        if dt >= _HEARTBEAT_S:
            return True
        prev_spot = self._last_spot.get(mid)
        if prev_spot and spot > 0 and abs(spot - prev_spot) / prev_spot >= _MOVE_BPS:
            return True
        return False

    def sample(self, *, market_id: str, seconds_remaining: float, phase: str,
               coinbase_price: float, coinbase_age: float, strike: float,
               bid_up: float, ask_up: float, bkts_up: float,
               bid_down: float, ask_down: float, bkts_down: float,
               chainlink_price: float = 0.0, chainlink_age: float = 0.0,
               model_prob_up: float | None = None) -> None:
        """Record one snapshot if the event-driven trigger fires. Fully guarded."""
        if not self.enabled:
            return
        try:
            now = time.time()
            mid = str(market_id)
            spot = float(coinbase_price or 0.0)
            if not self._should_sample(mid, now, spot, float(seconds_remaining or 0.0)):
                return
            self._last_ts[mid] = now
            if spot > 0:
                self._last_spot[mid] = spot

            def _r(v, n):
                try:
                    return round(float(v), n)
                except (TypeError, ValueError):
                    return None

            row = {
                "ts": round(now, 3),
                "mid": mid,
                "secs": _r(seconds_remaining, 1),
                "phase": phase,                       # "flat" | "hold"
                "cb": _r(coinbase_price, 2),           # Coinbase spot
                "cb_age": _r(coinbase_age, 2),
                "strike": _r(strike, 2),
                "cl": _r(chainlink_price, 2),          # Chainlink (resolution source)
                "cl_age": _r(chainlink_age, 2),
                # Executable CLOB BBO per side + the book's last-update epoch (bkts).
                # Staleness = ts - bkts; reprice lag = when bkts advances after a spot move.
                "bid_up": _r(bid_up, 4), "ask_up": _r(ask_up, 4), "bkts_up": _r(bkts_up, 3),
                "bid_dn": _r(bid_down, 4), "ask_dn": _r(ask_down, 4), "bkts_dn": _r(bkts_down, 3),
                "mp_up": _r(model_prob_up, 4) if model_prob_up is not None else None,
            }
            path = self._path_for_today()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._rows_written += 1
        except Exception as e:
            # Telemetry must never break trading. Log once-ish and move on.
            if self._rows_written % 500 == 0:
                logger.debug(f"microstructure sample failed (non-critical): {e}")
