"""OutcomeReviewer: writes per-trade outcome JSON files and rolls them up into daily files.

Each resolved or scalped trade writes an outcome record capturing model probability,
fill quality, realized edge, and indicator context. The pipeline reads these for Platt
calibration, weight optimization, and bias analysis. Daily rollup keeps the file count
manageable for git.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
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
        logger.debug(f"Recorded outcome for position {position_id}: profitable={profitable}")

    def load_all_outcomes(self) -> list[dict[str, Any]]:
        """Load all outcomes from both individual files and daily rollup files.

        Deduplicates by position_id — a trade present in both (e.g., after a partial
        rollup run) is only counted once. Sorted by exit_timestamp for correct
        walk-forward fold ordering.
        """
        outcomes = []
        seen_ids: set = set()
        for filepath in self.outcomes_dir.glob("*.json"):
            try:
                raw = json.loads(filepath.read_text())
                records = raw if isinstance(raw, list) else [raw]
                for record in records:
                    pid = record.get("position_id")
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        outcomes.append(record)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load outcome {filepath}: {e}")
        return sorted(outcomes, key=lambda x: x.get("exit_timestamp", x.get("timestamp", "")))

    def rollup_old_outcomes(self) -> int:
        """Roll up previous days' individual outcome files into one file per day.

        At 400+ trades/day, individual files become 150k+ files/year in git.
        One rollup file per day keeps the repo manageable. Only touches days before
        today so live intraday files are never disturbed. Atomic write (tmp → rename)
        means a crash leaves data intact. Returns number of files rolled up.
        """
        from collections import defaultdict
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        files_by_date: dict[str, list[tuple[Path, dict]]] = defaultdict(list)

        for filepath in self.outcomes_dir.glob("*.json"):
            if filepath.name.startswith("rollup_"):
                continue
            try:
                data = json.loads(filepath.read_text())
                if isinstance(data, list):
                    continue
                ts = data.get("exit_timestamp", data.get("timestamp", ""))
                date = ts[:10] if ts else ""
                if date and date <= today:
                    files_by_date[date].append((filepath, data))
            except Exception:
                pass

        rolled = 0
        for date, pairs in files_by_date.items():
            rollup_path = self.outcomes_dir / f"rollup_{date}.json"
            existing: list[dict] = []
            if rollup_path.exists():
                try:
                    existing = json.loads(rollup_path.read_text())
                except Exception:
                    existing = []
            existing_ids = {o.get("position_id") for o in existing}
            new_records = [d for _, d in pairs if d.get("position_id") not in existing_ids]
            combined = sorted(
                existing + new_records,
                key=lambda x: x.get("exit_timestamp", x.get("timestamp", "")),
            )
            tmp = rollup_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(combined, indent=2))
            tmp.rename(rollup_path)
            for fp, _ in pairs:
                fp.unlink(missing_ok=True)
            rolled += len(pairs)
            logger.info(f"Rolled up {len(pairs)} outcomes into {rollup_path.name}")

        return rolled
