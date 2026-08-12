# polybot/main.py
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import math
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Force UTF-8 on stdout/stderr — Windows cp1252 consoles choke on box-drawing
# chars; errors='replace' survives anything still unrenderable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from polybot.config.loader import load_config, get_secret
from polybot.paths import (
    PREV_MARGIN_PATH, DAY_OPEN_PATH, FEED_STALENESS_PATH, GATE_STATS_PATH,
    GATE_STATS_CURRENT_PATH, PRICE_SUM_OUTLIERS_PATH,
    fold_gate_day, write_json_atomic,
)
from polybot.execution.base import entry_fee_shares, slippage_pct, EFFECTIVE_FEE_PEAK, compute_buy_vwap
from polybot.db.models import Database
from polybot.feeds.market_scanner import BTCMarketScanner
from polybot.feeds.clob_ws import ClobWebSocket
from polybot.core.signal_engine import (
    SignalEngine, TradeSignal, TWAP_MARGIN_MAX, TWAP_MARGIN_P995, twap_margin,
)
from polybot.execution.paper_trader import PaperTrader
from polybot.execution.live_trader import AuthError, LiveTrader, OrphanPositionError, verify_auth
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.scheduler import NightlyScheduler
from polybot.agents.ghost_tracker import GhostTracker
from polybot.discord_bot.bot import create_bot
from polybot.discord_bot.alerts import AlertManager
from polybot.execution.circuit_breaker import CircuitBreaker
from polybot.execution.correlation import concurrent_multiplier
from polybot.feeds._staleness import snapshot_feeds as _staleness_snapshot, write_feeds as _staleness_write

import re
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


from polybot.agents.pipeline_analytics import slug_to_window as _slug_to_window


class _StripAnsiFormatter(logging.Formatter):
    """Strips ANSI color codes so log files stay clean."""
    def format(self, record):
        result = super().format(record)
        return _ANSI_RE.sub('', result)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
# backupCount must be >= 1: RotatingFileHandler never rolls over when it is 0.
_file_handler = logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=2, mode="a", encoding="utf-8")
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
# Stale-while-revalidate: the wake path must NEVER wait on Gamma — an inline
# fetch put a full HTTP RTT in front of the sniper evaluation.
_contract_refresh_inflight: set[str] = set()
_CONTRACT_SERVE_MAX_AGE_S = 900.0  # never serve a cache entry older than this
# Loop-segment marks for the latency breakdown stamped into trade_context.
_loop_marks: dict[str, float] = {}
_WS_STALE_S = 10.0  # max age for CLOB WS BBA/book before treating as stale

# Aux-signal freshness limit: aux trade_context fields stamp None (never 0.0) when
# the source is missing/stale, so "feed cold" stays distinguishable from "real zero".
_AUX_FRESH_S_COINBASE = 10.0
_AUX_FRESH_S_TRADES = 3.0

def _clob_book_aux(clob_ws: Any, token_up: str, token_down: str,
                   book_up: dict[str, Any], book_down: dict[str, Any]) -> dict[str, Any]:
    """Per-side CLOB top-5 ask depth (USD) + book age for the entry trade_context.

    These are the market's own books (depth_usd_top20 is Binance BTC depth).
    None = no book on that side; age is None when a side lacks a timestamped
    WS snapshot (HTTP books carry no ts)."""
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


# ── Regime-Kelly SHADOW stamps ────────────────────────────────────────────────
# Bucket cuts are FROZEN — never re-fit them during the shadow (re-fitting
# invalidates the test). Stamps + nightly arithmetic ONLY: nothing here touches
# sizing, entries, or vetoes; deployment needs the burst SPRT and the
# counterfactual-D SPRT to both accept.
_REGIME_CUTS_ATR_REGIME = (0.694, 1.041)   # atr / atr_long_term_mean
_REGIME_CUTS_ATR_SHORT = (0.789, 1.004)    # atr / atr_rolling_20
_REGIME_CUTS_FRV = (4.1e-5, 6.9e-5)        # fast_realized_vol_60s
_REGIME_BURST_HOT_RATIO = 2.0              # n_ticks_1s / (n_ticks_30s/30)
_REGIME_MULT_TABLE = {"HOT": 1.15, "COLD": 0.80}
_REGIME_MULT_CLAMP = (0.5, 1.5)


# ── Single settled OPEN banner ────────────────────────────────────────────────
# Live prints a short FILLED line at fill and the full OPEN banner ONCE from the
# +8s chain audit — the fill-time number is usually the padded FOK limit, and a
# provisional banner would disagree with the settled books. Log-only; paper
# prints the full banner at fill (its fills are exact).
from collections import OrderedDict as _OrderedDict

_pending_settled_banners: _OrderedDict = _OrderedDict()
_PENDING_BANNERS_MAX = 32


def _realized_entry_fee(ctx: dict[str, Any], entry_price: float,
                        shares: float | None) -> float:
    """Fee actually taken, for the OPEN surfaces.

    With settled chain shares: fee = notional − shares×entry (exactly what the
    wallet paid beyond its shares; $0.00 while Polymarket charges none). Without
    them (paper fill-time = the sim's charged fee; provisional) fall back to
    the model."""
    if shares is not None and entry_price > 0:
        return max(0.0, ctx["size"] - shares * entry_price)
    est_shares = ctx["size"] / entry_price if entry_price > 0 else 0.0
    return entry_fee_shares(est_shares, entry_price, ctx["fee_rate"]) * entry_price


def _log_open_banner(ctx: dict[str, Any], entry_price: float, settled: str,
                     shares: float | None = None) -> None:
    """The yellow OPEN banner. settled: "paper" (fill-time, exact), "chain"
    (audit-confirmed), or "provisional" (chain lookup failed — flagged)."""
    fee_usd = _realized_entry_fee(ctx, entry_price, shares)
    fee_str = (f"fee ~${fee_usd:.2f} (est)" if settled == "provisional"
               else f"fee ${fee_usd:.2f}")
    chase = ctx["posted"] - ctx["signal_ask"]
    if abs(chase) > 0.001 or abs(entry_price - ctx["signal_ask"]) > 0.001:
        slip_note = (f"  [signal {ctx['signal_ask']:.3f} → posted {ctx['posted']:.3f} "
                     f"(+{chase:.3f}) → filled {entry_price:.3f}]")
    else:
        slip_note = ""
    prov = ("  ⚠ provisional — the chain lookup failed, this is the booked price"
            if settled == "provisional" else "")
    _phase = ctx["phase"]
    logger.info(
        f"{_C.YELLOW}{'=' * 60}{_C.RESET}\n"
        f"  {_C.YELLOW}{_C.BOLD}OPEN {ctx['side']}{_C.RESET} @{entry_price:.2f}  ${ctx['size']:.2f}  "
        f"{fee_str}  |  "
        f"{_slug_to_window(ctx['cid'])}{'' if _phase == 'normal' else f' [{_phase}]'}{slip_note}{prov}\n"
        f"  {_C.DIM}strike {ctx['strike']:,.2f} · prob {ctx['prob']:.0%} "
        f"edge {ctx['edge']:+.0%} · bank ${ctx['bankroll']:.2f}{_C.RESET}\n"
        f"{_C.YELLOW}{'=' * 69}{_C.RESET}")


def _on_entry_settled(pos_id: int, final_price: float, source: str,
                      shares: float | None = None) -> None:
    """LiveTrader.on_entry_settled hook — OPEN banner + Discord OPEN ping at the settled entry.

    Both surfaces must agree with the RESOLVED ping and the books.
    Must never raise into the audit."""
    try:
        ctx = _pending_settled_banners.pop(int(pos_id), None)
        if ctx is None:
            return
        provisional = source != "chain"
        _log_open_banner(ctx, final_price,
                         settled=("provisional" if provisional else "chain"),
                         shares=shares)
        am = ctx.get("alert_manager")
        if am is not None:
            fee_usd = _realized_entry_fee(ctx, final_price, shares)
            asyncio.create_task(am.send_trade_opened(
                question=ctx.get("question", ""), side=ctx["side"], size=ctx["size"],
                entry_price=final_price, ev=ctx["edge"], model_prob=ctx["prob"],
                market_price=ctx.get("mkt_price", 0.0), fee=fee_usd,
                bankroll=ctx["bankroll"],
                provisional=provisional))
    except Exception:
        pass


# Outlier-log throttle: one line per market per second — a stuck out-of-band
# window otherwise grows the JSONL unboundedly at tick rate.
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
# TWAP LOCK line once per (window, side) per 10s — the loop re-evaluates on every
# Chainlink report and book event while a lock holds, which spams the same line.
_last_snipe_log: dict[tuple[int, str], float] = {}
_resolve_oracle_logged: set[str] = set()  # market_id — RESOLVE oracle line printed once
_gamma_strikes: set[int] = set()   # window_ts whose strike came from Gamma (sticky)
_tape_verdict_logged: set[str] = set()    # market_id — early TAPE VERDICT printed once
_tape_mismatch_logged: set[str] = set()   # market_id — RESOLUTION DRIFT warned once

# Window-path recorder (recording.WindowPathRecorder) — set by main() at boot.
_window_recorder = None

# Windows whose strike has been logged — one line per window, at the moment the
# Chainlink boundary value LOCKS (suppresses the cold-start settle churn).
_strike_logged: set[int] = set()

# Windows whose strike is TRUSTED for capital: Gamma price_to_beat, or a
# Chainlink boundary capture with no delivery hole (strike_reliable). The sniper
# never fires on an untrusted strike — an RTDS gap can lock a value $35+ off
# Polymarket's (~1-2% of windows), and firing on the wrong strike trades noise.
_strike_trusted: dict[int, bool] = {}

# Previous window's resolution margin — telemetry only (no model reads it).
# None until a resolution is seen — never 0.0, which would read downstream as
# a genuine hairline resolution.
_prev_resolution_margin: float | None = None
_PREV_MARGIN_PATH = PREV_MARGIN_PATH
# Older than this and the margin isn't adjacent to the current window — stamp unknown.
_PREV_MARGIN_STALE_S = 1800  # 30 min ≈ six 5-min windows

def _load_prev_resolution_margin() -> float | None:
    """Restore margin from last session iff written within _PREV_MARGIN_STALE_S."""
    try:
        if _PREV_MARGIN_PATH.exists():
            data = json.loads(_PREV_MARGIN_PATH.read_text())
            if data.get("margin") is None:
                return None
            margin = float(data["margin"])
            saved_at = float(data.get("saved_at", 0.0))
            if saved_at > 0 and (time.time() - saved_at) > _PREV_MARGIN_STALE_S:
                return None
            return margin
    except Exception:
        pass
    return None

def _save_prev_resolution_margin(margin: float) -> None:
    try:
        write_json_atomic(_PREV_MARGIN_PATH, {"margin": margin, "saved_at": time.time()})
    except Exception:
        pass

_current_window_id: str = ""
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

def _pregate_should_eval(now: float, last_eval_ts: float, sec_rem: float,
                         hot: bool, zone_s: float) -> bool:
    """µs pre-gate: does this wake deserve the full 30-80ms evaluation?

    A fire-adjacent wake (near-locked displacement inside the averaging zone)
    ALWAYS evaluates — no dip can be missed. Everything else is throttled
    (4Hz in-zone, 1Hz otherwise): chained full evaluations on every burst
    book-tick were the 392ms queue in front of real signals. Ghost/skip
    records are per-(window, gate) deduped, so throttling changes their
    timestamp by <1s and their content not at all.
    """
    if hot:
        return True
    return (now - last_eval_ts) >= (0.25 if sec_rem <= zone_s else 1.0)


