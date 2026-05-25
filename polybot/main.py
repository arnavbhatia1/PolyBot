# polybot/main.py
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

try:
    import orjson as _orjson
    def _fast_dumps(obj: Any) -> str:
        return _orjson.dumps(obj).decode("utf-8")
except ImportError:
    def _fast_dumps(obj: Any) -> str:
        return json.dumps(obj)

# Force UTF-8 on stdout/stderr so Windows cp1252 consoles don't choke on box-drawing
# chars (═ ─ Δ ± ✓ ✗ ⚠ →) used in pipeline summary output. errors='replace' keeps the
# process alive if a terminal still can't render a given codepoint.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from polybot.config.loader import load_config, get_secret
from polybot.config.param_registry import default_for as _d
from polybot.execution.base import entry_fee_shares, slippage_pct, DEFAULT_FEE_RATE, compute_buy_vwap
from polybot.db.models import Database
from polybot.feeds.binance_feed import BinanceFeed
from polybot.feeds.market_scanner import BTCMarketScanner
from polybot.feeds.clob_ws import ClobWebSocket
from polybot.indicators.engine import IndicatorEngine, IndicatorNormalizer
from polybot.core.signal_engine import SignalEngine
from polybot.core.order_flow import compute_flow_signal
from polybot.agents.claude_client import ClaudeClient
from polybot.execution.paper_trader import PaperTrader
from polybot.execution.live_trader import AuthError, LiveTrader, OrphanPositionError, verify_auth
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.bias_detector import BiasDetector
from polybot.agents.ta_evolver import TAEvolver
from polybot.agents.weight_optimizer import WeightOptimizer
from polybot.agents.scheduler import AgentScheduler
from polybot.agents.counterfactual_tracker import CounterfactualTracker
from polybot.agents.ghost_tracker import GhostTracker
from polybot.discord_bot.bot import create_bot
from polybot.discord_bot.alerts import AlertManager
from polybot.execution.circuit_breaker import CircuitBreaker
from polybot.execution.correlation import concurrent_multiplier
import math
from polybot.feeds.binance_depth import BinanceDepthFeed
from polybot.feeds.binance_trades import BinanceTradesFeed, BinanceTradeAccumulator
from polybot.feeds.bybit_feed import BybitFeed
from polybot.feeds.coinbase_feed import CoinbaseFeed
from polybot.core.sprt import SPRTAccumulator
from polybot.core.regime import RegimeDetector
from polybot.core.liquidation import compute_liquidation_pressure
from polybot.core.signal_engine import compute_signal_consensus
from polybot.core.adverse_selection import AdverseSelectionMonitor

import re
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


def _slug_to_window(slug: str) -> str:
    """Convert btc-updown-5m-1776691500 to '9:25-9:30 ET'."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone, timedelta
        ts = int(slug.rsplit("-", 1)[-1])
        ET = ZoneInfo("America/New_York")
        start = datetime.fromtimestamp(ts, tz=ET)
        end = start + timedelta(minutes=5)
        return f"{start.strftime('%I:%M').lstrip('0')}-{end.strftime('%I:%M ET').lstrip('0')}"
    except Exception:
        return slug

class _StripAnsiFormatter(logging.Formatter):
    """Strips ANSI color codes so log files stay clean."""
    def format(self, record):
        result = super().format(record)
        return _ANSI_RE.sub('', result)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_file_handler = logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=0, mode="a", encoding="utf-8")
_file_handler.setFormatter(_StripAnsiFormatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

# Async logging: ost 1-5ms on disk and matter when scalp decisions chain 5-10 log lines.
import queue as _queue
_log_queue: _queue.Queue = _queue.Queue(-1)  # unbounded so logging never blocks
_queue_handler = logging.handlers.QueueHandler(_log_queue)
_queue_handler.setFormatter(logging.Formatter("%(message)s"))
_queue_listener = logging.handlers.QueueListener(
    _log_queue, _console_handler, _file_handler, respect_handler_level=True
)
_queue_listener.start()

import atexit as _atexit
_atexit.register(_queue_listener.stop)

logging.basicConfig(
    level=logging.ERROR,
    handlers=[_queue_handler],
)
logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)
# Suppress discord.py's internal reconnect tracebacks — run_discord() already logs these cleanly
logging.getLogger("discord.gateway").setLevel(logging.CRITICAL)
logging.getLogger("discord.client").setLevel(logging.CRITICAL)

# ANSI color codes for terminal readability
class _C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
# Only polybot and discord bot loggers show INFO. Everything else (httpx, discord.client, websockets) is silent.
logger = logging.getLogger("polybot")
logger.setLevel(logging.INFO)
logging.getLogger("polybot.discord_bot.bot").setLevel(logging.INFO)



# Cache for _get_contract_prices — avoid hammering Gamma API every tick
_contract_price_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # market_id -> (timestamp, contract)
_CONTRACT_CACHE_TTL = 5.0  # seconds — re-fetch at most every 5s per contract
_CONTRACT_RESOLUTION_TTL = 2.0  # faster polling when contract might be resolving
_WS_STALE_S = 10.0  # max age for CLOB WS BBA/book before treating as stale

# Throttled logging for hold evaluations and resolution waiting
_last_hold_log: dict[str, float] = {}  # market_id -> last log timestamp
_last_resolve_wait_log: dict[str, float] = {}  # market_id -> last log timestamp
_abandoned_scalp_positions: set[int] = set()  # position IDs too small to sell, hold to resolution

# Previous window resolution margin for adjacent window momentum (D2)
_prev_resolution_margin: float = 0.0
_PREV_MARGIN_PATH = Path("polybot/memory/prev_resolution_margin.json")

def _load_prev_resolution_margin() -> float:
    """Restore margin from last session so L5 signal isn't zeroed out on restart."""
    try:
        if _PREV_MARGIN_PATH.exists():
            return float(json.loads(_PREV_MARGIN_PATH.read_text()).get("margin", 0.0))
    except Exception:
        pass
    return 0.0

def _save_prev_resolution_margin(margin: float) -> None:
    try:
        _PREV_MARGIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREV_MARGIN_PATH.write_text(json.dumps({"margin": margin}))
    except Exception:
        pass

_sprt: SPRTAccumulator | None = None
_regime_detector: RegimeDetector | None = None
_cvd_normalizer: IndicatorNormalizer | None = None
_current_window_id: str = ""
_early_entry_fired: bool = False
_adverse_monitor: AdverseSelectionMonitor | None = None
_last_adverse_skip_log_window: int = 0  # throttle adverse-skip logs to once per 5-min window
_last_logged_action: str = ""  # suppress repeated EVAL blocks when action hasn't changed
_last_eval_buy_window: int = 0  # show full BUY block only once per window
_gate_skip_counts: dict[str, int] = {}  # gate_name -> skip count since last reset
_GATE_STATS_PATH = Path("polybot/memory/gate_stats.json")
from collections import OrderedDict as _OrderedDict
# Bounded LRUs — each entry is keyed by cid (or (cid, gate_key)). Without an
# eviction bound these grow forever; the bot runs for days at a time so
# unbounded would slowly leak ~1MB/week across the three dicts.
_PENDING_CTX_MAX = 32          # ~32 most recent markets — plenty for active windows
_GATE_STATE_MAX = 1024         # ~32 markets × ~32 gate keys
_pending_eval_ctx: _OrderedDict[str, dict] = _OrderedDict()
_last_gate_skip_state: _OrderedDict[tuple[str, str], float] = _OrderedDict()
_last_skip_log: _OrderedDict[tuple[str, str], int] = _OrderedDict()


def _lru_set(d: _OrderedDict, key, value, max_size: int) -> None:
    """LRU insert with eviction. Touch on overwrite, drop oldest past max_size."""
    if key in d:
        d.move_to_end(key)
    d[key] = value
    while len(d) > max_size:
        d.popitem(last=False)


def _log_skip_once(cid: str, key: str, msg: str) -> None:
    """Log a pre-signal skip at most once per 5-min window per (cid, reason)."""
    window = int(time.time() // 300) * 300
    k = (cid, key)
    if _last_skip_log.get(k) != window:
        _lru_set(_last_skip_log, k, window, _GATE_STATE_MAX)
        logger.info(msg)


def _log_hold_heartbeat_stale(pos: dict[str, Any], live: dict[str, Any], reason: str) -> None:
    """30s-throttled HOLD heartbeat for the exit-path stale-feed branch.

    Shares the _last_hold_log throttle with the normal HOLD log so the
    operator sees a steady pulse confirming the position is being monitored
    and *why* the bot won't act. Surfacing the reason is the whole point —
    silent fallbacks are what produced the "moved against us (2%)" pathology.
    """
    now_ts = time.time()
    mid = pos.get("market_id", "")
    if now_ts - _last_hold_log.get(mid, 0) >= 30:
        _last_hold_log[mid] = now_ts
        logger.info(
            f"  {_C.DIM}HOLD {pos.get('side', '?')}{_C.RESET}  "
            f"{_fmt_secs(live.get('seconds_remaining', 0))}  |  "
            f"deferring decision — {reason}"
        )


def _fastest_btc_price(coinbase_feed: Any, trades_feed: Any, binance_feed: Any) -> tuple[float, str]:
    """Return the freshest available BTC price + its source label.

    Each leg has a hard freshness gate based on local receipt time. The candle
    leg uses CandleBuffer.latest_age_s — receipt time of the most recent
    kline message (partial or close), NOT the candle's "t" field. The "t"
    field only advances on minute boundaries, so the old check (cdl_age based
    on timestamp < 5s) almost never fired and the candle leg was effectively
    dead code. With the receipt-time check, the candle leg correctly fires
    when partial klines have been arriving recently (every ~1s during a
    normal minute). Returns (0.0, "stale") when every leg is stale — callers
    must treat that as "skip this decision", not as a zero price.

    Priority order:
      1. Coinbase WS (<2s) — direct exchange ticker, sub-second tick stream
      2. Binance aggTrade (<3s) — per-trade WS stream, also sub-second
      3. Binance 1-min candle (<5s since last kline message) — the buffer's
         latest.close is updated by every partial kline, so receipt time
         within 5s means latest.close is current within the minute
    """
    if coinbase_feed:
        cb_age = coinbase_feed.state.age_seconds
        cb_price = coinbase_feed.state.price
        if cb_price > 0 and cb_age < 2:
            return cb_price, f"coinbase ({cb_age:.2f}s)"
    if trades_feed and trades_feed.accumulator:
        bt_age = trades_feed.accumulator.latest_age_s
        bt_price = trades_feed.accumulator.latest_price
        if bt_price > 0 and bt_age < 3:
            return bt_price, f"binance_trades ({bt_age:.2f}s)"
    latest_candle = binance_feed.buffer.latest() if binance_feed and binance_feed.buffer else None
    if latest_candle:
        cdl_age = binance_feed.buffer.latest_age_s
        if cdl_age < 5:
            return latest_candle.close, f"binance_candle ({cdl_age:.1f}s)"
    return 0.0, "stale"


def _fmt_secs(s: float) -> str:
    """Seconds remaining formatted as M:SS — 298 → '4:58'. Easier to scan than '298s'."""
    s_int = max(0, int(s))
    return f"{s_int // 60}:{s_int % 60:02d}"


def _emit_gate_skip(cid: str, gate_key: str, reason: str) -> None:
    """Emit one combined SKIP line (signal context + gate reason).

    Throttled per (cid, gate_key) — direction is intentionally NOT in the key, so
    rapid Up/Down ping-pong on the same gate (e.g. "No edge" firing alternately
    for each side as model prob hovers around the threshold) emits a single
    SKIP rather than 20× in 5 seconds. New gate or new window logs immediately.
    """
    ctx = _pending_eval_ctx.get(cid)
    if not ctx:
        logger.info(f"{_C.DIM}SKIP — {reason}{_C.RESET}")
        return
    now = time.time()
    key = (cid, gate_key)
    prev_time = _last_gate_skip_state.get(key)
    if prev_time is not None and (now - prev_time) < 30:
        return
    _lru_set(_last_gate_skip_state, key, now, _GATE_STATE_MAX)
    _sprt_part = f" | {ctx['sprt']}" if ctx.get("sprt") and not gate_key.startswith("sprt") else ""
    logger.info(
        f"{_C.DIM}SKIP {ctx['direction']} {ctx['window_slug']} | "
        f"model {ctx['prob']:.0%} {ctx['direction']}, BTC {ctx['dist']:+,.0f} vs strike | "
        f"{reason}{_sprt_part}{_C.RESET}"
    )

# Startup banner — emitted once after all systems are ready, inside trading_loop
_startup_banner_logged: bool = False


def _record_skip(gate: str) -> None:
    """Increment the per-gate skip counter. Called at every entry skip point."""
    _gate_skip_counts[gate] = _gate_skip_counts.get(gate, 0) + 1


def flush_gate_stats() -> None:
    """Write accumulated skip counts to disk for the pipeline to read."""
    from datetime import datetime, timezone
    try:
        _GATE_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GATE_STATS_PATH.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counts": dict(_gate_skip_counts),
            "total_skips": sum(_gate_skip_counts.values()),
        }, indent=2))
    except Exception:
        pass
# Per-window flip state: tracks flip count and last side
_window_flip_state: dict[str, dict] = {}  # window_id -> {flip_count, last_side}

# 1-second open-positions cache: avoids repeated SQLite round-trips in the hot path.
_open_positions_cache: list = []
_open_positions_cache_ts: float = 0.0

async def _get_open_positions_cached(db: Any) -> list:
    global _open_positions_cache, _open_positions_cache_ts
    now = time.time()
    if now - _open_positions_cache_ts < 1.0:
        return _open_positions_cache
    _open_positions_cache = await db.get_open_positions()
    _open_positions_cache_ts = now
    return _open_positions_cache


def _invalidate_open_positions_cache() -> None:
    """Force the next _get_open_positions_cached call to re-read the DB.
    Call after any successful open/close/resolve so concurrent-position math
    and entry-gate checks see the new state immediately instead of trailing
    the 1s TTL.
    """
    global _open_positions_cache_ts
    _open_positions_cache_ts = 0.0

# Rate-limit counterfactual resolution checks (Gamma REST calls, no need every tick).
_last_cf_check_ts: float = 0.0
_CF_CHECK_INTERVAL = 30.0  # seconds


def _build_signal_engine(signal_cfg: dict, config: dict) -> SignalEngine:
    """Construct SignalEngine from config — shared between pipeline and main."""
    return SignalEngine(
        min_edge=signal_cfg.get("min_edge", _d("min_edge")),
        kelly_fraction=config["math"].get("kelly_fraction", _d("kelly_fraction")),
        momentum_weight=signal_cfg.get("momentum_weight", _d("momentum_weight")),
        weights=signal_cfg.get("weights", _d("weights")),
        min_model_probability=signal_cfg.get("min_model_probability", _d("min_model_probability")),
        student_t_df=signal_cfg.get("student_t_df", _d("student_t_df")),
        regime_weight=signal_cfg.get("regime_weight", _d("regime_weight")),
        flow_weight=signal_cfg.get("flow_weight", _d("flow_weight")),
        regime_lookback=signal_cfg.get("regime_lookback", _d("regime_lookback")),
        min_kelly=signal_cfg.get("min_kelly", _d("min_kelly")),
        atr_sigma_ratio=signal_cfg.get("atr_sigma_ratio", _d("atr_sigma_ratio")),
        spot_flow_weight=signal_cfg.get("spot_flow_weight", _d("spot_flow_weight")),
        prev_margin_weight=signal_cfg.get("prev_margin_weight", _d("prev_margin_weight")),
        min_atr=signal_cfg.get("min_atr", _d("min_atr")),
        liquidation_weight=signal_cfg.get("liquidation_weight", _d("liquidation_weight")),
        logit_scale=signal_cfg.get("logit_scale", _d("logit_scale")),
        loss_cut_fraction=signal_cfg.get("loss_cut_fraction", _d("loss_cut_fraction")),
        loss_cut_time_s=signal_cfg.get("loss_cut_time_s", _d("loss_cut_time_s")),
        consensus_dead_zone=signal_cfg.get("consensus_dead_zone", _d("consensus_dead_zone")),
        consensus_config=signal_cfg.get("consensus"),
        regime_momentum_threshold=signal_cfg.get("regime_momentum_threshold", _d("regime_momentum_threshold")),
        flow_combined_cap=signal_cfg.get("flow_combined_cap", _d("flow_combined_cap")),
        final_logit_clamp=signal_cfg.get("final_logit_clamp", _d("final_logit_clamp")),
        deep_loss_hold_threshold=signal_cfg.get("deep_loss_hold_threshold", _d("deep_loss_hold_threshold")),
        l5_regime_damp_cap=signal_cfg.get("l5_regime_damp_cap", _d("l5_regime_damp_cap")),
        atr_regime_shift_threshold=signal_cfg.get("atr_regime_shift_threshold", _d("atr_regime_shift_threshold")),
        derived_weights=signal_cfg.get("derived") or {},
    )


def compute_time_multiplier(prob: float, seconds_remaining: float,
                            window_seconds: float = 300.0,
                            normal_fraction: float = 0.60,
                            late_max_penalty: float = 0.30) -> tuple[float, str]:
    """Returns (kelly_multiplier, phase). High-conviction entries barely penalized late."""
    time_fraction = seconds_remaining / window_seconds
    conviction = 2.0 * abs(prob - 0.5)
    if time_fraction >= normal_fraction:
        return 1.0, "normal"
    phase = "late" if seconds_remaining >= 30 else "final"
    late_depth = (normal_fraction - time_fraction) / normal_fraction
    penalty = late_depth * (1.0 - conviction) * late_max_penalty
    return max(0.40, 1.0 - penalty), phase


