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

# Force UTF-8 on stdout/stderr so Windows cp1252 consoles don't choke on box-drawing
# chars in pipeline summaries; errors='replace' survives any still-unrenderable codepoint.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from polybot.config.loader import load_config, get_secret
from polybot.config.param_registry import default_for as _d
from polybot.paths import (
    PREV_MARGIN_PATH, FEED_STALENESS_PATH, GATE_STATS_PATH,
    GATE_STATS_CURRENT_PATH, STRATEGY_LOG_PATH, PIPELINE_HISTORY_PATH,
    CALIBRATION_PARAMS_PATH, PRICE_SUM_OUTLIERS_PATH, fold_gate_day,
)
from polybot.execution.base import entry_fee_shares, slippage_pct, DEFAULT_FEE_RATE, EFFECTIVE_FEE_PEAK, compute_buy_vwap
from polybot.db.models import Database
from polybot.feeds.binance_feed import BinanceFeed
from polybot.feeds.market_scanner import BTCMarketScanner
from polybot.feeds.clob_ws import ClobWebSocket
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine
from polybot.core.order_flow import compute_flow_signal
from polybot.core.aux_layers import compute_spot_flow_signal, regime_vol_factor
from polybot.execution.paper_trader import PaperTrader
from polybot.execution.live_trader import AuthError, LiveTrader, OrphanPositionError, verify_auth
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.scheduler import NightlyScheduler
from polybot.agents.counterfactual_tracker import CounterfactualTracker
from polybot.agents.ghost_tracker import GhostTracker
from polybot.discord_bot.bot import create_bot
from polybot.discord_bot.alerts import AlertManager
from polybot.execution.circuit_breaker import CircuitBreaker
from polybot.execution.correlation import concurrent_multiplier
import math
from polybot.feeds.binance_depth import BinanceDepthFeed
from polybot.feeds.binance_trades import BinanceTradesFeed, BinanceTradeAccumulator
from polybot.feeds.coinbase_feed import CoinbaseFeed
from polybot.feeds._staleness import snapshot_feeds as _staleness_snapshot, write_feeds as _staleness_write
from polybot.core.adverse_selection import AdverseSelectionMonitor

import re
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


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

class _StripAnsiFormatter(logging.Formatter):
    """Strips ANSI color codes so log files stay clean."""
    def format(self, record):
        result = super().format(record)
        return _ANSI_RE.sub('', result)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_file_handler = logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=0, mode="a", encoding="utf-8")
_file_handler.setFormatter(_StripAnsiFormatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

# Async logging: disk writes (1-5ms each) are offloaded to a queue thread, off the hot path.
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

# Aux-signal freshness limit: aux trade_context fields stamp None (never 0.0) when
# the source is missing/stale, so "feed cold" stays distinguishable from "real zero".
_AUX_FRESH_S_COINBASE = 10.0
_AUX_FRESH_S_TRADES = 3.0

# Sub-second CVD-acceleration scale — local to the deceleration gate. L3b uses
# the canonical helper in `polybot/core/aux_layers.py`.


def _build_aux_signals(coinbase_feed: Any, trades_feed: Any = None) -> dict[str, Any]:
    """Auxiliary microstructure signals shared between trade_context, ghost replay,
    and the counterfactual exit contexts (E3 latency features included).

    Every field is ``None`` when the source feed is missing, not warm, stale, or
    its trade buffer doesn't yet span the 60s window (post-reconnect) — never
    0.0, which would collide with a legitimate zero reading.
    """
    cb_fresh = (coinbase_feed is not None
                and coinbase_feed.state.age_seconds < _AUX_FRESH_S_COINBASE
                and coinbase_feed.covers(60.0))

    cb_cvd = coinbase_feed.get_cvd(60.0) if cb_fresh else None
    if cb_fresh:
        cb_taker, cb_taker_n = coinbase_feed.get_taker_ratio(60.0)
    else:
        cb_taker, cb_taker_n = None, 0

    # E3 latency features. cross_venue_gap = Coinbase (resolution venue) minus
    # Binance latest trade — the lead the exit engine monetizes. fast vol from
    # the 1s-bucketed Coinbase price history.
    bt_acc = trades_feed.accumulator if trades_feed else None
    bt_fresh = bt_acc is not None and bt_acc.latest_age_s < _AUX_FRESH_S_TRADES
    cb_tick_fresh = (coinbase_feed is not None
                     and coinbase_feed.state.age_seconds < _AUX_FRESH_S_COINBASE)
    cb_price = coinbase_feed.state.price if cb_tick_fresh else None
    bn_price = bt_acc.latest_price if bt_fresh else None
    gap = (cb_price - bn_price) if (cb_price and bn_price) else None
    fast_rv = coinbase_feed.realized_vol(60.0) if cb_fresh else None

    def _r(v: float | None, ndigits: int) -> float | None:
        return None if v is None else round(v, ndigits)

    return {
        "coinbase_cvd_60s": _r(cb_cvd, 4),
        "coinbase_taker_60s": _r(cb_taker, 4),
        "coinbase_taker_n": cb_taker_n,
        "cross_venue_gap": _r(gap, 2),
        "fast_realized_vol_60s": _r(fast_rv, 6),
    }

def _clob_book_aux(clob_ws: Any, token_up: str, token_down: str,
                   book_up: dict[str, Any], book_down: dict[str, Any]) -> dict[str, Any]:
    """E2 fields: per-side CLOB top-5 ask depth (USD) + book age, stamped into the
    entry trade_context for trades and ghosts. depth_usd_top20 is BINANCE BTC
    depth — these are the market's own books. None = no book on that side; age
    is None when either side lacks a timestamped WS snapshot (HTTP books are
    fetch-fresh but carry no ts)."""
    now = time.time()

    def _side(token: str, http_book: dict[str, Any]) -> tuple[float | None, float | None]:
        ws_book = clob_ws.get_book(token) if clob_ws else None
        ws_ts = float(ws_book.get("ts", 0) or 0) if ws_book else 0.0
        book = ws_book if (ws_book and ws_ts > 0 and ws_book.get("asks")) else (http_book or {})
        asks = book.get("asks") or []
        if not asks:
            return None, None
        try:
            depth = sum(float(a["price"]) * float(a["size"]) for a in asks[:5])
        except (KeyError, ValueError, TypeError):
            return None, None
        age = (now - ws_ts) if book is ws_book else None
        return depth, age

    depth_up, age_up = _side(token_up, book_up)
    depth_down, age_down = _side(token_down, book_down)
    age = max(age_up, age_down) if (age_up is not None and age_down is not None) else None
    return {
        "clob_depth_top5_up_usd": None if depth_up is None else round(depth_up, 2),
        "clob_depth_top5_down_usd": None if depth_down is None else round(depth_down, 2),
        "clob_book_age_s": None if age is None else round(age, 3),
    }


# E1 recorder throttle: one line per market per second, so a stuck out-of-band
# window can't grow the JSONL unboundedly at tick rate.
_last_price_sum_log: dict[str, float] = {}

def _log_price_sum_outlier(market_id: str, price_up: float, price_down: float,
                           size_up: float, size_down: float) -> None:
    """Append one out-of-band price-sum moment (the [0.98, 1.02] gate's skip) to
    PRICE_SUM_OUTLIERS_PATH. Pure telemetry: never raises, never blocks the gate."""
    try:
        now = time.time()
        if now - _last_price_sum_log.get(market_id, 0.0) < 1.0:
            return
        _last_price_sum_log[market_id] = now
        if len(_last_price_sum_log) > 500:
            _last_price_sum_log.clear()
        PRICE_SUM_OUTLIERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PRICE_SUM_OUTLIERS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": round(now, 3), "market": market_id,
                "ask_up": price_up, "ask_down": price_down,
                "sum": round(price_up + price_down, 4),
                "size_up": round(size_up, 2), "size_down": round(size_down, 2),
            }) + "\n")
    except Exception:
        pass


# Throttled logging for hold evaluations and resolution waiting
_last_hold_log: dict[str, float] = {}  # market_id -> last log timestamp
_last_resolve_wait_log: dict[str, float] = {}  # market_id -> last log timestamp
_abandoned_scalp_positions: set[int] = set()  # position IDs too small to sell, hold to resolution

# Phase 1 passive exits: position_id -> resting SELL state. In-memory by design —
# a restart simply re-evaluates the position and falls back to FOK.
_resting_exits: dict[int, dict[str, Any]] = {}


def _resting_fill_price(prints: list[dict[str, Any]], level: float, posted_ts: float) -> float | None:
    """Conservative passive-fill rule (same as the shadow sim): a resting SELL at
    ``level`` fills only when a BUY-side print lands STRICTLY above it after
    posting — queue position is unknowable, so at-level prints don't count."""
    for t in prints:
        if float(t.get("timestamp", 0) or 0) <= posted_ts:
            continue
        if (str(t.get("side", "")).upper() == "BUY"
                and float(t.get("price", 0) or 0) > level):
            return level
    return None

# Window-path recorder (recording.WindowPathRecorder) — set by main() at boot.
_window_recorder = None

# Previous window resolution margin — recorded telemetry (no model layer consumes it)
_prev_resolution_margin: float = 0.0
_PREV_MARGIN_PATH = PREV_MARGIN_PATH
# Beyond this many seconds the margin is no longer adjacent to the current
# window and stamps as zero.
_PREV_MARGIN_STALE_S = 1800  # 30 min ≈ six 5-min windows

def _load_prev_resolution_margin() -> float:
    """Restore margin from last session iff written within _PREV_MARGIN_STALE_S."""
    try:
        if _PREV_MARGIN_PATH.exists():
            data = json.loads(_PREV_MARGIN_PATH.read_text())
            margin = float(data.get("margin", 0.0))
            saved_at = float(data.get("saved_at", 0.0))
            if saved_at > 0 and (time.time() - saved_at) > _PREV_MARGIN_STALE_S:
                return 0.0
            return margin
    except Exception:
        pass
    return 0.0

def _save_prev_resolution_margin(margin: float) -> None:
    try:
        _PREV_MARGIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREV_MARGIN_PATH.write_text(json.dumps({"margin": margin, "saved_at": time.time()}))
    except Exception:
        pass

_current_window_id: str = ""
_adverse_monitor: AdverseSelectionMonitor | None = None
_last_adverse_skip_log_window: int = 0  # throttle adverse-skip logs to once per 5-min window
_last_logged_action: str = ""  # suppress repeated EVAL blocks when action hasn't changed
_gate_skip_counts: dict[str, int] = {}  # gate_name -> skip count for the current ET day
_gate_stats_day_key: str = ""           # ET date string keyed to _gate_skip_counts
from collections import OrderedDict as _OrderedDict
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

    Shares the _last_hold_log throttle with the normal HOLD log. Must surface WHY
    the bot won't act — silent fallbacks produced the "moved against us (2%)" pathology.
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
    """Return the Coinbase BTC price + source label, or (0.0, "stale").

    Coinbase (the venue Chainlink resolves against) is the sole decision price;
    callers must treat (0.0, "stale") as "skip this decision", not a zero price.
    No Binance fallback — a divergent transient print could flip P(side) on a tick
    the resolver never sees. Binance is read only to log the cross-venue gap.
    """
    cb_price = cb_age = bt_price = bt_age = 0.0
    if coinbase_feed:
        cb_age = coinbase_feed.state.age_seconds
        cb_price = coinbase_feed.state.price
    if trades_feed and trades_feed.accumulator:
        bt_age = trades_feed.accumulator.latest_age_s
        bt_price = trades_feed.accumulator.latest_price

    if cb_price > 0 and bt_price > 0 and cb_age < 2 and bt_age < 3:
        # Cross-venue gap (positive → Coinbase leading higher than Binance).
        logger.debug("cross_venue_gap coinbase=%.2f binance=%.2f delta=%+.2f", cb_price, bt_price, cb_price - bt_price)
    if cb_price > 0 and cb_age < 2:
        return cb_price, f"coinbase ({cb_age:.2f}s)"
    return 0.0, "stale"