def _twap_hot(chainlink_feed: Any, window_strikes: dict[int, float],
              now: float, zone_s: float) -> bool:
    """µs fire-adjacency check: inside the averaging zone with the projected
    TWAP's displacement at ≥90% of the p99.5 lock margin (0.9× so a borderline
    lock is never throttled past its dip). Cold feed / missing strike / no
    projection all read False — throttle to the slow path, never crash."""
    if chainlink_feed is None:
        return False
    sec_rem = 300.0 - (now % 300.0)
    if sec_rem > zone_s:
        return False
    w_ts = int(now // 300) * 300
    strike = window_strikes.get(w_ts)
    if not strike or strike <= 0:
        return False
    try:
        proj = chainlink_feed.projected_final_twap(w_ts + 300, now=now)
    except Exception:
        return False
    if proj is None:
        return False
    return abs(proj - strike) >= 0.9 * twap_margin(TWAP_MARGIN_P995, sec_rem)


def _fee_breakdown(result: Any) -> str:
    """Close-summary fee string: total with an entry/exit split so the line can't be
    misread as a single charge."""
    entry, exit_ = result.entry_fee_usd, result.exit_fee_usd
    total = entry + exit_
    return f"${total:.2f}  (entry ${entry:.2f} + exit ${exit_:.2f})"


def _emit_gate_skip(cid: str, gate_key: str, reason: str, quiet: bool = False) -> None:
    """Emit one combined SKIP line (signal context + gate reason).

    Throttled per (cid, gate_key) — direction is intentionally NOT in the key, so
    rapid Up/Down ping-pong on the same gate emits one SKIP, not 20× in 5 seconds.
    quiet=True routes to DEBUG (sniper-only mode: base-model skips are unactionable).
    """
    emit = logger.debug if quiet else logger.info
    ctx = _pending_eval_ctx.get(cid)
    if not ctx:
        emit(f"{_C.DIM}SKIP — {reason}{_C.RESET}")
        return
    now = time.time()
    key = (cid, gate_key)
    prev_time = _last_gate_skip_state.get(key)
    if prev_time is not None and (now - prev_time) < 30:
        return
    _lru_set(_last_gate_skip_state, key, now, _GATE_STATE_MAX)
    emit(f"{_C.DIM}SKIP {ctx['direction']} — {reason}{_C.RESET}")

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
        write_json_atomic(GATE_STATS_CURRENT_PATH, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "et_date": _et_date_key(),
            "counts": dict(counts),
            "total_skips": sum(counts.values()),
        })
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
# Per-window flip state — arms the flip hurdle for re-entries
_window_flip_state: dict[str, dict] = {}  # window_id -> {flip_count}

# Lock-informed maker bid manager (execution.maker_bid) — set at boot when
# maker.maker_bid_enabled; module-level like the other fire-path state.
_MAKER_MGR: Any = None

# Killed sniper FOKs this window: window_ts -> side -> [decision asks]. Feeds
# Swept with the _strike_trusted 600s idiom.
_window_killed_asks: dict[int, dict[str, list[float]]] = {}

def _record_killed_ask(cid: str, side: str, ask: float) -> None:
    try:
        wts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return
    now_ts = int(time.time())
    for k in [k for k in _window_killed_asks if now_ts - k >= 600]:
        del _window_killed_asks[k]
    _window_killed_asks.setdefault(wts, {}).setdefault(side, []).append(ask)

# 5-second open-positions cache: correctness comes from event-driven
# invalidation (every open/close/resolve calls _invalidate_open_positions_cache),
# not the TTL — a longer TTL just means fewer DB yields into a burst's backlog.
_open_positions_cache: list = []
_open_positions_cache_ts: float = 0.0

async def _get_open_positions_cached(db: Any) -> list:
    global _open_positions_cache, _open_positions_cache_ts
    now = time.time()
    if now - _open_positions_cache_ts < 5.0:
        return _open_positions_cache
    # Flat book = the firing case: the sync mirror proves there is nothing to
    # fetch, so a cache miss costs 0 yields instead of a DB round-trip that
    # re-enters the loop queue behind a burst's backlog.
    _n = db.open_or_pending_count() if hasattr(db, "open_or_pending_count") else None
    if _n == 0:
        _open_positions_cache = []
    else:
        _open_positions_cache = await db.get_open_positions()
    _open_positions_cache_ts = now
    return _open_positions_cache


def _persist_day_open(day: str, bankroll: float) -> None:
    """Snapshot the ET day's opening bankroll so a mid-day restart reloads it.

    Never reconstruct from (bankroll − trade sum): that drifts when money
    settles on-chain outside recorded trades and poisons the day-close P&L.
    Best-effort: never raises."""
    try:
        write_json_atomic(DAY_OPEN_PATH, {"day": day, "bankroll": bankroll})
    except Exception:
        pass


def _load_day_open(day: str) -> float | None:
    """The persisted opening bankroll for `day`, or None (no/foreign snapshot)."""
    try:
        saved = json.loads(DAY_OPEN_PATH.read_text())
        if saved.get("day") == day:
            return float(saved["bankroll"])
    except Exception:
        pass
    return None


def _invalidate_open_positions_cache() -> None:
    """Force the next positions/bankroll cache read to hit the DB.

    Call after every successful open/close/resolve — concurrent-position math
    and entry gates must see the new state now, not trail the 1s TTL.
    """
    global _open_positions_cache_ts, _bankroll_cache_ts
    _open_positions_cache_ts = 0.0
    _bankroll_cache_ts = 0.0


# 1-second bankroll cache: bankroll only moves on open/close/resolve, which all
# call _invalidate_open_positions_cache — the eval tick never needs a fresh read.
_bankroll_cache: float = 0.0
_bankroll_cache_ts: float = 0.0

async def _get_bankroll_cached(db: Any) -> float:
    global _bankroll_cache, _bankroll_cache_ts
    now = time.time()
    if now - _bankroll_cache_ts < 5.0:
        return _bankroll_cache
    _bankroll_cache = await db.get_bankroll()
    _bankroll_cache_ts = now
    return _bankroll_cache

# Rate-limit counterfactual resolution checks (Gamma REST calls, no need every tick).
_last_cf_check_ts: float = 0.0
_CF_CHECK_INTERVAL = 30.0  # seconds
_cf_check_task: Any = None  # in-flight guard — one background ghost sweep at a time
_bg_refresh_tasks: set = set()  # strong refs — a bare create_task can be GC'd mid-flight


