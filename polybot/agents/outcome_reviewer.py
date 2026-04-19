from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class OutcomeReviewer:
    def __init__(self, outcomes_dir: str) -> None:
        self.outcomes_dir: Path = Path(outcomes_dir)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)

    def record_outcome(self, position_id: int, market_id: str, question: str,
                       side: str, signal_score: float,
                       profitable: bool, entry_price: float, exit_price: float,
                       log_return: float, weight_version: str,
                       category: str = "", indicator_snapshot: dict[str, Any] | None = None,
                       exit_reason: str = "resolution", size: float = 0.0,
                       pnl: float = 0.0, fees: float = 0.0,
                       exit_timestamp: str = "",
                       seconds_remaining_at_exit: float = 0.0) -> None:
        now_utc = datetime.now(timezone.utc).isoformat()
        # Realized edge: model prob for the chosen side minus actual fill price.
        # Compares what the model expected at signal time to what it actually cost.
        # Negative realized_edge means the fill price was worse than the model believed.
        realized_edge = round(signal_score - entry_price, 4) if entry_price > 0 else 0.0

        # Fill slippage: fill_price - signal_moment_market_price for the chosen side.
        # Non-zero when paper/live latency caused the ask to move between signal and fill.
        ctx = (indicator_snapshot or {}).get("trade_context", {})
        signal_price = ctx.get("market_price_up") if side == "Up" else ctx.get("market_price_down")
        fill_slippage = round(entry_price - signal_price, 4) if signal_price and entry_price > 0 else 0.0

        record = {"position_id": position_id, "market_id": market_id, "question": question,
                  "side": side, "signal_score": signal_score,
                  "correct": profitable, "entry_price": entry_price,
                  "exit_price": exit_price, "log_return": log_return,
                  "size": size, "pnl": pnl, "fees": fees,
                  "gain_pct": round(pnl / size, 6) if size > 0 else 0.0,
                  "realized_edge": realized_edge,
                  "fill_slippage": fill_slippage,
                  "weight_version": weight_version, "category": category,
                  "indicator_snapshot": indicator_snapshot or {},
                  "exit_reason": exit_reason,
                  # 0.0 = held to resolution; > 0 = scalp with this many seconds left in window
                  "seconds_remaining_at_exit": seconds_remaining_at_exit,
                  "exit_timestamp": exit_timestamp or now_utc,
                  "timestamp": now_utc}
        filename = f"{position_id}_{market_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.json"
        filepath = self.outcomes_dir / filename
        filepath.write_text(json.dumps(record, indent=2))
        logger.info(f"Recorded outcome for position {position_id}: profitable={profitable}")

    def load_all_outcomes(self) -> list[dict[str, Any]]:
        outcomes = []
        for filepath in self.outcomes_dir.glob("*.json"):
            try:
                outcomes.append(json.loads(filepath.read_text()))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load outcome {filepath}: {e}")
        # Sort by exit_timestamp (when the trade actually closed) so walk-forward folds
        # respect trade chronology. Old outcomes without exit_timestamp fall back to
        # the write-time timestamp, which is correct for them (no Gamma delay).
        return sorted(outcomes, key=lambda x: x.get("exit_timestamp", x.get("timestamp", "")))