async def _get_contract_prices(market_scanner: Any, market_id: str, http_client: Any = None) -> dict[str, Any] | None:
    """Fetch current Up/Down prices for an active contract via Gamma API.

    Caches results per market_id to avoid redundant HTTP calls during
    position management ticks. Polls faster near expiry for resolution.
    """
    import httpx
    from datetime import datetime, timezone

    now = time.time()
    cached = _contract_price_cache.get(market_id)
    if cached:
        cache_ts, contract = cached
        # Recompute seconds_remaining from stored end_date (no HTTP needed)
        end_str = contract.get("end_date", "")
        if end_str:
            try:
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                contract["seconds_remaining"] = max(0.0, (end - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                pass
        # Use longer cache TTL while active, shorter near/past expiry
        ttl = _CONTRACT_RESOLUTION_TTL if contract.get("seconds_remaining", 999) <= 10 else _CONTRACT_CACHE_TTL
        if (now - cache_ts) < ttl:
            return contract

    window_ts = int(time.time() // 300) * 300
    for ts in [window_ts, window_ts + 300, window_ts - 300]:
        slug = market_scanner._make_slug(ts)
        try:
            resp = await http_client.get(f"{market_scanner.GAMMA_API}/events",
                                         params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
            if data:
                event = data[0] if isinstance(data, list) else data
                contract = market_scanner.parse_contract(event)
                if contract and contract.get("slug", "") == market_id:
                    _contract_price_cache[market_id] = (now, contract)
                    return contract
        except httpx.TimeoutException:
            continue
        except Exception as e:
            logger.warning(f"Price fetch error for {slug}: {e}")
            continue

    # Fallback: fetch directly by stored slug (handles expired contracts outside ±1 window)
    try:
        resp = await http_client.get(f"{market_scanner.GAMMA_API}/events",
                                     params={"slug": market_id})
        resp.raise_for_status()
        data = resp.json()
        if data:
            event = data[0] if isinstance(data, list) else data
            contract = market_scanner.parse_contract(event)
            if contract:
                _contract_price_cache[market_id] = (now, contract)
                return contract
    except Exception as e:
        logger.debug(f"Direct slug lookup failed for {market_id}: {e}")

    return None


def _get_token_midprice(clob_ws: Any):
    """Return a callable ``token_id -> midprice`` for AdverseSelectionMonitor.

    Mid is ``(best_bid + best_ask) / 2`` from CLOB WS; returns 0.0 when we have no
    fresh book for that token, which the caller treats as "skip this checkpoint."
    """
    def _mid(token_id: str) -> float:
        bba = clob_ws.best_bid_ask.get(token_id, {}) if clob_ws else {}
        try:
            ts = float(bba.get("ts", 0) or 0)
            if ts <= 0 or (time.time() - ts) > _WS_STALE_S:
                return 0.0
            bid = float(bba.get("best_bid", 0))
            ask = float(bba.get("best_ask", 0))
        except (TypeError, ValueError):
            return 0.0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return 0.0
    return _mid


def _btc_at_expiry(binance_feed: Any, market_id: str) -> float:
    """Get BTC price at contract expiry from candle buffer.

    Parses window_ts from the slug (btc-updown-5m-{window_ts}),
    computes expiry = window_ts + 300, finds the 1-min candle
    covering that moment. Falls back to latest price if not in buffer.
    """
    try:
        window_ts = int(market_id.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        latest = binance_feed.buffer.latest()
        return latest.close if latest else 0

    expiry_ms = (window_ts + 300) * 1000
    for c in reversed(binance_feed.buffer.get_last_n(30)):
        if c.timestamp <= expiry_ms < c.timestamp + 60_000:
            return c.close

    latest = binance_feed.buffer.latest()
    return latest.close if latest else 0


async def _record_outcome(outcome_reviewer: Any, pos: dict[str, Any], exit_price: float,
                          log_return: float, gain_pct: float,
                          exit_reason: str = "resolution", pnl: float = 0.0,
                          fees: float = 0.0,
                          seconds_remaining_at_exit: float = 0.0) -> None:
    """Persist a resolved/scalped trade outcome for the learning pipeline."""
    edge_decay = None
    if _adverse_monitor is not None:
        edge_decay = _adverse_monitor.get_decay_for_position(pos["id"])
    try:
        outcome_reviewer.record_outcome(
            position_id=pos["id"],
            market_id=pos["market_id"],
            question=pos["question"],
            side=pos["side"],
            signal_score=pos["signal_score"],
            profitable=gain_pct > 0,
            entry_price=pos["entry_price"],
            exit_price=exit_price,
            log_return=log_return,
            indicator_snapshot=json.loads(pos.get("indicator_snapshot", "{}")),
            exit_reason=exit_reason,
            size=pos.get("size", 0.0),
            pnl=pnl,
            fees=fees,
            exit_timestamp=pos.get("exit_timestamp", ""),
            seconds_remaining_at_exit=seconds_remaining_at_exit,
            edge_decay=edge_decay,
        )
    except Exception as e:
        logger.error(f"Failed to record outcome: {e}")


async def _evaluate_signal_and_enter(
        contract: dict[str, Any], cid: str, binance_feed: Any, indicator_engine: Any,
        signal_engine: Any, market_scanner: Any, http_client: Any, clob_ws: Any,
        trader: Any, alert_manager: Any, db: Any, config: dict[str, Any], breaker: Any,
        price_up: float, price_down: float, price_source: str,
        book_up: dict[str, Any], book_down: dict[str, Any],
        depth_usd_up: float, depth_usd_down: float,
        btc_price: float, strike: float, eval_window: int, last_eval_log_window: int,
        token_up: str, token_down: str, signal_config: dict[str, Any],
        max_bankroll_pct: float,
        now_ts: int,
        bankroll: float = 0.0,
        depth_feed: Any = None,
        trades_feed: Any = None,
        bybit_feed: Any = None,
        coinbase_feed: Any = None,
        chainlink_feed: Any = None,
        ghost_tracker: Any = None) -> tuple[str | None, int]:
    """Compute indicators/flow/signal, check for entry, size the trade, execute."""

    def _ghost(gate: str, signal: Any, snap: dict) -> None:
        """Record a ghost trade when a downstream gate rejects a real BUY signal."""
        if ghost_tracker is None or signal is None:
            return
        if signal.action not in ("BUY_YES", "BUY_NO"):
            return  # model-level skip — not a valid ghost
        side = "Up" if signal.action == "BUY_YES" else "Down"
        ghost_tracker.record_rejection(
            gate_name=gate,
            side=side,
            signal_prob=signal.prob,
            signal_edge=signal.edge,
            market_id=cid,
            seconds_remaining=float(contract.get("seconds_remaining", 0)),
            indicator_snapshot=snap,
        )

    # Feed freshness gate: skip entries when any critical price/strike feed has
    # gone silent. A connected-but-idle WebSocket can leave stale state in place
    # while we evaluate; better to skip the window than to size on stale data.
    stale_feeds: list[str] = []
    if coinbase_feed and coinbase_feed.state.age_seconds > 30:
        stale_feeds.append(f"coinbase={coinbase_feed.state.age_seconds:.0f}s")
    if chainlink_feed and chainlink_feed.age_seconds > 60:
        stale_feeds.append(f"chainlink={chainlink_feed.age_seconds:.0f}s")
    # Bybit OI underpins L3e (liquidation pressure). Updates ~5s normally;
    # >60s of silence means we'd be reading a frozen OI snapshot.
    if bybit_feed is not None and bybit_feed.state.oi_updated > 0:
        bybit_age = time.time() - bybit_feed.state.oi_updated
        if bybit_age > 60:
            stale_feeds.append(f"bybit_oi={bybit_age:.0f}s")
    # Binance aggTrade underpins L3b (CVD/taker) and the CVD-deceleration gate.
    # Previously the staleness check lived inside the spot_flow computation and
    # silently zeroed L3b on stale data; now we skip entirely (matches CLAUDE.md's
    # documented behavior) so we don't size off a degraded model when the feed
    # goes silent.
    if trades_feed is not None and trades_feed.accumulator is not None:
        agg_age = trades_feed.accumulator.latest_age_s
        if agg_age > 30:
            stale_feeds.append(f"binance_aggtrade={agg_age:.0f}s")
    if binance_feed and binance_feed.buffer and len(binance_feed.buffer) > 0:
        kline_age = binance_feed.buffer.latest_age_s
        if kline_age > 45:
            stale_feeds.append(f"binance_kline={kline_age:.0f}s")
    if stale_feeds:
        _record_skip("stale_feed")
        _log_skip_once(cid, f"stale_{cid}", f"SKIP: stale feeds — {', '.join(stale_feeds)}")
        return None, last_eval_log_window

    in_window = market_scanner.in_entry_window(contract["seconds_remaining"])

    # SPRT: accumulate evidence on new windows (no hard observe block)
    global _sprt, _current_window_id, _early_entry_fired
    window_id = contract.get("market_id", contract.get("slug", ""))
    if window_id != _current_window_id:
        _current_window_id = window_id
        if _sprt: _sprt.reset()
        _early_entry_fired = False
        _last_skip_log.pop(cid, None)  # fresh window — allow skip reasons to log again

    # Compute indicators and evaluate probability model
    indicators = indicator_engine.compute_all(binance_feed.buffer)

    # Compute order flow signal from CLOB data
    trades_up = clob_ws.get_trade_history(token_up) if clob_ws else []
    trades_down = clob_ws.get_trade_history(token_down) if clob_ws else []
    flow_data = compute_flow_signal(book_up, book_down, trades_up, trades_down)
    flow_score = flow_data["flow_score"]

    # --- New signals from extended feeds ---
    spot_flow_signal = 0.0

    if trades_feed and trades_feed.accumulator and trades_feed.accumulator.latest_age_s <= 30:
        acc = trades_feed.accumulator
        cvd = acc.get_cvd(window_s=120)
        taker = acc.get_taker_ratio(window_s=60)
        # CVD-dominant: taker_ratio is degenerate on Binance.US (87% are 0.0/0.5/1.0).
        # CVD has real signal above noise floor (r=0.14 vs taker r=0.025).
        # Gate taker: only trust when trade count >= 5 in window (not 1-trade noise).
        trade_count = acc.trade_count
        cvd_z = _cvd_normalizer.normalize("cvd", cvd) if _cvd_normalizer else 0.0
        cvd_component = math.tanh(cvd_z) * 0.8
        taker_component = (taker - 0.5) * 2 * 0.2 if trade_count >= 5 else 0.0
        spot_flow_signal = max(-1.0, min(1.0, cvd_component + taker_component))

    # CVD acceleration (first derivative of buying pressure). Raw value (BTC/sec)
    # drives the deceleration gate; the normalized [-1, 1] version is what the
    # consensus check sees, so its dead_zone is comparable to flow / spot_flow.
    cvd_accel_val = 0.0
    cvd_accel_norm = 0.0
    if trades_feed and trades_feed.accumulator and trades_feed.accumulator.latest_age_s <= 30:
        cvd_accel_val = trades_feed.accumulator.get_cvd_acceleration(recent_s=15, baseline_s=45)
        if _cvd_normalizer is not None:
            cvd_accel_norm = math.tanh(_cvd_normalizer.normalize("cvd_accel", cvd_accel_val))

    # Liquidation pressure from Bybit OI changes
    liquidation_val = 0.0
    if bybit_feed and bybit_feed.state.open_interest > 0 and bybit_feed.state.open_interest_prev > 0:
        _oi_elapsed = bybit_feed.state.oi_updated - bybit_feed.state.oi_updated_prev
        liquidation_val = compute_liquidation_pressure(
            bybit_feed.state.open_interest, bybit_feed.state.open_interest_prev,
            bybit_feed.state.price_at_oi, bybit_feed.state.price_at_oi_prev,
            elapsed_seconds=_oi_elapsed if _oi_elapsed > 0 else 1.0)

    # Get closes array for regime detection
    closes = binance_feed.buffer.get_closes()

    # Fetch the live fee rate so Kelly sizes against the cost the trader will
    # actually pay, not the hardcoded default. Constant today (no HTTP call),
    # but plumbing it through means a future per-token rate Just Works.
    fee_rate = await market_scanner.fetch_fee_rate(token_up, http_client)

    signal = signal_engine.evaluate(
        indicators, has_position=False, in_entry_window=in_window,
        btc_price=btc_price, strike_price=strike,
        seconds_remaining=contract["seconds_remaining"],
        market_price_up=price_up, market_price_down=price_down,
        closes=closes, flow_signal=flow_score,
        spot_flow_signal=spot_flow_signal,
        prev_resolution_margin=_prev_resolution_margin,
        liquidation_pressure=liquidation_val,
        fee_rate=fee_rate,
    )

    # SPRT: feed the signal into the accumulator (used by SPRT side gate below).
    # signal.prob is the probability of the CHOSEN side, so invert for BUY_NO to
    # produce the prob_up that SPRT expects.
    if _sprt:
        if signal.action == "BUY_YES":
            _sprt.update(signal.prob)
        elif signal.action == "BUY_NO":
            _sprt.update(1.0 - signal.prob)
        else:
            _sprt.update(0.5)

    # Continuous time multiplier: penalizes ATM trades late, barely penalizes high-conviction trades
    timing_cfg = config.get("entry_timing", {})
    time_mult, phase = compute_time_multiplier(
        prob=signal.prob,
        seconds_remaining=contract["seconds_remaining"],
        normal_fraction=timing_cfg.get("normal_fraction", _d("normal_fraction")),
        late_max_penalty=timing_cfg.get("late_max_penalty", _d("late_max_penalty")),
    )

    # Populate eval context for all evaluations — BUY uses actual direction,
    # model-level SKIP infers it from signal.prob 
    global _last_logged_action, _last_eval_buy_window
    _is_buy = signal.action in ("BUY_YES", "BUY_NO")
    _direction = "Up" if signal.action == "BUY_YES" else ("Down" if signal.action == "BUY_NO"
                 else ("Up" if signal.prob >= 0.5 else "Down"))
    action_changed = _direction != _last_logged_action or eval_window != last_eval_log_window
    dist = btc_price - strike
    _sprt_info = ""
    if _sprt:
        _s, _c, _f, _n = _sprt.get_status(), _sprt.get_confidence(), _sprt.favored_side(), _sprt.observation_count()
        if _s != "ACCUMULATING":
            _sprt_info = f"SPRT {_s} {_c:.0%}"
        elif _f and _c >= 0.20:
            _sprt_info = f"SPRT {_c:.0%} leaning {_f}"
        else:
            _sprt_info = f"SPRT {_c:.0%} ({_n} obs)"
    _lru_set(_pending_eval_ctx, cid, {
        "direction": _direction,
        "prob": signal.prob,
        "edge": signal.edge,
        "dist": dist,
        "window_slug": _slug_to_window(cid),
        "sprt": _sprt_info,
    }, _PENDING_CTX_MAX)
    if _is_buy:
        if action_changed:
            last_eval_log_window = eval_window
            _last_logged_action = _direction
            _last_eval_buy_window = eval_window
            _last_gate_skip_state.pop(cid, None)
    else:
        last_eval_log_window = eval_window
        _reason_type = signal.reason.split(":")[0].strip()
        _emit_gate_skip(cid, f"model_{_reason_type}", signal.reason)

    if signal.action not in ("BUY_YES", "BUY_NO"):
        _record_skip(f"model:{signal.reason[:30]}")
        return None, last_eval_log_window

    # --- ADVERSE SELECTION GATE ---
    if _adverse_monitor is not None:
        adverse_threshold = config["signal"]["adverse_selection_threshold"]
        adverse_rate = _adverse_monitor.get_adverse_rate(30.0)
        if adverse_rate > adverse_threshold:
            _record_skip("adverse_selection")
            _ghost("adverse_selection", signal, {})
            global _last_adverse_skip_log_window
            if eval_window != _last_adverse_skip_log_window:
                _last_adverse_skip_log_window = eval_window
                logger.info(
                    f"{_C.DIM}SKIP adverse selection — fade rate {adverse_rate:.0%} > {adverse_threshold:.0%}{_C.RESET}"
                )
            else:
                logger.debug(
                    f"SKIP adverse selection — fade rate {adverse_rate:.0%} > {adverse_threshold:.0%}"
                )
            return None, last_eval_log_window

    # --- EDGE DECAY GATE ---
    # Mean side-signed mid drift in the 15s window after recent fills. The
    # adverse_selection gate above counts fills crossing the wrong way; this
    # gate measures HOW HARD they cross. A consistently negative drift means
    # quotes are repositioning against us faster than we can scalp out — a
    # direct read on whether the structural edge has decayed.
    if _adverse_monitor is not None:
        edge_decay_threshold = config.get("signal", {}).get("edge_decay_threshold", -0.05)
        recent_decay = _adverse_monitor.get_recent_decay_mean(window_s=15.0, lookback_s=1800.0,
                                                              min_samples=15)
        if recent_decay is not None and recent_decay < edge_decay_threshold:
            _record_skip("edge_decay")
            _ghost("edge_decay", signal, {})
            _emit_gate_skip(
                cid, "edge_decay",
                f"15s post-fill drift {recent_decay:+.3f} < {edge_decay_threshold:+.3f}"
            )
            return None, last_eval_log_window

    # --- EDGE CAP GATE ---
    max_edge = config.get("signal", {}).get("max_edge", 0.20)
    if signal.edge > max_edge:
        _record_skip("edge_cap")
        _ghost("edge_cap", signal, {})
        return None, last_eval_log_window

    side = "Up" if signal.action == "BUY_YES" else "Down"
    token_id = contract["token_id_up"] if side == "Up" else contract["token_id_down"]
    cid = contract.get("slug", contract.get("market_id", ""))

    flip_state = _window_flip_state.setdefault(cid, {
        "flip_count": 0, "last_side": None,
    })
    flip_count = flip_state["flip_count"]
    if flip_count >= 1:
        # Opposite-side flip needs a meaningful model shift past 0.5 — without
        # this, the bot whipsaws on 1-2pp model noise after every scalp.
        if flip_state["last_side"] is not None and side != flip_state["last_side"]:
            if signal.prob < 0.53:
                _record_skip("opposite_flip_weak_prob")
                _emit_gate_skip(cid, f"opposite_flip_{side}",
                                f"opposite-side flip needs prob ≥ 53%, got {signal.prob:.0%}")
                return None, last_eval_log_window

        flip_premium = config.get("entry_timing", {}).get("flip_edge_premium", _d("flip_edge_premium"))
        spread_est = -1.0
        if clob_ws:
            bba = clob_ws.best_bid_ask.get(token_id, {})
            bba_ts = float(bba.get("ts", 0) or 0)
            if bba_ts > 0 and (time.time() - bba_ts) <= _WS_STALE_S:
                try:
                    spread_est = float(bba.get("spread", -1)) if bba.get("spread") else -1.0
                except (TypeError, ValueError):
                    spread_est = -1.0
        # Real round-trip cost in price units: enter at ask (cross half-spread),
        # exit at bid (cross half-spread) = full `spread` plus fee impact on both
        # legs. Polymarket fee impact = fee_rate × p × (1-p), max ~0.45% at ATM.
        # Old formula `spread/2 + fee_rate` mixed half-spread (price units) with
        # raw fee_rate (a rate), over-conservative on tight spreads and
        # under-conservative on wide ones.
        side_price = price_up if side == "Up" else price_down
        if spread_est >= 0:
            fee_impact_one_leg = DEFAULT_FEE_RATE * side_price * (1.0 - side_price)
            spread_cost = spread_est + 2.0 * fee_impact_one_leg
        else:
            spread_cost = flip_premium
        flip_hurdle = signal_engine.min_edge + max(flip_premium, spread_cost)
        if signal.edge < flip_hurdle:
            _record_skip("flip_insufficient_edge")
            return None, last_eval_log_window

    # --- SPRT GATE ---
    # Blocks entries when sequential evidence is definitively weak (SKIP status)
    # or hasn't yet accumulated enough directional support (low confidence).
    # Placed after side/cid assignment so _log_skip_once and _ghost have proper context.
    if _sprt:
        sprt_cfg = config["sprt"]
        min_sprt_conf = sprt_cfg["min_confidence"]
        sprt_status = _sprt.get_status()
        sprt_conf = _sprt.get_confidence()
        sprt_obs = _sprt.observation_count()
        if sprt_status == "SKIP":
            _record_skip("sprt_skip")
            _ghost("sprt_skip", signal, {})
            _emit_gate_skip(cid, f"sprt_skip_{side}", f"SPRT: signal too weak ({sprt_obs} obs)")
            return None, last_eval_log_window
        # Minimum-evidence floor: after the 6-obs warmup, require at least
        # `min_confidence` accumulated LLR before allowing entry.
        if sprt_obs >= 6 and sprt_conf < min_sprt_conf:
            _record_skip("sprt_low_confidence")
            _ghost("sprt_low_confidence", signal, {})
            _emit_gate_skip(cid, f"sprt_low_conf_{side}",
                            f"SPRT confidence {sprt_conf:.1%} < {min_sprt_conf:.1%}")
            return None, last_eval_log_window
        # Side mismatch: only veto when SPRT has built strong opposite evidence (60%+, 6+ obs)
        if sprt_obs >= 6 and sprt_conf > 0.60 and _sprt.favored_side() != side:
            _record_skip("sprt_side_mismatch")
            _emit_gate_skip(cid, f"sprt_{side}",
                            f"SPRT {sprt_conf:.0%} leaning {_sprt.favored_side()} (opposite side)")
            return None, last_eval_log_window

    # --- LAYER DISAGREEMENT GATE ---
    # `compute_momentum` is regime-aware and sign-coherent (positive = bullish in
    # the current regime). Reuse the cached value from compute_probability above —
    # the inputs (indicators, regime) haven't changed since signal_engine.evaluate.
    # Threshold 0.5 matches a strong-disagreement signal on the unit-clamped
    # output (group sums × weights, regime-conditioned).
    momentum_score = signal_engine.last_momentum_score
    momentum_opposes = (
        (side == "Up" and momentum_score < -0.5)
        or (side == "Down" and momentum_score > 0.5)
    )
    if momentum_opposes and signal.edge * 0.5 < signal_engine.min_edge:
        _record_skip("layer_disagreement")
        _ghost("layer_disagreement", signal, {})
        _emit_gate_skip(cid, f"layer_disagree_{side}", f"layer disagree — momentum {momentum_score:+.2f} opposes {side}")
        return None, last_eval_log_window

    # --- CVD DECELERATION GATE ---
    # Skip when spot-flow is materially driving the entry but buying pressure is
    # already fading. spot_flow_signal × cvd_accel_val < 0 means the CVD spike
    # has peaked and is reverting — these entries resolve at $0 rather than
    # recovering, because the momentum that created the signal is already gone.
    if abs(spot_flow_signal) >= 0.20 and spot_flow_signal * cvd_accel_val < 0:
        _record_skip("cvd_decel")
        _ghost("cvd_decel", signal, {})
        _flow_label = "sells" if spot_flow_signal < 0 else "buys"
        _emit_gate_skip(cid, f"cvd_decel_{side}",
                        f"CVD reversing — {_flow_label} ({spot_flow_signal:+.2f}) decelerating ({cvd_accel_val:+.2f})")
        return None, last_eval_log_window

    price = price_up if side == "Up" else price_down
    if not bankroll:
        bankroll = await db.get_bankroll()
    kelly_mult = breaker.kelly_multiplier if breaker else 1.0


    raw_kelly_size = bankroll * signal.kelly_size
    size = round(raw_kelly_size * kelly_mult * time_mult, 2)

    # Regime-based Kelly adjustment
    regime_state = None
    if _regime_detector:
        atr_val = indicators.get("atr", {}).get("atr", 0)
        atr_history = [c.high - c.low for c in binance_feed.buffer.get_last_n(50)]
        cvd_now = trades_feed.accumulator.get_cvd(120) if trades_feed and trades_feed.accumulator else 0
        regime_state = _regime_detector.classify(
            closes, atr_val, atr_history, cvd_now,
            autocorr=signal_engine.last_regime_autocorr,  # already computed in compute_probability
        )
        if regime_state.skip:
            _record_skip(f"regime:{regime_state.name}")
            _emit_gate_skip(cid, f"regime_{regime_state.name}", f"regime={regime_state.name}")
            return None, last_eval_log_window
        # Regime: logged for pipeline, NOT applied to sizing (operates near noise at SE=0.14)

    # Signal consensus: scales size by how many flow signals agree with the chosen side.
    consensus_signals = {
        "flow": flow_score,
        "spot_flow": spot_flow_signal,
        "cvd_accel": cvd_accel_norm,
    }
    consensus_mult = compute_signal_consensus(
        consensus_signals, side,
        dead_zone=signal_engine.consensus_dead_zone,
        consensus_config=signal_engine.consensus_config)
    size = round(size * consensus_mult, 2)

    logger.debug(
        f"  REGIME {regime_state.name if regime_state else 'N/A'}  |  "
        f"SPRT {_sprt.get_status() if _sprt else 'N/A'} ({_sprt.get_confidence():.0%})  |  "
        f"consensus {consensus_mult:.1f}x")

    # Late-window underdog gate
    if contract.get("seconds_remaining", 300) < 120:
        late_underdog_floor = config.get("signal", {}).get("late_window_min_prob", 0.40)
        if signal.prob < late_underdog_floor:
            _record_skip("late_window_underdog")
            _ghost("late_window_underdog", signal, {})
            logger.debug(
                f"SKIP: late window underdog — chosen side prob {signal.prob:.0%} < "
                f"{late_underdog_floor:.0%} with {contract.get('seconds_remaining', 0):.0f}s left"
            )
            return None, last_eval_log_window

    open_positions = await _get_open_positions_cached(db)
    active_positions = [p for p in open_positions if p.get("status") == "open"]
    if active_positions:
        cc_mult = concurrent_multiplier(side, cid, active_positions)
        size = round(size * cc_mult, 2)

    # Total-deployment cap (across all open positions) stays at the single-trade level
    # as a defensive clip; base.py also enforces it at the trader layer.
    if size > bankroll * max_bankroll_pct:
        size = round(bankroll * max_bankroll_pct, 2)

    # Cap size to fraction of book depth (realistic fill constraint — unlike risk caps,
    # this is about whether the order can actually fill).
    #
    # The thin-CLOB-depth gate upstream uses AND across both sides (proceeds if EITHER
    # side has depth ≥ min). When the chosen side is the empty/thin one of a one-sided
    # book, we'd previously skip this entire branch (`side_depth > 0` was False) and
    # blast a full-Kelly order into a 0-liquidity book. Treat an empty/below-floor
    # chosen-side depth as an explicit skip instead.
    side_depth = depth_usd_up if side == "Up" else depth_usd_down
    max_fill_pct = config.get("execution", {}).get("max_book_fill_pct", 0.50)
    # Reuse the same floor as the upstream both-sides-thin gate so configuration
    # has one source of truth and the two gates can't drift apart.
    min_side_depth = market_scanner.min_book_depth_usd
    if side_depth < min_side_depth:
        _record_skip("thin_book_depth")
        _emit_gate_skip(cid, "thin_book_depth",
                        f"chosen side {side} depth ${side_depth:.0f} < ${min_side_depth:.0f}")
        return None, last_eval_log_window
    max_fill = side_depth * max_fill_pct
    if size > max_fill:
        size = round(max_fill, 2)
        if size < 0.10:
            _record_skip("thin_book_depth")
            _emit_gate_skip(cid, "thin_book_depth", f"thin book ${side_depth:.0f}")
            return None, last_eval_log_window

    # Net-edge gate: reject if slippage eats the edge below threshold.
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    est_slip = slippage_pct(size, side_depth, impact)
    net_edge = signal.edge - price * est_slip
    if net_edge < signal_engine.min_edge:
        _record_skip("net_edge_after_slippage")
        _ghost("net_edge_after_slippage", signal, {})
        _emit_gate_skip(cid, "net_edge_slippage", f"net edge {net_edge:+.1%} after {est_slip:.2%} slippage")
        return None, last_eval_log_window

    # Final minimum size check — after all caps have been applied. Polymarket's
    # CLOB rejects marketable orders below $1 notional, so gate here to avoid
    # spamming attempts that can never fill. Paper mode mirrors the same floor
    # so backtest sample matches live execution.
    if size < 1.0:
        _record_skip("min_size")
        _emit_gate_skip(cid, "min_size", f"size ${size:.2f} < $1 min")
        return None, last_eval_log_window

    # fee_rate already fetched before signal eval (used by Kelly). tick_size
    # is per-chosen-side so fetched here.
    tick_size = await market_scanner.fetch_tick_size(token_id, http_client)
    fresh_bba = clob_ws.best_bid_ask.get(token_id, {}) if clob_ws else {}
    _fresh_bba_ts = float(fresh_bba.get("ts", 0) or 0)
    fresh_ask = (float(fresh_bba.get("best_ask", 0) or 0)
                 if _fresh_bba_ts > 0 and (time.time() - _fresh_bba_ts) <= _WS_STALE_S
                 else 0.0)
    # Maker/FOK blend simulation in paper mode: ~65% maker (0% fee), ~35% taker FOK.
    if config.get("execution", {}).get("use_maker_orders", False):
        import random
        if random.random() < 0.65:
            fee_rate = 0.0  # maker fill

    # Apply slippage to the price returned by _fetch_market_prices (already from GET /price?side=BUY).
    # Entries deliberately use a tight slip (no FOK-cross floor): we WANT the FOK to
    # reject when prices have moved adversely between signal and fill — that adverse-
    # selection rejection is a feature, it stops us from buying post-reversal tops.
    # Exits use a loose floor instead (see exit_fill in evaluate_hold) — there we
    # must fill to avoid death-spiral lockout.
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    slip = slippage_pct(size, side_depth, impact)
    price = market_scanner.snap_to_tick(price * (1 + slip), tick_size)

    snapshot = indicator_engine.get_snapshot(indicators)
    # Last two closes are stored so the L6 backtest can reconstruct `last_return`
    # for the autocorr_signed_mag feature — without them that feature is dormant
    # in replay and the pipeline can't adopt it even if it would help live.
    _closes_buf = binance_feed.buffer.get_closes()
    _closes_tail = (
        [float(_closes_buf[-2]), float(_closes_buf[-1])]
        if len(_closes_buf) >= 2 else None
    )
    snapshot["trade_context"] = {
        # Entry-time facts — needed by backtest replay
        "btc_price": btc_price,
        "strike_price": strike,
        "seconds_remaining": contract["seconds_remaining"],
        "market_price_up": price_up,
        "market_price_down": price_down,
        "closes_tail": _closes_tail,
        "model_probability": signal.prob,
        # Pre-calibrator P(side). Stored separately from `model_probability` so the
        # next pipeline cycle's isotonic re-fit sees raw probabilities, not calibrate(calibrate(...)).
        "model_probability_raw": (
            signal_engine.last_raw_prob_up if side == "Up"
            else 1.0 - signal_engine.last_raw_prob_up
        ),
        "edge": signal.edge,
        "atr": indicators.get("atr", {}).get("atr", 0),
        "size": size,
        "prev_resolution_margin": _prev_resolution_margin,
        # Composite signals used by the model — pipeline replays L1-L5 from these
        "flow_score": flow_score,
        "spot_flow_signal": spot_flow_signal,
        "liquidation_pressure": liquidation_val,
        # Regime + L2 inputs (autocorr + direction stored exactly for backtest fidelity)
        "regime_state": regime_state.name if regime_state else "unknown",
        "regime_autocorr": round(signal_engine.last_regime_autocorr, 4),
        "regime_direction": round(signal_engine.last_regime_direction, 4),
        # Time-of-window classification (used by bias_detector time_patterns + flip analysis)
        "entry_phase": phase,
        "flip_count": flip_count,
        "is_flip": flip_count > 0,
        # Order-book depth used for the entry gate; useful for retrospective analysis
        "depth_usd_top20": depth_feed.get_depth_usd() if depth_feed else 0,
        # SPRT diagnostic state (consumed by pipeline_analytics.aggregate_sprt_evidence)
        "sprt_confidence": _sprt.get_confidence() if _sprt else 0,
        "sprt_status": _sprt.get_status() if _sprt else "N/A",
        # Adverse-selection rolling state (gate diagnostic). Field name reads as
        # "fraction of fills that moved against us measured AT 30s post-fill" over
        # the monitor's 30-minute lookback — 30s is the checkpoint, not the lookback.
        "adverse_rate_at_30s": _adverse_monitor.get_adverse_rate(30.0) if _adverse_monitor else 0.5,
        # Which calibrator was live at fill time — lets the pipeline stratify
        # outcomes by calibrator-in-effect so the 7d calibration window and
        # 60d backtest window don't blend trades decided under different curves.
        "calibrator_hash": (
            signal_engine.calibrator.state_hash if signal_engine.calibrator else "identity"
        ),
        # Token IDs for both outcomes — required for startup reconciliation and dust sweeping.
        "token_id_up": contract.get("token_id_up", ""),
        "token_id_down": contract.get("token_id_down", ""),
    }
    # Pre-submit edge re-check.
    # Prefer walking the current ask ladder for the actual expected FOK VWAP —
    # the modeled `slip` (slippage_pct) approximates this against `side_depth`,
    # but the book itself is the ground truth. When the book is unavailable or
    # too thin to walk (returns None), fall back to the BBA-only fresh_ask gate
    # so this never tightens-by-skipping a path the old gate would have passed.
    max_edge_live = config.get("signal", {}).get("max_edge", 0.20)
    book_for_walk = clob_ws.get_book(token_id) if clob_ws else None
    if book_for_walk:
        _book_ts = float(book_for_walk.get("ts", 0) or 0)
        if _book_ts <= 0 or (time.time() - _book_ts) > _WS_STALE_S:
            book_for_walk = None
    fok_vwap = compute_buy_vwap(book_for_walk, size) if book_for_walk else None
    if fok_vwap is not None:
        vwap_net_edge = signal.prob - fok_vwap  # VWAP already absorbs book-walk slippage
        if vwap_net_edge < signal_engine.min_edge or vwap_net_edge > max_edge_live:
            _record_skip("pre_submit_vwap_drift")
            _ghost("pre_submit_vwap_drift", signal, snapshot)
            _emit_gate_skip(cid, "pre_submit_vwap_drift",
                            f"vwap walk {price:.3f}→{fok_vwap:.3f}, net edge {vwap_net_edge:+.1%}")
            return None, last_eval_log_window
    elif fresh_ask > 0 and fresh_ask != price:
        fresh_gross_edge = signal.prob - fresh_ask
        fresh_net_edge = fresh_gross_edge - fresh_ask * slip
        if fresh_net_edge < signal_engine.min_edge or fresh_gross_edge > max_edge_live:
            _record_skip("pre_submit_edge_drift")
            _ghost("pre_submit_edge_drift", signal, snapshot)
            _emit_gate_skip(cid, "pre_submit_drift",
                            f"ask drifted {price:.3f}→{fresh_ask:.3f}, net edge {fresh_net_edge:+.1%}")
            return None, last_eval_log_window

    result = await trader.open_trade(
        market_id=cid,
        question=contract["question"],
        side=side,
        price=price,
        size=size,
        signal_score=signal.prob,
        signal_strength=f"edge={signal.edge:.0%}",
        indicator_snapshot=snapshot,
        token_id=token_id,
        fee_rate=fee_rate,
    )

    if not result.success:
        reason = result.reason or "unknown"
        _log_skip_once(
            cid, f"open_rejected_{reason}",
            f"{_C.DIM}OPEN {side} REJECTED  ${size:.2f} @ {price:.3f}  |  "
            f"{_slug_to_window(cid)}  |  {reason}{_C.RESET}"
        )
        return None, last_eval_log_window

    if result.success:
        # Drop the open-positions cache so the next entry tick sees this new
        # position instead of trailing the 1s TTL.
        _invalidate_open_positions_cache()
        # Use the actual fill price (may differ from signal-moment price due to
        # paper trader latency + book-walk, or live FOK slippage).
        fill_price = result.fill_price if result.fill_price > 0 else price
        slip_note = f"  [filled @ {fill_price:.3f} vs signal {price:.3f}]" if abs(fill_price - price) > 0.001 else ""

        # Update flip state: record this token as active, track side
        flip_state["last_side"] = side
        shares_ordered = size / fill_price
        fee_shares = entry_fee_shares(shares_ordered, fill_price, fee_rate)
        fee_usd = fee_shares * fill_price
        bankroll_now = await db.get_bankroll()
        _dist = btc_price - strike
        _why_parts = []
        # BTC position vs strike
        if side == "Up":
            _why_parts.append(f"BTC ${abs(_dist):,.0f} {'above' if _dist > 0 else 'below'} strike — {'Favors Up' if _dist > 0 else 'fighting strike'}")
        else:
            _why_parts.append(f"BTC ${abs(_dist):,.0f} {'below' if _dist < 0 else 'above'} strike — {'Favors Down' if _dist < 0 else 'fighting strike'}")
        # Order flow
        if flow_score > 0.1:
            _why_parts.append(f"Strong buy pressure in book (flow {flow_score:+.2f})")
        elif flow_score < -0.1:
            _why_parts.append(f"Strong sell pressure in book (flow {flow_score:+.2f})")
        else:
            _why_parts.append(f"neutral book flow ({flow_score:+.2f})")
        # CVD / spot flow
        if spot_flow_signal > 0.05:
            _why_parts.append(f"Buyers dominating on Binance (cvd {spot_flow_signal:+.2f})")
        elif spot_flow_signal < -0.05:
            _why_parts.append(f"Sellers dominating on Binance (cvd {spot_flow_signal:+.2f})")
        else:
            _why_parts.append(f"Neutral CVD ({spot_flow_signal:+.2f})")
        # Regime
        if regime_state and regime_state.name == "trending":
            _why_parts.append(f"Market trending {side.lower()}")
        elif regime_state and regime_state.name == "reverting":
            _why_parts.append("Market mean-reverting")
        else:
            _why_parts.append("Neutral regime")
        _why = ", ".join(_why_parts)
        logger.info(
            f"{_C.YELLOW}{'=' * 60}{_C.RESET}\n"
            f"  {_C.YELLOW}{_C.BOLD}OPEN {side}{_C.RESET}  @ {fill_price:.3f}  |  ${size:.2f}  |  fee ${fee_usd:.2f}{slip_note}  |  "
            f"{_slug_to_window(cid)}{'' if phase == 'normal' else f' [{phase}]'}\n"
            f"  {_C.DIM}Why: {_why}{_C.RESET}\n"
            f"  {_C.DIM}Bankroll ${bankroll_now:.2f}  |  {signal.reason}{_C.RESET}\n"
            f"{_C.YELLOW}{'=' * 60}{_C.RESET}")
        if _adverse_monitor:
            mkt_mid = (price_up + price_down) / 2 if price_up + price_down > 0 else fill_price
            _adverse_monitor.record_fill(side=side, fill_price=fill_price, token_id=token_id,
                                         midprice=mkt_mid, position_id=result.position_id)
        if alert_manager:
            mkt_price = price_up if side == "Up" else price_down
            await alert_manager.send_trade_opened(
                question=contract["question"], side=side, size=size,
                entry_price=fill_price, ev=signal.edge,
                model_prob=signal.prob, market_price=mkt_price,
                fee=fee_usd, flow=flow_score, bankroll=bankroll_now)
        return cid, last_eval_log_window

    return None, last_eval_log_window


def _compute_strike_and_btc(cid: str, binance_feed: Any, window_strikes: dict[int, float],
                            eval_window: int,
                            last_eval_log_window: int,
                            chainlink_feed: Any = None,
                            coinbase_feed: Any = None,
                            contract: Any = None,
                            **kwargs) -> tuple[float | None, float | None, dict[int, float], int, str]:
    """Derive strike and BTC price, preferring Chainlink (resolution source) over Binance."""
    now_ts = int(time.time())

    try:
        contract_window_ts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        contract_window_ts = int(now_ts // 300) * 300  # fallback

    # Always prefer Gamma's priceToBeat — it's the authoritative resolution value.
    # Override any cached strike if Gamma now has it (it may not be available at
    # window open but appears mid-window as Gamma catches up).
    ptb = (contract or {}).get("event_metadata") or {}
    ptb = ptb.get("price_to_beat") if isinstance(ptb, dict) else None
    if ptb and window_strikes.get(contract_window_ts) != ptb:
        if contract_window_ts in window_strikes:
            logger.info(f"STRIKE UPDATE {_slug_to_window(cid)} | ${window_strikes[contract_window_ts]:,.2f} → ${ptb:,.2f} (Polymarket)")
        else:
            logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${ptb:,.2f} (Polymarket){_C.RESET}")
        window_strikes[contract_window_ts] = ptb

    if contract_window_ts not in window_strikes:
        # Chainlink boundary capture (fallback when Gamma hasn't sent priceToBeat yet)
        if chainlink_feed:
            cl_strike = chainlink_feed.get_strike(contract_window_ts)
            if cl_strike:
                window_strikes[contract_window_ts] = cl_strike
                logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${cl_strike:,.2f} (Chainlink){_C.RESET}")

        # Fall back to Binance candle if Chainlink didn't capture it
        if contract_window_ts not in window_strikes:
            target_ms = contract_window_ts * 1000
            candles = binance_feed.buffer.get_last_n(10)
            _bn_strike = None
            for c in reversed(candles):
                if c.timestamp == target_ms:
                    _bn_strike = c.open
                    break
                elif c.timestamp < target_ms <= c.timestamp + 60_000:
                    _bn_strike = c.close
                    break
            else:
                latest = binance_feed.buffer.latest()
                if latest and now_ts - contract_window_ts < 10:
                    _bn_strike = latest.close
            if _bn_strike is not None:
                window_strikes[contract_window_ts] = _bn_strike
                logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${_bn_strike:,.2f} (Binance, fallback){_C.RESET}")

    # Clean old strikes
    window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}

    strike = window_strikes.get(contract_window_ts, 0)
    if strike <= 0:
        buf_len = len(binance_feed.buffer) if binance_feed.buffer else 0
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL {_slug_to_window(cid)}: No strike yet — buffer has {buf_len} candles")
        return None, None, window_strikes, last_eval_log_window, "none"

    # BTC price priority: Coinbase WS > Binance aggTrade > Binance 1-min candle
    trades_feed = kwargs.get("trades_feed")
    btc_price, _price_source = _fastest_btc_price(coinbase_feed, trades_feed, binance_feed)
    if btc_price <= 0:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL {_slug_to_window(cid)}: No BTC price — Binance feed not ready")
        return None, None, window_strikes, last_eval_log_window, "none"

    # Skip if candle data is stale (WebSocket may have disconnected)
    latest_candle_age = binance_feed.buffer.latest_age_s if binance_feed and binance_feed.buffer else float("inf")
    if latest_candle_age > 180:
        logger.warning(f"Stale Binance candle ({latest_candle_age:.0f}s old) — skipping entry")
        return None, None, window_strikes, last_eval_log_window, "none"

    return strike, btc_price, window_strikes, last_eval_log_window, _price_source


async def _fetch_market_prices(contract: dict[str, Any], token_up: str, token_down: str,
                               market_scanner: Any,
                               http_client: Any, clob_ws: Any, max_spread: float,
                               last_eval_log_window: int) -> tuple[dict[str, Any] | None, int]:
    """Read order books, fetch negRisk prices, apply sanity/depth/spread gates."""
    now_ts = int(time.time())

    # Read order books — WebSocket state (instant) with HTTP fallback, parallelized
    ws_book_up = clob_ws.get_book(token_up) if (clob_ws and clob_ws.connected) else None
    ws_book_down = clob_ws.get_book(token_down) if (clob_ws and clob_ws.connected) else None

    async def _get_book(ws_book: Any, token: str) -> dict:
        if ws_book and ws_book.get("asks"):
            ws_ts = float(ws_book.get("ts", 0) or 0)
            if ws_ts > 0 and (time.time() - ws_ts) <= _WS_STALE_S:
                return ws_book
        return await market_scanner.fetch_clob_book(token, http_client)

    # Fetch both books in parallel. We derive entry prices from the direct CLOB
    # best_ask (the actual price you'd PAY to buy this token via FOK), NOT the
    # /price cross-matched API which can return phantom executable prices that
    # don't reflect what live trading actually fills against.
    book_up, book_down = await asyncio.gather(
        _get_book(ws_book_up, token_up),
        _get_book(ws_book_down, token_down),
    )

    # Direct best_ask from the CLOB book — what FOK buys would actually pay.
    # Treat stale BBA entries (no recent WS update) as missing so we fall
    # through to the freshly-fetched book or Gamma fallback.
    bba_up = clob_ws.best_bid_ask.get(token_up, {}) if clob_ws else {}
    bba_down = clob_ws.best_bid_ask.get(token_down, {}) if clob_ws else {}
    def _bba_fresh(bba: dict) -> bool:
        ts = float(bba.get("ts", 0) or 0)
        return ts > 0 and (now_ts - ts) <= _WS_STALE_S
    bba_up_fresh = _bba_fresh(bba_up)
    bba_down_fresh = _bba_fresh(bba_down)
    ws_ask_up = float(bba_up.get("best_ask", 0) or 0) if bba_up_fresh else 0.0
    ws_ask_down = float(bba_down.get("best_ask", 0) or 0) if bba_down_fresh else 0.0

    # Raw book depth — computed here so we can use book best_ask as WS fallback.
    ask_up, depth_up = market_scanner.clob_best_ask(book_up)
    ask_down, depth_down = market_scanner.clob_best_ask(book_down)

    # Price source priority: WS BBO → HTTP book best_ask → Gamma (last resort).
    # HTTP book was just fetched above so it's always fresh. Gamma outcomePrices
    # are the last-trade price and can be stale — only use when we have nothing else.
    if ws_ask_up > 0 and ws_ask_down > 0:
        price_up, price_down, price_source = ws_ask_up, ws_ask_down, "clob"
    elif ask_up > 0 and ask_down > 0:
        price_up, price_down, price_source = ask_up, ask_down, "clob"
    else:
        price_up, price_down, price_source = contract["price_up"], contract["price_down"], "gamma"

    # Price sanity gate: best_ask + best_ask naturally exceeds 1.00 by the full
    # spread. ±2% accommodates normal 1-4 cent spreads; tighter thresholds reject
    # valid markets every tick.
    price_sum = price_up + price_down
    if price_source == "clob" and (price_sum < 0.98 or price_sum > 1.02):
        _record_skip("stale_prices")
        eval_window = int(now_ts // 300) * 300
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.debug(f"EVAL: stale prices | Up={price_up:.2f} + Dn={price_down:.2f} = {price_sum:.2f} — skipping")
        return None, last_eval_log_window

    eval_window = int(now_ts // 300) * 300

    # Book depth in USD
    depth_usd_up = depth_up * ask_up if ask_up > 0 else 0
    depth_usd_down = depth_down * ask_down if ask_down > 0 else 0

    # Skip if no real depth to fill against
    if price_source == "clob":
        min_depth = market_scanner.min_book_depth_usd
        if depth_usd_up < min_depth and depth_usd_down < min_depth:
            _record_skip("thin_clob_depth")
            if eval_window != last_eval_log_window:
                last_eval_log_window = eval_window
                _cid = contract.get("slug", contract.get("market_id", ""))
                logger.info(f"EVAL {_slug_to_window(_cid)}: Thin CLOB depth — Up=${depth_usd_up:.0f} Dn=${depth_usd_down:.0f}, skipping window")
            return None, last_eval_log_window

    # Skip if effective execution cost (ask-distance + taker fee) too wide on
    # either side — the side we end up trading governs the cost, and we don't
    # know it yet.
    if price_source == "clob":
        def _ws_spread(bba: dict, fresh: bool) -> float:
            if not fresh:
                return -1.0
            s = bba.get("spread")
            if s is None:
                return -1.0
            try:
                return float(s)
            except (TypeError, ValueError):
                return -1.0
        spread_up = _ws_spread(bba_up, bba_up_fresh)
        spread_down = _ws_spread(bba_down, bba_down_fresh)
        if spread_up < 0:
            spread_up = await market_scanner.get_spread(token_up, http_client)
        if spread_down < 0:
            spread_down = await market_scanner.get_spread(token_down, http_client)
        spread_val = max(spread_up, spread_down)
        if spread_val >= 0:
            # Paying the ask = roughly half-spread above mid; add the default taker
            # fee as a proxy for full execution cost. Gate is still max_spread so
            # we don't accidentally tighten into illiquid markets — we just account
            # for the fee-eaten portion of tight-spread entries.
            effective_cost = spread_val * 0.5 + DEFAULT_FEE_RATE
            if effective_cost > max_spread:
                _record_skip("spread_too_wide")
                logger.debug(
                    f"Effective exec cost {effective_cost:.3f} (spread/2={spread_val/2:.3f} + fee={DEFAULT_FEE_RATE:.3f}) "
                    f"> {max_spread:.3f} — skipping"
                )
                return None, last_eval_log_window

    return {
        "price_up": price_up, "price_down": price_down, "price_source": price_source,
        "book_up": book_up, "book_down": book_down,
        "depth_usd_up": depth_usd_up, "depth_usd_down": depth_usd_down,
        "eval_window": eval_window,
    }, last_eval_log_window


async def _discover_contract_and_subscribe(market_scanner: Any, traded_contracts: dict[str, int],
                                           ws_subscribed_tokens: list[str],
                                           clob_ws: Any,
                                           prev_contract_tokens: list[str] | None = None,
                                           db: Any = None,
                                           http_client: Any = None,
                                           ) -> tuple[dict[str, Any] | None, str | None, dict[str, int], list[str], list[str]]:
    """Find an active contract and subscribe its WebSocket tokens. Returns (contract, cid, ..., prev_tokens)."""
    if prev_contract_tokens is None:
        prev_contract_tokens = []
    contract = await market_scanner.find_active_contract(http_client=http_client)
    if not contract:
        return None, None, traded_contracts, ws_subscribed_tokens, prev_contract_tokens

    cid = contract["slug"]  # Use slug as market_id — US API needs marketSlug, not condition_id

    # Clean old entries
    now_ts = int(time.time())
    traded_contracts = {k: v for k, v in traded_contracts.items() if now_ts - v < 600}

    # On first entry into a window, defer to DB to avoid duplicate-position
    # races; on subsequent flips we know the previous position scalped clean.
    state = _window_flip_state.get(cid, {})
    flip_count = state.get("flip_count", 0)
    if flip_count == 0 and db is not None and await db.has_position_for_market(cid):
        return None, None, traded_contracts, ws_subscribed_tokens, prev_contract_tokens

    # Subscribe WebSocket to this contract's tokens (idempotent)
    token_up = contract["token_id_up"]
    token_down = contract["token_id_down"]
    current_tokens = [t for t in [token_up, token_down] if t]
    new_tokens = [t for t in current_tokens if t not in ws_subscribed_tokens]

    # Unsubscribe tokens from previous contracts that are no longer needed
    if prev_contract_tokens and clob_ws:
        stale_tokens = [t for t in prev_contract_tokens if t not in current_tokens]
        if stale_tokens:
            await clob_ws.unsubscribe(stale_tokens)
            ws_subscribed_tokens = [t for t in ws_subscribed_tokens if t not in stale_tokens]

    if new_tokens and clob_ws:
        await clob_ws.subscribe(new_tokens)
        ws_subscribed_tokens.extend(new_tokens)

    # Pre-warm tick_size cache for both tokens so the entry path hits cache
    # and avoids paying ~30-100ms of HTTP latency right before order submit.
    # 1-hour TTL — well beyond the 5-minute window lifespan.
    if http_client and market_scanner and current_tokens:
        await asyncio.gather(
            *[market_scanner.fetch_tick_size(t, http_client) for t in current_tokens],
            return_exceptions=True,
        )

    return contract, cid, traded_contracts, ws_subscribed_tokens, current_tokens


async def _check_counterfactuals(counterfactual_tracker: Any, ghost_tracker: Any,
                                 market_scanner: Any,
                                 http_client: Any, binance_feed: Any,
                                 event_metadata_cache: dict[str, Any] | None = None) -> None:
    """Pre-fetch Gamma metadata for watched scalps/ghosts and check resolutions."""
    cf_event_metadata = dict(event_metadata_cache or {})
    markets_to_fetch = [m for m in counterfactual_tracker.watched_markets if m not in cf_event_metadata]
    if markets_to_fetch:
        # Look up each watched market by its exact slug — _get_contract_prices only checks
        # the current ±1 window, so it returns None for markets from 10+ minutes ago.
        async def _fetch_by_slug(slug: str) -> dict | None:
            try:
                resp = await http_client.get(
                    f"{market_scanner.GAMMA_API}/events", params={"slug": slug})
                resp.raise_for_status()
                data = resp.json()
                if data:
                    return market_scanner.parse_contract(data[0] if isinstance(data, list) else data)
            except Exception:
                pass
            return None

        results = await asyncio.gather(
            *[_fetch_by_slug(m) for m in markets_to_fetch],
            return_exceptions=True,
        )
        for cf_mid, cf_live in zip(markets_to_fetch, results):
            if isinstance(cf_live, Exception) or not cf_live:
                continue
            if cf_live.get("event_metadata"):
                cf_event_metadata[cf_mid] = cf_live["event_metadata"]
    counterfactual_tracker.check_resolutions(event_metadata=cf_event_metadata)

    if ghost_tracker is not None:
        ghost_tracker.check_resolutions(
            event_metadata=cf_event_metadata,
            btc_at_expiry_fn=_btc_at_expiry,
            binance_feed=binance_feed,
        )


async def _evaluate_and_exit_position(
        pos: dict[str, Any], live: dict[str, Any], binance_feed: Any,
        indicator_engine: Any, signal_engine: Any, market_scanner: Any,
        http_client: Any, clob_ws: Any, trader: Any, alert_manager: Any, db: Any,
        outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        config: dict[str, Any], scheduler: Any, default_exit_threshold: float,
        day_wins: int, day_losses: int, day_fees: float,
        depth_feed: Any = None, trades_feed: Any = None,
        bybit_feed: Any = None,
        coinbase_feed: Any = None,
        chainlink_feed: Any = None) -> tuple[int, int, float, str | None]:
    """Re-evaluate an active position and exit (scalp) if holding edge is gone."""
    # NOTE: previously we short-circuited the whole function for any position in
    # _abandoned_scalp_positions, which silenced 30s hold logs AND prevented any
    # re-attempt if the price recovered above the $1 CLOB minimum. The deferral
    # now happens at the actual scalp step (just before close_trade), so we keep
    # monitoring, keep emitting heartbeats, and resume scalping on recovery.
    # BTC price priority: Coinbase WS > Binance aggTrade > Binance 1-min candle.
    # Each leg is hard-gated in _fastest_btc_price; if all three are stale
    # btc_now is 0 and we HOLD without scalping. Acting on a stale BTC
    # produced the "moved against us (2%)" pathology mid-window.
    btc_now, _btc_src = _fastest_btc_price(coinbase_feed, trades_feed, binance_feed)
    if btc_now <= 0:
        _log_hold_heartbeat_stale(pos, live, "no fresh BTC price")
        return day_wins, day_losses, day_fees, None

    # Mirror the entry-path staleness gate (main.py ~540-562). Every
    # signal-input feed must be fresh, or we defer the decision. Previously
    # the exit path checked only candle_age>180; if Coinbase / aggTrade /
    # Bybit / Chainlink went stale the bot would still scalp on degraded
    # state. CLAUDE.md spec: Coinbase >30s, Chainlink >60s,
    # aggTrade >30s (L3b CVD/taker), Bybit OI >60s (L3e liquidation).
    # kline >90s catches a frozen indicator/ATR buffer.
    _stale: list[str] = []
    if coinbase_feed and coinbase_feed.state.age_seconds > 30:
        _stale.append(f"coinbase={coinbase_feed.state.age_seconds:.0f}s")
    if chainlink_feed and chainlink_feed.age_seconds > 60:
        _stale.append(f"chainlink={chainlink_feed.age_seconds:.0f}s")
    if bybit_feed is not None and bybit_feed.state.oi_updated > 0:
        _bybit_age = time.time() - bybit_feed.state.oi_updated
        if _bybit_age > 60:
            _stale.append(f"bybit_oi={_bybit_age:.0f}s")
    if trades_feed is not None and trades_feed.accumulator is not None:
        _agg_age = trades_feed.accumulator.latest_age_s
        if _agg_age > 30:
            _stale.append(f"binance_aggtrade={_agg_age:.0f}s")
    _candle_age = binance_feed.buffer.latest_age_s if binance_feed and binance_feed.buffer else float("inf")
    if _candle_age > 45:
        _stale.append(f"binance_kline={_candle_age:.0f}s")
    # Safer staleness policy: loss-cut math (BTC vs strike + ATR) is independent
    # of the L3b/L3e/Chainlink signals. Only candle staleness corrupts ATR; the
    # other stale feeds degrade the scalp signals but can't fake a loss-cut.
    # Allow evaluate_hold to fire under non-critical staleness so loss-cut can
    # still protect the position; revert any non-loss-cut EXIT below.
    scalp_gated_by_stale = False
    if _stale:
        if any("kline" in s for s in _stale):
            _log_hold_heartbeat_stale(pos, live, "stale feeds — " + ", ".join(_stale))
            return day_wins, day_losses, day_fees, None
        scalp_gated_by_stale = True

    # Get strike from the position's stored trade_context (correct for this contract)
    pos_ctx = json.loads(pos.get("indicator_snapshot", "{}")).get("trade_context", {})
    strike_now = pos_ctx.get("strike_price", 0)
    if strike_now <= 0:
        return day_wins, day_losses, day_fees, None

    indicators = indicator_engine.compute_all(binance_feed.buffer)

    # Hold/scalp decisions use the direct best_bid from the CLOB WebSocket BBO — the
    # actual price you'd RECEIVE when selling, matching what live FOK fills against.
    # We deliberately avoid the /price cross-matched API, which can spike to phantom
    # values near expiry from stale cross-match offers that wouldn't actually fill.
    hold_token = live.get("token_id_up", "") if pos["side"] == "Up" else live.get("token_id_down", "")
    other_token = live.get("token_id_down", "") if pos["side"] == "Up" else live.get("token_id_up", "")
    bba = clob_ws.best_bid_ask.get(hold_token, {}) if clob_ws else {}
    ws_bid = float(bba.get("best_bid", 0) or 0)
    ws_ask = float(bba.get("best_ask", 0) or 0)
    market_mid = (ws_bid + ws_ask) / 2.0 if (ws_bid > 0 and ws_ask > 0) else 0.0
    bid_age = time.time() - float(bba.get("ts", 0) or 0)
    if not (ws_bid > 0 and bid_age <= 10):
        # No fresh bid — can't make exit decisions, but still emit the HOLD heartbeat
        # so the operator knows the position is being monitored.
        now_ts = time.time()
        mid = pos["market_id"]
        if now_ts - _last_hold_log.get(mid, 0) >= 30:
            _last_hold_log[mid] = now_ts
            cl_str = f"  cl ${chainlink_feed.price:,.0f}" if chainlink_feed and chainlink_feed.price > 0 else ""
            logger.info(
                f"  {_C.DIM}HOLD {pos['side']}{_C.RESET}  {_fmt_secs(live['seconds_remaining'])}  |  "
                f"BTC ${btc_now:,.0f} [{_btc_src}]{cl_str}  (no fresh bid)"
            )
        return day_wins, day_losses, day_fees, None

    market_price = ws_bid

    exit_threshold = (scheduler._exit_edge_threshold if scheduler and scheduler._exit_edge_threshold is not None
                      else default_exit_threshold)
    closes = binance_feed.buffer.get_closes()

    # Compute order flow for hold evaluation
    hold_trades_up = clob_ws.get_trade_history(live.get("token_id_up", "")) if clob_ws else []
    hold_trades_down = clob_ws.get_trade_history(live.get("token_id_down", "")) if clob_ws else []
    hold_flow = compute_flow_signal(
        clob_ws.get_book(live.get("token_id_up", "")) if clob_ws else {},
        clob_ws.get_book(live.get("token_id_down", "")) if clob_ws else {},
        hold_trades_up, hold_trades_down,
    )

    # New signals for hold evaluation. Per-layer age guards mirror the entry
    # path (main.py:587, 605, 612): contribution is 0 when the source is
    # stale, never the cached last-fresh value. The top-level gate above
    # already short-circuits stale states, but these belt-and-suspenders
    # guards keep the layers correct if that gate is ever bypassed.
    hold_spot_flow = 0.0

    if trades_feed and trades_feed.accumulator and trades_feed.accumulator.latest_age_s <= 30:
        acc = trades_feed.accumulator
        cvd = acc.get_cvd(window_s=120)
        taker = acc.get_taker_ratio(window_s=60)
        trade_count = acc.trade_count
        cvd_z = _cvd_normalizer.normalize("cvd_hold", cvd) if _cvd_normalizer else 0.0
        cvd_comp = math.tanh(cvd_z) * 0.8
        taker_comp = (taker - 0.5) * 2 * 0.2 if trade_count >= 5 else 0.0
        hold_spot_flow = max(-1.0, min(1.0, cvd_comp + taker_comp))

    # Liquidation pressure for hold evaluation — must mirror the entry-side
    # elapsed_seconds normalization, otherwise the hold path applies the new
    # per-minute formula with raw-second inputs and over-saturates at 5-10×.
    hold_liquidation = 0.0
    if (bybit_feed and bybit_feed.state.open_interest > 0
            and bybit_feed.state.open_interest_prev > 0
            and bybit_feed.state.oi_updated > 0
            and (time.time() - bybit_feed.state.oi_updated) <= 60):
        _hold_oi_elapsed = bybit_feed.state.oi_updated - bybit_feed.state.oi_updated_prev
        hold_liquidation = compute_liquidation_pressure(
            bybit_feed.state.open_interest, bybit_feed.state.open_interest_prev,
            bybit_feed.state.price_at_oi, bybit_feed.state.price_at_oi_prev,
            elapsed_seconds=_hold_oi_elapsed if _hold_oi_elapsed > 0 else 1.0)

    action, model_prob, holding_edge, reason = signal_engine.evaluate_hold(
        indicators, btc_now, strike_now, live["seconds_remaining"],
        market_price, pos["side"], exit_threshold,
        entry_price=pos["entry_price"],
        fee_rate=pos.get("fee_rate") or DEFAULT_FEE_RATE,
        closes=closes, flow_signal=hold_flow["flow_score"],
        spot_flow_signal=hold_spot_flow,
        prev_resolution_margin=_prev_resolution_margin,
        liquidation_pressure=hold_liquidation,
        market_mid_for_side=market_mid)

    # Under non-critical staleness, only loss-cut is safe — the scalp-band signals
    # were computed against degraded layers. Demote any other EXIT to HOLD so a
    # stale-driven scalp can't slip through.
    if scalp_gated_by_stale and action == "EXIT" and not reason.startswith("cutting loss"):
        _log_hold_heartbeat_stale(pos, live, "stale feeds — scalp gated, loss-cut only: " + ", ".join(_stale))
        return day_wins, day_losses, day_fees, None

    mid = pos["market_id"]

    if action == "HOLD":
        # Log hold status every 30s so the operator knows the bot is alive
        now_ts = time.time()
        if now_ts - _last_hold_log.get(mid, 0) >= 30:
            _last_hold_log[mid] = now_ts
            if abs(holding_edge) < 0.005:
                edge_color, edge_str = _C.GREEN, "0%"
            else:
                edge_color = _C.GREEN if holding_edge > 0 else _C.RED
                edge_str = f"{holding_edge:+.0%}"
            cl_str = f"  cl ${chainlink_feed.price:,.0f}" if chainlink_feed and chainlink_feed.price > 0 else ""
            logger.info(
                f"  {_C.DIM}HOLD {pos['side']}{_C.RESET}  {_fmt_secs(live['seconds_remaining'])}  |  "
                f"prob {model_prob:.0%}  {edge_color}edge {edge_str}{_C.RESET}  |  "
                f"BTC ${btc_now:,.0f} [{_btc_src}]{cl_str}  mkt {market_price:.2f}")
        if counterfactual_tracker:
            _cf_atr = indicators.get("atr", {}).get("atr", 1.0) or 1.0
            counterfactual_tracker.track_hold_moment(pos["market_id"], pos, {
                "holding_edge": holding_edge, "model_prob": model_prob,
                "market_price": market_price, "seconds_remaining": live["seconds_remaining"],
                "exit_threshold": exit_threshold, "strike_price": strike_now,
                "btc_price": btc_now,
                "flow_score": hold_flow.get("flow_score", 0.0),
                "spot_flow_signal": hold_spot_flow,
                "regime": pos_ctx.get("regime_state", "unknown"),
                "btc_distance_atr": round((btc_now - strike_now) / _cf_atr, 3),
            })

        # Pre-sign the SELL FOK in the background when scalp is imminent
        # (holding_edge approaching exit_threshold). Saves ~150ms of ECDSA
        # sign work from the hot path when PRE-SCALP eventually fires.
        # Best-effort: PaperTrader doesn't expose warm_sell_signature, so the
        # hasattr check makes this a no-op there.
        if (hasattr(trader, 'warm_sell_signature')
                and -0.05 < holding_edge < -0.005):
            _sell_token = (live.get("token_id_up", "") if pos["side"] == "Up"
                           else live.get("token_id_down", ""))
            if _sell_token:
                _shares = pos.get("shares_held") or pos["size"] / pos["entry_price"]
                # Approximate exit_fill = market_price × (1 − 8% cross floor).
                # _take_sell_warmup tolerates ±1¢ drift between this estimate
                # and the actual exit_fill computed at PRE-SCALP time.
                _warm_price = round(market_price * 0.92, 4)
                asyncio.create_task(trader.warm_sell_signature(
                    _sell_token, _shares, _warm_price,
                    fee_rate=pos.get("fee_rate") or DEFAULT_FEE_RATE,
                ))

    traded_market_id = None
    if action == "EXIT":
        sell_token = live.get("token_id_up", "") if pos["side"] == "Up" else live.get("token_id_down", "")

        # PRICE VERIFICATION: guard against the CLOB WS carrying a phantom best_bid
        # (timestamp refreshed by an unrelated price_change event, stale price value).
        # Fast-path: if the other side's bid is also fresh and both sum to ~1.0,
        # no-arb is satisfied — ws_bid must be real, skip the HTTP round-trip.
        other_bba = clob_ws.best_bid_ask.get(other_token, {}) if clob_ws else {}
        other_bid = float(other_bba.get("best_bid", 0) or 0)
        other_age = time.time() - float(other_bba.get("ts", 0) or 0)
        noarb_ok = other_bid > 0 and other_age <= 5 and 0.95 <= ws_bid + other_bid <= 1.05
        verified_price = 0.0
        if not noarb_ok and market_scanner and http_client and sell_token:
            verified_price = await market_scanner.fetch_market_price(sell_token, "SELL", http_client)
        if verified_price > 0 and verified_price < ws_bid * 0.70:
            # Dramatic mismatch — ws_bid is phantom. Re-evaluate with the real price.
            real_edge = model_prob - verified_price
            logger.info(
                f"  SCALP VERIFY {pos['side']}  {_fmt_secs(live['seconds_remaining'])}  |  "
                f"ws_bid={ws_bid:.3f} vs /price={verified_price:.3f} — using real price  "
                f"real_edge={real_edge:+.0%} thresh={exit_threshold:+.0%}"
            )
            if real_edge > exit_threshold:
                # Real market not bad enough to scalp — hold
                return day_wins, day_losses, day_fees, None
            market_price = verified_price

        # Apply slippage to sell price (worse fill for seller).
        # Prefer the WS BBO bid size over the book snapshot — the snapshot can be
        # stale (>30s) while ws_bid is required to be fresh (≤10s, checked above).
        # When both are available, take the larger (more conservative slippage).
        hold_book = clob_ws.get_book(hold_token) if clob_ws else {}
        book_bid_depth_usd = sum(
            float(b.get("size", 0)) * float(b.get("price", 0))
            for b in (hold_book or {}).get("bids", [])
        )
        bba_size = float(bba.get("size", 0) or 0) * ws_bid  # WS BBO size in USD
        bid_depth_usd = book_bid_depth_usd if book_bid_depth_usd > 0 else bba_size
        shares_held = pos.get("shares_held") or pos["size"] / pos["entry_price"]
        exit_size_usd = shares_held * market_price
        impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
        fok_floor = config.get("execution", {}).get("fok_spread_cross_floor", 0.08)
        slip = max(slippage_pct(exit_size_usd, bid_depth_usd, impact), fok_floor)
        exit_fill = round(market_price * (1 - slip), 4)

        # Polymarket rejects marketable orders below $1 notional. Pre-check here
        # so we don't spam the CLOB with guaranteed-fail attempts. Mark deferred
        # (not abandoned) so subsequent ticks keep monitoring — if the bid
        # recovers above $1 (e.g., BTC moves in our favor), we'll resume scalping.
        # Heartbeat log every 30s mirrors the normal HOLD cadence so the operator
        # always knows the position is being watched.
        if exit_size_usd < 1.0:
            now_ts = time.time()
            if pos["id"] not in _abandoned_scalp_positions:
                _abandoned_scalp_positions.add(pos["id"])
                logger.info(
                    f"  SCALP DEFERRED — position too small (${exit_size_usd:.2f} < $1.00), "
                    f"monitoring for recovery or resolution"
                )
                _last_hold_log[mid] = now_ts
            elif now_ts - _last_hold_log.get(mid, 0) >= 30:
                _last_hold_log[mid] = now_ts
                logger.info(
                    f"  {_C.DIM}HOLD (small) {pos['side']}{_C.RESET}  "
                    f"{_fmt_secs(live['seconds_remaining'])}  |  size ${exit_size_usd:.2f}  "
                    f"prob {model_prob:.0%}  edge {holding_edge:+.0%}  |  mkt {market_price:.2f}"
                )
            return day_wins, day_losses, day_fees, traded_market_id

        # Size recovered above the $1 floor — clear the deferred flag and proceed
        # with the scalp. Surface the transition so the operator sees the resume.
        if pos["id"] in _abandoned_scalp_positions:
            _abandoned_scalp_positions.discard(pos["id"])
            logger.info(
                f"  SCALP RESUMED — position recovered to ${exit_size_usd:.2f}, "
                f"attempting exit"
            )

        # Emit the pre-scalp snapshot here (after size guard) so the price the
        # scalp triggers on is always visible, without spamming on deferred ticks.
        logger.info(
            f"  {_C.DIM}PRE-SCALP {pos['side']}{_C.RESET}  {_fmt_secs(live['seconds_remaining'])}  |  "
            f"prob {model_prob:.0%}  edge {holding_edge:+.0%}  |  "
            f"BTC ${btc_now:,.0f} [{_btc_src}]  mkt {market_price:.2f}"
        )

        result = await trader.close_trade(pos["id"], exit_fill, token_id=sell_token, position=pos)
        if not result.success:
            if "CLOB minimum" in (result.reason or ""):
                # Race: size was >= $1 when we checked but the price dropped
                # by the time the order hit. Treat the same as the pre-check
                # path — defer, monitor, retry next tick.
                _abandoned_scalp_positions.add(pos["id"])
                logger.info(
                    f"  SCALP DEFERRED — order rejected by CLOB minimum (${exit_size_usd:.2f}), "
                    f"monitoring for recovery"
                )
                return day_wins, day_losses, day_fees, traded_market_id
            logger.warning(f"  SCALP FAILED — Retrying next tick: {result.reason}")
        elif result.success:
            _invalidate_open_positions_cache()
            pnl = result.pnl
            gain_pct = result.gain_pct
            total_fees = result.entry_fee_usd + result.exit_fee_usd
            exit_fill = result.fill_price  # use actual fill from book walk, not requested price
            won = "WIN" if pnl > 0 else "LOSS"
            # Pull authoritative day stats from DB rather than in-memory counters so
            # any quarantined/corrected trade_history rows are reflected immediately.
            today_str = datetime.now(ET).strftime("%Y-%m-%d")
            day_wins, day_losses, day_fees, _ = await db.get_day_stats(today_str)
            color = _C.GREEN if pnl >= 0 else _C.RED
            bankroll_after = await db.get_bankroll()
            logger.info(
                f"{color}{'=' * 60}{_C.RESET}\n"
                f"  {color}{_C.BOLD}SCALP {won} {pos['side']}{_C.RESET}  |  {pos['entry_price']:.3f} -> {exit_fill:.3f}  |  "
                f"{gain_pct:+.1%}  |  {color}${pnl:+.2f}{_C.RESET}  |  {_slug_to_window(pos['market_id'])}\n"
                f"  {_C.DIM}Why: {reason}{_C.RESET}\n"
                f"  {_C.DIM}Day: {day_wins}W/{day_losses}L  |  Bankroll ${bankroll_after:.2f}  |  fees ${total_fees:.2f}{_C.RESET}\n"
                f"{color}{'=' * 60}{_C.RESET}")
            if alert_manager:
                await alert_manager.send_trade_closed(
                    question=pos.get("question", ""), exit_price=exit_fill, log_return=0, hold_hours=0,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                    gain_pct=gain_pct, reason=f"scalp {won.lower()}", fees=total_fees,
                    bankroll=bankroll_after, day_wins=day_wins, day_losses=day_losses)
            if breaker:
                breaker.update_bankroll(bankroll_after)
                await db.set_peak_bankroll(breaker.peak_bankroll)
                cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
                if cb_event and alert_manager:
                    await alert_manager.send_circuit_breaker(cb_event, breaker)
            await _record_outcome(outcome_reviewer, pos, exit_fill, result.log_return or 0, gain_pct,
                                  exit_reason="scalp", pnl=pnl, fees=total_fees,
                                  seconds_remaining_at_exit=float(live.get("seconds_remaining", 0)))
            # Update flip state: increment flip count
            traded_market_id = pos["market_id"]
            fs = _window_flip_state.setdefault(traded_market_id, {
                "flip_count": 0, "last_side": None,
            })
            fs["flip_count"] += 1

            if counterfactual_tracker:
                _cf_atr2 = indicators.get("atr", {}).get("atr", 1.0) or 1.0
                counterfactual_tracker.watch(pos, {
                    "exit_fill": exit_fill, "pnl": pnl, "gain_pct": gain_pct,
                    "holding_edge": holding_edge, "model_prob": model_prob,
                    "market_price": market_price, "seconds_remaining": live["seconds_remaining"],
                    "exit_threshold": exit_threshold, "strike_price": strike_now,
                    "btc_price": btc_now,
                    "flow_score": hold_flow.get("flow_score", 0.0),
                    "spot_flow_signal": hold_spot_flow,
                    "regime": pos_ctx.get("regime_state", "unknown"),
                    "btc_distance_atr": round((btc_now - strike_now) / _cf_atr2, 3),
                })

    return day_wins, day_losses, day_fees, traded_market_id


async def _resolve_expired_position(
        pos: dict[str, Any], live: dict[str, Any], trader: Any, alert_manager: Any,
        db: Any, outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        day_wins: int, day_losses: int, day_fees: float,
        signal_engine: Any = None) -> tuple[bool, int, int, float, str | None]:
    """Resolve a position whose contract has expired (seconds_remaining <= 0)."""
    global _prev_resolution_margin
    if live.get("closed") and (live["price_up"] >= 0.99 or live["price_up"] <= 0.01):
        # Polymarket has resolved: use the actual outcome prices
        exit_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]
    elif live.get("event_metadata") and live["event_metadata"].get("final_price") is not None:
        # Gamma has Chainlink oracle prices but outcome prices not yet clear
        meta = live["event_metadata"]
        up_won = meta["final_price"] >= meta["price_to_beat"]
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        logger.info(f"RESOLVE {_slug_to_window(pos['market_id'])} | Strike {meta['price_to_beat']:,.2f} → Final {meta['final_price']:,.2f} — {'Up' if up_won else 'Down'} wins")
    else:
        # Gamma hasn't resolved yet — wait for next tick (polls every 2s)
        now_ts = time.time()
        mid = pos["market_id"]
        if mid not in _last_resolve_wait_log:
            _last_resolve_wait_log[mid] = now_ts
            logger.info(f"Waiting for resolution — {_slug_to_window(mid)}")
        return False, day_wins, day_losses, day_fees, None

    result = await trader.resolve_position(pos["id"], exit_price)
    traded_market_id = None
    if result.success:
        _invalidate_open_positions_cache()
        pnl = result.pnl
        gain_pct = result.gain_pct
        total_fees = result.entry_fee_usd + result.exit_fee_usd
        won = "WIN" if pnl > 0 else "LOSS"
        # Pull authoritative day stats from DB rather than in-memory counters.
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        day_wins, day_losses, day_fees, _ = await db.get_day_stats(today_str)
        color = _C.GREEN if pnl >= 0 else _C.RED
        bankroll_after = await db.get_bankroll()
        logger.info(
            f"{color}{'=' * 60}{_C.RESET}\n"
            f"  {color}{_C.BOLD}RESOLVED {won} {pos['side']}{_C.RESET}  |  {pos['entry_price']:.3f} -> {exit_price:.3f}  |  "
            f"{gain_pct:+.1%}  |  {color}${pnl:+.2f}{_C.RESET}  |  {_slug_to_window(pos['market_id'])}\n"
            f"  {_C.DIM}Day: {day_wins}W/{day_losses}L  |  Bankroll ${bankroll_after:.2f}  |  fees ${total_fees:.2f}{_C.RESET}\n"
            f"{color}{'=' * 60}{_C.RESET}")
        if alert_manager:
            await alert_manager.send_trade_closed(
                question=pos.get("question", ""), exit_price=exit_price, log_return=0, hold_hours=0,
                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                gain_pct=gain_pct, reason=won.lower(), fees=total_fees,
                bankroll=bankroll_after, day_wins=day_wins, day_losses=day_losses)
        if breaker:
            breaker.update_bankroll(bankroll_after)
            await db.set_peak_bankroll(breaker.peak_bankroll)
            cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
            if cb_event and alert_manager:
                await alert_manager.send_circuit_breaker(cb_event, breaker)
        _abandoned_scalp_positions.discard(pos["id"])
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=total_fees)
        if counterfactual_tracker:
            counterfactual_tracker.record_hold_resolution(
                pos["market_id"], exit_price, pnl, gain_pct)
        traded_market_id = pos["market_id"]
        # Track resolution margin (final - strike) for next window's L5 carry.
        # Pulled from event_metadata regardless of which branch above set
        # exit_price — the previous `elif`-only placement silently zeroed L5
        # for the closed+binary path even when meta carried the Chainlink
        # final price.
        meta = live.get("event_metadata")
        if meta and meta.get("final_price") is not None and meta.get("price_to_beat") is not None:
            _prev_resolution_margin = meta["final_price"] - meta["price_to_beat"]
            # Defer disk writes off the resolution path — pipeline reads happen
            # at ≥ 5-minute granularity, well beyond any background-task delay.
            asyncio.create_task(asyncio.to_thread(_save_prev_resolution_margin, _prev_resolution_margin))
            asyncio.create_task(asyncio.to_thread(flush_gate_stats))
    return True, day_wins, day_losses, day_fees, traded_market_id


async def _manage_orphaned_position(
        pos: dict[str, Any], market_scanner: Any, http_client: Any, trader: Any,
        alert_manager: Any, db: Any, outcome_reviewer: Any, breaker: Any,
        day_wins: int, day_losses: int, day_fees: float,
        signal_engine: Any = None,
        chainlink_feed: Any = None) -> tuple[bool, int, int, float, str | None]:
    """Resolve positions where the contract can no longer be found via Gamma API."""
    from datetime import datetime, timezone
    global _prev_resolution_margin

    try:
        entry_dt = datetime.fromisoformat(pos.get("entry_timestamp", ""))
        age = (datetime.now(timezone.utc) - entry_dt).total_seconds()
    except (ValueError, TypeError):
        age = 0
    if age < 600:
        return True, day_wins, day_losses, day_fees, None  # too young, skip
    # Track (final_price, strike) for L5 carry — populated by whichever branch
    # below has the data. Saved at the end alongside resolve_position.
    resolved_final: float | None = None
    resolved_strike: float | None = None
    # Try direct Gamma fetch for eventMetadata (Chainlink oracle)
    direct = await _get_contract_prices(market_scanner, pos["market_id"], http_client)
    if direct and direct.get("event_metadata") and direct["event_metadata"].get("final_price") is not None:
        meta = direct["event_metadata"]
        up_won = meta["final_price"] >= meta["price_to_beat"]
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        resolved_final = meta.get("final_price")
        resolved_strike = meta.get("price_to_beat")
        logger.info(f"RESOLVE orphan {_slug_to_window(pos['market_id'])} | Strike {meta['price_to_beat']:,.2f} → Final {meta['final_price']:,.2f} — {'Up' if up_won else 'Down'} wins")
    elif direct and direct.get("closed") and (direct["price_up"] >= 0.99 or direct["price_up"] <= 0.01):
        exit_price = direct["price_up"] if pos["side"] == "Up" else direct["price_down"]
    elif age > 1800 and chainlink_feed and chainlink_feed.price > 0:
        # Gamma silent for 30+ min — Polymarket has already auto-credited the Safe
        # via on-chain settlement, so the bankroll is correct. Use the Chainlink
        # oracle directly to mark the DB record so the position stops blocking.
        try:
            window_ts = int(pos["market_id"].rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            window_ts = 0
        strike_at_boundary = chainlink_feed.get_strike(window_ts) if window_ts else None
        if strike_at_boundary is None or strike_at_boundary <= 0:
            # No captured strike (feed wasn't running at boundary) — keep waiting
            logger.info(f"Orphan {_slug_to_window(pos['market_id'])} (age {age:.0f}s) — Chainlink strike not captured, still waiting")
            return True, day_wins, day_losses, day_fees, None
        up_won = chainlink_feed.price >= strike_at_boundary
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        resolved_final = chainlink_feed.price
        resolved_strike = strike_at_boundary
        logger.warning(
            f"RESOLVE orphan {_slug_to_window(pos['market_id'])} via Chainlink fallback "
            f"(Gamma silent {age:.0f}s) | Strike ${strike_at_boundary:,.2f} → ${chainlink_feed.price:,.2f} — "
            f"{'Up' if up_won else 'Down'} wins (exit={exit_price})"
        )
        if alert_manager:
            try:
                await alert_manager.send_error(
                    f"Resolved orphaned {pos['market_id']} via Chainlink fallback "
                    f"(Gamma silent for {age:.0f}s). exit_price={exit_price}"
                )
            except Exception:
                pass
    else:
        # No official resolution data yet — keep waiting (Polymarket auto-credits
        # the Safe regardless, so bankroll is correct on next sync).
        if age > 3600:
            logger.error(f"ORPHANED >1hr: {_slug_to_window(pos['market_id'])} — No Gamma resolution data, waiting for Chainlink oracle")
            if alert_manager:
                await alert_manager.send_trade_closed(
                    question=pos.get("question", ""), exit_price=0, log_return=0, hold_hours=age / 3600,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=0,
                    gain_pct=0, reason="orphaned — awaiting resolution", fees=0)
        else:
            logger.info(f"Orphan {_slug_to_window(pos['market_id'])} (age {age:.0f}s) — Waiting for Gamma resolution")
        return True, day_wins, day_losses, day_fees, None  # still waiting
    result = await trader.resolve_position(pos["id"], exit_price)
    traded_market_id = None
    if result.success:
        _invalidate_open_positions_cache()
        pnl = result.pnl
        gain_pct = result.gain_pct
        total_fees = result.entry_fee_usd + result.exit_fee_usd
        won = "WIN" if pnl > 0 else "LOSS"
        # Pull authoritative day stats from DB rather than in-memory counters.
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        day_wins, day_losses, day_fees, _ = await db.get_day_stats(today_str)
        color = _C.GREEN if pnl >= 0 else _C.RED
        bankroll_after = await db.get_bankroll()
        logger.info(
            f"{color}{'=' * 60}{_C.RESET}\n"
            f"  {color}{_C.BOLD}RESOLVED {won} {pos['side']} (orphan){_C.RESET}  |  {pos['entry_price']:.3f} -> {exit_price:.3f}  |  "
            f"{gain_pct:+.1%}  |  {color}${pnl:+.2f}{_C.RESET}  |  {_slug_to_window(pos['market_id'])}\n"
            f"  {_C.DIM}Day: {day_wins}W/{day_losses}L  |  Bankroll ${bankroll_after:.2f}  |  fees ${total_fees:.2f}{_C.RESET}\n"
            f"{color}{'=' * 60}{_C.RESET}")
        if alert_manager:
            await alert_manager.send_trade_closed(
                question=pos.get("question", ""), exit_price=exit_price, log_return=0, hold_hours=0,
                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                gain_pct=gain_pct, reason=won.lower(), fees=total_fees,
                bankroll=bankroll_after, day_wins=day_wins, day_losses=day_losses)
        if breaker:
            breaker.update_bankroll(bankroll_after)
            await db.set_peak_bankroll(breaker.peak_bankroll)
            cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
            if cb_event and alert_manager:
                await alert_manager.send_circuit_breaker(cb_event, breaker)
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=total_fees)
        traded_market_id = pos["market_id"]
        # L5 carry — orphan path previously dropped this even when both
        # final_price and strike were known (eventMetadata or Chainlink
        # fallback branches). Persist whichever branch captured them.
        if resolved_final is not None and resolved_strike is not None:
            _prev_resolution_margin = resolved_final - resolved_strike
            asyncio.create_task(asyncio.to_thread(_save_prev_resolution_margin, _prev_resolution_margin))
            asyncio.create_task(asyncio.to_thread(flush_gate_stats))
    return True, day_wins, day_losses, day_fees, traded_market_id


async def _check_trading_schedule(
        now_et: Any, scheduler: Any, sched_start_et: tuple[int, int],
        sched_end_et: tuple[int, int],
        current_trading_day: str | None, day_open_bankroll: float, day_wins: int,
        day_losses: int, day_fees: float, alert_manager: Any, db: Any,
        config: dict[str, Any], breaker: Any) -> tuple[bool, str | None, float, int, int, float]:
    """Check trading hours and emit day open/close banners."""
    now_time_et = (now_et.hour, now_et.minute)
    active_start = scheduler._trading_start if scheduler and scheduler._trading_start else sched_start_et
    active_end = scheduler._trading_end if scheduler and scheduler._trading_end else sched_end_et
    today_str = now_et.strftime("%Y-%m-%d")
    in_trading_hours = now_time_et >= active_start and now_time_et < active_end

    if in_trading_hours and current_trading_day != today_str:
        # New trading day — send day open banner
        if current_trading_day is not None and alert_manager:
            # Close previous day first (if bot ran overnight)
            bankroll = await db.get_bankroll()
            day_pnl = bankroll - day_open_bankroll
            await alert_manager.send_day_close(bankroll, day_pnl, day_wins, day_losses, day_fees)
        current_trading_day = today_str
        day_open_bankroll = await db.get_bankroll()
        # Restore from DB in case of mid-day restart (4-tuple: wins, losses, fees, pnl_sum)
        day_wins, day_losses, day_fees, _ = await db.get_day_stats(today_str)
        if breaker:
            breaker.reset()
        if alert_manager:
            await alert_manager.send_day_open(config.get("mode", "paper"), day_open_bankroll)

    if not in_trading_hours and current_trading_day is not None:
        # Wait for all pending_resolution positions to resolve before closing the day
        open_positions = await db.get_open_positions()
        pending = [p for p in open_positions if p["status"] == "pending_resolution"]
        if not pending:
            if alert_manager:
                bankroll = await db.get_bankroll()
                day_pnl = bankroll - day_open_bankroll
                await alert_manager.send_day_close(bankroll, day_pnl, day_wins, day_losses, day_fees)
            current_trading_day = None

    return in_trading_hours, current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees


async def trading_loop(binance_feed: BinanceFeed, market_scanner: BTCMarketScanner,
                       indicator_engine: IndicatorEngine, signal_engine: SignalEngine,
                       trader: Any, alert_manager: AlertManager | None, db: Any,
                       config: dict[str, Any], outcome_reviewer: Any,
                       is_paused_fn: Any,
                       scheduler: Any = None, clob_ws: ClobWebSocket | None = None,
                       breaker: CircuitBreaker | None = None,
                       counterfactual_tracker: Any = None,
                       ghost_tracker: Any = None,
                       http_client: Any = None,
                       depth_feed: Any = None,
                       trades_feed: Any = None,
                       bybit_feed: Any = None,
                       chainlink_feed: Any = None,
                       coinbase_feed: Any = None) -> None:
    import httpx
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    signal_config = config["signal"]
    max_bankroll_pct = config["execution"]["max_bankroll_deployed"]
    default_exit_threshold = signal_config.get("exit_edge_threshold", -0.10)
    max_spread = config.get("market", {}).get("max_spread", 0.10)

    # Trading schedule in ET (handles EST/EDT automatically)
    sched = config.get("schedule", {})
    sched_start_et = (sched.get("trading_start_hour_et", 0), sched.get("trading_start_minute", 15))
    sched_end_et = (sched.get("trading_end_hour_et", 23), sched.get("trading_end_minute", 59))

    traded_contracts: dict[str, int] = {}      # condition_id -> timestamp (one trade per contract)
    window_strikes: dict[int, float] = {}      # window_ts -> BTC price at window open
    ws_subscribed_tokens: list[str] = []       # currently subscribed token_ids
    last_eval_log_window: int = 0              # track which window we last logged eval for
    prev_contract_tokens: list[str] = []       # tokens from previous contract (for unsubscribe)

    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=5,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60),
        )

    # Day tracking for open/close banners
    # At scheduled restart (12:15 AM ET), start fresh at 0W/0L.
    # Only restore from DB if it's a mid-day restart (trading already happened today).
    from zoneinfo import ZoneInfo
    from datetime import datetime
    ET_tz = ZoneInfo("America/New_York")
    _now_et = datetime.now(ET_tz)
    _today_et = _now_et.strftime("%Y-%m-%d")
    _is_scheduled_restart = _now_et.hour == 0 and _now_et.minute < 30  # 12:00-12:30 AM = fresh start
    if _is_scheduled_restart:
        current_trading_day: str | None = None
        day_open_bankroll: float = await db.get_bankroll()
        day_wins: int = 0
        day_losses: int = 0
        day_fees: float = 0.0
        logger.debug("Fresh day start (scheduled restart)")
    else:
        _db_wins, _db_losses, _db_fees, _db_pnl_sum = await db.get_day_stats(_today_et)
        current_trading_day = _today_et if (_db_wins + _db_losses) > 0 else None
        _current_bankroll = await db.get_bankroll()
        day_open_bankroll = _current_bankroll - _db_pnl_sum  # Reconstruct opening bankroll from today's net PnL
        day_wins = _db_wins
        day_losses = _db_losses
        day_fees = _db_fees
        if _db_wins + _db_losses > 0:
            logger.debug(f"Mid-day restart: restored {_db_wins}W/{_db_losses}L from DB")

    # --- Startup banner ---
    _mode_label = "LIVE" if not isinstance(trader, PaperTrader) else "PAPER"
    _bankroll = await db.get_bankroll()
    _cal = signal_engine.calibrator
    if _cal is None:
        _cal_str = "uncalibrated"
    elif getattr(_cal, "is_identity", True):
        _cal_str = "identity"
    else:
        _cal_str = f"isotonic knots={_cal.n_knots} Δll={_cal.log_loss_improvement:+.4f}"
    def _f(feed: Any) -> str: return "OK" if feed is not None else "--"
    logger.info(
        f"PolyBot [{_mode_label}] ready  |  Bankroll ${_bankroll:,.2f}  |  "
        f"Today: {day_wins}W/{day_losses}L  |  Calibration: {_cal_str}"
    )
    logger.info(
        f"Feeds: Coinbase {_f(coinbase_feed)} · Binance {_f(binance_feed)} · "
        f"Chainlink {_f(chainlink_feed)} · Bybit {_f(bybit_feed)} · "
        f"CLOB WS {'Ready' if clob_ws is not None else 'Disconnected'} · "
        f"Discord {'Connected' if alert_manager is not None else 'Unavailable'}"
    )

    # Closure captures clob_ws once — reused across all book-update ticks instead
    # of recreating the function each iteration.
    _midprice_fn = _get_token_midprice(clob_ws) if clob_ws else None

    while True:
        # Check if scheduler requested shutdown (auto-restart cycle after pipeline)
        if scheduler and getattr(scheduler, '_shutdown_requested', False):
            break

        # Event-driven: react instantly to WebSocket book/resolution updates, timeout 1s for housekeeping
        if clob_ws:
            try:
                # Wake on book update OR market resolution — whichever comes first
                book_task = asyncio.create_task(clob_ws.book_updated.wait())
                resolve_task = asyncio.create_task(clob_ws.market_resolved.wait())
                done, pending = await asyncio.wait(
                    {book_task, resolve_task}, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if clob_ws.book_updated.is_set():
                    clob_ws.book_updated.clear()
                # Resolve pending adverse-selection checkpoints every loop tick,
                # not only on book_updated — otherwise a WS-quiet token leaves
                # multiple post-fill checkpoints collapsing onto the next event.
                # _get_token_midprice already returns 0 for stale BBAs, so this
                # never records a stale mid as a fresh checkpoint.
                if _adverse_monitor is not None and _midprice_fn is not None:
                    _adverse_monitor.update_prices(_midprice_fn)
                if clob_ws.market_resolved.is_set():
                    clob_ws.market_resolved.clear()
                    # Invalidate price cache — Gamma should have resolution data now
                    _contract_price_cache.clear()
                    logger.info("Market resolved via WS — cache cleared, checking resolution")
            except asyncio.TimeoutError:
                pass  # housekeeping tick — contract discovery, day banners
        else:
            await asyncio.sleep(0.1)  # fallback polling if no WebSocket
        try:
            # --- DAY OPEN / CLOSE ---
            now_et = datetime.now(ET)
            in_trading_hours, current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees = \
                await _check_trading_schedule(
                    now_et, scheduler, sched_start_et, sched_end_et,
                    current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees,
                    alert_manager, db, config, breaker)

            # --- POSITION MANAGEMENT: resolution check + active re-evaluation ---
            positions = await db.get_open_positions()
            has_active_position = False  # Track if any position has a live (non-expired) contract
            live_results = await asyncio.gather(
                *[_get_contract_prices(market_scanner, pos["market_id"], http_client) for pos in positions],
                return_exceptions=True,
            )
            for pos, live in zip(positions, live_results):
                if isinstance(live, Exception):
                    live = None

                if not live:
                    handled, day_wins, day_losses, day_fees, traded_mid = \
                        await _manage_orphaned_position(
                            pos, market_scanner, http_client, trader,
                            alert_manager, db, outcome_reviewer, breaker,
                            day_wins, day_losses, day_fees,
                            signal_engine=signal_engine,
                            chainlink_feed=chainlink_feed)
                    if traded_mid:
                        traded_contracts[traded_mid] = int(time.time())
                    continue

                if live["seconds_remaining"] <= 0:
                    # Contract expired — check if Polymarket has resolved it.
                    # Mark as pending so it doesn't block new entries
                    if pos["status"] == "open":
                        await db.mark_pending_resolution(pos["id"])
                    resolved, day_wins, day_losses, day_fees, traded_mid = \
                        await _resolve_expired_position(
                            pos, live, trader, alert_manager, db,
                            outcome_reviewer, breaker, counterfactual_tracker,
                            day_wins, day_losses, day_fees,
                            signal_engine=signal_engine)
                    if not resolved:
                        continue  # Gamma hasn't resolved yet — wait for next tick
                    if traded_mid:
                        traded_contracts[traded_mid] = int(time.time())
                else:
                    # Active position — re-evaluate using probability model
                    has_active_position = True
                    day_wins, day_losses, day_fees, traded_mid = \
                        await _evaluate_and_exit_position(
                            pos, live, binance_feed, indicator_engine,
                            signal_engine, market_scanner, http_client,
                            clob_ws, trader, alert_manager, db,
                            outcome_reviewer, breaker, counterfactual_tracker,
                            config, scheduler, default_exit_threshold,
                            day_wins, day_losses, day_fees,
                            depth_feed=depth_feed, trades_feed=trades_feed,
                            bybit_feed=bybit_feed,
                            coinbase_feed=coinbase_feed,
                            chainlink_feed=chainlink_feed)
                    if traded_mid:
                        traded_contracts[traded_mid] = int(time.time())

            # --- COUNTERFACTUAL: check watched scalps for resolution (every 30s) ---
            if counterfactual_tracker:
                global _last_cf_check_ts
                _now_cf = time.time()
                if _now_cf - _last_cf_check_ts >= _CF_CHECK_INTERVAL:
                    _last_cf_check_ts = _now_cf
                    await _check_counterfactuals(counterfactual_tracker, ghost_tracker,
                                                 market_scanner, http_client, binance_feed)

            # --- ENTRY: find contract and evaluate for edge ---
            # Skip new entries when paused (positions still managed above)
            if is_paused_fn():
                continue

            # Skip new entries outside trading hours (positions still managed above)
            if not in_trading_hours:
                continue

            # Concurrent windows: allow up to max_concurrent_positions from DIFFERENT markets.
            # Expired positions waiting for Gamma resolution don't block new entries.
            max_concurrent = config.get("execution", {}).get("max_concurrent_positions", 1)
            active_count = sum(1 for p in positions if p["status"] == "open")
            if active_count >= max_concurrent:
                continue

            contract, cid, traded_contracts, ws_subscribed_tokens, prev_contract_tokens = \
                await _discover_contract_and_subscribe(
                    market_scanner, traded_contracts, ws_subscribed_tokens, clob_ws,
                    prev_contract_tokens, db=db, http_client=http_client)
            if not contract:
                continue

            # Never attempt entry when already holding a position in this window.
            if any(p["market_id"] == cid and p["status"] == "open" for p in positions):
                continue

            now_ts = int(time.time())
            token_up = contract["token_id_up"]
            token_down = contract["token_id_down"]

            prices, last_eval_log_window = await _fetch_market_prices(
                contract, token_up, token_down, market_scanner,
                http_client, clob_ws, max_spread, last_eval_log_window)
            if not prices:
                continue

            price_up = prices["price_up"]
            price_down = prices["price_down"]
            price_source = prices["price_source"]
            book_up = prices["book_up"]
            book_down = prices["book_down"]
            depth_usd_up = prices["depth_usd_up"]
            depth_usd_down = prices["depth_usd_down"]
            eval_window = prices["eval_window"]

            strike, btc_price, window_strikes, last_eval_log_window, btc_price_source = \
                _compute_strike_and_btc(cid, binance_feed, window_strikes,
                                        eval_window, last_eval_log_window,
                                        chainlink_feed=chainlink_feed,
                                        coinbase_feed=coinbase_feed,
                                        trades_feed=trades_feed,
                                        contract=contract)
            if strike is None:
                continue

            current_bankroll = await db.get_bankroll()
            traded_cid, last_eval_log_window = await _evaluate_signal_and_enter(
                contract, cid, binance_feed, indicator_engine,
                signal_engine, market_scanner, http_client, clob_ws,
                trader, alert_manager, db, config, breaker,
                price_up, price_down, price_source,
                book_up, book_down, depth_usd_up, depth_usd_down,
                btc_price, strike, eval_window, last_eval_log_window,
                token_up, token_down, signal_config, max_bankroll_pct,
                now_ts, bankroll=current_bankroll,
                depth_feed=depth_feed, trades_feed=trades_feed,
                bybit_feed=bybit_feed,
                coinbase_feed=coinbase_feed,
                chainlink_feed=chainlink_feed,
                ghost_tracker=ghost_tracker)
            if traded_cid:
                traded_contracts[traded_cid] = now_ts

        except AuthError as e:
            # Auth/signing failure — every subsequent order will fail the same way.
            # Bail loudly so the operator notices instead of letting entries silently
            # skip for hours. run_polybot.ps1 won't auto-restart on hard exit.
            logger.error("AUTH FAILURE — stopping trading loop: %s", e)
            if alert_manager:
                try:
                    await alert_manager.send_error(
                        f"AUTH BROKEN — bot stopped. Re-approve USDC to CTF Exchange "
                        f"or check POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER. ({e})"
                    )
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            if alert_manager:
                await alert_manager.send_error(str(e))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PolyBot — 5-min BTC Up/Down trader")
    parser.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Trading mode (overrides settings.yaml)")
    parser.add_argument("--auto-restart", action="store_true",
                        help="Exit after daily pipeline for wrapper script to git commit/push and restart")
    parser.add_argument("--run-pipeline", action="store_true",
                        help="Run the daily learning pipeline once and exit (no trading)")
    parser.add_argument("--allow-orphans", action="store_true",
                        help="LIVE ONLY: proceed even if on-chain positions exist that the DB doesn't know about. "
                             "Use only after manual review of memory/orphan_positions.json — these shares will not be managed.")
    return parser.parse_args()


async def run_pipeline() -> None:
    """Run the daily learning pipeline once and exit. No trading, no WebSockets."""
    config = load_config()
    base_dir = Path(__file__).parent

    # Logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    signal_cfg = config.get("signal", {})
    market_cfg = config.get("market", {})
    sched_cfg = config.get("schedule", {})
    ind_cfg = config.get("indicators", {})

    indicator_params = {
        "rsi": {"period": ind_cfg.get("rsi", {}).get("period", 14),
                "overbought": ind_cfg.get("rsi", {}).get("overbought", 70),
                "oversold": ind_cfg.get("rsi", {}).get("oversold", 30)},
        "macd": {"fast": ind_cfg.get("macd", {}).get("fast_period", 12),
                 "slow": ind_cfg.get("macd", {}).get("slow_period", 26),
                 "signal_period": ind_cfg.get("macd", {}).get("signal_period", 9)},
        "stochastic": {"k_period": ind_cfg.get("stochastic", {}).get("k_period", 14),
                       "d_smoothing": ind_cfg.get("stochastic", {}).get("d_smoothing", 3),
                       "overbought": ind_cfg.get("stochastic", {}).get("overbought", 80),
                       "oversold": ind_cfg.get("stochastic", {}).get("oversold", 20)},
        "ema": {"fast_period": ind_cfg.get("ema", {}).get("fast_period", 9),
                "slow_period": ind_cfg.get("ema", {}).get("slow_period", 21),
                "chop_threshold": ind_cfg.get("ema", {}).get("chop_threshold", 0.0001)},
        "obv": {"slope_period": ind_cfg.get("obv", {}).get("slope_period", 5)},
        "atr": {"period": ind_cfg.get("atr", {}).get("period", 14),
                "low_pct": ind_cfg.get("atr", {}).get("low_percentile", 5),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(weights=signal_cfg.get("weights"),
                                       params=indicator_params)

    signal_engine = _build_signal_engine(signal_cfg, config)

    from polybot.core.calibrator import IsotonicCalibrator
    calibrator = IsotonicCalibrator()
    _cal_path = Path(base_dir) / "memory" / "calibration" / "isotonic_params.json"
    calibrator.load(_cal_path)
    signal_engine.calibrator = calibrator

    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model="claude-sonnet-4-6")

    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    ghost_tracker = GhostTracker(memory_dir=str(base_dir / "memory"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    ta_evolver = TAEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"),
                          claude_client=claude)
    weight_optimizer = WeightOptimizer()
    from polybot.agents.pipeline_tracker import PipelineTracker
    pipeline_tracker = PipelineTracker(path=base_dir / "memory" / "pipeline_history.json")

    # Discord — connect briefly to send pipeline report
    alert_manager = None
    discord_bot = None
    discord_token = None
    try:
        discord_token = get_secret("DISCORD_BOT_TOKEN")
    except Exception:
        logger.info("No DISCORD_BOT_TOKEN — pipeline report will be logged only")
    if discord_token:
        discord_bot = create_bot(db=None, trader=None, scanner=None, scheduler=None, config=config)
        alert_manager = AlertManager(bot=discord_bot,
            trade_channel_name=config["discord"]["trade_channel_name"],
            control_channel_name=config["discord"]["control_channel_name"],
            daily_channel_name=config["discord"].get("daily_channel_name", "polybot-daily"))

    agents_cfg = config["agents"]
    scheduler = AgentScheduler(
        outcome_reviewer=outcome_reviewer,
        bias_detector=bias_detector,
        ta_evolver=ta_evolver,
        weight_optimizer=weight_optimizer,
        indicator_engine=indicator_engine,
        signal_engine=signal_engine,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
        config=config,
        counterfactual_tracker=counterfactual_tracker,
        pipeline_tracker=pipeline_tracker,
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", _d("exit_edge_threshold"))
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    scheduler._trading_start = (sched_cfg.get("trading_start_hour_et", 0), sched_cfg.get("trading_start_minute", 15))
    scheduler._trading_end = (sched_cfg.get("trading_end_hour_et", 23), sched_cfg.get("trading_end_minute", 59))
    scheduler.ghost_tracker = ghost_tracker

    async def _run_with_discord():
        if discord_bot and discord_token:
            @discord_bot.event
            async def on_ready():
                logger.info(f"Discord connected as {discord_bot.user} — running pipeline")
                try:
                    await scheduler.run_daily_pipeline()
                except Exception as e:
                    logger.error(f"Daily pipeline error: {e}", exc_info=True)
                finally:
                    logger.info("Pipeline complete.")
                    await discord_bot.close()
            await discord_bot.start(discord_token)
        else:
            logger.info("Running daily learning pipeline (manual trigger, no Discord)...")
            await scheduler.run_daily_pipeline()
            logger.info("Pipeline complete.")

    await _run_with_discord()


async def main() -> None:
    args = parse_args()
    config = load_config()
    mode = args.mode or config.get("mode", "paper")
    config["mode"] = mode
    base_dir = Path(__file__).parent

    # Database — always per-mode (polybot_paper.db / polybot_live.db).
    # Positions, bankroll, peak, and trade_history are isolated by mode so flipping
    # paper -> live can never inherit stale paper state. Pipeline learnings live in
    # polybot/memory/ and are shared (calibration + weights transfer across modes).
    db_path = config["database"]["path"].replace(".db", f"_{mode}.db")

    # One-time migration: legacy installs had paper writing to the unsuffixed file.
    # If the suffixed file doesn't exist yet but the legacy one does, rename it.
    legacy_path = config["database"]["path"]
    if mode == "paper" and not Path(db_path).exists() and Path(legacy_path).exists():
        try:
            Path(legacy_path).rename(db_path)
            logger.info(f"DB migration: {legacy_path} -> {db_path}")
        except OSError as e:
            logger.warning(f"DB migration failed (non-fatal, will use legacy path): {e}")
            db_path = legacy_path

    db = Database(db_path)
    await db.initialize()
    logger.debug(f"Database: {db_path} (mode: {mode})")
    if await db.get_bankroll() == 0:
        await db.set_bankroll(config["execution"]["initial_bankroll"])

    # Binance feed
    binance_cfg = config.get("binance", {})
    binance_feed = BinanceFeed(
        symbol=binance_cfg.get("symbol", "btcusdt"),
        buffer_size=binance_cfg.get("candle_buffer_size", 200),
        ws_url=binance_cfg.get("ws_url", "wss://stream.binance.us:9443/ws"),
        rest_url=binance_cfg.get("rest_url", "https://api.binance.us/api/v3"),
    )

    # BTC market scanner
    market_cfg = config.get("market", {})
    market_scanner = BTCMarketScanner(
        entry_window_seconds=market_cfg.get("entry_window_seconds", 120),
        min_time_remaining=market_cfg.get("min_time_remaining_seconds", 20),
        cache_seconds=market_cfg.get("scan_cache_seconds", 5),
        min_book_depth_usd=market_cfg.get("min_book_depth_usd", 50.0),
        clob_url=market_cfg.get("clob_url"),
    )

    # Indicator engine
    signal_cfg = config.get("signal", {})
    ind_cfg = config.get("indicators", {})
    indicator_params = {
        "rsi": {"period": ind_cfg.get("rsi", {}).get("period", 14),
                "overbought": ind_cfg.get("rsi", {}).get("overbought", 70),
                "oversold": ind_cfg.get("rsi", {}).get("oversold", 30)},
        "macd": {"fast": ind_cfg.get("macd", {}).get("fast_period", 12),
                 "slow": ind_cfg.get("macd", {}).get("slow_period", 26),
                 "signal_period": ind_cfg.get("macd", {}).get("signal_period", 9)},
        "stochastic": {"k_period": ind_cfg.get("stochastic", {}).get("k_period", 14),
                       "d_smoothing": ind_cfg.get("stochastic", {}).get("d_smoothing", 3),
                       "overbought": ind_cfg.get("stochastic", {}).get("overbought", 80),
                       "oversold": ind_cfg.get("stochastic", {}).get("oversold", 20)},
        "ema": {"fast_period": ind_cfg.get("ema", {}).get("fast_period", 9),
                "slow_period": ind_cfg.get("ema", {}).get("slow_period", 21),
                "chop_threshold": ind_cfg.get("ema", {}).get("chop_threshold", 0.0001)},
        "obv": {"slope_period": ind_cfg.get("obv", {}).get("slope_period", 5)},
        "atr": {"period": ind_cfg.get("atr", {}).get("period", 14),
                "low_pct": ind_cfg.get("atr", {}).get("low_percentile", 5),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(
        weights=signal_cfg.get("weights"),
        params=indicator_params,
    )

    # Signal engine — probability model with edge-based entry
    from polybot.core.calibrator import IsotonicCalibrator

    signal_engine = _build_signal_engine(signal_cfg, config)

    # Load isotonic calibrator (identity if file doesn't exist)
    calibrator = IsotonicCalibrator()
    _cal_path = Path(base_dir) / "memory" / "calibration" / "isotonic_params.json"
    calibrator.load(_cal_path)
    signal_engine.calibrator = calibrator

    # Brain (Claude client kept for TA evolver analysis calls)
    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model="claude-sonnet-4-6")

    # Execution — route based on mode
    exec_cfg = config["execution"]
    if mode == "live":
        # Allowance floor: cover at least 10 rounds of max-sized concurrent positions so a
        # revoked or run-down allowance is caught before it silently kills order fills.
        _preflight_bankroll = await db.get_bankroll()
        _kelly_fraction = config.get("signal", {}).get("kelly_fraction", 0.15)
        _max_single = _preflight_bankroll * _kelly_fraction
        _max_concurrent = exec_cfg.get("max_concurrent_positions", _d("max_concurrent_positions"))
        _min_allowance = _max_single * _max_concurrent * 10.0
        ok, msg, live_balance = verify_auth(min_allowance_usd=_min_allowance)
        if not ok:
            logger.error(f"LIVE MODE preflight failed: {msg}")
            return
        logger.debug(f"LIVE MODE — {msg}")
        trader = LiveTrader(db=db, max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"],
            use_maker_orders=exec_cfg.get("use_maker_orders", False),
            maker_timeout_s=exec_cfg.get("maker_timeout_s", 60.0))
    else:
        trader = PaperTrader(db=db, max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"],
            paper_latency_mean_s=exec_cfg.get("paper_latency_mean_s", 1.5),
            paper_latency_jitter_s=exec_cfg.get("paper_latency_jitter_s", 0.8),
            paper_network_fail_rate=exec_cfg.get("paper_network_fail_rate", 0.02))
        logger.debug(
            f"PAPER MODE — simulated trading with live-realistic fills "
            f"(latency={exec_cfg.get('paper_latency_mean_s', 1.5)}±{exec_cfg.get('paper_latency_jitter_s', 0.8)}s, "
            f"net_fail={exec_cfg.get('paper_network_fail_rate', 0.02):.0%})"
        )

    # Circuit breaker (drawdown-based Kelly scaling)
    cb_cfg = config.get("circuit_breaker", {})
    init_bankroll = await db.get_bankroll()
    breaker = CircuitBreaker(
        initial_bankroll=init_bankroll,
        floor_pct=cb_cfg.get("floor_pct", _d("circuit_breaker.floor_pct")),
        min_multiplier=cb_cfg.get("min_multiplier", _d("circuit_breaker.min_multiplier")),
        losses_to_reduce=cb_cfg.get("losses_to_reduce", 3),
        wins_to_restore=cb_cfg.get("wins_to_restore", 2),
    )
    # Restore locked_tier from persisted peak so floor survives restarts.
    # Compare against breaker.peak_bankroll (which CircuitBreaker.__init__ seeds
    # from initial_bankroll), not init_bankroll directly — otherwise restarting
    # with a healthy live_balance that's below the historical peak silently
    # drops the floor protection. e.g. peak=$1000, drawdown to $700, restart
    # with live_balance=$700: old condition `persisted_peak > init_bankroll`
    # → False → floor reset to $700. New condition keeps the $1000 floor.
    persisted_peak = await db.get_peak_bankroll()
    if persisted_peak is not None and persisted_peak > breaker.peak_bankroll:
        breaker.peak_bankroll = persisted_peak
        breaker.update_bankroll(persisted_peak)
        breaker.current_bankroll = init_bankroll
        logger.debug(f"CIRCUIT BREAKER: restored persisted peak ${persisted_peak:,.2f} (current ${init_bankroll:,.2f}, drawdown={breaker.drawdown_pct:.1%})")
    else:
        await db.set_peak_bankroll(init_bankroll)

    # Agents
    agents_cfg = config["agents"]
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    ghost_tracker = GhostTracker(memory_dir=str(base_dir / "memory"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    ta_evolver = TAEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"),
                          claude_client=claude)
    weight_optimizer = WeightOptimizer()

    # Discord (created before scheduler so alert_manager can be passed in)
    discord_bot = create_bot(db, trader, market_scanner, None, config)
    alert_manager = AlertManager(bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"],
        daily_channel_name=config["discord"].get("daily_channel_name", "polybot-daily"))
    discord_bot.alert_manager = alert_manager

    from polybot.agents.pipeline_tracker import PipelineTracker
    pipeline_tracker = PipelineTracker(path=base_dir / "memory" / "pipeline_history.json")

    scheduler = AgentScheduler(
        outcome_reviewer=outcome_reviewer,
        bias_detector=bias_detector,
        ta_evolver=ta_evolver,
        weight_optimizer=weight_optimizer,
        indicator_engine=indicator_engine,
        signal_engine=signal_engine,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
        market_scanner=market_scanner,
        config=config,
        counterfactual_tracker=counterfactual_tracker,
        pipeline_tracker=pipeline_tracker,
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", _d("exit_edge_threshold"))
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    scheduler._auto_shutdown = args.auto_restart
    scheduler.ghost_tracker = ghost_tracker
    discord_bot.scheduler = scheduler
    if mode == "live":
        # Sync DB bankroll with real Polymarket balance (fetched during preflight)
        await db.set_bankroll(live_balance)

        # Orphan-position gate runs BEFORE reconcile so the operator sees orphans
        # before any DB mutations happen. OrphanPositionError propagates to the
        # outer handler — it intentionally aborts startup so the operator can
        # inspect memory/orphan_positions.json. Pass --allow-orphans after review.
        if hasattr(trader, "detect_orphan_positions"):
            try:
                await trader.detect_orphan_positions(db, allow_orphans=args.allow_orphans)
            except OrphanPositionError:
                raise  # bubble up to the AuthError-style clean-exit handler
            except Exception as e:
                logger.warning(f"Orphan detection failed unexpectedly (non-blocking): {e}")

        try:
            if hasattr(trader, "reconcile_open"):
                # Pass outcome_reviewer + signal_engine so the missed-close recovery
                # path can write a real trade_history row + outcome JSON instead of
                # silently zeroing exit_price (Phase-1 Flow-5(c)). Exit_reason is
                # stamped "reconcile_recovery_*" so the pipeline pool can be
                # filtered post-hoc if the operator wants to quarantine these.
                await trader.reconcile_open(
                    db, outcome_reviewer=outcome_reviewer, signal_engine=signal_engine,
                )
            if hasattr(trader, "reconcile_dust"):
                await trader.reconcile_dust(db, max_age_hours=24)
        except Exception as e:
            logger.warning(f"Startup reconciliation failed (non-blocking): {e}")

    # CLOB WebSocket — real-time order book feed
    clob_ws_url = market_cfg.get("clob_ws_url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    clob_ws = ClobWebSocket(url=clob_ws_url)
    await clob_ws.start()

    # Give LiveTrader access to CLOB WS for fast maker fill detection
    if hasattr(trader, "set_clob_ws"):
        trader.set_clob_ws(clob_ws)
    if hasattr(trader, "start_keepalive"):
        await trader.start_keepalive()

    # --- New data feeds ---
    depth_cfg = config.get("binance_depth", {})
    depth_feed = BinanceDepthFeed(
        ws_url=depth_cfg.get("ws_url", "wss://stream.binance.com:9443/ws"),
        rest_url=depth_cfg.get("rest_url", "https://api.binance.com/api/v3"),
        rest_interval=86400,  # REST snapshot effectively disabled; top-20 WS provides depth sizing.
    )
    trades_cfg = config.get("binance_trades", {})
    trades_accumulator = BinanceTradeAccumulator(max_age_s=trades_cfg.get("max_age_s", 300))
    trades_feed = BinanceTradesFeed(
        accumulator=trades_accumulator,
        ws_url=trades_cfg.get("ws_url", "wss://stream.binance.com:9443/ws"),
    )
    bybit_cfg = config.get("bybit", {})
    bybit_feed_inst = BybitFeed(
        ws_url=bybit_cfg.get("ws_url", "wss://stream.bybit.com/v5/public/linear"),
    )
    # Coinbase feed — faster BTC price (leads Binance.US by 0.5-2s)
    coinbase_cfg = config.get("coinbase", {})
    coinbase_feed = CoinbaseFeed(
        ws_url=coinbase_cfg.get("ws_url", "wss://ws-feed.exchange.coinbase.com"),
        product_id=coinbase_cfg.get("product_id", "BTC-USD"),
    )


    # Restore L5 prev_resolution_margin from last session — without this, every restart
    # zeroes out the feature for the first few trades, creating a systematic training bias.
    global _prev_resolution_margin
    _prev_resolution_margin = _load_prev_resolution_margin()
    if _prev_resolution_margin != 0.0:
        logger.debug(f"Restored prev_resolution_margin: {_prev_resolution_margin:+.2f}")

    # Reset skip counter for this session — pipeline reads the persisted stats before reset.
    global _gate_skip_counts
    _gate_skip_counts = {}
    flush_gate_stats()  # write empty baseline so pipeline always has a fresh file to read

    # SPRT + regime detector — module-level state for trading loop
    global _sprt, _regime_detector
    _sprt = SPRTAccumulator(
        alpha=config.get("sprt", {}).get("alpha", 0.05),
        beta=config.get("sprt", {}).get("beta", 0.10),
        min_interval_s=config.get("sprt", {}).get("observation_interval_s", 5.0),
    )
    regime_cfg = config.get("regime", {})
    _regime_detector = RegimeDetector(
        lookback=regime_cfg.get("lookback", 50),
        vol_high_pct=regime_cfg.get("vol_high_percentile", 75),
        vol_low_pct=regime_cfg.get("vol_low_percentile", 25),
        autocorr_threshold=regime_cfg.get("autocorr_threshold", 0.25),
    )

    global _adverse_monitor
    _adverse_monitor = AdverseSelectionMonitor()

    global _cvd_normalizer
    _cvd_normalizer = IndicatorNormalizer(alpha=0.02, warmup=50)

    await scheduler.start()
    await binance_feed.start()
    await depth_feed.start()
    await trades_feed.start()
    await bybit_feed_inst.start()
    await coinbase_feed.start()
    # Chainlink oracle feed — resolution price source (Polymarket uses this, not Binance)
    from polybot.feeds.chainlink_feed import ChainlinkFeed
    chainlink_feed = ChainlinkFeed()
    await chainlink_feed.start()

    # Shared HTTP client — lifecycle managed here in main()
    import httpx
    http_client = httpx.AsyncClient(
        timeout=5,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60),
    )

    async def run_discord():
        backoff = 5
        while True:
            try:
                await discord_bot.start(get_secret("DISCORD_BOT_TOKEN"))
                return  # clean shutdown (scheduler exit)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Discord bot error: %s — reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    # Wait for Discord to connect before starting the trading loop
    discord_task = asyncio.create_task(run_discord())
    try:
        await asyncio.wait_for(discord_bot.ready_event.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("Discord did not connect within 15s — starting trading loop anyway")

    trading_task = asyncio.create_task(trading_loop(
        binance_feed, market_scanner, indicator_engine, signal_engine,
        trader, alert_manager, db, config, outcome_reviewer,
        is_paused_fn=lambda: discord_bot.is_paused,
        scheduler=scheduler, clob_ws=clob_ws, breaker=breaker,
        counterfactual_tracker=counterfactual_tracker,
        ghost_tracker=ghost_tracker,
        http_client=http_client,
        depth_feed=depth_feed, trades_feed=trades_feed,
        bybit_feed=bybit_feed_inst,
        chainlink_feed=chainlink_feed, coinbase_feed=coinbase_feed))
    background_tasks = [
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        discord_task,
    ]
    logger.debug("PolyBot started — all systems running (WebSocket + event-driven)")

    try:
        # Wait for trading loop — it exits after pipeline sets _shutdown_requested
        await trading_task
    except asyncio.CancelledError:
        pass
    finally:
        for t in background_tasks:
            t.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await http_client.aclose()
        async def _stop(coro):
            try: await asyncio.wait_for(coro, timeout=2.0)
            except Exception: pass
        if hasattr(trader, "stop_keepalive"):
            await _stop(trader.stop_keepalive())
        await _stop(clob_ws.close())
        await _stop(scheduler.stop())
        await _stop(binance_feed.stop())
        await _stop(depth_feed.stop())
        await _stop(trades_feed.stop())
        await _stop(bybit_feed_inst.stop())
        await _stop(chainlink_feed.stop())
        bankroll = await db.get_bankroll()
        await db.close()
        logger.info(f"PolyBot stopped — Bankroll ${bankroll:.2f} · Feeds/WS/DB closed")
        await discord_bot.close()


if __name__ == "__main__":
    args = parse_args()
    try:
        if args.run_pipeline:
            asyncio.run(run_pipeline())
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except OrphanPositionError as e:
        # Print to stderr with a clear remediation hint — no stack trace, since
        # the situation is operator-actionable, not a code bug. run_polybot.ps1
        # treats a non-zero exit as do-not-restart, which is what we want here.
        import sys as _sys
        _sys.stderr.write(
            "\n" + "=" * 70 + "\n"
            "ORPHAN POSITION GATE TRIPPED\n"
            "=" * 70 + "\n"
            f"{e}\n\n"
            "Next steps:\n"
            "  1) cat polybot/memory/orphan_positions.json\n"
            "  2) Manually sweep or resolve any genuine orphan shares on Polymarket\n"
            "  3) Re-run with --allow-orphans to acknowledge known leftover shares\n"
            + "=" * 70 + "\n"
        )
        _sys.exit(2)