def _spawn_bg(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_refresh_tasks.add(t)
    t.add_done_callback(_bg_refresh_tasks.discard)


async def _refresh_contract_prices(market_scanner: Any, market_id: str, http_client: Any) -> None:
    """Background cache refresh — the wake path serves stale and never waits."""
    try:
        await _fetch_contract_prices(market_scanner, market_id, http_client)
    except Exception as e:
        logger.debug("background contract refresh failed for %s: %s", market_id, e)
    finally:
        _contract_refresh_inflight.discard(market_id)


async def _get_contract_prices(market_scanner: Any, market_id: str, http_client: Any = None) -> dict[str, Any] | None:
    """Current Up/Down contract state, stale-while-revalidate.

    Serves the cache immediately (seconds_remaining recomputed locally),
    refreshing in the background on TTL expiry. Blocks only with no servable
    cache (first call, or older than _CONTRACT_SERVE_MAX_AGE_S).
    """
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
        # Longer TTL while active, shorter near/past expiry (resolution watch)
        ttl = _CONTRACT_RESOLUTION_TTL if contract.get("seconds_remaining", 999) <= 10 else _CONTRACT_CACHE_TTL
        if (now - cache_ts) >= ttl and market_id not in _contract_refresh_inflight:
            _contract_refresh_inflight.add(market_id)
            _spawn_bg(_refresh_contract_prices(market_scanner, market_id, http_client))
        if (now - cache_ts) < _CONTRACT_SERVE_MAX_AGE_S:
            return contract
    return await _fetch_contract_prices(market_scanner, market_id, http_client)


async def _fetch_contract_prices(market_scanner: Any, market_id: str, http_client: Any = None) -> dict[str, Any] | None:
    """Blocking Gamma fetch + cache write (the SWR path's slow half)."""
    import httpx

    now = time.time()
    window_ts = int(time.time() // 300) * 300
    for ts in [window_ts, window_ts + 300, window_ts - 300]:
        slug = market_scanner._make_slug(ts)
        try:
            data = await market_scanner.gamma_events_by_slug(http_client, slug)
            if data:
                contract = market_scanner.parse_contract(data[0])
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
        data = await market_scanner.gamma_events_by_slug(http_client, market_id)
        if data:
            contract = market_scanner.parse_contract(data[0])
            if contract:
                _contract_price_cache[market_id] = (now, contract)
                return contract
    except Exception as e:
        logger.debug(f"Direct slug lookup failed for {market_id}: {e}")

    return None


async def _record_outcome(outcome_reviewer: Any, pos: dict[str, Any], exit_price: float,
                          log_return: float, gain_pct: float,
                          exit_reason: str = "resolution", pnl: float = 0.0,
                          fees: float = 0.0,
                          seconds_remaining_at_exit: float = 0.0) -> None:
    """Persist a resolved/scalped trade outcome for the learning pipeline."""
    edge_decay = None
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
        contract: dict[str, Any], cid: str,
        signal_engine: Any, market_scanner: Any, http_client: Any, clob_ws: Any,
        trader: Any, alert_manager: Any, db: Any, config: dict[str, Any], breaker: Any,
        price_up: float, price_down: float,
        book_up: dict[str, Any], book_down: dict[str, Any],
        depth_usd_up: float, depth_usd_down: float,
        strike: float, eval_window: int, last_eval_log_window: int,
        token_up: str, token_down: str,
        max_bankroll_pct: float,
        bankroll: float = 0.0,
        chainlink_feed: Any = None,
        ghost_tracker: Any = None) -> tuple[str | None, int]:
    """Evaluate the TWAP legs for this window and fire/rest/skip.

    The whole strategy lives here: lock-dip taker + maker placement in the
    final 30s. No model, no
    features — frozen empirical bounds against live asks, then the execution
    gates and the FOK/GTC paths.
    """
    # The raw Chainlink report this evaluation decides on — its delta to the
    # pre-submit moment is the per-fill race meter. Sign + POST legs are
    # recorded in latency_stats.
    _eval_cl_rx = chainlink_feed.last_report_rx if chainlink_feed is not None else 0.0
    _eval_clob_delay_ms = clob_ws.feed_delay_ms if clob_ws is not None else None

    # CLOB book microstructure aux — stamped once per evaluation so ghosts and
    # filled outcomes share one schema; a value or None, never a 0.0 stand-in.
    aux_signals = _clob_book_aux(clob_ws, token_up, token_down, book_up, book_down)

    _signal_leg = None      # "lock_dip" — per-leg ledger attribution
    _proj = None
    lw_cfg = config.get("late_window", {})
    phase = ""
    signal = TradeSignal("SKIP", 0.5, 0, 0, "no leg armed for this second")

    def _ghost(gate: str, sig: Any, snap: dict) -> None:
        """Record a ghost when a downstream gate rejects a real leg BUY —
        the per-leg evidence for every veto's cost."""
        if ghost_tracker is None or sig is None:
            return
        if sig.action not in ("BUY_YES", "BUY_NO"):
            return
        g_side = "Up" if sig.action == "BUY_YES" else "Down"
        base_ctx: dict[str, Any] = {
            "model_probability": sig.prob,
            "edge": sig.edge,
            "market_price_up": price_up,
            "market_price_down": price_down,
            "strike_price": strike,
            "seconds_remaining": contract.get("seconds_remaining", 0),
            "entry_phase": phase,
            "signal_leg": _signal_leg,
            "twap_proj": (round(_proj, 2) if _proj is not None else None),
            "twap_disp": (round(_proj - strike, 2) if _proj is not None else None),
            **aux_signals,
        }
        merged_snap = dict(snap or {})
        caller_ctx = merged_snap.get("trade_context", {}) or {}
        merged_ctx = dict(base_ctx)
        merged_ctx.update(caller_ctx)
        merged_snap["trade_context"] = merged_ctx
        ghost_tracker.record_rejection(
            gate_name=gate,
            side=g_side,
            signal_prob=sig.prob,
            signal_edge=sig.edge,
            market_id=cid,
            seconds_remaining=float(contract.get("seconds_remaining", 0)),
            indicator_snapshot=merged_snap,
        )

    # Feed freshness: the strategy's only price inputs are the Chainlink
    # streams and the CLOB books — skip rather than size on stale data.
    if chainlink_feed is not None and chainlink_feed.age_seconds > 60:
        _record_skip("stale_feed")
        _log_skip_once(cid, f"stale_{cid}",
                       f"SKIP: stale chainlink ({chainlink_feed.age_seconds:.0f}s)")
        return None, last_eval_log_window

    # The resolution source itself can stall while raw spot keeps moving — a
    # 35s freeze on 08-10 left our reconstruction $5.59 off the served final,
    # which is breach-capable at low k. The freshness gate above cannot see it
    # (it reads the RAW stream, which stays healthy through this).
    if chainlink_feed is not None and chainlink_feed.twap_frozen():
        _record_skip("twap_frozen")
        _log_skip_once(cid, f"twapfrozen_{cid}",
                       "SKIP: official TWAP stream stalled — resolution source untrustworthy")
        return None, last_eval_log_window

    global _current_window_id
    window_id = contract.get("market_id", contract.get("slug", ""))
    if window_id != _current_window_id:
        _current_window_id = window_id
        _last_skip_log.pop(cid, None)  # fresh window — allow skip reasons to log again

    # Live fee rate so Kelly sizes against the actual cost (constant today; plumbed
    # so a future per-token rate Just Works).
    fee_rate = await market_scanner.fetch_fee_rate(token_up, http_client)

    try:
        _w_ts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        _w_ts = -1

    # --- LOCK-DIP TAKER + MAKER PLACEMENT (final-30s averaging zone) ------------
    if (lw_cfg["sniper_enabled"]
            and chainlink_feed is not None
            and contract["seconds_remaining"] <= lw_cfg["twap_zone_s"]):
        # Capital only deploys on a TRUSTED strike (Gamma price_to_beat, or a
        # TWAP-topic boundary capture with no delivery hole). An untrusted
        # strike makes the displacement math fiction.
        if not _strike_trusted.get(_w_ts, False):
            # INFO, never quiet: this veto suppresses capital — the operator
            # must be able to count what the trust gate is costing.
            _emit_gate_skip(cid, "sniper_strike_unverified",
                            "sniper: strike unverified (TWAP-topic boundary gap — value may "
                            "differ from Polymarket's price_to_beat)")
        elif _MAKER_MGR is not None and _MAKER_MGR.resting_on(_w_ts):
            pass  # a maker bid rests here — one entry path per window
        else:
            _proj = chainlink_feed.projected_final_twap(_w_ts + 300) if _w_ts > 0 else None
            _snipe = signal_engine.evaluate_twap_lock(
                _proj, strike, contract["seconds_remaining"],
                price_up, price_down,
                lw_cfg["twap_zone_s"],
                lw_cfg["twap_k_min_s"],
                lw_cfg["sniper_min_edge"],
                fee_rate=fee_rate,
                require_max_tier=lw_cfg.get("require_max_tier", True))
            # Locked but no dip to take -> rest the maker bid where the next
            # dip lands (leg 3). Placement is one POST ~20s before close, off
            # the FOK race path entirely.
            _mk = config.get("maker", {})
            # Post-close budget rides BANKROLL, not the ladder's Kelly budget —
            # a settled outcome is not a probabilistic bet. Same number for the
            # ladder-promoted and standalone arms so the common path is not
            # sized 47x smaller than the rare one.
            _pcb = round(bankroll
                         * float(_mk.get("post_close_bankroll_frac", 0.10))
                         * (breaker.kelly_multiplier if breaker else 1.0), 2)
            if (_MAKER_MGR is not None and _proj is not None
                    and _snipe.action == "SKIP"
                    and _mk.get("maker_k_place_min", 3.0) <= contract["seconds_remaining"]
                        <= _mk.get("maker_k_place_max", 25.0)):
                _mdisp = _proj - strike
                _mside = "Up" if _mdisp >= 0 else "Down"
                # A resting ladder lives through displacement decay, so it
                # demands the NEVER-BREACHED tier (max-ever error), not p99.5;
                # deeper rungs demand extra headroom (manager enforces).
                _mmargin = twap_margin(TWAP_MARGIN_MAX, contract["seconds_remaining"])
                if abs(_mdisp) >= _mmargin:
                    # Budget = Kelly at the ladder's mid rung, defended-edge
                    # anchored like every leg; the manager splits it by the
                    # frozen fractions.
                    _mkelly = signal_engine._kelly(
                        0.92 + lw_cfg["sniper_min_edge"], 0.92, fee_rate=fee_rate)
                    _mbudget = round(bankroll * _mkelly
                                     * (breaker.kelly_multiplier if breaker else 1.0), 2)
                    await _MAKER_MGR.consider_placement(
                        _w_ts, cid, contract.get("question", ""), _mside,
                        token_up if _mside == "Up" else token_down,
                        _mbudget, abs(_mdisp) / _mmargin,
                        {"trade_context": {
                            "signal_leg": "maker_bid",
                            "strike_price": strike,
                            "seconds_remaining": contract["seconds_remaining"],
                            "twap_proj": round(_proj, 2),
                            "twap_disp": round(_mdisp, 2),
                            # startup reconciliation + dust sweep key on these
                            "token_id_up": token_up,
                            "token_id_down": token_down,
                        }, "strike_price": strike},
                        pc_budget=_pcb)
            # POST-CLOSE CERTAINTY, decoupled from the ladder. The outcome is
            # settled fact in EVERY window, but the ladder only rests on the few
            # that lock at max tier inside k [3,25]s — tying post-close to it
            # threw away all but a handful of windows a day. Arming is safe on
            # any window: certain_winner re-verifies both boundary captures at
            # promotion and fails closed. Fills book as their own leg; a
            # ladder-promoted post-close stays "maker_bid" because those fills
            # blend with pre-close rungs into one position.
            if (_MAKER_MGR is not None and _mk.get("post_close_enabled")):
                _MAKER_MGR.arm_post_close(
                    _w_ts, cid, contract.get("question", ""),
                    token_up, token_down, _pcb,
                    {"trade_context": {
                        "signal_leg": "post_close",
                        "strike_price": strike,
                        "token_id_up": token_up,
                        "token_id_down": token_down,
                    }, "strike_price": strike})
            if _snipe.action in ("LATE_SNIPE_YES", "LATE_SNIPE_NO"):
                _snipe.action = "BUY_YES" if _snipe.action == "LATE_SNIPE_YES" else "BUY_NO"
                signal = _snipe
                _signal_leg = "lock_dip"
                phase = "late_sniper"
                _snipe_key = (_w_ts, signal.side)
                _snipe_now = time.time()
                if _snipe_now - _last_snipe_log.get(_snipe_key, 0.0) >= 10.0:
                    if len(_last_snipe_log) > 64:
                        _last_snipe_log.clear()
                    _last_snipe_log[_snipe_key] = _snipe_now
                    logger.info(f"{_C.DIM}TWAP LOCK {signal.side} — disp ${abs((_proj or 0) - strike):.1f} "
                                f"with {contract['seconds_remaining']:.0f}s left | "
                                f"Ask edge {signal.edge:+.1%}{_C.RESET}")

    # Eval context for the SKIP log dedup + gate-skip lines.
    global _last_logged_action
    _direction = signal.side or "Up"
    _lru_set(_pending_eval_ctx, cid, {
        "direction": _direction,
        "prob": signal.prob,
        "edge": signal.edge,
        "dist": (_proj - strike) if _proj is not None else None,
        "window_slug": _slug_to_window(cid),
    }, _PENDING_CTX_MAX)
    if signal.action not in ("BUY_YES", "BUY_NO"):
        # No leg fired this second — the normal state. Leg-level SKIP detail
        # goes to DEBUG (lock state changes ~1Hz; INFO would be noise).
        last_eval_log_window = eval_window
        return None, last_eval_log_window
    if _direction != _last_logged_action or eval_window != last_eval_log_window:
        last_eval_log_window = eval_window
        _last_logged_action = _direction
        _last_gate_skip_state.pop(cid, None)

    # --- EDGE CAP (sniper sanity cap — wider = stale phantom price) --------------
    if signal.edge > lw_cfg["sniper_max_edge"]:
        _record_skip("edge_cap")
        _ghost("edge_cap", signal, {})
        return None, last_eval_log_window

    side = "Up" if signal.action == "BUY_YES" else "Down"
    token_id = contract["token_id_up"] if side == "Up" else contract["token_id_down"]

    price = price_up if side == "Up" else price_down
    signal_ask = price   # executable ask the leg decided on, before the FOK-limit chase pad
    if not bankroll:
        bankroll = await db.get_bankroll()
    kelly_mult = breaker.kelly_multiplier if breaker else 1.0

    size = round(bankroll * signal.kelly_size * kelly_mult, 2)

    open_positions = await _get_open_positions_cached(db)
    _loop_marks["m_evalpos"] = time.time()
    active_positions = [p for p in open_positions if p.get("status") == "open"]
    if active_positions:
        cc_mult = concurrent_multiplier(side, cid, active_positions)
        size = round(size * cc_mult, 2)

    # Total-deployment cap (across all open positions) stays at the single-trade level
    # as a defensive clip; base.py also enforces it at the trader layer.
    if size > bankroll * max_bankroll_pct:
        size = round(bankroll * max_bankroll_pct, 2)

    # Book-depth fill cap: never fire full Kelly into an empty leg of a
    # one-sided book.
    side_depth = depth_usd_up if side == "Up" else depth_usd_down
    max_fill_pct = config.get("execution", {}).get("max_book_fill_pct", 0.50)
    min_side_depth = market_scanner.min_book_depth_usd
    if side_depth < min_side_depth:
        _record_skip("thin_book_depth")
        _ghost("thin_book_depth", signal, {})
        _emit_gate_skip(cid, "thin_book_depth",
                        f"Thin book on the {side} side (${side_depth:.0f} < ${min_side_depth:.0f})")
        return None, last_eval_log_window
    max_fill = side_depth * max_fill_pct
    if size > max_fill:
        size = round(max_fill, 2)

    # Net-edge gate: reject if slippage eats the edge below threshold.
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    est_slip = slippage_pct(size, side_depth, impact)
    net_edge = signal.edge - price * est_slip
    if net_edge < signal_engine.min_edge:
        _record_skip("net_edge_after_slippage")
        _ghost("net_edge_after_slippage", signal, {})
        _emit_gate_skip(cid, "net_edge_slippage", f"Edge gone after slippage ({net_edge:+.1%})")
        return None, last_eval_log_window

    # Final min-size check after all caps: Polymarket's CLOB rejects orders below
    # $1 notional. Paper mirrors the floor so paper and live behave identically.
    if size < 1.0:
        _record_skip("min_size")
        # The most active veto at small bankrolls (anchored Kelly on cheap asks
        # lands under $1) — the resolved ghosts are the funding-case evidence.
        _ghost("min_size", signal, {})
        _emit_gate_skip(cid, "min_size", f"Order below minimum (${size:.2f} < $1)")
        return None, last_eval_log_window

    tick_size = await market_scanner.fetch_tick_size(token_id, http_client)
    _loop_marks["m_tick"] = time.time()
    fresh_bba = clob_ws.best_bid_ask.get(token_id, {}) if clob_ws else {}
    _fresh_bba_ts = float(fresh_bba.get("ts", 0) or 0)
    fresh_ask = (float(fresh_bba.get("best_ask", 0) or 0)
                 if _fresh_bba_ts > 0 and (time.time() - _fresh_bba_ts) <= _WS_STALE_S
                 else 0.0)
    slip = slippage_pct(size, side_depth, impact)

    # FOK limit: pad the decision ask by sniper_fok_slip (~one tick), then die.
    # The pad absorbs jitter; a genuine reprice KILLS the order, and that kill
    # IS the adverse-selection filter (a dip that vanished must not be chased).
    # Cap at prob − min_edge so a chase can never fill below the edge floor.
    _fok_slip = lw_cfg["sniper_fok_slip"]
    _limit_cap = signal.prob - signal_engine.min_edge
    price = market_scanner.snap_to_tick(
        max(price, min(price + _fok_slip, _limit_cap)), tick_size)

    _cl_age_at_fire = None
    _cl_px_at_fire = None
    if chainlink_feed is not None:
        _cl_age = getattr(chainlink_feed, "age_seconds", None)
        if _cl_age is not None and math.isfinite(_cl_age):
            _cl_age_at_fire = round(_cl_age, 3)
            _cl_px = getattr(chainlink_feed, "price", 0.0)
            if _cl_px > 0 and _cl_age <= 5.0:
                _cl_px_at_fire = _cl_px
    snapshot: dict[str, Any] = {}
    snapshot["trade_context"] = {
        # Entry-time facts — the per-leg ledgers and harness read
        # all key off these.
        "strike_price": strike,
        "seconds_remaining": contract["seconds_remaining"],
        "market_price_up": price_up,
        "market_price_down": price_down,
        "model_probability": signal.prob,
        "edge": signal.edge,
        "size": size,
        "entry_phase": phase,
        **aux_signals,
        "cl_report_to_submit_ms": (round((time.time() - _eval_cl_rx) * 1000.0, 1)
                                   if _eval_cl_rx > 0 else None),
        # Latency breakdown (observational, this iteration's marks).
        "lat_wake_to_eval_ms": (round((_loop_marks["pre_eval"] - _loop_marks["wake"]) * 1000.0, 1)
                                if _loop_marks.get("wake", 0) > 0
                                and _loop_marks.get("pre_eval", 0) >= _loop_marks.get("wake", 0)
                                else None),
        "lat_fast_path": bool(_loop_marks.get("fast")),
        "lat_sig_woke": bool(_loop_marks.get("sig_woke")),
        "lat_clob_feed_ms": _eval_clob_delay_ms,
        "lat_segs_ms": {
            a: round((_loop_marks[b] - _loop_marks[c]) * 1000.0, 1)
            for a, b, c in (
                ("sched", "m_sched", "wake"), ("gate", "m_gate", "m_sched"),
                ("disc", "m_disc", "m_gate"), ("books", "m_books", "m_disc"),
                ("bkgates", "m_px", "m_books"), ("px", "m_px", "m_disc"),
                ("pos", "m_evalpos", "pre_eval"), ("tick", "m_tick", "m_evalpos"),
            )
            if _loop_marks.get(b, 0) >= _loop_marks.get(c, 0) > 0
        },
        "lat_ctx_ms": (round((time.time() - _loop_marks["m_tick"]) * 1000.0, 1)
                       if _loop_marks.get("m_tick", 0) > 0 else None),
        # Scar stamps (fire-time facts for the nightly scan), on the DECISION
        # ask — `price` is already the padded FOK limit here.
        # Fire facts per leg — what the fire stood on.
        "signal_leg": _signal_leg,
        "twap_proj": (round(_proj, 2) if _signal_leg == "lock_dip" and _proj is not None else None),
        "twap_disp": (round(_proj - strike, 2) if _signal_leg == "lock_dip" and _proj is not None else None),
        "twap_k_s": round(contract["seconds_remaining"], 1),
        "twap_tier": (("max" if signal.prob >= 0.999 else "p995")
                      if _signal_leg == "lock_dip" else None),
        "chainlink_price_at_fire": _cl_px_at_fire,
        "chainlink_age_s_at_fire": _cl_age_at_fire,
        # Token IDs for both outcomes — startup reconciliation and dust sweeping.
        "token_id_up": contract.get("token_id_up", ""),
        "token_id_down": contract.get("token_id_down", ""),
    }
    # Pre-submit edge re-check: walk the ask ladder for the actual expected FOK
    # VWAP (the book is ground truth vs the modeled slip). Book unavailable/too
    # thin → fall back to the BBA-only fresh_ask gate.
    max_edge_live = lw_cfg["sniper_max_edge"]
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
                            f"Ask moved {price:.3f} → {fok_vwap:.3f} (edge gone)")
            return None, last_eval_log_window
    elif fresh_ask > 0 and fresh_ask != price:
        fresh_gross_edge = signal.prob - fresh_ask
        fresh_net_edge = fresh_gross_edge - fresh_ask * slip
        if fresh_net_edge < signal_engine.min_edge or fresh_gross_edge > max_edge_live:
            _record_skip("pre_submit_edge_drift")
            _ghost("pre_submit_edge_drift", signal, snapshot)
            _emit_gate_skip(cid, "pre_submit_drift",
                            f"Ask moved {price:.3f} → {fresh_ask:.3f} (edge gone)")
            return None, last_eval_log_window

    # Pre-sign concurrently with the submit's preflight, spawned only after the
    # last veto gate so a vetoed tick never burns a doomed sign on the shared
    # core. The submit AWAITS this in-flight sign — never race a duplicate
    # against it (two concurrent pure-python signs contend on the GIL).
    if hasattr(trader, "warm_buy_signature"):
        asyncio.create_task(trader.warm_buy_signature(
            token_id, size, price, fee_rate=fee_rate,
        ))

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
        # FOK killed by a repricing book — remember the DECISION ask (not the
        # padded limit in `price`) so a later fire this window can stamp its
        _rl = reason.lower()
        _killed = "no fill" in _rl or _rl.startswith("price moved")
        if _killed:
            _record_killed_ask(cid, side, signal_ask)
        _log_skip_once(
            cid, f"open_rejected_{reason}",
            f"{_C.DIM}OPEN {side} REJECTED  ${size:.2f} @ {price:.2f} — "
            f"{'Book repriced' if _killed else reason}{_C.RESET}"
        )
        return None, last_eval_log_window

    # Drop the open-positions cache so the next tick sees this position immediately.
    _invalidate_open_positions_cache()
    if _window_recorder is not None:
        _window_recorder.mark_traded(cid)
    fill_price = result.fill_price if result.fill_price > 0 else price
    shares_ordered = size / fill_price
    fee_shares = entry_fee_shares(shares_ordered, fill_price, fee_rate)
    fee_usd = fee_shares * fill_price
    bankroll_now = await db.get_bankroll()
    # signal = the ask the leg decided on; posted = the (padded) FOK limit;
    # filled = the realized fill.
    _banner_ctx = {
        "side": side, "size": size, "cid": cid, "phase": phase,
        "signal_ask": signal_ask, "posted": price,
        "strike": strike,
        "prob": signal.prob, "edge": signal.edge,
        "fee_rate": fee_rate, "bankroll": bankroll_now,
        "question": contract["question"],
        "mkt_price": price_up if side == "Up" else price_down,
        "alert_manager": alert_manager,
    }
    if getattr(trader, "on_entry_settled", None) is not None and result.position_id:
        # LIVE: fill-time price is usually the padded limit (the tape loses
        # the indexer race) — short line now; the OPEN banner + Discord ping
        # come from the +8s chain audit so every surface agrees with the books.
        _lru_set(_pending_settled_banners, int(result.position_id),
                 _banner_ctx, _PENDING_BANNERS_MAX)
        logger.info(
            f"{_C.YELLOW}{_C.BOLD}FILLED {side}{_C.RESET}{_C.YELLOW}  ${size:.2f}  |  "
            f"{_slug_to_window(cid)} — price settling…{_C.RESET}")
    else:
        _log_open_banner(_banner_ctx, fill_price, settled="paper")
        if alert_manager:
            await alert_manager.send_trade_opened(
                question=contract["question"], side=side, size=size,
                entry_price=fill_price, ev=signal.edge,
                model_prob=signal.prob, market_price=_banner_ctx["mkt_price"],
                fee=fee_usd, bankroll=bankroll_now)
    return cid, last_eval_log_window