def _fmt_secs(s: float) -> str:
    """Seconds remaining formatted as M:SS — 298 → '4:58'. Easier to scan than '298s'."""
    s_int = max(0, int(s))
    return f"{s_int // 60}:{s_int % 60:02d}"


def _emit_gate_skip(cid: str, gate_key: str, reason: str) -> None:
    """Emit one combined SKIP line (signal context + gate reason).

    Throttled per (cid, gate_key) — direction is intentionally NOT in the key, so
    rapid Up/Down ping-pong on the same gate emits one SKIP, not 20× in 5 seconds.
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
    logger.info(
        f"{_C.DIM}SKIP {ctx['direction']} {ctx['window_slug']} | "
        f"model {ctx['prob']:.0%} {ctx['direction']}, BTC {ctx['dist']:+,.0f} vs strike | "
        f"{reason}{_C.RESET}"
    )

def _et_date_key() -> str:
    """Current ET calendar date as 'YYYYMMDD' — the rollover key for daily gate stats."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _read_gate_current() -> "tuple[str, dict]":
    """Return (et_date, counts) from the current-day gate-stats file, or ("", {})."""
    try:
        if GATE_STATS_CURRENT_PATH.exists():
            d = json.loads(GATE_STATS_CURRENT_PATH.read_text())
            if isinstance(d, dict) and isinstance(d.get("counts"), dict):
                return str(d.get("et_date", "")), {str(k): int(v) for k, v in d["counts"].items()}
    except Exception:
        pass
    return "", {}


def _write_gate_current(counts: dict) -> None:
    """Persist today's live counts to GATE_STATS_CURRENT_PATH (restart-safe)."""
    from datetime import datetime, timezone
    try:
        GATE_STATS_CURRENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        GATE_STATS_CURRENT_PATH.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "et_date": _et_date_key(),
            "counts": dict(counts),
            "total_skips": sum(counts.values()),
        }, indent=2))
    except Exception:
        pass


def _fold_gate_day_into_accumulator(day_key: str, counts: dict) -> None:
    """Add one finished ET day's counts into the lifetime accumulator (GATE_STATS_PATH)."""
    fold_gate_day(GATE_STATS_PATH, counts, day_key)


def _ensure_gate_stats_day_loaded() -> None:
    """Rollover guard for the gate-skip counters.

    On a NEW ET day, fold the just-finished day's counts into the lifetime
    accumulator (GATE_STATS_PATH) and start the new day empty. On the first call
    of a process, reload today's live counts from GATE_STATS_CURRENT_PATH so a
    mid-day restart keeps accumulating; if that file holds a PAST day (a crash
    left it un-folded), fold it in first so no day is ever lost.
    """
    global _gate_skip_counts, _gate_stats_day_key
    today = _et_date_key()
    if _gate_stats_day_key == today:
        return
    if _gate_stats_day_key:  # crossed midnight ET within this process
        _fold_gate_day_into_accumulator(_gate_stats_day_key, _gate_skip_counts)
        _gate_skip_counts = {}
        _gate_stats_day_key = today
        _write_gate_current({})
        return
    # First load this process.
    loaded_key, loaded_counts = _read_gate_current()
    if loaded_counts and loaded_key and loaded_key != today:
        # A previous day's counts were left un-folded (crash) — fold before resetting.
        _fold_gate_day_into_accumulator(loaded_key, loaded_counts)
        _gate_skip_counts = {}
        _gate_stats_day_key = today
        _write_gate_current({})
    else:
        _gate_skip_counts = dict(loaded_counts)
        _gate_stats_day_key = today


def _record_skip(gate: str) -> None:
    """Increment the per-gate skip counter. Called at every entry skip point."""
    _ensure_gate_stats_day_loaded()
    _gate_skip_counts[gate] = _gate_skip_counts.get(gate, 0) + 1


def flush_gate_stats() -> None:
    """Persist today's live skip counts to GATE_STATS_CURRENT_PATH."""
    _ensure_gate_stats_day_loaded()
    _write_gate_current(_gate_skip_counts)
# Per-window flip state: tracks flip count and last side
_window_flip_state: dict[str, dict] = {}  # window_id -> {flip_count}

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
        min_model_probability=signal_cfg.get("min_model_probability", _d("min_model_probability")),
        student_t_df=signal_cfg.get("student_t_df", _d("student_t_df")),
        regime_lookback=signal_cfg.get("regime_lookback", _d("regime_lookback")),
        min_kelly=signal_cfg.get("min_kelly", _d("min_kelly")),
        atr_sigma_ratio=signal_cfg.get("atr_sigma_ratio", _d("atr_sigma_ratio")),
        min_atr=signal_cfg.get("min_atr", _d("min_atr")),
        loss_cut_fraction=signal_cfg.get("loss_cut_fraction", _d("loss_cut_fraction")),
        loss_cut_time_s=signal_cfg.get("loss_cut_time_s", _d("loss_cut_time_s")),
        deep_loss_hold_threshold=signal_cfg.get("deep_loss_hold_threshold", _d("deep_loss_hold_threshold")),
        atr_regime_shift_threshold=signal_cfg.get("atr_regime_shift_threshold", _d("atr_regime_shift_threshold")),
    )


