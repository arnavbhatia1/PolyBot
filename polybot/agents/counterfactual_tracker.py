"""Track what would have happened if scalped positions were held to resolution.

When the bot scalps (exits early), the contract still has time remaining.
This tracker watches those contracts until resolution, then records the
counterfactual outcome: was the scalp optimal, or would holding have been better?

Data feeds into the daily learning pipeline to tune exit_edge_threshold.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class CounterfactualTracker:
    def __init__(self, memory_dir: str):
        self.memory_dir = Path(memory_dir) / "counterfactuals"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._watchlist: dict[str, dict] = {}  # market_id -> scalp context

    def watch(self, pos: dict, scalp_context: dict):
        """Add a scalped position to the watch list for post-resolution comparison.

        Called immediately after a scalp exit in main.py. All data needed to
        compute the counterfactual is captured here — no DB lookups later.
        """
        market_id = pos.get("market_id", "")
        if not market_id:
            return

        self._watchlist[market_id] = {
            "position_id": pos.get("id", 0),
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
        logger.info(f"COUNTERFACTUAL: watching {market_id} (scalped {pos.get('side', '?')} @ "
                     f"{scalp_context.get('exit_fill', 0):.3f}, edge={scalp_context.get('holding_edge', 0):+.2f})")

    def check_resolutions(self, binance_feed, btc_at_expiry_fn) -> list[dict]:
        """Check if any watched contracts have expired and compute counterfactuals.

        Args:
            binance_feed: BinanceFeed instance with candle buffer.
            btc_at_expiry_fn: Callable(binance_feed, market_id) -> float.
                              Reuses _btc_at_expiry from main.py.

        Returns:
            List of resolved counterfactual records (for logging/alerts).
        """
        if not self._watchlist:
            return []

        now = time.time()
        resolved = []
        to_remove = []

        for market_id, ctx in self._watchlist.items():
            # Parse window_ts from slug: btc-updown-5m-{window_ts}
            try:
                window_ts = int(market_id.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                logger.warning(f"COUNTERFACTUAL: cannot parse window_ts from {market_id}, removing")
                to_remove.append(market_id)
                continue

            expiry_ts = window_ts + 300  # 5-min window
            # Wait 30s after expiry for candle to settle in buffer
            if now < expiry_ts + 30:
                continue

            # Expire stale entries (watched for > 10 min — candle data may be gone)
            if now > expiry_ts + 600:
                logger.warning(f"COUNTERFACTUAL: {market_id} expired > 10 min ago, removing without record")
                to_remove.append(market_id)
                continue

            # Resolve: get BTC at expiry and compute outcome
            btc_at_expiry = btc_at_expiry_fn(binance_feed, market_id)
            if btc_at_expiry <= 0:
                continue  # candle not yet in buffer, try next tick

            strike = ctx["strike_price"]
            if strike <= 0:
                to_remove.append(market_id)
                continue

            up_won = btc_at_expiry >= strike
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
                    "strike_price": strike,
                    "btc_at_scalp": ctx["btc_at_scalp"],
                    "btc_at_expiry": btc_at_expiry,
                },
            }

            self._save(record)
            resolved.append(record)
            to_remove.append(market_id)

            verdict = "CORRECT" if scalp_was_optimal else "SUBOPTIMAL"
            logger.info(
                f"COUNTERFACTUAL: {market_id} resolved — scalp was {verdict} | "
                f"scalp PnL=${ctx['scalp_pnl']:+.2f} vs hold PnL=${hypothetical_pnl:+.2f} "
                f"(delta=${delta_pnl:+.2f}) | BTC@expiry={btc_at_expiry:,.2f} vs strike={strike:,.2f}"
            )

        for mid in to_remove:
            self._watchlist.pop(mid, None)

        return resolved

    def _save(self, record: dict):
        """Write counterfactual record to JSON file."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        filename = f"{record['position_id']}_{record['market_id']}_{ts}.json"
        filepath = self.memory_dir / filename
        filepath.write_text(json.dumps(record, indent=2))

    def load_all(self) -> list[dict]:
        """Load all counterfactual records sorted by timestamp."""
        records = []
        for filepath in self.memory_dir.glob("*.json"):
            try:
                records.append(json.loads(filepath.read_text()))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load counterfactual {filepath}: {e}")
        return sorted(records, key=lambda x: x.get("timestamp", ""))

    @property
    def watching_count(self) -> int:
        return len(self._watchlist)