def _compute_strike(cid: str, window_strikes: dict[int, float],
                    eval_window: int, last_eval_log_window: int,
                    chainlink_feed: Any = None,
                    contract: Any = None) -> tuple[float | None, dict[int, float], int]:
    """Derive the window's strike.

    Strike = the official 30s-TWAP stream's first report at/after the boundary
    (the exact price_to_beat rule, bit-exact verified); Gamma's price_to_beat
    WINS whenever served."""
    now_ts = int(time.time())

    try:
        contract_window_ts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        contract_window_ts = int(now_ts // 300) * 300  # fallback

    cl_strike = chainlink_feed.get_strike(contract_window_ts) if chainlink_feed else None
    ptb = (contract or {}).get("event_metadata") or {}
    ptb = ptb.get("price_to_beat") if isinstance(ptb, dict) else None
    if ptb and ptb > 0:
        # Gamma's price_to_beat is the RESOLVED truth — once served it WINS over
        # our own capture: an RTDS delivery hole can lock a first-received
        # report that is NOT Polymarket's.
        prev = window_strikes.get(contract_window_ts)
        if prev is not None and abs(prev - ptb) > 0.005:
            logger.warning(f"Strike Corrected {_slug_to_window(cid)}: ${prev:,.2f} → ${ptb:,.2f}")
        window_strikes[contract_window_ts] = ptb
        _strike_trusted[contract_window_ts] = True
        _gamma_strikes.add(contract_window_ts)   # sticky — a later metadata-less
                                                 # fetch must not revert it
        if contract_window_ts not in _strike_logged:
            logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${ptb:,.2f} (Polymarket){_C.RESET}")
            _strike_logged.add(contract_window_ts)
    elif cl_strike and cl_strike > 0 and not (
            contract_window_ts in _gamma_strikes
            and contract_window_ts in window_strikes):
        # Never DOWNGRADE a Gamma-served strike to our own capture (a later
        # metadata-less contract refresh would flip-flop the number mid-window).
        window_strikes[contract_window_ts] = cl_strike     # the legs read this — set every loop
        _strike_trusted[contract_window_ts] = (
            chainlink_feed.strike_reliable(contract_window_ts) if chainlink_feed else False)
        # Log ONE line per window, when the boundary value LOCKS — before that
        # get_strike serves a cold-start fallback that ticks with the live TWAP.
        locked = chainlink_feed.boundary_captured(contract_window_ts) if chainlink_feed else False
        if locked and contract_window_ts not in _strike_logged:
            logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${cl_strike:,.2f} (TWAP stream){_C.RESET}")
            _strike_logged.add(contract_window_ts)
            _strike_logged.difference_update({k for k in _strike_logged if now_ts - k >= 600})

    window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}
    for k in [k for k in _strike_trusted if now_ts - k >= 600]:
        del _strike_trusted[k]
    _gamma_strikes.difference_update({k for k in _gamma_strikes if now_ts - k >= 600})

    strike = window_strikes.get(contract_window_ts, 0)
    if strike <= 0:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL {_slug_to_window(cid)} - No Polymarket strike captured yet")
        return None, window_strikes, last_eval_log_window
    return strike, window_strikes, last_eval_log_window



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
    # Sequential awaits, not gather: with fresh WS books neither call yields, so
    # the fire path never re-enters the loop queue behind a burst's backlog
    # (gather always schedules tasks = a guaranteed yield).
    book_up = await _get_book(ws_book_up, token_up)
    book_down = await _get_book(ws_book_down, token_down)
    _loop_marks["m_books"] = time.time()

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
                logger.info(f"SKIP {_slug_to_window(_cid)} — Thin book (Up ${depth_usd_up:.0f} / Down ${depth_usd_down:.0f})")
            return None, last_eval_log_window

    # Effective execution cost must clear max_spread on EITHER side — we don't yet
    # know which side we'll trade.
    if price_source == "clob":
        def _ws_spread(bba: dict, fresh: bool) -> float:
            if not fresh:
                return -1.0
            s = bba.get("spread")
            if s is None:
                # price_change events carry best_bid/best_ask but never a
                # spread field — derive it (identical to REST /spread, which
                # is ask−bid of the same book). The REST fallback here was a
                # ~90ms hit on nearly every burst fire (fill 305: px=93.3ms).
                try:
                    bid = float(bba.get("best_bid", 0) or 0)
                    ask = float(bba.get("best_ask", 0) or 0)
                except (TypeError, ValueError):
                    return -1.0
                return round(ask - bid, 4) if (bid > 0 and ask > 0) else -1.0
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
            # Fail closed: WS BBO and REST /spread both failed — skip the tick
            # rather than waive the only cost-vs-max_spread gate.
            _record_skip("spread_unavailable")
            logger.debug("Spread unavailable from WS + REST — skipping tick (fail-closed)")
            return None, last_eval_log_window
        # Half-spread above mid + the EFFECTIVE peak taker fee (flat per-share
        # proxy — never the raw coefficient, the two fee models must not mix).
        # Gate stays max_spread so it doesn't tighten into illiquid markets.
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
    if flip_count == 0 and db is not None:
        _has = db.has_open_or_pending_market(cid) if hasattr(db, "has_open_or_pending_market") else None
        if _has is None:
            _has = await db.has_position_for_market(cid)
        if _has:
            return None, None, ws_subscribed_tokens, prev_contract_tokens

    # Subscribe WebSocket to this contract's tokens (idempotent)
    token_up = contract["token_id_up"]
    token_down = contract["token_id_down"]
    current_tokens = [t for t in [token_up, token_down] if t]
    new_tokens = [t for t in current_tokens if t not in ws_subscribed_tokens]

    # Drop every subscription we no longer need — EXCEPT a token the maker
    # ladder still has orders resting on. Its post-close phase outlives the
    # window, and unsubscribing under it blinds the paper fill matcher entirely
    # (a closed window's 1,015 prints reached us; 0 arrived after the close).
    # Computed against what we are actually subscribed to rather than just the
    # previous contract, so a deferred token still gets swept once it retires.
    if clob_ws:
        keep = set(current_tokens)
        if _MAKER_MGR is not None:
            keep |= _MAKER_MGR.holding_tokens()
        stale_tokens = [t for t in ws_subscribed_tokens if t not in keep]
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