def compute_time_multiplier(prob: float, seconds_remaining: float,
                            window_seconds: float = 300.0,
                            normal_fraction: float = 0.60,
                            late_max_penalty: float = 0.30) -> tuple[float, str]:
    """Returns (kelly_multiplier, phase). High-conviction entries barely penalized late.

    Full Kelly for the first ``normal_fraction`` of the window (by elapsed time);
    past that the penalty ramps across the remaining ``(1 - normal_fraction)``.
    """
    elapsed_fraction = max(0.0, 1.0 - seconds_remaining / window_seconds)
    conviction = 2.0 * abs(prob - 0.5)
    if elapsed_fraction <= normal_fraction:
        return 1.0, "normal"
    phase = "late" if seconds_remaining >= 30 else "final"
    late_depth = (elapsed_fraction - normal_fraction) / max(1e-9, 1.0 - normal_fraction)
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
    # Sync gate_stats to disk on every outcome so intraday telemetry never trails
    # the last resolution; background thread keeps the close path off disk I/O.
    asyncio.create_task(asyncio.to_thread(flush_gate_stats))


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
        coinbase_feed: Any = None,
        chainlink_feed: Any = None,
        ghost_tracker: Any = None) -> tuple[str | None, int]:
    """Compute indicators/flow/signal, check for entry, size the trade, execute."""

    # Stamped once per evaluation so ghosts and filled outcomes share one schema;
    # aux fields are a real value or None — never a 0.0 stand-in.
    aux_signals = _build_aux_signals(coinbase_feed, trades_feed)
    aux_signals.update(_clob_book_aux(clob_ws, token_up, token_down, book_up, book_down))

    def _ghost(gate: str, signal: Any, snap: dict) -> None:
        """Record a ghost trade when a downstream gate rejects a real BUY signal.

        Base trade_context is built from closure vars at gate-fire time so the
        ghost survives downstream record consumers; a caller-supplied snap merges on
        top (caller wins on overlapping keys).
        """
        if ghost_tracker is None or signal is None:
            return
        if signal.action not in ("BUY_YES", "BUY_NO"):
            return  # model-level skip — not a valid ghost
        side = "Up" if signal.action == "BUY_YES" else "Down"
        raw_prob_side = (
            signal_engine.last_raw_prob_up if side == "Up"
            else 1.0 - signal_engine.last_raw_prob_up
        )
        _closes_tail = (
            [float(closes[-2]), float(closes[-1])]
            if len(closes) >= 2 else None
        )
        _ghost_cid = contract.get("slug", contract.get("market_id", ""))
        _ghost_flip_count = int(_window_flip_state.get(_ghost_cid, {}).get("flip_count", 0))
        base_ctx: dict[str, Any] = {
            "model_probability": signal.prob,
            "model_probability_raw": raw_prob_side,
            "edge": signal.edge,
            "market_price_up": price_up,
            "market_price_down": price_down,
            "btc_price": btc_price,
            "strike_price": strike,
            "seconds_remaining": contract.get("seconds_remaining", 0),
            "atr": indicators.get("atr", {}).get("atr", 0),
            "atr_rolling_20": round(signal_engine.last_atr_rolling_20, 6),
            "atr_long_term_mean": round(signal_engine.last_atr_long_term_mean, 6),
            "flow_score": flow_score_rec,
            "spot_flow_signal": spot_flow_rec,
            "prev_resolution_margin": _prev_resolution_margin,
            "regime_autocorr": round(signal_engine.last_regime_autocorr, 4),
            "regime_direction": round(signal_engine.last_regime_direction, 4),
            "closes_tail": _closes_tail,
            "entry_phase": phase,
            "flip_count": _ghost_flip_count,
            "is_flip": _ghost_flip_count > 0,
            **aux_signals,
        }
        merged_snap = dict(snap or {})
        caller_ctx = merged_snap.get("trade_context", {}) or {}
        merged_ctx = dict(base_ctx)
        merged_ctx.update(caller_ctx)
        merged_snap["trade_context"] = merged_ctx
        ghost_tracker.record_rejection(
            gate_name=gate,
            side=side,
            signal_prob=signal.prob,
            signal_edge=signal.edge,
            market_id=cid,
            seconds_remaining=float(contract.get("seconds_remaining", 0)),
            indicator_snapshot=merged_snap,
        )

    # Feed freshness gate: a connected-but-idle WebSocket can leave stale state in
    # place — better to skip the window than size on stale data.
    stale_feeds: list[str] = []
    if coinbase_feed and coinbase_feed.state.age_seconds > 30:
        stale_feeds.append(f"coinbase={coinbase_feed.state.age_seconds:.0f}s")
    if chainlink_feed and chainlink_feed.age_seconds > 60:
        stale_feeds.append(f"chainlink={chainlink_feed.age_seconds:.0f}s")
    # Binance aggTrade underpins the recorded flow telemetry and the cross-venue
    # gap: skip rather than size on stale data.
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

    global _current_window_id
    window_id = contract.get("market_id", contract.get("slug", ""))
    if window_id != _current_window_id:
        _current_window_id = window_id
        _last_skip_log.pop(cid, None)  # fresh window — allow skip reasons to log again

    indicators = indicator_engine.compute_all(binance_feed.buffer)

    trades_up = clob_ws.get_trade_history(token_up) if clob_ws else []
    trades_down = clob_ws.get_trade_history(token_down) if clob_ws else []
    flow_data = compute_flow_signal(book_up, book_down, trades_up, trades_down)
    flow_score = flow_data["flow_score"]

    # L3b — shared helper in `polybot/core/aux_layers.py`. Live and
    # backtest replay both call it so the model math is identical.
    _vol_factor = regime_vol_factor(
        indicators.get("atr", {}).get("atr", 0.0), signal_engine.last_atr_long_term_mean)
    spot_flow_signal = compute_spot_flow_signal(
        aux_signals.get("coinbase_cvd_60s"),
        aux_signals.get("coinbase_taker_60s"),
        aux_signals.get("coinbase_taker_n", 0),
        vol_factor=_vol_factor,
    )
    # Cold-vs-real-zero split (CLAUDE.md §10): the live model consumes a number
    # (cold collapses to 0.0), but the *recorded* trade_context value must be None
    # when the feed is cold — spot_flow cold when Coinbase CVD is None; book flow
    # cold when neither CLOB book nor any trade is present.
    spot_flow_rec = spot_flow_signal if aux_signals.get("coinbase_cvd_60s") is not None else None
    _book_present = bool(
        book_up.get("bids") or book_up.get("asks")
        or book_down.get("bids") or book_down.get("asks")
    )
    flow_score_rec = flow_score if (_book_present or flow_data.get("trade_count", 0) > 0) else None
    closes = binance_feed.buffer.get_closes()

    # Live fee rate so Kelly sizes against the actual cost (constant today; plumbed
    # so a future per-token rate Just Works).
    fee_rate = await market_scanner.fetch_fee_rate(token_up, http_client)

    signal = signal_engine.evaluate(
        indicators, has_position=False, in_entry_window=in_window,
        btc_price=btc_price, strike_price=strike,
        seconds_remaining=contract["seconds_remaining"],
        market_price_up=price_up, market_price_down=price_down,
        closes=closes,
        fee_rate=fee_rate,
    )

    # Continuous time multiplier: penalizes ATM trades late, barely penalizes high-conviction trades
    timing_cfg = config.get("entry_timing", {})
    time_mult, phase = compute_time_multiplier(
        prob=signal.prob,
        seconds_remaining=contract["seconds_remaining"],
        normal_fraction=timing_cfg.get("normal_fraction", _d("normal_fraction")),
        late_max_penalty=timing_cfg.get("late_max_penalty", _d("late_max_penalty")),
    )

    # Populate eval context for all evaluations. signal.side is the side the
    # prob/edge refer to (the edge-best side can be the sub-50% one); the
    # prob>=0.5 heuristic remains only for pre-model skips that carry no side.
    global _last_logged_action
    _is_buy = signal.action in ("BUY_YES", "BUY_NO")
    _direction = signal.side or ("Up" if signal.prob >= 0.5 else "Down")
    action_changed = _direction != _last_logged_action or eval_window != last_eval_log_window
    dist = btc_price - strike
    _lru_set(_pending_eval_ctx, cid, {
        "direction": _direction,
        "prob": signal.prob,
        "edge": signal.edge,
        "dist": dist,
        "window_slug": _slug_to_window(cid),
    }, _PENDING_CTX_MAX)
    if _is_buy:
        if action_changed:
            last_eval_log_window = eval_window
            _last_logged_action = _direction
            _last_gate_skip_state.pop(cid, None)
    else:
        last_eval_log_window = eval_window
        _reason_type = signal.reason.split(":")[0].strip()
        _emit_gate_skip(cid, f"model_{_reason_type}", signal.reason)

    if signal.action not in ("BUY_YES", "BUY_NO"):
        _record_skip(f"model:{signal.reason[:30]}")
        if ghost_tracker is not None and "below min prob" in signal.reason:
            prob_up = signal_engine.last_raw_prob_up
            if prob_up >= 0.5:
                side, signal_prob = "Up", prob_up
                mkt_price = price_up
            else:
                side, signal_prob = "Down", 1.0 - prob_up
                mkt_price = price_down
            _closes_tail = (
                [float(closes[-2]), float(closes[-1])]
                if len(closes) >= 2 else None
            )
            _st_cid = contract.get("slug", contract.get("market_id", ""))
            _st_flip_count = int(_window_flip_state.get(_st_cid, {}).get("flip_count", 0))
            ghost_tracker.record_rejection(
                gate_name="sub_threshold_prob",
                side=side,
                signal_prob=signal_prob,
                signal_edge=signal_prob - mkt_price,
                market_id=cid,
                seconds_remaining=float(contract.get("seconds_remaining", 0)),
                indicator_snapshot={"trade_context": {
                    "model_probability_raw": signal_prob,
                    "market_price_up": price_up,
                    "market_price_down": price_down,
                    "btc_price": btc_price,
                    "strike_price": strike,
                    "seconds_remaining": contract.get("seconds_remaining", 0),
                    "atr": indicators.get("atr", {}).get("atr", 0),
                    "atr_rolling_20": round(signal_engine.last_atr_rolling_20, 6),
                    "atr_long_term_mean": round(signal_engine.last_atr_long_term_mean, 6),
                    "flow_score": flow_score_rec,
                    "spot_flow_signal": spot_flow_rec,
                    "prev_resolution_margin": _prev_resolution_margin,
                    "regime_autocorr": round(signal_engine.last_regime_autocorr, 4),
                    "regime_direction": round(signal_engine.last_regime_direction, 4),
                    "closes_tail": _closes_tail,
                    # Ghost schema parity with _ghost(): by-phase / flip-segmented
                    # bias cards need these on the sub-threshold population too.
                    "entry_phase": phase,
                    "flip_count": _st_flip_count,
                    "is_flip": _st_flip_count > 0,
                    **aux_signals,
                }},
            )
        return None, last_eval_log_window

    # --- ADVERSE SELECTION (sizing penalty + emergency hard-skip) ---
    adverse_kelly_mult = 1.0
    adverse_rate_at_30s = -1.0
    if _adverse_monitor is not None:
        adverse_rate_at_30s = _adverse_monitor.get_adverse_rate(30.0)
        sig_cfg = config.get("signal", {})
        hard_skip_at = float(sig_cfg.get("adverse_selection_threshold", _d("adverse_selection_threshold")))
        penalty_floor = float(sig_cfg.get("adverse_penalty_floor", 0.45))
        penalty_slope = float(sig_cfg.get("adverse_penalty_slope", 1.5))
        penalty_min = float(sig_cfg.get("adverse_penalty_min", 0.30))
        if adverse_rate_at_30s >= hard_skip_at:
            _record_skip("adverse_selection")
            _ghost("adverse_selection", signal, {})
            global _last_adverse_skip_log_window
            if eval_window != _last_adverse_skip_log_window:
                _last_adverse_skip_log_window = eval_window
                logger.info(
                    f"{_C.DIM}SKIP adverse selection (hard) — fade rate "
                    f"{adverse_rate_at_30s:.0%} ≥ emergency floor {hard_skip_at:.0%}{_C.RESET}"
                )
            return None, last_eval_log_window
        if adverse_rate_at_30s > penalty_floor:
            adverse_kelly_mult = max(
                penalty_min,
                1.0 - penalty_slope * (adverse_rate_at_30s - penalty_floor),
            )

    # --- EDGE DECAY GATE ---
    # Adverse-selection counts fills crossing the wrong way; this measures HOW HARD
    # they cross (mean 15s post-fill mid drift) — a read on structural edge decay.
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

    flip_state = _window_flip_state.setdefault(cid, {"flip_count": 0})
    flip_count = flip_state["flip_count"]
    if flip_count >= 1:
        # Flips 1–2 pay the base premium; +0.5pp per flip beyond the 2nd, unbounded.
        flip_premium_base = config.get("entry_timing", {}).get("flip_edge_premium", _d("flip_edge_premium"))
        flip_premium = flip_premium_base + 0.005 * max(0, flip_count - 2)
        spread_est = -1.0
        if clob_ws:
            bba = clob_ws.best_bid_ask.get(token_id, {})
            bba_ts = float(bba.get("ts", 0) or 0)
            if bba_ts > 0 and (time.time() - bba_ts) <= _WS_STALE_S:
                try:
                    spread_est = float(bba.get("spread", -1)) if bba.get("spread") else -1.0
                except (TypeError, ValueError):
                    spread_est = -1.0
        # Real round-trip cost: full `spread` (half-spread crossed each leg) plus
        # fee impact on both legs (fee_rate × p × (1-p), max ~1.75% at ATM).
        side_price = price_up if side == "Up" else price_down
        if spread_est >= 0:
            fee_impact_one_leg = DEFAULT_FEE_RATE * side_price * (1.0 - side_price)
            spread_cost = spread_est + 2.0 * fee_impact_one_leg
        else:
            spread_cost = flip_premium
        flip_hurdle = signal_engine.min_edge + max(flip_premium, spread_cost)
        if signal.edge < flip_hurdle:
            _record_skip("flip_insufficient_edge")
            _ghost("flip_insufficient_edge", signal, {})
            return None, last_eval_log_window

    price = price_up if side == "Up" else price_down
    if not bankroll:
        bankroll = await db.get_bankroll()
    kelly_mult = breaker.kelly_multiplier if breaker else 1.0


    raw_kelly_size = bankroll * signal.kelly_size
    size = round(raw_kelly_size * kelly_mult * time_mult, 2)

    size = round(size * adverse_kelly_mult, 2)

    open_positions = await _get_open_positions_cached(db)
    active_positions = [p for p in open_positions if p.get("status") == "open"]
    if active_positions:
        cc_mult = concurrent_multiplier(side, cid, active_positions)
        size = round(size * cc_mult, 2)

    # Total-deployment cap (across all open positions) stays at the single-trade level
    # as a defensive clip; base.py also enforces it at the trader layer.
    if size > bankroll * max_bankroll_pct:
        size = round(bankroll * max_bankroll_pct, 2)

    # Book-depth fill cap. The upstream thin-CLOB gate passes if EITHER side has
    # depth ≥ min, so the chosen side can still be the empty leg of a one-sided
    # book — explicit skip rather than a full-Kelly order into 0 liquidity.
    side_depth = depth_usd_up if side == "Up" else depth_usd_down
    max_fill_pct = config.get("execution", {}).get("max_book_fill_pct", 0.50)
    # Same floor as the upstream both-sides-thin gate so the two can't drift apart.
    min_side_depth = market_scanner.min_book_depth_usd
    if side_depth < min_side_depth:
        _record_skip("thin_book_depth")
        _emit_gate_skip(cid, "thin_book_depth",
                        f"chosen side {side} depth ${side_depth:.0f} < ${min_side_depth:.0f}")
        return None, last_eval_log_window
    max_fill = side_depth * max_fill_pct
    if size > max_fill:
        # side_depth ≥ $50 is enforced above, so max_fill sits well above the $1
        # CLOB floor; the min_size gate below handles any residual sub-$1 size.
        size = round(max_fill, 2)

    # Net-edge gate: reject if slippage eats the edge below threshold.
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    est_slip = slippage_pct(size, side_depth, impact)
    net_edge = signal.edge - price * est_slip
    if net_edge < signal_engine.min_edge:
        _record_skip("net_edge_after_slippage")
        _ghost("net_edge_after_slippage", signal, {})
        _emit_gate_skip(cid, "net_edge_slippage", f"net edge {net_edge:+.1%} after {est_slip:.2%} slippage")
        return None, last_eval_log_window

    # Final min-size check after all caps: Polymarket's CLOB rejects orders below
    # $1 notional. Paper mirrors the floor so the backtest sample matches live.
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

    # Entries deliberately use a tight slip (no FOK-cross floor): an FOK reject on
    # adverse movement is a feature — it stops buying post-reversal tops. Exits use
    # a loose floor instead (see exit_fill) — there we must fill to avoid lockout.
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    slip = slippage_pct(size, side_depth, impact)
    price = market_scanner.snap_to_tick(price * (1 + slip), tick_size)

    if hasattr(trader, "warm_buy_signature"):
        asyncio.create_task(trader.warm_buy_signature(
            token_id, size, price, fee_rate=fee_rate,
        ))

    snapshot = indicator_engine.get_snapshot(indicators)
    # Last two closes let the L6 backtest reconstruct `last_return` for
    # autocorr_signed_mag — without them the feature is dormant in replay.
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
        # Kept for record-schema continuity — L1 prob is uncalibrated, so raw == prob.
        "model_probability_raw": (
            signal_engine.last_raw_prob_up if side == "Up"
            else 1.0 - signal_engine.last_raw_prob_up
        ),
        "edge": signal.edge,
        "atr": indicators.get("atr", {}).get("atr", 0),
        "atr_rolling_20": round(signal_engine.last_atr_rolling_20, 6),
        "atr_long_term_mean": round(signal_engine.last_atr_long_term_mean, 6),
        "size": size,
        "prev_resolution_margin": _prev_resolution_margin,
        # Recorded flow telemetry (no logit consumes these — exit-model features)
        "flow_score": flow_score_rec,
        "spot_flow_signal": spot_flow_rec,
        "regime_autocorr": round(signal_engine.last_regime_autocorr, 4),
        "regime_direction": round(signal_engine.last_regime_direction, 4),
        # Time-of-window classification (used by bias_detector time_patterns + flip analysis)
        "entry_phase": phase,
        "flip_count": flip_count,
        "is_flip": flip_count > 0,
        # Microstructure aux, stamped from the once-per-evaluation `aux_signals`
        # dict (same schema as ghosts). None means "feed cold/stale", never 0.0.
        "depth_usd_top20": depth_feed.get_depth_usd() if depth_feed else 0,
        **aux_signals,
        # Adverse-selection diagnostic — 30s is the post-fill checkpoint, not the
        # lookback (that's the monitor's 30-minute window).
        "adverse_rate_at_30s": adverse_rate_at_30s if adverse_rate_at_30s >= 0 else 0.5,
        "adverse_kelly_mult": round(adverse_kelly_mult, 3),
        # Token IDs for both outcomes — required for startup reconciliation and dust sweeping.
        "token_id_up": contract.get("token_id_up", ""),
        "token_id_down": contract.get("token_id_down", ""),
    }
    # Pre-submit edge re-check: walk the ask ladder for the actual expected FOK
    # VWAP (the book is ground truth vs the modeled slip). Book unavailable/too
    # thin → fall back to the BBA-only fresh_ask gate, so this never tightens a
    # path the BBA gate would have passed.
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
        # Drop the open-positions cache so the next tick sees this position immediately.
        _invalidate_open_positions_cache()
        if _window_recorder is not None:
            _window_recorder.mark_traded(cid)
        # Actual fill price (paper latency/book-walk or live FOK slippage may differ).
        fill_price = result.fill_price if result.fill_price > 0 else price
        slip_note = f"  [filled @ {fill_price:.3f} vs signal {price:.3f}]" if abs(fill_price - price) > 0.001 else ""

        shares_ordered = size / fill_price
        fee_shares = entry_fee_shares(shares_ordered, fill_price, fee_rate)
        fee_usd = fee_shares * fill_price
        bankroll_now = await db.get_bankroll()
        _dist = btc_price - strike
        _why_parts = []
        if side == "Up":
            _why_parts.append(f"BTC ${abs(_dist):,.0f} {'above' if _dist > 0 else 'below'} strike — {'Favors Up' if _dist > 0 else 'fighting strike'}")
        else:
            _why_parts.append(f"BTC ${abs(_dist):,.0f} {'below' if _dist < 0 else 'above'} strike — {'Favors Down' if _dist < 0 else 'fighting strike'}")
        if flow_score > 0.1:
            _why_parts.append(f"Strong buy pressure in book (flow {flow_score:+.2f})")
        elif flow_score < -0.1:
            _why_parts.append(f"Strong sell pressure in book (flow {flow_score:+.2f})")
        else:
            _why_parts.append(f"neutral book flow ({flow_score:+.2f})")
        if spot_flow_signal > 0.05:
            _why_parts.append(f"Buyers dominating on Binance (cvd {spot_flow_signal:+.2f})")
        elif spot_flow_signal < -0.05:
            _why_parts.append(f"Sellers dominating on Binance (cvd {spot_flow_signal:+.2f})")
        else:
            _why_parts.append(f"Neutral CVD ({spot_flow_signal:+.2f})")
        _why = ", ".join(_why_parts)
        logger.info(
            f"{_C.YELLOW}{'=' * 60}{_C.RESET}\n"
            f"  {_C.YELLOW}{_C.BOLD}OPEN {side}{_C.RESET}  @ {fill_price:.3f}  |  ${size:.2f}  |  fee ${fee_usd:.2f}{slip_note}  |  "
            f"{_slug_to_window(cid)}{'' if phase == 'normal' else f' [{phase}]'}\n"
            f"  {_C.DIM}Why: {_why}{_C.RESET}\n"
            f"  {_C.DIM}Bankroll ${bankroll_now:.2f}  |  {signal.reason}{_C.RESET}\n"
            f"{_C.YELLOW}{'=' * 60}{_C.RESET}")
        if _adverse_monitor:
            # Baseline must live on the same axis as the post-fill checkpoints
            # (update_prices): the traded token's own mid. Falls back to the
            # fill price (same axis) when the WS book is stale.
            token_mid = _get_token_midprice(clob_ws)(token_id) if clob_ws else 0.0
            _adverse_monitor.record_fill(side=side, fill_price=fill_price, token_id=token_id,
                                         midprice=token_mid or fill_price,
                                         position_id=result.position_id)
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

    # Gamma's priceToBeat is the authoritative resolution value — override any
    # cached strike once it appears (often mid-window, after Gamma catches up).
    ptb = (contract or {}).get("event_metadata") or {}
    ptb = ptb.get("price_to_beat") if isinstance(ptb, dict) else None
    if ptb and window_strikes.get(contract_window_ts) != ptb:
        if contract_window_ts in window_strikes:
            logger.info(f"STRIKE UPDATE {_slug_to_window(cid)} | ${window_strikes[contract_window_ts]:,.2f} → ${ptb:,.2f} (Polymarket)")
        else:
            logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${ptb:,.2f} (Polymarket){_C.RESET}")
        window_strikes[contract_window_ts] = ptb

    if contract_window_ts not in window_strikes:
        if chainlink_feed:
            cl_strike = chainlink_feed.get_strike(contract_window_ts)
            if cl_strike:
                window_strikes[contract_window_ts] = cl_strike
                logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${cl_strike:,.2f} (Chainlink){_C.RESET}")

    window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}

    strike = window_strikes.get(contract_window_ts, 0)
    if strike <= 0:
        buf_len = len(binance_feed.buffer) if binance_feed.buffer else 0
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL {_slug_to_window(cid)}: No strike yet — buffer has {buf_len} candles")
        return None, None, window_strikes, last_eval_log_window, "none"

    # BTC price comes from Coinbase WS only (the venue Chainlink resolves against);
    # a stale Coinbase feed returns 0 here and skips the decision.
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

    # Entry prices derive from the direct CLOB best_ask (what a FOK actually pays),
    # NOT the /price cross-matched API, which can return phantom executable prices.
    book_up, book_down = await asyncio.gather(
        _get_book(ws_book_up, token_up),
        _get_book(ws_book_down, token_down),
    )

    # Stale BBA entries are treated as missing so we fall through to the
    # freshly-fetched book or Gamma fallback.
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

    # Per-token freshness gate: one side stale (yet under _WS_STALE_S) would make
    # the price_sum check reject valid markets when skew, not no-arb, is the culprit.
    if (
        price_source == "clob"
        and clob_ws is not None
        and not clob_ws.both_books_fresh(token_up, token_down, _WS_STALE_S)
    ):
        _record_skip("book_freshness_skew")
        return None, last_eval_log_window

    # Price sanity gate: best_ask + best_ask naturally exceeds 1.00 by the full
    # spread. ±2% accommodates normal 1-4 cent spreads; tighter thresholds reject
    # valid markets every tick.
    price_sum = price_up + price_down
    if price_source == "clob" and (price_sum < 0.98 or price_sum > 1.02):
        _record_skip("stale_prices")
        _log_price_sum_outlier(
            contract.get("slug", contract.get("market_id", "")),
            price_up, price_down,
            float(book_up.get("asks", [{}])[0].get("size", 0) or 0) if book_up.get("asks") else 0.0,
            float(book_down.get("asks", [{}])[0].get("size", 0) or 0) if book_down.get("asks") else 0.0,
        )
        eval_window = int(now_ts // 300) * 300
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.debug(f"EVAL: stale prices | Up={price_up:.2f} + Dn={price_down:.2f} = {price_sum:.2f} — skipping")
        return None, last_eval_log_window

    eval_window = int(now_ts // 300) * 300

    depth_usd_up = depth_up * ask_up if ask_up > 0 else 0
    depth_usd_down = depth_down * ask_down if ask_down > 0 else 0

    if price_source == "clob":
        min_depth = market_scanner.min_book_depth_usd
        if depth_usd_up < min_depth and depth_usd_down < min_depth:
            _record_skip("thin_clob_depth")
            if eval_window != last_eval_log_window:
                last_eval_log_window = eval_window
                _cid = contract.get("slug", contract.get("market_id", ""))
                logger.info(f"EVAL {_slug_to_window(_cid)}: Thin CLOB depth — Up=${depth_usd_up:.0f} Dn=${depth_usd_down:.0f}, skipping window")
            return None, last_eval_log_window

    # Effective execution cost must clear max_spread on EITHER side — we don't yet
    # know which side we'll trade.
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
        if spread_val < 0:
            # Fail closed: both the WS BBO and the REST /spread fallback failed —
            # without a spread there is no execution-cost check, so skip the tick
            # rather than waive the only cost-vs-max_spread gate.
            _record_skip("spread_unavailable")
            logger.debug("Spread unavailable from WS + REST — skipping tick (fail-closed)")
            return None, last_eval_log_window
        # Half-spread above mid + the EFFECTIVE peak taker fee (flat per-share
        # proxy, NOT the raw coefficient). Gate stays max_spread — accounts for the
        # fee-eaten portion without tightening into illiquid markets.
        effective_cost = spread_val * 0.5 + EFFECTIVE_FEE_PEAK
        if effective_cost > max_spread:
            _record_skip("spread_too_wide")
            logger.debug(
                f"Effective exec cost {effective_cost:.3f} (spread/2={spread_val/2:.3f} + fee={EFFECTIVE_FEE_PEAK:.3f}) "
                f"> {max_spread:.3f} — skipping"
            )
            return None, last_eval_log_window

    return {
        "price_up": price_up, "price_down": price_down, "price_source": price_source,
        "book_up": book_up, "book_down": book_down,
        "depth_usd_up": depth_usd_up, "depth_usd_down": depth_usd_down,
        "eval_window": eval_window,
    }, last_eval_log_window


async def _discover_contract_and_subscribe(market_scanner: Any,
                                           ws_subscribed_tokens: list[str],
                                           clob_ws: Any,
                                           prev_contract_tokens: list[str] | None = None,
                                           db: Any = None,
                                           http_client: Any = None,
                                           ) -> tuple[dict[str, Any] | None, str | None, list[str], list[str]]:
    """Find an active contract and subscribe its WebSocket tokens. Returns (contract, cid, subscribed_tokens, prev_tokens)."""
    if prev_contract_tokens is None:
        prev_contract_tokens = []
    contract = await market_scanner.find_active_contract(http_client=http_client)
    if not contract:
        return None, None, ws_subscribed_tokens, prev_contract_tokens

    cid = contract["slug"]  # Use slug as market_id — US API needs marketSlug, not condition_id

    # On first entry into a window, defer to DB to avoid duplicate-position
    # races; on subsequent flips we know the previous position scalped clean.
    state = _window_flip_state.get(cid, {})
    flip_count = state.get("flip_count", 0)
    if flip_count == 0 and db is not None and await db.has_position_for_market(cid):
        return None, None, ws_subscribed_tokens, prev_contract_tokens

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

    # Pre-warm tick_size cache so the entry path avoids ~30-100ms of HTTP latency
    # right before order submit (1-hour TTL outlives the 5-minute window).
    if http_client and market_scanner and current_tokens:
        await asyncio.gather(
            *[market_scanner.fetch_tick_size(t, http_client) for t in current_tokens],
            return_exceptions=True,
        )

    return contract, cid, ws_subscribed_tokens, current_tokens


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
        ghost_tracker.check_resolutions(event_metadata=cf_event_metadata)


async def _evaluate_and_exit_position(
        pos: dict[str, Any], live: dict[str, Any], binance_feed: Any,
        indicator_engine: Any, signal_engine: Any, market_scanner: Any,
        http_client: Any, clob_ws: Any, trader: Any, alert_manager: Any, db: Any,
        outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        config: dict[str, Any], scheduler: Any, default_exit_threshold: float,
        day_wins: int, day_losses: int, day_fees: float,
        depth_feed: Any = None, trades_feed: Any = None,
        coinbase_feed: Any = None,
        chainlink_feed: Any = None) -> tuple[int, int, float]:
    """Re-evaluate an active position and exit (scalp) if holding edge is gone."""
    # Too-small-position deferral happens at the scalp step, NOT here — abandoned
    # positions keep being monitored and resume scalping if the bid recovers ≥ $1.
    # Stale Coinbase → btc_now 0 → HOLD without scalping (acting on a stale BTC
    # produced the "moved against us (2%)" pathology mid-window).
    btc_now, _btc_src = _fastest_btc_price(coinbase_feed, trades_feed, binance_feed)
    if btc_now <= 0:
        _log_hold_heartbeat_stale(pos, live, "no fresh BTC price")
        return day_wins, day_losses, day_fees

    # Mirrors the entry-path staleness gate (CLAUDE.md §3 thresholds); kline >45s
    # catches a stale indicator/ATR buffer.
    _stale: list[str] = []
    if coinbase_feed and coinbase_feed.state.age_seconds > 30:
        _stale.append(f"coinbase={coinbase_feed.state.age_seconds:.0f}s")
    if chainlink_feed and chainlink_feed.age_seconds > 60:
        _stale.append(f"chainlink={chainlink_feed.age_seconds:.0f}s")
    if trades_feed is not None and trades_feed.accumulator is not None:
        _agg_age = trades_feed.accumulator.latest_age_s
        if _agg_age > 30:
            _stale.append(f"binance_aggtrade={_agg_age:.0f}s")
    _candle_age = binance_feed.buffer.latest_age_s if binance_feed and binance_feed.buffer else float("inf")
    if _candle_age > 45:
        _stale.append(f"binance_kline={_candle_age:.0f}s")
    # Loss-cut math (BTC vs strike + ATR) is independent of L3b/Chainlink; only
    # candle staleness corrupts ATR. Under non-critical staleness evaluate_hold
    # still fires so loss-cut can protect — any non-loss-cut EXIT is reverted below.
    scalp_gated_by_stale = False
    if _stale:
        if any("kline" in s for s in _stale):
            _log_hold_heartbeat_stale(pos, live, "stale feeds — " + ", ".join(_stale))
            return day_wins, day_losses, day_fees
        scalp_gated_by_stale = True

    # Get strike from the position's stored trade_context (correct for this contract)
    pos_ctx = json.loads(pos.get("indicator_snapshot", "{}")).get("trade_context", {})
    strike_now = pos_ctx.get("strike_price", 0)
    if strike_now <= 0:
        return day_wins, day_losses, day_fees

    indicators = indicator_engine.compute_all(binance_feed.buffer)

    # Hold/scalp decisions use the CLOB WS best_bid (what a SELL FOK receives) —
    # never the /price cross-matched API, which can spike to phantom values near expiry.
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
        return day_wins, day_losses, day_fees

    market_price = ws_bid

    exit_threshold = (scheduler._exit_edge_threshold if scheduler and scheduler._exit_edge_threshold is not None
                      else default_exit_threshold)
    closes = binance_feed.buffer.get_closes()

    hold_trades_up = clob_ws.get_trade_history(live.get("token_id_up", "")) if clob_ws else []
    hold_trades_down = clob_ws.get_trade_history(live.get("token_id_down", "")) if clob_ws else []
    hold_flow = compute_flow_signal(
        clob_ws.get_book(live.get("token_id_up", "")) if clob_ws else {},
        clob_ws.get_book(live.get("token_id_down", "")) if clob_ws else {},
        hold_trades_up, hold_trades_down,
    )

    # Same L3b helper used at entry — identical math via aux_layers.
    _hold_aux_local = _build_aux_signals(coinbase_feed, trades_feed)
    _hold_vol_factor = regime_vol_factor(
        indicators.get("atr", {}).get("atr", 0.0), signal_engine.last_atr_long_term_mean)
    hold_spot_flow = compute_spot_flow_signal(
        _hold_aux_local.get("coinbase_cvd_60s"),
        _hold_aux_local.get("coinbase_taker_60s"),
        _hold_aux_local.get("coinbase_taker_n", 0),
        vol_factor=_hold_vol_factor,
    )
    action, model_prob, holding_edge, reason = signal_engine.evaluate_hold(
        indicators, btc_now, strike_now, live["seconds_remaining"],
        market_price, pos["side"], exit_threshold,
        entry_price=pos["entry_price"],
        fee_rate=pos.get("fee_rate") or DEFAULT_FEE_RATE,
        closes=closes,
        market_mid_for_side=market_mid)
    _lc_evt = getattr(signal_engine, "last_loss_cut_event", "")
    if _lc_evt == "fired":
        _record_skip("loss_cut_fired")
    elif _lc_evt == "whipsaw_blocked":
        _record_skip("loss_cut_whipsaw_blocked")

    # Under non-critical staleness, only loss-cut is safe — the scalp-band signals
    # were computed against degraded layers. Demote any other EXIT to HOLD so a
    # stale-driven scalp can't slip through.
    if scalp_gated_by_stale and action == "EXIT" and not reason.startswith("cutting loss"):
        _log_hold_heartbeat_stale(pos, live, "stale feeds — scalp gated, loss-cut only: " + ", ".join(_stale))
        return day_wins, day_losses, day_fees

    mid = pos["market_id"]

    if action == "HOLD":
        if pos["id"] in _resting_exits:
            _resting_exits.pop(pos["id"], None)
            logger.info(f"  RESTING EXIT cancelled — model back to HOLD on {pos['side']}")
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
            _hold_aux = _build_aux_signals(coinbase_feed, trades_feed)
            counterfactual_tracker.track_hold_moment(pos["market_id"], pos, {
                "holding_edge": holding_edge, "model_prob": model_prob,
                "market_price": market_price, "seconds_remaining": live["seconds_remaining"],
                "exit_threshold": exit_threshold, "strike_price": strike_now,
                "btc_price": btc_now,
                "flow_score": hold_flow.get("flow_score", 0.0),
                "spot_flow_signal": hold_spot_flow,
                "regime": pos_ctx.get("regime_state", "unknown"),
                "btc_distance_atr": round((btc_now - strike_now) / _cf_atr, 3),
            }, aux_signals=_hold_aux)

        # Pre-sign the SELL FOK when a scalp is imminent — saves ~150ms of ECDSA
        # work from the hot path; hasattr guards a trader without warm_sell_signature.
        if (hasattr(trader, 'warm_sell_signature')
                and -0.05 < holding_edge < -0.005):
            _sell_token = (live.get("token_id_up", "") if pos["side"] == "Up"
                           else live.get("token_id_down", ""))
            if _sell_token:
                _shares = pos.get("shares_held") or pos["size"] / pos["entry_price"]
                # Approximate exit_fill = market_price × (1 − 8% cross floor);
                # _take_sell_warmup tolerates ±1¢ drift vs the actual exit_fill.
                _warm_price = round(market_price * 0.92, 4)
                asyncio.create_task(trader.warm_sell_signature(
                    _sell_token, _shares, _warm_price,
                    fee_rate=pos.get("fee_rate") or DEFAULT_FEE_RATE,
                ))

    if action == "EXIT":
        sell_token = live.get("token_id_up", "") if pos["side"] == "Up" else live.get("token_id_down", "")

        # PRICE VERIFICATION: guards a phantom ws best_bid (ts refreshed by an
        # unrelated price_change event). Fast-path: both sides fresh and summing
        # to ~1.0 satisfies no-arb — ws_bid is real, skip the HTTP round-trip.
        other_bba = clob_ws.best_bid_ask.get(other_token, {}) if clob_ws else {}
        other_bid = float(other_bba.get("best_bid", 0) or 0)
        other_age = time.time() - float(other_bba.get("ts", 0) or 0)
        noarb_ok = other_bid > 0 and other_age <= 5 and 0.95 <= ws_bid + other_bid <= 1.05
        verified_price = 0.0
        if not noarb_ok and market_scanner and http_client and sell_token:
            verified_price = await market_scanner.fetch_market_price(sell_token, "SELL", http_client)
        if verified_price > 0 and verified_price < ws_bid * 0.70:
            # ws_bid is phantom — re-evaluate with the real price, gated against the
            # SAME blended threshold evaluate_hold fired on, not the raw config value:
            # else deep-ITM re-checks too strictly and an OTM-urgency position
            # (effective threshold can go positive) re-holds past forced exit.
            effective_exit_threshold = signal_engine.last_effective_exit_threshold
            real_edge = model_prob - verified_price
            if pos["id"] not in _abandoned_scalp_positions:
                logger.info(
                    f"  SCALP VERIFY {pos['side']}  {_fmt_secs(live['seconds_remaining'])}  |  "
                    f"ws_bid={ws_bid:.3f} vs /price={verified_price:.3f} — using real price  "
                    f"real_edge={real_edge:+.0%} thresh={effective_exit_threshold:+.0%}"
                )
            if real_edge > effective_exit_threshold:
                # Real market not bad enough to scalp — hold
                return day_wins, day_losses, day_fees
            market_price = verified_price

        # Sell-side slippage vs available bid depth: book snapshot's bid depth
        # first, WS BBO size as fallback when the snapshot has no bids.
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

        # Polymarket rejects orders below $1 notional — defer (not abandon) so
        # subsequent ticks keep monitoring and resume scalping if the bid recovers;
        # 30s heartbeat mirrors the normal HOLD cadence.
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
            return day_wins, day_losses, day_fees

        # Size recovered above the $1 floor — clear the deferred flag and scalp.
        if pos["id"] in _abandoned_scalp_positions:
            _abandoned_scalp_positions.discard(pos["id"])
            logger.info(
                f"  SCALP RESUMED — position recovered to ${exit_size_usd:.2f}, "
                f"attempting exit"
            )

        # Phase 1 passive exit (two-stage, kill-bar-gated by config): rest a SELL
        # at mid for a few seconds — the late chasers who'd otherwise be our FOK
        # counterparty lift us instead (no taker fee, no half-spread crossed) —
        # then fall back to the FOK. Loss-cuts always go straight to FOK.
        result = None
        _exec_cfg = config.get("execution", {})
        _is_loss_cut = getattr(signal_engine, "last_loss_cut_event", "") == "fired"
        if _is_loss_cut:
            _resting_exits.pop(pos["id"], None)
        elif (_exec_cfg.get("passive_exit_enabled", False)
                and getattr(trader, "supports_passive_exit", False)):
            _timeout = float(_exec_cfg.get("passive_exit_timeout_s", 10.0))
            _st = _resting_exits.get(pos["id"])
            if _st is None:
                if ws_ask > ws_bid > 0 and live["seconds_remaining"] > _timeout + 5.0:
                    _level = min(round(market_mid + 1e-9, 2), round(ws_ask, 2))
                    _level = max(_level, round(ws_bid + 0.01, 2))
                    if len(_resting_exits) > 50:  # lazy sweep of long-dead entries
                        _cut = time.time() - 600
                        for _pid in [k for k, v in _resting_exits.items() if v["deadline"] < _cut]:
                            _resting_exits.pop(_pid, None)
                    _resting_exits[pos["id"]] = {
                        "token": sell_token, "level": _level,
                        "posted_ts": time.time(), "deadline": time.time() + _timeout,
                    }
                    logger.info(
                        f"  RESTING EXIT {pos['side']}  @ {_level:.2f} "
                        f"(bid {ws_bid:.2f}/ask {ws_ask:.2f})  {_timeout:.0f}s then FOK")
                    return day_wins, day_losses, day_fees
                # no usable two-sided quote / window too close — straight to FOK
            else:
                _fill = _resting_fill_price(
                    clob_ws.trades_since(_st["token"], _st["posted_ts"]) if clob_ws else [],
                    _st["level"], _st["posted_ts"])
                if _fill is not None:
                    _resting_exits.pop(pos["id"], None)
                    result = await trader.close_trade(
                        pos["id"], _fill, token_id=sell_token, position=pos,
                        maker_fill=True)
                    if result.success:
                        logger.info(f"  PASSIVE FILL — lifted at {_fill:.2f} (maker, no taker fee)")
                    else:
                        result = None  # fall back to FOK below
                elif time.time() >= _st["deadline"]:
                    _resting_exits.pop(pos["id"], None)  # timeout -> FOK below
                else:
                    return day_wins, day_losses, day_fees  # still resting

        if result is None:
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
                # Race: size was >= $1 at the pre-check but dropped by order time —
                # defer, monitor, retry next tick.
                _abandoned_scalp_positions.add(pos["id"])
                logger.info(
                    f"  SCALP DEFERRED — order rejected by CLOB minimum (${exit_size_usd:.2f}), "
                    f"monitoring for recovery"
                )
                return day_wins, day_losses, day_fees
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
                    question=pos.get("question", ""), exit_price=exit_fill,
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
            # A successful scalp arms the flip hurdle for this window's re-entries.
            fs = _window_flip_state.setdefault(pos["market_id"], {"flip_count": 0})
            fs["flip_count"] += 1

            if counterfactual_tracker:
                _cf_atr2 = indicators.get("atr", {}).get("atr", 1.0) or 1.0
                _cf_aux = _build_aux_signals(coinbase_feed, trades_feed)
                counterfactual_tracker.watch(pos, {
                    "exit_fill": exit_fill, "pnl": pnl, "gain_pct": gain_pct,
                    "holding_edge": holding_edge, "model_prob": model_prob,
                    "market_price": market_price, "seconds_remaining": live["seconds_remaining"],
                    "exit_threshold": exit_threshold, "strike_price": strike_now,
                    # Threshold the scalp actually fired on + whether this close was a
                    # loss-cut (threshold-independent) — the exit-threshold replay needs
                    # both to score candidates against live's real fire criterion.
                    "effective_exit_threshold": getattr(signal_engine, "last_effective_exit_threshold", None),
                    "loss_cut": getattr(signal_engine, "last_loss_cut_event", "") == "fired",
                    "btc_price": btc_now,
                    "flow_score": hold_flow.get("flow_score", 0.0),
                    "spot_flow_signal": hold_spot_flow,
                    "regime": pos_ctx.get("regime_state", "unknown"),
                    "btc_distance_atr": round((btc_now - strike_now) / _cf_atr2, 3),
                }, aux_signals=_cf_aux)

    return day_wins, day_losses, day_fees


def _resolved_exit_price(live: dict[str, Any], side: str) -> tuple[float | None, str | None]:
    """Decide a resolved position's binary exit price from current market state.

    Returns ``(exit_price, oracle_log)``: ``exit_price`` is the binary payoff
    (1.0 winner / 0.0 loser) or ``None`` when the window hasn't resolved yet
    (caller keeps waiting); ``oracle_log`` is a human log fragment when the
    Chainlink oracle decided, else ``None``.

    Source priority matches §8 (Chainlink is the source of truth, never Binance):
      1. ``event_metadata`` (final_price vs price_to_beat) — the Chainlink oracle.
      2. A *coherent* resolved CLOB book (``closed``, prices sum ~1, one side at an
         extreme), paid the exact binary 1.0/0.0 — zero taker fee at the extreme.
         An incoherent book (stale/phantom print) is rejected so a winning side
         can't mis-resolve; the caller falls through to the oracle/orphan path.
    """
    if not live:
        return None, None
    meta = live.get("event_metadata") or {}
    final_price = meta.get("final_price")
    strike = meta.get("price_to_beat")
    if final_price is not None and strike is not None:
        up_won = final_price >= strike
        # Cross-check: if the CLOB book has ALSO clearly resolved, surface any
        # disagreement with the Chainlink oracle (a feed-health signal). The oracle
        # still decides — this only logs.
        pu = live.get("price_up")
        if pu is not None and (pu >= 0.99 or pu <= 0.01) and (pu >= 0.5) != up_won:
            logger.warning(
                "RESOLVE disagreement: oracle says %s (final %.2f vs strike %.2f) but CLOB "
                "book implies %s (price_up=%.3f) — trusting oracle",
                "Up" if up_won else "Down", final_price, strike,
                "Up" if pu >= 0.5 else "Down", pu,
            )
        exit_price = 1.0 if (side == "Up") == up_won else 0.0
        return exit_price, (f"Strike {strike:,.2f} → Final {final_price:,.2f} "
                            f"— {'Up' if up_won else 'Down'} wins")
    price_up = live.get("price_up")
    price_down = live.get("price_down")
    if (live.get("closed") and price_up is not None and price_down is not None
            and 0.98 <= price_up + price_down <= 1.02
            and (price_up >= 0.99 or price_up <= 0.01)):
        up_won = price_up >= 0.5
        exit_price = 1.0 if (side == "Up") == up_won else 0.0
        return exit_price, None
    return None, None


async def _resolve_expired_position(
        pos: dict[str, Any], live: dict[str, Any], trader: Any, alert_manager: Any,
        db: Any, outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        day_wins: int, day_losses: int, day_fees: float,
        signal_engine: Any = None) -> tuple[bool, int, int, float]:
    """Resolve a position whose contract has expired (seconds_remaining <= 0)."""
    global _prev_resolution_margin
    # Chainlink oracle first (authoritative), then a coherent resolved CLOB book.
    exit_price, resolve_log = _resolved_exit_price(live, pos["side"])
    if exit_price is None:
        # Window hasn't resolved yet — wait for the next tick (polls every 2s).
        now_ts = time.time()
        mid = pos["market_id"]
        if mid not in _last_resolve_wait_log:
            _last_resolve_wait_log[mid] = now_ts
            logger.info(f"Waiting for resolution — {_slug_to_window(mid)}")
        return False, day_wins, day_losses, day_fees
    if resolve_log:
        logger.info(f"RESOLVE {_slug_to_window(pos['market_id'])} | {resolve_log}")

    result = await trader.resolve_position(pos["id"], exit_price)
    if result.pending:
        # Winning redeem hasn't landed on-chain yet — retry next tick.
        return False, day_wins, day_losses, day_fees
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
                question=pos.get("question", ""), exit_price=exit_price,
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
                pos["market_id"], exit_price, pnl, gain_pct, position_id=pos["id"])
        # Track resolution margin (final - strike) for next window's L5 carry —
        # from event_metadata regardless of which branch above set exit_price.
        meta = live.get("event_metadata")
        if meta and meta.get("final_price") is not None and meta.get("price_to_beat") is not None:
            _prev_resolution_margin = meta["final_price"] - meta["price_to_beat"]
            # Defer disk writes off the resolution path — pipeline reads happen
            # at ≥ 5-minute granularity, well beyond any background-task delay.
            asyncio.create_task(asyncio.to_thread(_save_prev_resolution_margin, _prev_resolution_margin))
    return True, day_wins, day_losses, day_fees


async def _manage_orphaned_position(
        pos: dict[str, Any], market_scanner: Any, http_client: Any, trader: Any,
        alert_manager: Any, db: Any, outcome_reviewer: Any, breaker: Any,
        day_wins: int, day_losses: int, day_fees: float,
        signal_engine: Any = None,
        chainlink_feed: Any = None) -> tuple[bool, int, int, float]:
    """Resolve positions where the contract can no longer be found via Gamma API."""
    from datetime import datetime, timezone
    global _prev_resolution_margin

    try:
        entry_dt = datetime.fromisoformat(pos.get("entry_timestamp", ""))
        age = (datetime.now(timezone.utc) - entry_dt).total_seconds()
    except (ValueError, TypeError):
        age = 0
    if age < 600:
        return True, day_wins, day_losses, day_fees  # too young, skip
    # Track (final_price, strike) for L5 carry — populated by whichever branch
    # below has the data. Saved at the end alongside resolve_position.
    resolved_final: float | None = None
    resolved_strike: float | None = None
    # Try direct Gamma fetch for eventMetadata (Chainlink oracle)
    direct = await _get_contract_prices(market_scanner, pos["market_id"], http_client)
    direct_price, direct_log = _resolved_exit_price(direct, pos["side"]) if direct else (None, None)
    if direct_price is not None:
        exit_price = direct_price
        meta = direct.get("event_metadata") or {}
        if meta.get("final_price") is not None and meta.get("price_to_beat") is not None:
            resolved_final = meta.get("final_price")
            resolved_strike = meta.get("price_to_beat")
        logger.info(f"RESOLVE orphan {_slug_to_window(pos['market_id'])} | "
                    f"{direct_log or 'coherent CLOB book'}")
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
            return True, day_wins, day_losses, day_fees
        # Compare strike (Chainlink at window_ts) vs final (Chainlink at window_ts+300),
        # matching Polymarket's own resolution rule. Falling back to the current price
        # would mis-classify when BTC has moved since expiry; the 2hr eviction window
        # in chainlink_feed keeps the expiry capture available for orphan fallback.
        final_at_expiry = chainlink_feed.get_strike(window_ts + 300) if window_ts else None
        if final_at_expiry is not None and final_at_expiry > 0:
            final_price = final_at_expiry
            final_source = "expiry boundary"
        else:
            final_price = chainlink_feed.price
            final_source = "current (expiry capture missing)"
        up_won = final_price >= strike_at_boundary
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        resolved_final = final_price
        resolved_strike = strike_at_boundary
        logger.warning(
            f"RESOLVE orphan {_slug_to_window(pos['market_id'])} via Chainlink fallback "
            f"(Gamma silent {age:.0f}s) | Strike ${strike_at_boundary:,.2f} → ${final_price:,.2f} "
            f"[{final_source}] — {'Up' if up_won else 'Down'} wins (exit={exit_price})"
        )
        if alert_manager:
            try:
                await alert_manager.send_error(
                    f"Resolved orphaned {pos['market_id']} via Chainlink fallback "
                    f"(Gamma silent for {age:.0f}s, price={final_source}). exit_price={exit_price}"
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
                    question=pos.get("question", ""), exit_price=0,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=0,
                    gain_pct=0, reason="orphaned — awaiting resolution", fees=0)
        else:
            logger.info(f"Orphan {_slug_to_window(pos['market_id'])} (age {age:.0f}s) — Waiting for Gamma resolution")
        return True, day_wins, day_losses, day_fees  # still waiting
    result = await trader.resolve_position(pos["id"], exit_price)
    if result.pending:
        # Winning redeem hasn't landed on-chain yet — retry next tick.
        return False, day_wins, day_losses, day_fees
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
                question=pos.get("question", ""), exit_price=exit_price,
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
        # L5 carry — persist whichever branch (eventMetadata or Chainlink
        # fallback) captured both final_price and strike.
        if resolved_final is not None and resolved_strike is not None:
            _prev_resolution_margin = resolved_final - resolved_strike
            asyncio.create_task(asyncio.to_thread(_save_prev_resolution_margin, _prev_resolution_margin))
    return True, day_wins, day_losses, day_fees


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
                       chainlink_feed: Any = None,
                       coinbase_feed: Any = None) -> None:
    import httpx
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    signal_config = config["signal"]
    max_bankroll_pct = config["execution"]["max_bankroll_deployed"]
    default_exit_threshold = signal_config.get("exit_edge_threshold", -0.10)
    max_spread = config.get("market", {}).get("max_spread", 0.10)

    # Trading schedule in ET (handles EST/EDT automatically)
    sched = config.get("schedule", {})
    sched_start_et = (sched.get("trading_start_hour_et", _d("trading_start_hour_et")), sched.get("trading_start_minute", _d("trading_start_minute")))
    sched_end_et = (sched.get("trading_end_hour_et", _d("trading_end_hour_et")), sched.get("trading_end_minute", _d("trading_end_minute")))

    window_strikes: dict[int, float] = {}      # window_ts -> BTC price at window open
    ws_subscribed_tokens: list[str] = []       # currently subscribed token_ids
    last_eval_log_window: int = 0              # track which window we last logged eval for
    prev_contract_tokens: list[str] = []       # tokens from previous contract (for unsubscribe)

    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=5,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60),
        )

    # Day tracking for open/close banners.
    # At the scheduled ~12:01 AM ET restart (12:00-12:30 window), start fresh at 0W/0L.
    # Only restore from DB on a mid-day restart (trading already happened today).
    from zoneinfo import ZoneInfo
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
    def _f(feed: Any) -> str:
        if feed is None:
            return "--"
        # A feed whose tracker has explicitly reported a dead socket is DOWN;
        # None (no report yet) reads as OK so a slow first connect isn't flagged.
        _state = getattr(getattr(feed, "staleness", None), "connected", None)
        return "DOWN" if _state is False else "OK"
    logger.info(
        f"PolyBot [{_mode_label}] ready  |  Bankroll ${_bankroll:,.2f}  |  "
        f"Today: {day_wins}W/{day_losses}L  |  Model: L1-only (entry = inventory sourcing)"
    )
    logger.info(
        f"Feeds: Coinbase {_f(coinbase_feed)} · Binance {_f(binance_feed)} · "
        f"Chainlink {_f(chainlink_feed)} · "
        f"CLOB WS {'Ready' if clob_ws is not None else 'Disconnected'} · "
        f"Discord {'Connected' if alert_manager is not None else 'Unavailable'}"
    )

    # Closure captures clob_ws once — reused across all book-update ticks.
    _midprice_fn = _get_token_midprice(clob_ws) if clob_ws else None

    while True:
        # Check if scheduler requested shutdown (auto-restart cycle after pipeline)
        if scheduler and getattr(scheduler, '_shutdown_requested', False):
            break

        # Event-driven: react instantly to WebSocket book/resolution updates; short timeout for housekeeping
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
                # Resolve adverse-selection checkpoints every loop tick, not only on
                # book_updated — a WS-quiet token would collapse multiple checkpoints
                # onto the next event. Stale BBAs read 0 mid, never a fresh checkpoint.
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
            live_results = await asyncio.gather(
                *[_get_contract_prices(market_scanner, pos["market_id"], http_client) for pos in positions],
                return_exceptions=True,
            )
            for pos, live in zip(positions, live_results):
                if isinstance(live, Exception):
                    live = None

                if not live:
                    _, day_wins, day_losses, day_fees = \
                        await _manage_orphaned_position(
                            pos, market_scanner, http_client, trader,
                            alert_manager, db, outcome_reviewer, breaker,
                            day_wins, day_losses, day_fees,
                            signal_engine=signal_engine,
                            chainlink_feed=chainlink_feed)
                    continue

                if live["seconds_remaining"] <= 0:
                    # Contract expired — check if Polymarket has resolved it.
                    # Mark as pending so it doesn't block new entries
                    if pos["status"] == "open":
                        await db.mark_pending_resolution(pos["id"])
                    resolved, day_wins, day_losses, day_fees = \
                        await _resolve_expired_position(
                            pos, live, trader, alert_manager, db,
                            outcome_reviewer, breaker, counterfactual_tracker,
                            day_wins, day_losses, day_fees,
                            signal_engine=signal_engine)
                    if not resolved:
                        continue  # Gamma hasn't resolved yet — wait for next tick
                else:
                    day_wins, day_losses, day_fees = \
                        await _evaluate_and_exit_position(
                            pos, live, binance_feed, indicator_engine,
                            signal_engine, market_scanner, http_client,
                            clob_ws, trader, alert_manager, db,
                            outcome_reviewer, breaker, counterfactual_tracker,
                            config, scheduler, default_exit_threshold,
                            day_wins, day_losses, day_fees,
                            depth_feed=depth_feed, trades_feed=trades_feed,
                            coinbase_feed=coinbase_feed,
                            chainlink_feed=chainlink_feed)

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

            contract, cid, ws_subscribed_tokens, prev_contract_tokens = \
                await _discover_contract_and_subscribe(
                    market_scanner, ws_subscribed_tokens, clob_ws,
                    prev_contract_tokens, db=db, http_client=http_client)
            if not contract:
                continue

            # Warm the py-clob market-info cache so the entry FOK signs without ~2
            # sequential REST round-trips; dedups per condition_id (PaperTrader: no-op).
            if hasattr(trader, "prewarm_market_info"):
                asyncio.create_task(trader.prewarm_market_info(contract.get("condition_id", "")))

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

            strike, btc_price, window_strikes, last_eval_log_window, _ = \
                _compute_strike_and_btc(cid, binance_feed, window_strikes,
                                        eval_window, last_eval_log_window,
                                        chainlink_feed=chainlink_feed,
                                        coinbase_feed=coinbase_feed,
                                        trades_feed=trades_feed,
                                        contract=contract)
            if strike is None:
                continue

            current_bankroll = await db.get_bankroll()
            _, last_eval_log_window = await _evaluate_signal_and_enter(
                contract, cid, binance_feed, indicator_engine,
                signal_engine, market_scanner, http_client, clob_ws,
                trader, alert_manager, db, config, breaker,
                price_up, price_down, price_source,
                book_up, book_down, depth_usd_up, depth_usd_down,
                btc_price, strike, eval_window, last_eval_log_window,
                token_up, token_down, signal_config, max_bankroll_pct,
                now_ts, bankroll=current_bankroll,
                depth_feed=depth_feed, trades_feed=trades_feed,
                coinbase_feed=coinbase_feed,
                chainlink_feed=chainlink_feed,
                ghost_tracker=ghost_tracker)

        except AuthError as e:
            # Every subsequent order would fail identically — bail loudly rather than
            # silently skipping entries for hours. run_polybot.ps1 keeps looping but
            # won't retry until the next 12:01 AM ET start — fix creds before then.
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
                             "Use only after manual review of memory/state/orphan_positions.json — these shares will not be managed.")
    return parser.parse_args()


