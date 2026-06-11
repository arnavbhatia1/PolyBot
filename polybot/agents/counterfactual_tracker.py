"""Track counterfactual outcomes for both scalps and holds.

Scalp counterfactuals: when the bot exits early, watch until resolution
and record whether holding would have been better.

Hold counterfactuals: when the bot holds to resolution, record the worst
moment during the hold (lowest holding_edge) and compute whether scalping
at that moment would have been better.

Data feeds into the daily learning pipeline to tune exit_edge_threshold.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from polybot.agents.pipeline_analytics import utc_ts_to_et_date as _utc_ts_to_et_date
from polybot.execution.base import DEFAULT_FEE_RATE

_ET = ZoneInfo("America/New_York")

_WATCHLIST_MAX_AGE_S = 1800.0

logger = logging.getLogger(__name__)

def _slug_to_window(slug: str) -> str:
    """Convert btc-updown-5m-1776691500 to '9:25-9:30 ET'."""
    try:
        from datetime import timedelta
        ts = int(slug.rsplit("-", 1)[-1])
        start = datetime.fromtimestamp(ts, tz=_ET)
        end = start + timedelta(minutes=5)
        return f"{start.strftime('%I:%M').lstrip('0')}-{end.strftime('%I:%M ET').lstrip('0')}"
    except Exception:
        return slug

class CounterfactualTracker:
    def __init__(self, memory_dir: str) -> None:
        self.memory_dir: Path = Path(memory_dir) / "counterfactuals"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._watchlist: dict[str, dict[str, Any]] = {}
        self._hold_worst: dict[str, dict[str, Any]] = {}
        self._watchlist_path: Path = Path(memory_dir) / "state" / "cf_watchlist.json"
        self._load_watchlist()

    def _load_watchlist(self) -> None:
        if not self._watchlist_path.exists():
            return
        try:
            data = json.loads(self._watchlist_path.read_text())
        except Exception as e:
            logger.warning(f"CF watchlist load failed ({e}); starting fresh")
            return
        cutoff = time.time() - _WATCHLIST_MAX_AGE_S
        for entry in data.get("watchlist", []):
            if entry.get("watched_at", 0) >= cutoff:
                pid = entry.get("position_id")
                if pid is not None:
                    self._watchlist[pid] = entry

    def _save_watchlist(self) -> None:
        try:
            self._watchlist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"saved_at": time.time(), "watchlist": list(self._watchlist.values())}
            self._watchlist_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning(f"CF watchlist save failed: {e}")

    def _schedule_save_watchlist(self) -> None:
        try:
            asyncio.get_running_loop().create_task(asyncio.to_thread(self._save_watchlist))
        except RuntimeError:
            self._save_watchlist()

    def watch(self, pos: dict[str, Any], scalp_context: dict[str, Any],
              aux_signals: dict[str, Any] | None = None) -> None:
        """Add a scalped position to the watch list for post-resolution comparison.

        ``aux_signals`` is the live ``_build_aux_signals`` output stamped at the
        scalp moment — preserved verbatim so the resolution record carries the
        same aux microstructure fields the entry trade_context carries.
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
            "fee_rate": pos.get("fee_rate", DEFAULT_FEE_RATE),
            "scalp_exit_price": scalp_context.get("exit_fill", 0),
            "scalp_pnl": scalp_context.get("pnl", 0),
            "scalp_gain_pct": scalp_context.get("gain_pct", 0),
            "holding_edge_at_scalp": scalp_context.get("holding_edge", 0),
            "model_prob_at_scalp": scalp_context.get("model_prob", 0),
            "market_price_at_scalp": scalp_context.get("market_price", 0),
            "seconds_remaining_at_scalp": scalp_context.get("seconds_remaining", 0),
            "exit_threshold_used": scalp_context.get("exit_threshold", -0.10),
            "effective_exit_threshold": scalp_context.get("effective_exit_threshold"),
            "loss_cut": bool(scalp_context.get("loss_cut", False)),
            "strike_price": scalp_context.get("strike_price", 0),
            "btc_at_scalp": scalp_context.get("btc_price", 0),
            "flow_score": scalp_context.get("flow_score", 0.0),
            "spot_flow_signal": scalp_context.get("spot_flow_signal", 0.0),
            "regime": scalp_context.get("regime", "unknown"),
            "btc_distance_atr": scalp_context.get("btc_distance_atr", 0.0),
            "aux_signals": dict(aux_signals or {}),
            "watched_at": time.time(),
        }
        # This position is no longer held — its worst-moment state must not
        # survive to be attributed to a later re-entry in the same window.
        if self._hold_worst.get(market_id, {}).get("position_id") == position_id:
            self._hold_worst.pop(market_id, None)
        self._schedule_save_watchlist()
        # Debug-level: the SCALP block already emitted this exit context to the console.
        logger.debug(
            f"SCALP watching {_slug_to_window(market_id)} | {pos.get('side', '?')} @ "
            f"{scalp_context.get('exit_fill', 0):.3f}, edge={scalp_context.get('holding_edge', 0):+.2f}"
        )

    def track_hold_moment(self, market_id: str, pos: dict[str, Any], hold_context: dict[str, Any],
                          aux_signals: dict[str, Any] | None = None) -> None:
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

        # A flip re-entry reuses the market_id. A surviving worst-moment from the
        # previous position would mis-key the resolution record to that position
        # (wrong id/side/entry/shares), so a position change always starts fresh.
        if current is not None and current.get("position_id") != pos.get("id", 0):
            current = None

        if current is None or holding_edge < current["worst_holding_edge"]:
            self._hold_worst[market_id] = {
                "position_id": pos.get("id", 0),
                "market_id": market_id,
                "side": pos.get("side", ""),
                "entry_price": pos.get("entry_price", 0),
                "size": pos.get("size", 0),
                "shares_held": pos.get("shares_held") or pos.get("size", 0) / max(pos.get("entry_price", 1), 0.001),
                "fee_rate": pos.get("fee_rate", DEFAULT_FEE_RATE),
                "worst_holding_edge": holding_edge,
                "worst_model_prob": hold_context.get("model_prob", 0),
                "worst_market_price": hold_context.get("market_price", 0),
                "worst_seconds_remaining": hold_context.get("seconds_remaining", 0),
                "worst_btc_price": hold_context.get("btc_price", 0),
                "exit_threshold_used": hold_context.get("exit_threshold", -0.10),
                "strike_price": hold_context.get("strike_price", 0),
                "flow_score": hold_context.get("flow_score", 0.0),
                "spot_flow_signal": hold_context.get("spot_flow_signal", 0.0),
                "regime": hold_context.get("regime", "unknown"),
                "btc_distance_atr": hold_context.get("btc_distance_atr", 0.0),
                "aux_signals": dict(aux_signals or {}),
                "worst_at": time.time(),
            }

    def record_hold_resolution(self, market_id: str, resolution_price: float,
                               actual_pnl: float, actual_gain_pct: float,
                               position_id: Any | None = None) -> dict[str, Any] | None:
        """Record counterfactual for a position that was held to resolution.

        Computes what would have happened if the bot had scalped at the worst
        holding moment (lowest holding_edge during the hold).

        Returns the counterfactual record, or None if no hold data was tracked.
        """
        ctx = self._hold_worst.pop(market_id, None)
        if ctx is None:
            return None
        # Never mix arms across positions: if the tracked moments belong to a
        # different position in this window (re-entry that resolved before its
        # first HOLD tick), there is no valid counterfactual for the resolver.
        if position_id is not None and ctx.get("position_id") != position_id:
            logger.debug(
                f"HOLD CF dropped for {_slug_to_window(market_id)}: tracked position "
                f"{ctx.get('position_id')} != resolving position {position_id}"
            )
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
                "flow_score": ctx.get("flow_score", 0.0),
                "spot_flow_signal": ctx.get("spot_flow_signal", 0.0),
                "regime": ctx.get("regime", "unknown"),
                "btc_distance_atr": ctx.get("btc_distance_atr", 0.0),
                "worst_at": ctx["worst_at"],
                **ctx.get("aux_signals", {}),
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

    def check_resolutions(self,
                          event_metadata: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Check if any watched contracts have expired and compute counterfactuals.

        Resolves only via Chainlink-derived ``event_metadata`` (price_to_beat /
        final_price per market). Returns the list of resolved records.
        """
        if event_metadata is None:
            event_metadata = {}

        if not self._watchlist:
            return []

        now = time.time()
        resolved = []
        to_remove = []
        logged_resolutions: set[str] = set()

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

            # Expire stale entries — give Chainlink/Gamma 20 min to post before giving up.
            if now > expiry_ts + 1200:
                logger.warning(f"COUNTERFACTUAL: {market_id} — Chainlink not available after 20 min, dropping")
                to_remove.append(position_id)
                continue

            # Resolve only via Chainlink eventMetadata — no Binance fallback
            # (Binance close ≠ Polymarket resolution price → wrong training data).
            meta = event_metadata.get(market_id)
            if not meta or meta.get("final_price") is None:
                continue  # keep waiting — Chainlink final_price not posted yet

            chainlink_ptb = meta["price_to_beat"]
            chainlink_fp = meta["final_price"]
            up_won = chainlink_fp >= chainlink_ptb
            btc_at_expiry = chainlink_fp
            if market_id not in logged_resolutions:
                logged_resolutions.add(market_id)
                logger.info(f"COUNTERFACTUAL: {_slug_to_window(market_id)} | Strike={chainlink_ptb:,.2f} → Final={chainlink_fp:,.2f}")
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
                    "effective_exit_threshold": ctx.get("effective_exit_threshold"),
                    "loss_cut": ctx.get("loss_cut", False),
                    "entry_price": ctx.get("entry_price", 0),
                    "fee_rate": ctx.get("fee_rate", DEFAULT_FEE_RATE),
                    "strike_price": ctx["strike_price"],
                    "btc_at_scalp": ctx["btc_at_scalp"],
                    "btc_at_expiry": btc_at_expiry,
                    "chainlink_price_to_beat": chainlink_ptb,
                    "chainlink_final_price": chainlink_fp,
                    "flow_score": ctx.get("flow_score", 0.0),
                    "spot_flow_signal": ctx.get("spot_flow_signal", 0.0),
                    "regime": ctx.get("regime", "unknown"),
                    "btc_distance_atr": ctx.get("btc_distance_atr", 0.0),
                    **ctx.get("aux_signals", {}),
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
        if to_remove:
            self._schedule_save_watchlist()

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
                if date and date < today:
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
