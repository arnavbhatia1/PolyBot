"""Track counterfactual outcomes for both scalps and holds.

Scalp counterfactuals: when the bot exits early, watch until resolution
and record whether holding would have been better.

Hold counterfactuals: when the bot holds to resolution, record the worst
moment during the hold (lowest holding_edge) and compute whether scalping
at that moment would have been better.

Data feeds into the daily learning pipeline to tune exit_edge_threshold.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")


def _utc_ts_to_et_date(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if ts else ""
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _slug_to_window(slug: str) -> str:
    """Convert btc-updown-5m-1776691500 to '9:25-9:30 ET'."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        ts = int(slug.rsplit("-", 1)[-1])
        ET = ZoneInfo("America/New_York")
        start = datetime.fromtimestamp(ts, tz=ET)
        end = start + timedelta(minutes=5)
        return f"{start.strftime('%I:%M').lstrip('0')}-{end.strftime('%I:%M ET').lstrip('0')}"
    except Exception:
        return slug


class CounterfactualTracker:
    def __init__(self, memory_dir: str) -> None:
        self.memory_dir: Path = Path(memory_dir) / "counterfactuals"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._watchlist: dict[str, dict[str, Any]] = {}  # market_id -> scalp context
        self._hold_worst: dict[str, dict[str, Any]] = {}  # market_id -> worst moment during hold

    def watch(self, pos: dict[str, Any], scalp_context: dict[str, Any]) -> None:
        """Add a scalped position to the watch list for post-resolution comparison.

        Called immediately after a scalp exit in main.py. All data needed to
        compute the counterfactual is captured here — no DB lookups later.
        """
        position_id = pos.get("id", 0)
        market_id = pos.get("market_id", "")
        if not market_id or not position_id:
            return

        self._watchlist[position_id] = {
            "position_id": position_id,
            "market_id": market_id,
            "side": pos.get("side", ""),
            "entry_price": pos.get("entry_price", 0),
            "size": pos.get("size", 0),
            "shares_held": pos.get("shares_held") or pos.get("size", 0) / max(pos.get("entry_price", 1), 0.001),
            "fee_rate": pos.get("fee_rate", 0.018),
            "weight_version": pos.get("weight_version", ""),
            "scalp_exit_price": scalp_context.get("exit_fill", 0),
            "scalp_pnl": scalp_context.get("pnl", 0),
            "scalp_gain_pct": scalp_context.get("gain_pct", 0),
            "holding_edge_at_scalp": scalp_context.get("holding_edge", 0),
            "model_prob_at_scalp": scalp_context.get("model_prob", 0),
            "market_price_at_scalp": scalp_context.get("market_price", 0),
            "seconds_remaining_at_scalp": scalp_context.get("seconds_remaining", 0),
            "exit_threshold_used": scalp_context.get("exit_threshold", -0.10),
            "strike_price": scalp_context.get("strike_price", 0),
            "btc_at_scalp": scalp_context.get("btc_price", 0),
            "watched_at": time.time(),
        }
        logger.info(f"SCALP watching {_slug_to_window(market_id)} | {pos.get('side', '?')} @ "
                    f"{scalp_context.get('exit_fill', 0):.3f}, edge={scalp_context.get('holding_edge', 0):+.2f}")

    def track_hold_moment(self, market_id: str, pos: dict[str, Any], hold_context: dict[str, Any]) -> None:
        """Track the worst holding moment for a position being held to resolution.

        Called on every HOLD tick. Updates only if this tick's holding_edge is
        lower than the previous worst. After resolution, record_hold_resolution()
        uses this to compute "what if I had scalped at the worst moment?"

        Only tracks while the window is still live (seconds_remaining > 0). After
        expiry the CLOB market converges toward $1.00/$0.00, producing a degenerate
        worst-moment at the resolution price and a meaningless $0 delta.
        """
        if not market_id:
            return

        if hold_context.get("seconds_remaining", 1) <= 0:
            return

        holding_edge = hold_context.get("holding_edge", 0)
        current = self._hold_worst.get(market_id)

        if current is None or holding_edge < current["worst_holding_edge"]:
            self._hold_worst[market_id] = {
                "position_id": pos.get("id", 0),
                "market_id": market_id,
                "side": pos.get("side", ""),
                "entry_price": pos.get("entry_price", 0),
                "size": pos.get("size", 0),
                "shares_held": pos.get("shares_held") or pos.get("size", 0) / max(pos.get("entry_price", 1), 0.001),
                "fee_rate": pos.get("fee_rate", 0.018),
                "weight_version": pos.get("weight_version", ""),
                "worst_holding_edge": holding_edge,
                "worst_model_prob": hold_context.get("model_prob", 0),
                "worst_market_price": hold_context.get("market_price", 0),
                "worst_seconds_remaining": hold_context.get("seconds_remaining", 0),
                "worst_btc_price": hold_context.get("btc_price", 0),
                "exit_threshold_used": hold_context.get("exit_threshold", -0.10),
                "strike_price": hold_context.get("strike_price", 0),
                "worst_at": time.time(),
            }

    def record_hold_resolution(self, market_id: str, resolution_price: float,
                               actual_pnl: float, actual_gain_pct: float) -> dict[str, Any] | None:
        """Record counterfactual for a position that was held to resolution.

        Computes what would have happened if the bot had scalped at the worst
        holding moment (lowest holding_edge during the hold).

        Returns the counterfactual record, or None if no hold data was tracked.
        """
        ctx = self._hold_worst.pop(market_id, None)
        if ctx is None:
            return None

        # Hypothetical scalp PnL at worst moment
        worst_sell_price = ctx["worst_market_price"]
        fee_rate = ctx["fee_rate"]
        shares = ctx["shares_held"]
        exit_fee = fee_rate * shares * worst_sell_price * (1.0 - worst_sell_price)
        hypo_revenue = shares * worst_sell_price - exit_fee
        hypo_pnl = hypo_revenue - ctx["size"]
        hypo_gain_pct = hypo_pnl / ctx["size"] if ctx["size"] > 0 else 0

        delta_pnl = actual_pnl - hypo_pnl
        hold_was_optimal = actual_pnl >= hypo_pnl

        record = {
            "position_id": ctx["position_id"],
            "market_id": market_id,
            "side": ctx["side"],
            "weight_version": ctx["weight_version"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actual": {
                "exit_reason": "hold",
                "exit_price": resolution_price,
                "pnl": round(actual_pnl, 6),
                "gain_pct": round(actual_gain_pct, 6),
            },
            "counterfactual": {
                "exit_reason": "hypothetical_scalp",
                "exit_price": round(worst_sell_price, 6),
                "pnl": round(hypo_pnl, 6),
                "gain_pct": round(hypo_gain_pct, 6),
            },
            "delta_pnl": round(delta_pnl, 6),
            "hold_was_optimal": hold_was_optimal,
            "context_at_worst_moment": {
                "holding_edge": ctx["worst_holding_edge"],
                "model_prob": ctx["worst_model_prob"],
                "market_price": ctx["worst_market_price"],
                "seconds_remaining": ctx["worst_seconds_remaining"],
                "exit_threshold_used": ctx["exit_threshold_used"],
                "strike_price": ctx["strike_price"],
                "btc_at_worst": ctx["worst_btc_price"],
                "worst_at": ctx["worst_at"],
            },
        }

        self._save(record)
        verdict = "CORRECT" if hold_was_optimal else "SUBOPTIMAL"
        moment = "scalp@worst" if hold_was_optimal else "scalp@best"
        logger.info(
            f"HOLD {verdict} {_slug_to_window(market_id)} | "
            f"held=${actual_pnl:+.2f}, {moment}=${hypo_pnl:+.2f} (delta=${delta_pnl:+.2f})"
        )
        return record

    def check_resolutions(self, binance_feed: Any, btc_at_expiry_fn: Callable[..., float],
                          event_metadata: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Check if any watched contracts have expired and compute counterfactuals.

        Args:
            binance_feed: BinanceFeed instance with candle buffer.
            btc_at_expiry_fn: Callable(binance_feed, market_id) -> float.
                              Reuses _btc_at_expiry from main.py.

        Returns:
            List of resolved counterfactual records (for logging/alerts).
        """
        if event_metadata is None:
            event_metadata = {}

        if not self._watchlist:
            return []

        now = time.time()
        resolved = []
        to_remove = []

        for position_id, ctx in self._watchlist.items():
            market_id = ctx["market_id"]
            # Parse window_ts from slug: btc-updown-5m-{window_ts}
            try:
                window_ts = int(market_id.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                logger.warning(f"COUNTERFACTUAL: cannot parse window_ts from {market_id}, removing")
                to_remove.append(position_id)
                continue

            expiry_ts = window_ts + 300  # 5-min window
            # Wait 30s after expiry for candle to settle in buffer
            if now < expiry_ts + 30:
                continue

            # Expire stale entries (watched for > 10 min — candle data may be gone)
            if now > expiry_ts + 600:
                logger.warning(f"COUNTERFACTUAL: {market_id} expired > 10 min ago, removing without record")
                to_remove.append(position_id)
                continue

            # Resolve only via Chainlink eventMetadata — no Binance fallback.
            # Binance candle close ≠ Polymarket resolution price; using it produces
            # wrong training data. The 10-min expiry window above gives Chainlink
            # enough time to post (typically 2-5 min after round close).
            meta = event_metadata.get(market_id)
            if not meta:
                continue  # keep waiting — Chainlink not posted yet

            chainlink_ptb = meta["price_to_beat"]
            chainlink_fp = meta["final_price"]
            up_won = chainlink_fp >= chainlink_ptb
            btc_at_expiry = chainlink_fp
            logger.info(f"COUNTERFACTUAL: {market_id} using Chainlink: priceToBeat={chainlink_ptb:,.2f} final={chainlink_fp:,.2f}")
            side = ctx["side"]
            resolution_price = 1.0 if (side == "Up") == up_won else 0.0

            # Hypothetical PnL if held to resolution (fee = $0 at $0/$1 extremes)
            hypothetical_revenue = ctx["shares_held"] * resolution_price
            hypothetical_pnl = hypothetical_revenue - ctx["size"]
            hypothetical_gain_pct = hypothetical_pnl / ctx["size"] if ctx["size"] > 0 else 0

            delta_pnl = hypothetical_pnl - ctx["scalp_pnl"]
            scalp_was_optimal = ctx["scalp_pnl"] >= hypothetical_pnl

            record = {
                "position_id": ctx["position_id"],
                "market_id": market_id,
                "side": side,
                "weight_version": ctx["weight_version"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actual": {
                    "exit_reason": "scalp",
                    "exit_price": ctx["scalp_exit_price"],
                    "pnl": round(ctx["scalp_pnl"], 6),
                    "gain_pct": round(ctx["scalp_gain_pct"], 6),
                },
                "counterfactual": {
                    "resolution_price": resolution_price,
                    "pnl": round(hypothetical_pnl, 6),
                    "gain_pct": round(hypothetical_gain_pct, 6),
                },
                "delta_pnl": round(delta_pnl, 6),
                "scalp_was_optimal": scalp_was_optimal,
                "context_at_scalp": {
                    "holding_edge": ctx["holding_edge_at_scalp"],
                    "model_prob": ctx["model_prob_at_scalp"],
                    "market_price": ctx["market_price_at_scalp"],
                    "seconds_remaining": ctx["seconds_remaining_at_scalp"],
                    "exit_threshold_used": ctx["exit_threshold_used"],
                    "strike_price": ctx["strike_price"],
                    "btc_at_scalp": ctx["btc_at_scalp"],
                    "btc_at_expiry": btc_at_expiry,
                    "chainlink_price_to_beat": chainlink_ptb,
                    "chainlink_final_price": chainlink_fp,
                },
            }

            self._save(record)
            resolved.append(record)
            to_remove.append(position_id)

            verdict = "CORRECT" if scalp_was_optimal else "SUBOPTIMAL"
            logger.info(
                f"SCALP {verdict} {_slug_to_window(market_id)} | "
                f"got=${ctx['scalp_pnl']:+.2f}, held=${hypothetical_pnl:+.2f} (delta=${delta_pnl:+.2f})"
            )

        for mid in to_remove:
            self._watchlist.pop(mid, None)

        return resolved

    def _save(self, record: dict[str, Any]) -> None:
        """Write counterfactual record to JSON file."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        filename = f"{record['position_id']}_{record['market_id']}_{ts}.json"
        filepath = self.memory_dir / filename
        filepath.write_text(json.dumps(record, indent=2))

    def load_all(self) -> list[dict[str, Any]]:
        """Load all counterfactual records from individual and rollup files, sorted by timestamp."""
        records = []
        seen: set = set()
        for filepath in self.memory_dir.glob("*.json"):
            try:
                data = json.loads(filepath.read_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    key = (item.get("position_id"), item.get("market_id"))
                    if key not in seen:
                        seen.add(key)
                        records.append(item)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load counterfactual {filepath}: {e}")
        return sorted(records, key=lambda x: x.get("timestamp", ""))

    def rollup_old_counterfactuals(self) -> int:
        """Roll up previous days' counterfactual files into one file per day."""
        from collections import defaultdict
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        files_by_date: dict[str, list[tuple[Path, dict]]] = defaultdict(list)

        for filepath in self.memory_dir.glob("*.json"):
            if filepath.name.startswith("rollup_"):
                continue
            try:
                data = json.loads(filepath.read_text())
                ts = data.get("timestamp", "")
                date = _utc_ts_to_et_date(ts)
                if date and date <= today:
                    files_by_date[date].append((filepath, data))
            except Exception:
                pass

        rolled = 0
        for date, pairs in files_by_date.items():
            rollup_path = self.memory_dir / f"rollup_{date}.json"
            existing: list[dict] = []
            if rollup_path.exists():
                try:
                    existing = json.loads(rollup_path.read_text())
                except Exception:
                    existing = []
            existing_keys = {(o.get("position_id"), o.get("market_id")) for o in existing}
            new_records = [d for _, d in pairs if (d.get("position_id"), d.get("market_id")) not in existing_keys]
            combined = sorted(existing + new_records, key=lambda x: x.get("timestamp", ""))
            tmp = rollup_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(combined, indent=2))
            tmp.replace(rollup_path)
            for fp, _ in pairs:
                fp.unlink(missing_ok=True)
            rolled += len(pairs)
            logger.debug(f"Rolled up {len(pairs)} counterfactuals into {rollup_path.name}")

        return rolled

    @property
    def watching_count(self) -> int:
        return len(self._watchlist)

    @property
    def watched_markets(self) -> list[str]:
        """List of market_ids currently being watched."""
        return list({ctx["market_id"] for ctx in self._watchlist.values()})