async def run_pipeline() -> None:
    """Run the daily learning pipeline once and exit. No trading, no WebSockets."""
    config = load_config()
    base_dir = Path(__file__).parent

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    signal_cfg = config.get("signal", {})
    market_cfg = config.get("market", {})
    sched_cfg = config.get("schedule", {})
    ind_cfg = config.get("indicators", {})

    indicator_params = {
        "atr": {"period": ind_cfg.get("atr", {}).get("period", 14),
                "low_pct": ind_cfg.get("atr", {}).get("low_percentile", 5),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(params=indicator_params)

    signal_engine = _build_signal_engine(signal_cfg, config)

    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    ghost_tracker = GhostTracker(memory_dir=str(base_dir / "memory"))

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
    scheduler = NightlyScheduler(
        outcome_reviewer=outcome_reviewer,
        counterfactual_tracker=counterfactual_tracker,
        ghost_tracker=ghost_tracker,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
        config=config,
    )

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

    # Per-mode DB (polybot_paper.db / polybot_live.db) so flipping paper -> live
    # never inherits stale paper state; memory/ learnings are shared across modes.
    db_path = config["database"]["path"].replace(".db", f"_{mode}.db")

    db = Database(db_path)
    await db.initialize()
    logger.debug(f"Database: {db_path} (mode: {mode})")
    if await db.get_bankroll() == 0:
        await db.set_bankroll(config["execution"]["initial_bankroll"])

    binance_cfg = config.get("binance", {})
    binance_feed = BinanceFeed(
        symbol=binance_cfg.get("symbol", "btcusdt"),
        buffer_size=binance_cfg.get("candle_buffer_size", 200),
        ws_url=binance_cfg.get("ws_url", "wss://stream.binance.com:9443/ws"),
        rest_url=binance_cfg.get("rest_url", "https://api.binance.com/api/v3"),
    )

    market_cfg = config.get("market", {})
    market_scanner = BTCMarketScanner(
        entry_window_seconds=market_cfg.get("entry_window_seconds", 120),
        min_time_remaining=market_cfg.get("min_time_remaining_seconds", 20),
        cache_seconds=market_cfg.get("scan_cache_seconds", 5),
        min_book_depth_usd=market_cfg.get("min_book_depth_usd", 50.0),
        clob_url=market_cfg.get("clob_url"),
    )

    signal_cfg = config.get("signal", {})
    ind_cfg = config.get("indicators", {})
    indicator_params = {
        "atr": {"period": ind_cfg.get("atr", {}).get("period", 14),
                "low_pct": ind_cfg.get("atr", {}).get("low_percentile", 5),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(params=indicator_params)

    signal_engine = _build_signal_engine(signal_cfg, config)

    exec_cfg = config["execution"]
    if mode == "live":
        # Allowance floor: cover at least 10 rounds of max-sized concurrent positions so a
        # revoked or run-down allowance is caught before it silently kills order fills.
        _preflight_bankroll = await db.get_bankroll()
        _kelly_fraction = config.get("math", {}).get("kelly_fraction", _d("kelly_fraction"))
        _max_single = _preflight_bankroll * _kelly_fraction
        _max_concurrent = exec_cfg.get("max_concurrent_positions", _d("max_concurrent_positions"))
        _min_allowance = _max_single * _max_concurrent * 10.0
        ok, msg, live_balance = verify_auth(min_allowance_usd=_min_allowance)
        if not ok:
            logger.error(f"LIVE MODE preflight failed: {msg}")
            return
        logger.debug(f"LIVE MODE — {msg}")
        trader = LiveTrader(db=db,
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
    else:
        trader = PaperTrader(db=db,
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
        wins_to_restore=cb_cfg.get("wins_to_restore", 3),
    )
    # Restore locked_tier from the persisted peak so the floor survives restarts.
    # Compare against breaker.peak_bankroll (seeded from initial_bankroll), not
    # init_bankroll — else a restart below the historical peak silently drops the
    # floor protection (peak $1000, restart at $700 → floor must stay $1000).
    persisted_peak = await db.get_peak_bankroll()
    if persisted_peak is not None and persisted_peak > breaker.peak_bankroll:
        breaker.restore_from_peak(persisted_peak, init_bankroll)
        logger.debug(f"CIRCUIT BREAKER: restored persisted peak ${persisted_peak:,.2f} (current ${init_bankroll:,.2f}, drawdown={breaker.drawdown_pct:.1%})")
    else:
        await db.set_peak_bankroll(init_bankroll)

    agents_cfg = config["agents"]
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    ghost_tracker = GhostTracker(memory_dir=str(base_dir / "memory"))

    # Discord (created before scheduler so alert_manager can be passed in)
    discord_bot = create_bot(db, trader, market_scanner, None, config)
    alert_manager = AlertManager(bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"],
        daily_channel_name=config["discord"].get("daily_channel_name", "polybot-daily"))
    discord_bot.alert_manager = alert_manager

    scheduler = NightlyScheduler(
        outcome_reviewer=outcome_reviewer,
        counterfactual_tracker=counterfactual_tracker,
        ghost_tracker=ghost_tracker,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
        config=config,
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", _d("exit_edge_threshold"))
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    scheduler._auto_shutdown = args.auto_restart
    discord_bot.scheduler = scheduler
    if mode == "live":
        # Sync DB bankroll with real Polymarket balance (fetched during preflight)
        await db.set_bankroll(live_balance)

        # Orphan-position gate runs BEFORE reconcile so the operator sees orphans
        # before any DB mutations happen. OrphanPositionError propagates to the
        # outer handler — it intentionally aborts startup so the operator can
        # inspect memory/state/orphan_positions.json. Pass --allow-orphans after review.
        if hasattr(trader, "detect_orphan_positions"):
            try:
                await trader.detect_orphan_positions(db, allow_orphans=args.allow_orphans)
            except OrphanPositionError:
                raise  # bubble up to the AuthError-style clean-exit handler
            except Exception as e:
                logger.warning(f"Orphan detection failed unexpectedly (non-blocking): {e}")

        try:
            if hasattr(trader, "reconcile_open"):
                # outcome_reviewer + signal_engine let missed-close recovery write a
                # real trade_history row + outcome JSON instead of silently zeroing
                # exit_price; exit_reason "reconcile_recovery_*" allows post-hoc filtering.
                await trader.reconcile_open(
                    db, outcome_reviewer=outcome_reviewer, signal_engine=signal_engine,
                )
            if hasattr(trader, "reconcile_dust"):
                await trader.reconcile_dust(db, max_age_hours=24)
        except Exception as e:
            logger.warning(f"Startup reconciliation failed (non-blocking): {e}")

    clob_ws_url = market_cfg.get("clob_ws_url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    clob_ws = ClobWebSocket(url=clob_ws_url)
    await clob_ws.start()

    # Give the trader access to the CLOB WS (FOK fast-fill path + paper book snapshots)
    if hasattr(trader, "set_clob_ws"):
        trader.set_clob_ws(clob_ws)
    if hasattr(trader, "prewarm_http"):
        await trader.prewarm_http()
    if hasattr(trader, "start_keepalive"):
        await trader.start_keepalive()

    depth_cfg = config.get("binance_depth", {})
    depth_feed = BinanceDepthFeed(
        ws_url=depth_cfg.get("ws_url", "wss://stream.binance.com:9443/ws"),
    )
    trades_cfg = config.get("binance_trades", {})
    trades_accumulator = BinanceTradeAccumulator(max_age_s=trades_cfg.get("max_age_s", 300))
    trades_feed = BinanceTradesFeed(
        accumulator=trades_accumulator,
        ws_url=trades_cfg.get("ws_url", "wss://stream.binance.com:9443/ws"),
    )
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

    # Gate-skip stats load lazily from _record_skip / flush_gate_stats; this just
    # syncs the current-day file to what's on disk.
    _ensure_gate_stats_day_loaded()
    flush_gate_stats()

    global _adverse_monitor
    _adverse_monitor = AdverseSelectionMonitor()

    await scheduler.start()
    await binance_feed.start()
    await depth_feed.start()
    await trades_feed.start()
    await coinbase_feed.start()
    from polybot.feeds.chainlink_feed import ChainlinkFeed
    chainlink_feed = ChainlinkFeed()
    await chainlink_feed.start()

    # Periodic feed-staleness telemetry (P50/P95/P99 inter-arrival per feed).
    _staleness_trackers = [
        binance_feed.staleness,
        depth_feed.staleness,
        trades_feed.staleness,
        coinbase_feed.staleness,
        chainlink_feed.staleness,
        clob_ws.staleness,
    ]
    _staleness_path = FEED_STALENESS_PATH

    async def _flush_staleness_loop() -> None:
        try:
            while True:
                await asyncio.sleep(60.0)
                try:
                    # Gather deque snapshots on the event loop, write in a worker.
                    _snaps = _staleness_snapshot(_staleness_trackers)
                    await asyncio.to_thread(_staleness_write, _snaps, _staleness_path)
                except Exception as e:
                    logger.debug("staleness flush failed: %s", e)
        except asyncio.CancelledError:
            pass

    # Shared HTTP client — lifecycle managed here in main()
    import httpx
    http_client = httpx.AsyncClient(
        timeout=5,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60),
    )

    # Recorders (Phases 1-2): window-path stream for the exit-value model +
    # CLOB tape for the passive-exit shadow sim. Write-behind; never block the loop.
    from polybot.recording import TapeRecorder, WindowPathRecorder
    tape_recorder = TapeRecorder()
    clob_ws.on_trade = tape_recorder.on_trade
    window_recorder = WindowPathRecorder(
        db=db, clob_ws=clob_ws, coinbase_feed=coinbase_feed,
        chainlink_feed=chainlink_feed, market_scanner=market_scanner,
        http_client=http_client)
    global _window_recorder
    _window_recorder = window_recorder

    # Nightly jobs (Phases 3-4): exit-value model refit (data-gated), window-path
    # retention sweep, wallet-fingerprint ingestion + classification.
    from polybot.exit_model import nightly_refit_job, cleanup_job
    from polybot.wallets import nightly_wallet_job
    scheduler.register_job("exit_model_refit", nightly_refit_job(db))
    scheduler.register_job("window_paths_retention", cleanup_job(db))
    scheduler.register_job("wallet_tables", nightly_wallet_job(db, http_client, market_scanner))

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
        chainlink_feed=chainlink_feed, coinbase_feed=coinbase_feed))
    background_tasks = [
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(_flush_staleness_loop()),
        asyncio.create_task(window_recorder.run()),
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
        async def _stop_rec(coro):
            try: await asyncio.wait_for(coro, timeout=2.0)
            except Exception: pass
        await _stop_rec(window_recorder.stop())
        tape_recorder.flush()
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
        await _stop(coinbase_feed.stop())
        await _stop(chainlink_feed.stop())
        await _stop(discord_bot.close())
        bankroll = await db.get_bankroll()
        await db.close()
        logger.info(f"PolyBot stopped — Bankroll ${bankroll:.2f} · Feeds/WS/DB closed")


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
        # Operator-actionable, not a code bug — remediation hint, no stack trace.
        # The orphan gate trips again at every boot until reconciled, so trading
        # stays down even though run_polybot.ps1 restarts at the next 12:01 AM ET.
        import sys as _sys
        _sys.stderr.write(
            "\n" + "=" * 70 + "\n"
            "ORPHAN POSITION GATE TRIPPED\n"
            "=" * 70 + "\n"
            f"{e}\n\n"
            "Next steps:\n"
            "  1) cat polybot/memory/state/orphan_positions.json\n"
            "  2) Manually sweep or resolve any genuine orphan shares on Polymarket\n"
            "  3) Re-run with --allow-orphans to acknowledge known leftover shares\n"
            + "=" * 70 + "\n"
        )
        _sys.exit(2)
