"""Ghost trade tracker: log and resolve downstream-gate rejections.

When the signal fires BUY_YES/BUY_NO but a downstream gate (adverse selection,
edge cap, late-window underdog, pre-submit drift, spread, etc.) blocks the entry,
we record the signal context as a "ghost trade" and track the window to resolution.

Ghost trades give the pipeline 5-10x more training data per day. They're used by
the BiasDetector and TA Evolver (to see which gates block profitable trades) but
NOT by Platt calibration (ghost trades were never filled, so probability → outcome
pairing is noisy).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from polybot.agents.pipeline_analytics import utc_ts_to_et_date as _utc_ts_to_et_date

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


class GhostTracker:
    def __init__(self, memory_dir: str) -> None:
        self._dir: Path = Path(memory_dir) / "ghost_outcomes"
        self._dir.mkdir(parents=True, exist_ok=True)
        # market_id -> ghost context (pending resolution)
        self._pending: dict[str, dict[str, Any]] = {}

    def record_rejection(
        self,
        gate_name: str,
        side: str,
        signal_prob: float,
        signal_edge: float,
        market_id: str,
        seconds_remaining: float,
        indicator_snapshot: dict[str, Any],
    ) -> None:
        """Log a ghost trade at the moment a downstream gate fires.

        Downstream = signal returned BUY_YES/BUY_NO but was vetoed by a gate in
        _evaluate_signal_and_enter. Model-level SKIPs (low confidence, ATR gate)
        are NOT recorded — those signals weren't actionable to begin with.
        """
        if not market_id:
            return
        # One ghost per market per session — first rejection wins so we capture
        # the cleanest signal moment (before the gate fired repeatedly on same tick).
        if market_id in self._pending:
            return

        self._pending[market_id] = {
            "market_id": market_id,
            "side": side,
            "gate_name": gate_name,
            "signal_prob": round(signal_prob, 4),
            "signal_edge": round(signal_edge, 4),
            "seconds_remaining": round(seconds_remaining, 1),
            "indicator_snapshot": indicator_snapshot,
            "recorded_at": time.time(),
            "resolved": False,
        }

    def check_resolutions(
        self,
        event_metadata: dict[str, dict[str, Any]] | None = None,
        btc_at_expiry_fn: Any = None,
        binance_feed: Any = None,
    ) -> list[dict[str, Any]]:
        """Resolve pending ghost trades against Gamma/Chainlink data.

        Called from the main resolution loop alongside CounterfactualTracker.
        """
        if not self._pending:
            return []

        if event_metadata is None:
            event_metadata = {}

        now = time.time()
        resolved: list[dict[str, Any]] = []
        to_remove: list[str] = []

        for market_id, ctx in self._pending.items():
            try:
                window_ts = int(market_id.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                to_remove.append(market_id)
                continue

            expiry_ts = window_ts + 300
            if now < expiry_ts + 30:
                continue  # window hasn't settled yet
            if now > expiry_ts + 600:
                to_remove.append(market_id)  # stale — candle gone
                continue

            # Resolve: prefer Chainlink eventMetadata (matches Polymarket)
            meta = event_metadata.get(market_id)
            if meta and meta.get("final_price") is not None and meta.get("price_to_beat") is not None:
                up_won = meta["final_price"] >= meta["price_to_beat"]
            elif btc_at_expiry_fn and binance_feed:
                btc = btc_at_expiry_fn(binance_feed, market_id)
                if btc <= 0:
                    continue
                ctx_snap = ctx.get("indicator_snapshot", {}).get("trade_context", {})
                strike = ctx_snap.get("strike_price", 0)
                if strike <= 0:
                    to_remove.append(market_id)
                    continue
                up_won = btc >= strike
            else:
                continue

            side = ctx["side"].lower()
            ghost_correct = (side == "up") == up_won
            # Approximate gain_pct: if correct, you'd get $1 on your signal_prob-priced token.
            # If wrong, you get $0. This is the EXPECTED gain at the SIGNAL price (not filled).
            signal_prob = ctx["signal_prob"]
            if signal_prob > 0:
                ghost_gain_pct = (1.0 / signal_prob - 1.0) if ghost_correct else -1.0
            else:
                ghost_gain_pct = 0.0

            record = {
                **ctx,
                "ghost_correct": ghost_correct,
                "ghost_gain_pct": round(ghost_gain_pct, 4),
                "resolved": True,
                "resolved_at": now,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._save(record)
            resolved.append(record)
            to_remove.append(market_id)
            logger.debug(
                f"GHOST resolved: {market_id} gate={ctx['gate_name']} side={ctx['side']} "
                f"correct={ghost_correct} gain={ghost_gain_pct:+.1%}"
            )

        for mid in to_remove:
            self._pending.pop(mid, None)

        return resolved

    def load_all(self) -> list[dict[str, Any]]:
        """Load all resolved ghost records, handling individual and rollup files."""
        records = []
        seen: set = set()
        for fp in self._dir.glob("*.json"):
            try:
                raw = json.loads(fp.read_text())
                items = raw if isinstance(raw, list) else [raw]
                for item in items:
                    key = (item.get("market_id", ""), item.get("gate_name", ""), item.get("recorded_at", 0))
                    if key not in seen:
                        seen.add(key)
                        records.append(item)
            except (json.JSONDecodeError, OSError):
                pass
        return sorted(records, key=lambda x: x.get("timestamp", ""))

    def rollup_old_ghosts(self) -> int:
        """Roll up previous days' resolved ghost files into one file per day.

        Same pattern as outcome rollup — keeps git manageable at high ghost volumes.
        Only touches resolved ghosts from before today. Returns number rolled up.
        """
        from collections import defaultdict
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        files_by_date: dict[str, list[Path]] = defaultdict(list)

        for fp in self._dir.glob("*.json"):
            if fp.name.startswith("rollup_"):
                continue
            try:
                data = json.loads(fp.read_text())
                if isinstance(data, list):
                    continue
                if not data.get("resolved", False):
                    continue
                ts = data.get("timestamp", "")
                date = _utc_ts_to_et_date(ts)
                if date and date <= today:
                    files_by_date[date].append(fp)
            except Exception:
                pass

        rolled = 0
        for date, fps in files_by_date.items():
            rollup_path = self._dir / f"rollup_{date}.json"
            existing: list[dict] = []
            if rollup_path.exists():
                try:
                    existing = json.loads(rollup_path.read_text())
                except Exception:
                    existing = []
            existing_keys = {(o.get("market_id"), o.get("gate_name"), o.get("recorded_at")) for o in existing}
            new_records = []
            for fp in fps:
                try:
                    d = json.loads(fp.read_text())
                    key = (d.get("market_id"), d.get("gate_name"), d.get("recorded_at"))
                    if key not in existing_keys:
                        new_records.append(d)
                except Exception:
                    pass
            combined = sorted(existing + new_records, key=lambda x: x.get("timestamp", ""))
            tmp = rollup_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(combined, indent=2))
            tmp.replace(rollup_path)
            for fp in fps:
                fp.unlink(missing_ok=True)
            rolled += len(fps)

        return rolled

    def _save(self, record: dict[str, Any]) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        fname = f"{record['market_id']}_{record['gate_name']}_{ts}.json"
        (self._dir / fname).write_text(json.dumps(record, indent=2))