def _resolved_exit_price(live: dict[str, Any], side: str,
                         market_id: str = "") -> tuple[float | None, str | None]:
    """Decide a resolved position's binary exit price from current market state.

    Returns (exit_price, oracle_log): 1.0/0.0, or None while unresolved
    (caller keeps waiting); oracle_log is set when the Chainlink oracle decided.

    Priority (Chainlink is the truth, never Binance):
      1. event_metadata final_price vs price_to_beat — the Chainlink oracle.
      2. A COHERENT resolved CLOB book (closed, sums ~1, one side at an extreme).
         Incoherent books are rejected — a stale/phantom print must never
         mis-resolve a winning side; the caller falls to the oracle/orphan path.
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
        _wnd = f" {_slug_to_window(market_id)}" if market_id else ""
        return exit_price, (f"{'UP' if up_won else 'DOWN'}{_wnd} | "
                            f"${strike:,.2f} → ${final_price:,.2f}")
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
        db: Any, outcome_reviewer: Any, breaker: Any,
        day_wins: int, day_losses: int, day_fees: float,
        chainlink_feed: Any = None) -> tuple[bool, int, int, float]:
    """Resolve a position whose contract has expired (seconds_remaining <= 0)."""
    global _prev_resolution_margin
    # Chainlink oracle first (authoritative), then a coherent resolved CLOB book.
    exit_price, resolve_log = _resolved_exit_price(live, pos["side"], pos["market_id"])
    mid = pos["market_id"]
    # Our own tape knows the outcome ~85s before Gamma serves it: the close
    # boundary's TWAP capture IS the resolving final. Verdict-only — Gamma
    # still books the money; a disagreement is a mechanism alarm, not a trade.
    _tape_final = _tape_strike = None
    if chainlink_feed is not None:
        try:
            # strike_reliable (not just captured): a delivery-hole capture is a
            # LATER second's value — a verdict or drift alarm from it blames
            # Polymarket for our own hole (last night's false "rule drift").
            _w = int(mid.rsplit("-", 1)[-1])
            _tape_strike = (chainlink_feed.get_strike(_w)
                            if chainlink_feed.strike_reliable(_w) else None)
            _tape_final = (chainlink_feed.get_strike(_w + 300)
                           if chainlink_feed.strike_reliable(_w + 300) else None)
        except (ValueError, IndexError):
            pass
    if exit_price is None:
        # Window hasn't resolved yet — wait for the next tick.
        now_ts = time.time()
        if mid not in _last_resolve_wait_log:
            _last_resolve_wait_log[mid] = now_ts
            logger.info(f"{_C.DIM}WAITING FOR RESOLUTION {_slug_to_window(mid)}{_C.RESET}")
        if (_tape_final is not None and _tape_strike and mid not in _tape_verdict_logged):
            # One early verdict line per window, the moment the close capture lands.
            _tape_verdict_logged.add(mid)
            _tv_up = _tape_final >= _tape_strike
            _ours = "✓" if (pos["side"] == "Up") == _tv_up else "✗"
            logger.info(f"TAPE VERDICT {_slug_to_window(mid)} — {'UP' if _tv_up else 'DOWN'} wins, "
                        f"our {pos['side']} {_ours}  (final ${_tape_final:,.2f} vs "
                        f"strike ${_tape_strike:,.2f}; Gamma confirms in ~1 min)")
        return False, day_wins, day_losses, day_fees
    if resolve_log and mid not in _resolve_oracle_logged:
        # Log once per market — a pending winning redeem retries this path every
        # tick and would otherwise repeat the same RESOLVE line for minutes.
        _resolve_oracle_logged.add(mid)
        logger.info(f"RESOLVED {resolve_log}")
    # Per-window mechanism check: our recorded final must equal Gamma's to the
    # cent (28/28 so far). A drift here means the resolution rule moved again —
    # scream now, not at the nightly watch.
    _meta = (live.get("event_metadata") or {})
    if (_tape_final is not None and _meta.get("final_price") is not None
            and abs(_tape_final - _meta["final_price"]) > 0.005
            and mid not in _tape_mismatch_logged):
        _tape_mismatch_logged.add(mid)
        logger.warning("RESOLUTION DRIFT %s — our tape final $%.2f vs Gamma $%.2f: "
                       "verify the resolution rule before trusting the lock",
                       _slug_to_window(mid), _tape_final, _meta["final_price"])

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
            f"  {color}{_C.BOLD}RESOLVED {won} {pos['side']}{_C.RESET} {pos['entry_price']:.2f}→{exit_price:.2f}  "
            f"{gain_pct:+.0%}  {color}${pnl:+.2f}{_C.RESET}  |  {_slug_to_window(pos['market_id'])}\n"
            f"  {_C.DIM}day {day_wins}W/{day_losses}L · bank ${bankroll_after:.2f} · fees {_fee_breakdown(result)}{_C.RESET}\n"
            f"{color}{'=' * 69}{_C.RESET}")
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
        # Resolution margin (final − strike) telemetry — from event_metadata
        # regardless of which branch above set exit_price.
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
    # (final_price, strike) for the prev_resolution_margin telemetry —
    # populated by whichever branch below has the data.
    resolved_final: float | None = None
    resolved_strike: float | None = None
    # Try direct Gamma fetch for eventMetadata (Chainlink oracle)
    direct = await _get_contract_prices(market_scanner, pos["market_id"], http_client)
    direct_price, direct_log = (_resolved_exit_price(direct, pos["side"], pos["market_id"])
                                if direct else (None, None))
    if direct_price is not None:
        exit_price = direct_price
        meta = direct.get("event_metadata") or {}
        if meta.get("final_price") is not None and meta.get("price_to_beat") is not None:
            resolved_final = meta.get("final_price")
            resolved_strike = meta.get("price_to_beat")
        if direct_log:
            logger.info(f"RESOLVED {direct_log} (orphan)")
        else:
            logger.info(f"RESOLVED {_slug_to_window(pos['market_id'])} | "
                        f"coherent CLOB book (orphan)")
    elif age > 1800 and chainlink_feed is not None:
        # Gamma silent for 30+ min — Polymarket has already auto-credited the Safe
        # via on-chain settlement, so the bankroll is correct. Use our own TWAP
        # boundary CAPTURES to mark the DB record so the position stops blocking.
        # Both ends must be genuine captures: get_strike's live fallback would
        # serve the SAME current value for strike and final (tie → fake Up win).
        # Never fabricate — an unresolvable orphan waits and pages the operator.
        try:
            window_ts = int(pos["market_id"].rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            window_ts = 0
        strike_at_boundary = (chainlink_feed.get_strike(window_ts)
                              if window_ts and chainlink_feed.boundary_captured(window_ts)
                              else None)
        final_at_expiry = (chainlink_feed.get_strike(window_ts + 300)
                           if window_ts and chainlink_feed.boundary_captured(window_ts + 300)
                           else None)
        if (strike_at_boundary is None or strike_at_boundary <= 0
                or final_at_expiry is None or final_at_expiry <= 0):
            logger.info(f"ORPHAN {_slug_to_window(pos['market_id'])} ({age:.0f}s old) — "
                        f"Waiting for resolution (boundary captures incomplete)")
            return True, day_wins, day_losses, day_fees
        final_price = final_at_expiry
        final_source = "expiry boundary"
        up_won = final_price >= strike_at_boundary
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        resolved_final = final_price
        resolved_strike = strike_at_boundary
        logger.warning(
            f"RESOLVE ORPHAN {'UP' if up_won else 'DOWN'} {_slug_to_window(pos['market_id'])} | "
            f"Via Chainlink fallback (Gamma silent for {age:.0f}s) | "
            f"Strike ${strike_at_boundary:,.2f} → Final ${final_price:,.2f} [{final_source}]"
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
            logger.error(f"ORPHANED >1hr {_slug_to_window(pos['market_id'])} | Waiting for Chainlink fallback")
            if alert_manager:
                await alert_manager.send_trade_closed(
                    question=pos.get("question", ""), exit_price=0,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=0,
                    gain_pct=0, reason="orphaned — awaiting resolution", fees=0)
        else:
            logger.info(f"ORPHAN {_slug_to_window(pos['market_id'])} ({age:.0f}s old) — Waiting for resolution")
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
            f"  {color}{_C.BOLD}RESOLVED {won} {pos['side']} (orphan){_C.RESET} {pos['entry_price']:.2f}→{exit_price:.2f}  "
            f"{gain_pct:+.0%}  {color}${pnl:+.2f}{_C.RESET}  |  {_slug_to_window(pos['market_id'])}\n"
            f"  {_C.DIM}day {day_wins}W/{day_losses}L · bank ${bankroll_after:.2f} · fees {_fee_breakdown(result)}{_C.RESET}\n"
            f"{color}{'=' * 69}{_C.RESET}")
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
        # prev_resolution_margin — persist whichever branch (eventMetadata or
        # Chainlink fallback) captured both final_price and strike.
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
    active_start = sched_start_et
    active_end = sched_end_et
    today_str = now_et.strftime("%Y-%m-%d")
    in_trading_hours = now_time_et >= active_start and now_time_et < active_end

    if in_trading_hours and current_trading_day != today_str:
        if current_trading_day is not None and alert_manager:
            # Close previous day first (if bot ran overnight)
            bankroll = await db.get_bankroll()
            day_pnl = bankroll - day_open_bankroll
            _, _, _, _trades_pnl = await db.get_day_stats(current_trading_day)
            await alert_manager.send_day_close(bankroll, day_pnl, day_wins, day_losses,
                                               day_fees, trades_pnl=_trades_pnl)
        current_trading_day = today_str
        day_open_bankroll = await db.get_bankroll()
        _persist_day_open(today_str, day_open_bankroll)
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
                _, _, _, _trades_pnl = await db.get_day_stats(current_trading_day)
                await alert_manager.send_day_close(bankroll, day_pnl, day_wins, day_losses,
                                                   day_fees, trades_pnl=_trades_pnl)
            current_trading_day = None

    return in_trading_hours, current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees


async def _check_ghosts(ghost_tracker: Any, market_scanner: Any,
                        http_client: Any) -> None:
    """Fetch Gamma metadata for windows with pending ghosts and resolve them —
    the zero-capital evidence stream every gate evaluation reads."""
    try:
        meta: dict[str, dict[str, Any]] = {}
        for mid in ghost_tracker.watched_markets:
            try:
                data = await market_scanner.gamma_events_by_slug(http_client, mid)
                if data:
                    parsed = market_scanner.parse_contract(data[0])
                    if parsed and parsed.get("event_metadata"):
                        meta[mid] = parsed["event_metadata"]
            except Exception:
                continue
        ghost_tracker.check_resolutions(event_metadata=meta)
    except Exception as e:
        logger.debug("ghost resolution sweep failed: %s", e)


async def trading_loop(market_scanner: BTCMarketScanner, signal_engine: SignalEngine,
                       trader: Any, alert_manager: AlertManager | None, db: Any,
                       config: dict[str, Any], outcome_reviewer: Any,
                       is_paused_fn: Any,
                       scheduler: Any = None, clob_ws: ClobWebSocket | None = None,
                       breaker: CircuitBreaker | None = None,
                       ghost_tracker: Any = None,
                       http_client: Any = None,
                       chainlink_feed: Any = None,
                       ) -> None:
    import httpx
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    max_bankroll_pct = config["execution"]["max_bankroll_deployed"]
    max_spread = config.get("market", {}).get("max_spread", 0.10)

    # Trading schedule in ET (handles EST/EDT automatically)
    sched = config.get("schedule", {})
    sched_start_et = (sched["trading_start_hour_et"], sched["trading_start_minute"])
    sched_end_et = (sched["trading_end_hour_et"], sched["trading_end_minute"])

    window_strikes: dict[int, float] = {}      # window_ts -> strike (Chainlink boundary / price_to_beat)
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
        _saved_open = _load_day_open(_today_et)
        if _saved_open is not None:
            day_open_bankroll = _saved_open
        else:
            # No snapshot for today — reconstruct from the trade ledger and
            # persist it so later restarts don't re-derive from a bankroll that
            # has since absorbed money settled outside recorded trades.
            day_open_bankroll = (await db.get_bankroll()) - _db_pnl_sum
            _persist_day_open(_today_et, day_open_bankroll)
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
        f"Today: {day_wins}W/{day_losses}L"
    )
    logger.info(
        f"Feeds: Chainlink {_f(chainlink_feed)} · "
        f"CLOB WS {'OK' if clob_ws is not None else '--'} · "
        f"Discord {'OK' if alert_manager is not None else '--'}"
    )

    # Closure captures clob_ws once — reused across all book-update ticks.

    _pg_lw = config.get("late_window", {})
    _pg_zone = float(_pg_lw.get("twap_zone_s", 30.0))
    _last_full_eval = 0.0

    async def _entry_pass(positions: list) -> None:
        """The single entry evaluation — one copy, two call sites in the loop.

        A feed wake with no open position runs this FIRST (before
        position management); every other wake runs it last, as before.
        """
        nonlocal ws_subscribed_tokens, prev_contract_tokens, last_eval_log_window, window_strikes
        nonlocal _last_full_eval
        # µs pre-gate: only fire-adjacent wakes pay the full evaluation.
        _pg_now = time.time()
        _pg_hot = _sniper_wake and _twap_hot(chainlink_feed, window_strikes, _pg_now, _pg_zone)
        if not _pregate_should_eval(_pg_now, _last_full_eval, 300.0 - (_pg_now % 300.0),
                                    _pg_hot, _pg_zone):
            return
        _last_full_eval = _pg_now
        _loop_marks["m_gate"] = time.time()
        # Skip new entries when paused / outside hours (positions still managed)
        if is_paused_fn():
            return
        if not in_trading_hours:
            return
        # Expired positions waiting for Gamma resolution don't block new entries.
        max_concurrent = config.get("execution", {}).get("max_concurrent_positions", 1)
        active_count = sum(1 for p in positions if p["status"] == "open")
        if active_count >= max_concurrent:
            return

        contract, cid, ws_subscribed_tokens, prev_contract_tokens = \
            await _discover_contract_and_subscribe(
                market_scanner, ws_subscribed_tokens, clob_ws,
                prev_contract_tokens, db=db, http_client=http_client)
        if not contract:
            return
        _loop_marks["m_disc"] = time.time()

        # Warm the py-clob market-info cache so the entry FOK signs without ~2
        # sequential REST round-trips; dedups per condition_id (PaperTrader: no-op).
        if hasattr(trader, "prewarm_market_info"):
            asyncio.create_task(trader.prewarm_market_info(contract.get("condition_id", "")))

        # Never attempt entry when already holding a position in this window.
        if any(p["market_id"] == cid and p["status"] == "open" for p in positions):
            return

        token_up = contract["token_id_up"]
        token_down = contract["token_id_down"]

        prices, last_eval_log_window = await _fetch_market_prices(
            contract, token_up, token_down, market_scanner,
            http_client, clob_ws, max_spread, last_eval_log_window)
        if not prices:
            return
        _loop_marks["m_px"] = time.time()

        price_up = prices["price_up"]
        price_down = prices["price_down"]
        book_up = prices["book_up"]
        book_down = prices["book_down"]
        depth_usd_up = prices["depth_usd_up"]
        depth_usd_down = prices["depth_usd_down"]
        eval_window = prices["eval_window"]

        strike, window_strikes, last_eval_log_window = \
            _compute_strike(cid, window_strikes,
                            eval_window, last_eval_log_window,
                            chainlink_feed=chainlink_feed,
                            contract=contract)
        if strike is None:
            return

        current_bankroll = await _get_bankroll_cached(db)
        _loop_marks["pre_eval"] = time.time()
        _, last_eval_log_window = await _evaluate_signal_and_enter(
            contract, cid,
            signal_engine, market_scanner, http_client, clob_ws,
            trader, alert_manager, db, config, breaker,
            price_up, price_down,
            book_up, book_down, depth_usd_up, depth_usd_down,
            strike, eval_window, last_eval_log_window,
            token_up, token_down, max_bankroll_pct,
            bankroll=current_bankroll,
            chainlink_feed=chainlink_feed,
            ghost_tracker=ghost_tracker)

    while True:
        # Check if scheduler requested shutdown (auto-restart cycle after pipeline)
        if scheduler and getattr(scheduler, '_shutdown_requested', False):
            break

        # Sniper legs (brake: sniper_enabled): also wake on raw Chainlink
        # reports — the resolution stream is the sniper's decision clock; a
        # displacement is acted on within one report, not the 100ms housekeeping
        # fallback. No effect on the loop when the brake is off.
        _sniper_wake = chainlink_feed is not None and bool(
            config.get("late_window", {})["sniper_enabled"])

        _sig_woke = False  # this wake was a Chainlink report (set below)
        # Event-driven: react instantly to WebSocket book/resolution updates; short timeout for housekeeping
        if clob_ws:
            try:
                # Wake on book update OR market resolution — whichever comes first
                book_task = asyncio.create_task(clob_ws.book_updated.wait())
                resolve_task = asyncio.create_task(clob_ws.market_resolved.wait())
                _wait_set = {book_task, resolve_task}
                if _sniper_wake:
                    _wait_set.add(asyncio.create_task(chainlink_feed.report_event.wait()))
                done, pending = await asyncio.wait(
                    _wait_set, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if _sniper_wake:
                    _sig_woke = chainlink_feed.report_event.is_set()
                    chainlink_feed.report_event.clear()
                if clob_ws.book_updated.is_set():
                    clob_ws.book_updated.clear()
                # Resolve adverse-selection checkpoints every loop tick, not only on
                # book_updated — a WS-quiet token would collapse multiple checkpoints
                # onto the next event. Stale BBAs read 0 mid, never a fresh checkpoint.
                if clob_ws.market_resolved.is_set():
                    clob_ws.market_resolved.clear()
                    # Invalidate price cache — Gamma should have resolution data now
                    _contract_price_cache.clear()
                    logger.info("Market Resolved — Checking resolution")
            except asyncio.TimeoutError:
                pass  # housekeeping tick — contract discovery, day banners
        else:
            await asyncio.sleep(0.1)  # fallback polling if no WebSocket
        _loop_marks["wake"] = time.time()
        try:
            # --- DAY OPEN / CLOSE ---
            now_et = datetime.now(ET)
            in_trading_hours, current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees = \
                await _check_trading_schedule(
                    now_et, scheduler, sched_start_et, sched_end_et,
                    current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees,
                    alert_manager, db, config, breaker)

            _loop_marks["m_sched"] = time.time()

            # Maker-bid lifecycle: cancel on lock-weaken/close, book fills.
            # Pure float math per tick (live polls at 1Hz in a thread) — runs
            # every iteration so a resting order can never outlive its lock.
            if _MAKER_MGR is not None:
                try:
                    await _MAKER_MGR.maintain()
                except Exception:
                    logger.exception("maker maintain failed")

            # --- FAST PATH: with nothing at risk, a Chainlink-report wake OR
            # any wake during a fire-adjacent displacement runs the entry
            # evaluation FIRST. The mirror answers "anything open?" sync, so
            # nothing — not even the positions cache — precedes a hot evaluation.
            _fast_entry = False
            _loop_marks["sig_woke"] = 1.0 if _sig_woke else 0.0
            _now_fp = time.time()
            _hot_fp = (_sniper_wake
                       and _twap_hot(chainlink_feed, window_strikes, _now_fp, _pg_zone))
            _open_n = db.open_market_count() if hasattr(db, "open_market_count") else None
            if (_sig_woke or _hot_fp) and _open_n == 0:
                _fast_entry = True
                _loop_marks["fast"] = 1.0
                await _entry_pass([])
            else:
                _loop_marks["fast"] = 0.0

            positions = await _get_open_positions_cached(db)
            if not _fast_entry and (_sig_woke or _hot_fp) \
                    and not any(p["status"] == "open" for p in positions):
                # Mirror wasn't ready — fall back to the cached-positions check.
                _fast_entry = True
                _loop_marks["fast"] = 1.0
                await _entry_pass(positions)

            # --- POSITION MANAGEMENT: resolution check + active re-evaluation ---
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
                            outcome_reviewer, breaker,
                            day_wins, day_losses, day_fees,
                            chainlink_feed=chainlink_feed)
                    if not resolved:
                        continue  # Gamma hasn't resolved yet — wait for next tick
                # Open positions HOLD TO RESOLUTION — every leg's edge was
                # measured that way; there is no exit engine.

            # --- GHOSTS: resolve rejected-entry evidence (every 30s, background) ---
            if ghost_tracker:
                global _last_cf_check_ts, _cf_check_task
                _now_cf = time.time()
                if (_now_cf - _last_cf_check_ts >= _CF_CHECK_INTERVAL
                        and (_cf_check_task is None or _cf_check_task.done())):
                    _last_cf_check_ts = _now_cf
                    _cf_check_task = asyncio.create_task(
                        _check_ghosts(ghost_tracker, market_scanner, http_client))

            # --- ENTRY (normal order) — the fast path already ran it this wake
            if not _fast_entry:
                await _entry_pass(positions)

        except AuthError as e:
            # Every subsequent order would fail identically — bail loudly rather than
            # silently skipping entries for hours. The run_polybot.sh loop keeps going but
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

    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
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
        discord_bot = create_bot(db=None, scanner=None, config=config)
        alert_manager = AlertManager(bot=discord_bot,
            trade_channel_name=config["discord"]["trade_channel_name"],
            control_channel_name=config["discord"]["control_channel_name"],
            daily_channel_name=config["discord"].get("daily_channel_name", "polybot-daily"))

    agents_cfg = config["agents"]
    scheduler = NightlyScheduler(
        outcome_reviewer=outcome_reviewer,
        ghost_tracker=ghost_tracker,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
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

    market_cfg = config.get("market", {})
    market_scanner = BTCMarketScanner(
        entry_window_seconds=market_cfg.get("entry_window_seconds", 120),
        min_time_remaining=market_cfg.get("min_time_remaining_seconds", 20),
        cache_seconds=market_cfg.get("scan_cache_seconds", 5),
        min_book_depth_usd=market_cfg.get("min_book_depth_usd", 50.0),
        clob_url=market_cfg.get("clob_url"),
    )

    signal_engine = SignalEngine(
        min_edge=config["late_window"]["sniper_min_edge"],
        kelly_fraction=config["math"]["kelly_fraction"])

    exec_cfg = config["execution"]
    if mode == "live":
        # Allowance floor: cover at least 10 rounds of max-sized concurrent positions so a
        # revoked or run-down allowance is caught before it silently kills order fills.
        _preflight_bankroll = await db.get_bankroll()
        _kelly_fraction = config.get("math", {})["kelly_fraction"]
        _max_single = _preflight_bankroll * _kelly_fraction
        _max_concurrent = exec_cfg["max_concurrent_positions"]
        _min_allowance = _max_single * _max_concurrent * 10.0
        ok, msg, live_balance = verify_auth(min_allowance_usd=_min_allowance)
        if not ok:
            logger.error(f"LIVE MODE preflight failed: {msg}")
            return
        logger.debug(f"LIVE MODE — {msg}")
        trader = LiveTrader(db=db,
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
        # Boot hygiene: cancel any resting order a crashed process left on the
        # exchange — a fill in the gap would be unbooked shares with no DB row.
        try:
            await asyncio.to_thread(trader.client.cancel_all)
            logger.info("Boot order sweep — no resting orders carried over")
        except Exception as e:
            logger.warning("boot cancel_all failed (check open orders by hand): %s", e)
        # The +8s chain audit reports the settled entry here → the OPEN banner
        # prints once, with the real fill (see _log_open_banner).
        trader.on_entry_settled = _on_entry_settled

        def _on_exit_corrected(pos_id: int, side: str, old_px: float,
                               new_px: float, delta: float) -> None:
            # The close banner already printed with the limit-booked exit — one
            # follow-up line keeps Discord agreeing with the wallet.
            if alert_manager:
                _spawn_bg(alert_manager.send_health(
                    f"CORRECTED {side.upper()} exit {old_px:.3f} → {new_px:.3f} "
                    f"({delta:+.2f}$) — earlier close line was the order's limit; "
                    f"books now match your wallet."))
        trader.on_exit_corrected = _on_exit_corrected
    else:
        # Fallbacks match settings.yaml's calibrated values (one source of truth
        # for the realism constants; the fallbacks only fire if settings omit keys).
        trader = PaperTrader(db=db,
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"],
            paper_latency_scale=exec_cfg.get("paper_latency_scale", 0.95),
            paper_latency_floor_s=exec_cfg.get("paper_latency_floor_s", 0.32),
            paper_network_fail_rate=exec_cfg.get("paper_network_fail_rate", 0.03))
        logger.debug(
            f"PAPER MODE — simulated trading with live-realistic fills "
            f"(latency=empirical live POST dist ×{exec_cfg.get('paper_latency_scale', 1.0)}, "
            f"net_fail={exec_cfg.get('paper_network_fail_rate', 0.03):.0%})"
        )

    # Circuit breaker (drawdown-based Kelly scaling)
    cb_cfg = config.get("circuit_breaker", {})
    init_bankroll = await db.get_bankroll()
    breaker = CircuitBreaker(
        initial_bankroll=init_bankroll,
        floor_pct=cb_cfg["floor_pct"],
        min_multiplier=cb_cfg["min_multiplier"],
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
    ghost_tracker = GhostTracker(memory_dir=str(base_dir / "memory"))

    # Discord (created before scheduler so alert_manager can be passed in)
    discord_bot = create_bot(db, market_scanner, config)
    alert_manager = AlertManager(bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"],
        daily_channel_name=config["discord"].get("daily_channel_name", "polybot-daily"))
    discord_bot.alert_manager = alert_manager

    scheduler = NightlyScheduler(
        outcome_reviewer=outcome_reviewer,
        ghost_tracker=ghost_tracker,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
    )
    scheduler._auto_shutdown = args.auto_restart
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

    # Restore prev_resolution_margin from last session so the recorded telemetry
    # field isn't zeroed for the first trades after each restart.
    global _prev_resolution_margin
    _prev_resolution_margin = _load_prev_resolution_margin()
    if _prev_resolution_margin is not None:
        logger.debug(f"Restored prev_resolution_margin: {_prev_resolution_margin:+.2f}")

    # Gate-skip stats load lazily from _record_skip / flush_gate_stats; this just
    # syncs the current-day file to what's on disk.
    _ensure_gate_stats_day_loaded()
    flush_gate_stats()

    await scheduler.start()
    from polybot.feeds.chainlink_feed import ChainlinkFeed
    chainlink_feed = ChainlinkFeed()
    await chainlink_feed.start()

    # Periodic feed-staleness telemetry (P50/P95/P99 inter-arrival per feed).
    _staleness_trackers = [
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

    # Recorders: window-path stream (the kill-bar feed + pivot-research corpus)
    # + CLOB tape + micro-tape (event-true BBO/tick/report stream — the sub-5Hz
    # resolution the sampled recorder can't see). Write-behind; never block the loop.
    from polybot.recording import MicroTape, TapeRecorder, WindowPathRecorder
    from polybot.execution.maker_bid import MakerBidManager
    tape_recorder = TapeRecorder()
    _maker_cfg = config.get("maker", {})
    global _MAKER_MGR
    _MAKER_MGR = (MakerBidManager(trader, chainlink_feed, _maker_cfg,
                                  paper=(mode != "live"))
                  if _maker_cfg.get("maker_bid_enabled") else None)

    def _on_trade_mux(asset_id: str, trade: dict) -> None:
        tape_recorder.on_trade(asset_id, trade)
        if _MAKER_MGR is not None:
            _MAKER_MGR.on_print(asset_id, trade)
    clob_ws.on_trade = _on_trade_mux
    micro_tape = MicroTape()
    clob_ws.on_bba = micro_tape.on_bba
    chainlink_feed.on_report = micro_tape.on_cl_report
    chainlink_feed.on_twap = micro_tape.on_twap_report
    window_recorder = WindowPathRecorder(
        db=db, clob_ws=clob_ws,
        chainlink_feed=chainlink_feed, market_scanner=market_scanner,
        http_client=http_client)
    global _window_recorder
    _window_recorder = window_recorder

    # Nightly jobs: window-path retention sweep + price-sum retention + the
    # sniper-edge health report (runs at 23:45 ET, during the wind-down).
    from polybot.recording import cleanup_job
    scheduler.register_job("window_paths_retention", cleanup_job(db))

    async def _price_sum_retention_job() -> dict:
        from polybot.paths import trim_jsonl_by_age, PRICE_SUM_OUTLIERS_PATH
        dropped = await asyncio.to_thread(trim_jsonl_by_age, PRICE_SUM_OUTLIERS_PATH, 90.0)
        return {"price_sum_lines_dropped": dropped}
    scheduler.register_job("price_sum_retention", _price_sum_retention_job)

    async def _maker_ladder_job() -> dict:
        """Nightly REPORT of the trailing tape's dip-depth CDF. Diagnostic
        only — rung prices come from break-even economics in settings.yaml and
        this job never writes them (a dip-frequency estimator drags the deep
        rungs shallow, the direction already measured wrong)."""
        import importlib.util as _ilu
        lp = Path(__file__).resolve().parent.parent / "scripts" / "analyze_twap_lock.py"
        spec = _ilu.spec_from_file_location("analyze_twap_lock_l", lp)
        lmod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(lmod)
        return await asyncio.to_thread(lmod.ladder_recalibrate)
    scheduler.register_job("maker_ladder", _maker_ladder_job)

    from polybot.recording import compress_recordings_job, recordings_cleanup_job
    scheduler.register_job("recordings_retention", recordings_cleanup_job())

    async def _sniper_health_job() -> dict:
        """Nightly sniper health: kill-bar read + post-live kill rule, pinged to Discord.

        Alert-only — never flips config (kill bars are operator authority).
        Reports the SIM corpus AND the realized fills with their gap; the
        kill-rule verdict is driven by the realized ledger once fills exist
        (the sim can't see live execution quality). Skipped when disabled."""
        if not config.get("late_window", {}).get("sniper_enabled"):
            return {"skipped": "sniper disabled"}
        import importlib.util
        hp = Path(__file__).resolve().parent.parent / "scripts" / "analyze_late_window.py"
        spec = importlib.util.spec_from_file_location("analyze_late_window", hp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _lw = config.get("late_window", {})
        # Each read individually guarded: the SIM corpus lives in gitignored
        # recordings that can be missing/corrupt while the realized ledger is
        # perfectly healthy — a dead corpus must degrade the ping, never
        # suppress the kill-rule readout for live money (and vice versa).
        # SIM = the TWAP lock replay over the micro-tape (analyze_twap_lock.py),
        # an at-the-decision-ask ceiling — context only, never the verdict.
        tmod = None
        try:
            tp = Path(__file__).resolve().parent.parent / "scripts" / "analyze_twap_lock.py"
            tspec = importlib.util.spec_from_file_location("analyze_twap_lock", tp)
            tmod = importlib.util.module_from_spec(tspec)
            tspec.loader.exec_module(tmod)
            sim = await asyncio.to_thread(
                tmod.health_read, None, _lw["sniper_min_edge"])
        except Exception as e:
            logger.warning("sniper health SIM read failed: %s", e)
            sim = None
        # The realized-fill read tracks the BINDING population for the current mode:
        # live -> the live ledger; paper (re-validation) -> the paper-shadow fills
        # since the validation epoch (pre-epoch fills ran different code/config).
        try:
            if mode == "live":
                # Scoped to the epoch too: without it, day 1 of a live run scores
                # the PREVIOUS live era and false-trips the kill rule the first
                # night. Pin validation_epoch at every go-live.
                live = await asyncio.to_thread(
                    mod.live_health_read, None, _lw.get("validation_epoch"))
            else:
                live = await asyncio.to_thread(
                    mod.live_health_read, mod.PAPER_DB, _lw.get("validation_epoch"))
        except Exception as e:
            logger.warning("sniper health realized-ledger read failed: %s", e)
            live = None
        _real_db = None if mode == "live" else mod.PAPER_DB
        # Resolution-mechanism tripwire: every window's official final must equal
        # the NEXT window's strike to the cent. Systematic divergence means
        # Polymarket changed the resolution rule again — the one event that
        # invalidates the whole lock premise, so it is checked nightly.
        try:
            twap = await asyncio.to_thread(mod.resolution_snapshot_read, _real_db)
        except Exception as e:
            logger.warning("resolution watch read failed: %s", e)
            twap = None

        def _money_line(r) -> str:
            if r is None or r["n_fills"] == 0:
                return "no realized fills yet"
            return (f"**{r['net_per_sh']*100:+.1f}¢/share** over {r['n_fills']} fills, "
                    f"{r['n_days']} days ({r['days_pos']} profitable) · wins {r['win_rate']:.0%}")

        def _shutoff_line(r) -> str:
            if r is None or r["n_fills"] == 0:
                return ""
            t4 = ("n/a — needs 4 days" if r["trailing4_mean"] is None
                  else f"{r['trailing4_mean']*100:+.1f}¢")
            t8 = ("n/a — needs 8 days" if r["trailing8_t"] is None
                  else f"{r['trailing8_t']:+.2f}")
            return (f"Shut-off line: last-4-days {t4} (must stay ≥ +2.0¢) · "
                    f"8-day consistency {t8} (must stay ≥ 2.0)\n")

        def _context_line() -> str:
            if sim is None or sim["n_fills"] == 0:
                return ""
            base = f"Research sim (a CEILING — fills at the decision ask): {sim['net_per_sh']*100:+.1f}¢/share"
            if live and live["n_fills"] > 0:
                gap = (sim["net_per_sh"] - live["net_per_sh"]) * 100
                if abs(gap) < 3:
                    base += " — real fills in line with it"
                elif gap > 0:
                    base += f" — real fills {gap:.1f}¢ below it (some gap is expected; watch it grow)"
                else:
                    base += f" — real fills ABOVE the ceiling by {-gap:.1f}¢ (odd — check the sim)"
            return base + "\n"

        def _legs_line() -> str:
            legs = (live or {}).get("legs") or {}
            parts = [f"{name} {v['net_per_sh']*100:+.1f}¢/sh × {v['n_fills']} "
                     f"(win {v['win_rate']:.0%})" for name, v in legs.items()]
            return ("Per-leg: " + " · ".join(parts) + "\n") if parts else ""

        def _twap_line() -> str:
            if not twap or not twap.get("checked"):
                return ""
            c, m = twap["checked"], twap["matched"]
            if m == c:
                return (f"Resolution watch: TWAP chain intact — each close equals "
                        f"the next strike, {m}/{c} to the cent\n")
            return (f"🚨 **RESOLUTION MECHANISM SHIFT: {c - m}/{c} windows broke "
                    f"the final==next-strike chain (worst ${twap['worst']:.2f} "
                    f"off)** — Polymarket changed the resolution rule again. "
                    f"**Set `late_window.sniper_enabled: false` now** and verify "
                    f"a resolved market by hand before re-enabling.\n")

        if kt:
            action = ("**→ ACTION: the pre-registered shut-off line is crossed. "
                      "Set `sniper_enabled: false` in settings.yaml and restart.**")
        elif kt is None:
            action = "→ Too few live days for a verdict yet — nothing to do."
        else:
            action = "→ Nothing for you to do today."

        msg = (
            f"🎯 **Sniper daily — {today}**   {status}\n"
            f"Real money: {_money_line(live)}\n"
            f"{_legs_line()}"
            f"{_shutoff_line(live)}"
            f"{_context_line()}"
            f"{_twap_line()}"
            f"{action}"
        )
        if alert_manager:
            await alert_manager.send_health(msg)

        def _pick(r):
            # .get throughout — the TWAP sim's dict is a subset of the ledger
            # read's (no trailing/kill keys, t_day None under 2 fire-days).
            if r is None:
                return None
            t = r.get("t_day")
            return {"net_per_sh": r.get("net_per_sh"),
                    "t_day": (round(t, 2) if isinstance(t, (int, float)) and t == t else None),
                    "n_fills": r.get("n_fills"), "n_days": r.get("n_days"),
                    "trailing4_mean": r.get("trailing4_mean"),
                    "trailing8_t": r.get("trailing8_t"),
                    "kill_rule_tripped": r.get("kill_rule_tripped")}
        return {"health": status, "kill_rule_tripped": kt,
                "live": _pick(live), "sim": _pick(sim),
                "legs": (live or {}).get("legs"),
                }
    scheduler.register_job("sniper_health", _sniper_health_job)
    # Last: the analysis jobs above read the tape, so compress only after they
    # are done with it (readers handle .gz either way).
    scheduler.register_job("compress_recordings", compress_recordings_job())

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

    # Freeze the boot heap: full-GC pauses (multi-ms on the 1-core box) can
    # land mid-fire; freezing long-lived objects shrinks gen-2 scans to the
    # small post-boot heap. Thresholds stay modest — the box has 1 GB.
    import gc
    gc.collect()
    gc.freeze()
    gc.set_threshold(10_000, 20, 20)

    trading_task = asyncio.create_task(trading_loop(
        market_scanner, signal_engine,
        trader, alert_manager, db, config, outcome_reviewer,
        is_paused_fn=lambda: discord_bot.is_paused,
        scheduler=scheduler, clob_ws=clob_ws, breaker=breaker,
        ghost_tracker=ghost_tracker,
        http_client=http_client,
        chainlink_feed=chainlink_feed))
    async def _book_warmer() -> None:
        """Keep both tokens' REST book cache warm through the sniper window.

        The WS stores full book snapshots only when the exchange sends one, so
        by fire time the local copy looks stale and the fire path's fallback
        paid an inline ~95ms REST fetch (measured, fill 304). Warming the
        scanner's 2s book cache off-path turns that fallback into a sync hit.
        """
        while True:
            try:
                sec_rem = 300.0 - (time.time() % 300.0)
                contract = market_scanner._cached_contract
                if contract and sec_rem <= 60.0:
                    for tok in (contract.get("token_id_up"), contract.get("token_id_down")):
                        if tok:
                            await market_scanner.fetch_clob_book(tok, http_client)
                    await asyncio.sleep(1.4)
                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(2.0)

    background_tasks = [
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(_flush_staleness_loop()),
        asyncio.create_task(window_recorder.run()),
        asyncio.create_task(_book_warmer()),
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
        async def _stop_rec(coro, timeout=2.0):
            try: await asyncio.wait_for(coro, timeout=timeout)
            except Exception: pass
        # Every cleanup await is time-boxed: one hung unwind (a dead socket's
        # close handshake, Discord teardown) must never stall shutdown past
        # db.close() — skipping that leaves aiosqlite's non-daemon worker
        # threads holding the interpreter open forever.
        await _stop_rec(
            asyncio.gather(*background_tasks, return_exceptions=True), timeout=5.0)
        # A resting maker bid must never outlive the process — cancel + book
        # any accrued fill before the feeds die.
        if _MAKER_MGR is not None and _MAKER_MGR.active is not None:
            await _stop_rec(_MAKER_MGR._retire("shutdown"), timeout=5.0)
        await _stop_rec(window_recorder.stop())
        tape_recorder.flush()
        micro_tape.flush()
        await _stop_rec(http_client.aclose())
        async def _stop(coro):
            try: await asyncio.wait_for(coro, timeout=2.0)
            except Exception: pass
        if hasattr(trader, "stop_keepalive"):
            await _stop(trader.stop_keepalive())
        await _stop(clob_ws.close())
        await _stop(scheduler.stop())
        await _stop(chainlink_feed.stop())
        await _stop(discord_bot.close())
        bankroll = await db.get_bankroll()
        await db.close()
        logger.info(f"PolyBot stopped — Bankroll ${bankroll:.2f} · Feeds/WS/DB closed")


_SINGLE_INSTANCE_SOCK = None


def _acquire_single_instance(port: int = 49653) -> bool:
    """OS-level single-instance lock: bind a localhost port.

    No SO_REUSEADDR, so a second bind fails on Windows and POSIX alike; the OS
    releases the port on any exit (even a crash), so no stale lock. Returns
    False if another instance holds it — the backstop against a double-launch
    running two bots on one DB."""
    global _SINGLE_INSTANCE_SOCK
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return False
    s.listen(1)
    _SINGLE_INSTANCE_SOCK = s  # held for the process lifetime
    return True


def _make_sigint_handler(force_quit=os._exit):
    """Ctrl+C handler: first press = graceful teardown, second press = os._exit.

    The force-quit exists because a repeat Ctrl+C lands while the interpreter
    joins a lingering non-daemon thread (feed/websocket/aiosqlite worker) and
    hangs the process. Exit 130 is non-zero so the wrapper skips the commit."""
    state = {"count": 0}

    def _handler(signum=None, frame=None):
        state["count"] += 1
        # os.write only: buffered stderr from signal context hits Python's
        # reentrancy guard mid-write — a RuntimeError killed a live teardown.
        if state["count"] >= 2:
            try:
                os.write(2, b"\nForce-quitting (second signal).\n")
            except OSError:
                pass
            force_quit(130)
            return
        try:
            os.write(2, b"\nStopping PolyBot - second signal force-quits.\n")
        except OSError:
            pass
        raise KeyboardInterrupt

    return _handler


if __name__ == "__main__":
    # Last-breath instrumentation: a hard/native fault (C extension, stack
    # overflow) dumps every thread to crash_native.log, and any exception that
    # escapes main() is written to polybot.log before the process dies — the
    # cause of an exit must never depend on someone watching the console.
    import faulthandler
    try:
        faulthandler.enable(file=open("crash_native.log", "a"))
    except OSError:
        pass
    # uvloop (Linux box only): 2-4x lower loop overhead — burst frames + wakes
    # drain faster, shrinking the queue in front of signal ticks.
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    args = parse_args()
    try:
        if args.run_pipeline:
            asyncio.run(run_pipeline())
        else:
            if not _acquire_single_instance():
                logging.critical(
                    "Another PolyBot trading instance is already running — refusing "
                    "to start a second (single-instance lock). Exiting.")
                raise SystemExit(1)
            signal.signal(signal.SIGINT, _make_sigint_handler())
            if hasattr(signal, "SIGTERM") and os.name == "posix":
                # systemd's `systemctl stop` sends SIGTERM — route it through the
                # same graceful teardown as Ctrl+C, else the unit gets SIGKILLed
                # at TimeoutStopSec with no shutdown/commit parity.
                signal.signal(signal.SIGTERM, _make_sigint_handler())
            asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except OrphanPositionError as e:
        # Operator-actionable, not a code bug — remediation hint, no stack trace.
        # The orphan gate trips again at every boot until reconciled, so trading
        # stays down even though the supervisor loop restarts at the next 12:01 AM ET.
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
        # Hard-exit (not sys.exit): a boot that got far enough to open the DB
        # has aiosqlite's non-daemon worker alive, and a SystemExit would hang
        # the interpreter at thread-join — a zombie the supervisor waits on
        # forever instead of restarting.
        logging.shutdown()
        os._exit(2)
    except SystemExit:
        raise
    except BaseException:
        logging.critical("FATAL: unhandled exception escaped main()", exc_info=True)
        # Same zombie hazard as above, paid for in production: a crash that
        # bypassed graceful teardown hung on non-daemon threads for hours while
        # the supervisor thought it was trading. Crash-restart only works if we die.
        logging.shutdown()
        os._exit(1)
