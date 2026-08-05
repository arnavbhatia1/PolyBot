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
    GATE_STATS_CURRENT_PATH, PRICE_SUM_OUTLIERS_PATH, SCAR_GATES_PATH,
    SCAR_VETOES_PATH, fold_gate_day, write_json_atomic,
)
from polybot.core import scar_scan
from polybot.execution.base import entry_fee_shares, slippage_pct, DEFAULT_FEE_RATE, EFFECTIVE_FEE_PEAK, compute_buy_vwap
from polybot.db.models import Database
from polybot.feeds.binance_feed import BinanceFeed
from polybot.feeds.market_scanner import BTCMarketScanner
from polybot.feeds.clob_ws import ClobWebSocket
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine, TradeSignal
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
from polybot.feeds.binance_depth import BinanceDepthFeed
from polybot.feeds.binance_trades import BinanceTradesFeed, BinanceTradeAccumulator
from polybot.feeds.coinbase_feed import CoinbaseFeed
from polybot.feeds._staleness import snapshot_feeds as _staleness_snapshot, write_feeds as _staleness_write
from polybot.core.adverse_selection import AdverseSelectionMonitor

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

def _build_aux_signals(coinbase_feed: Any, trades_feed: Any = None) -> dict[str, Any]:
    """Aux microstructure signals shared by trade_context, ghosts, and counterfactual contexts.

    Every field is None when its feed is missing/stale or doesn't span the
    window yet — never 0.0, which would collide with a real zero reading.
    """
    cb_fresh = (coinbase_feed is not None
                and coinbase_feed.state.age_seconds < _AUX_FRESH_S_COINBASE
                and coinbase_feed.covers(60.0))

    cb_cvd = coinbase_feed.get_cvd(60.0) if cb_fresh else None
    if cb_fresh:
        cb_taker, cb_taker_n = coinbase_feed.get_taker_ratio(60.0)
    else:
        cb_taker, cb_taker_n = None, 0

    # cross_venue_gap = Coinbase (resolution venue) minus Binance last trade —
    # the lead the exit engine monetizes; fast vol from 1s Coinbase history.
    bt_acc = trades_feed.accumulator if trades_feed else None
    bt_fresh = bt_acc is not None and bt_acc.latest_age_s < _AUX_FRESH_S_TRADES
    cb_tick_fresh = (coinbase_feed is not None
                     and coinbase_feed.state.age_seconds < _AUX_FRESH_S_COINBASE)
    cb_price = coinbase_feed.state.price if cb_tick_fresh else None
    bn_price = bt_acc.latest_price if bt_fresh else None
    gap = (cb_price - bn_price) if (cb_price and bn_price) else None
    fast_rv = coinbase_feed.realized_vol(60.0) if cb_fresh else None

    # Burst counters for the HOT/COLD shadow tag (HOT = 1s tick rate >= 2x its
    # 30s baseline). Evidence only — nothing in the decision path reads these.
    cb_burst_fresh = (coinbase_feed is not None
                      and coinbase_feed.state.age_seconds < _AUX_FRESH_S_COINBASE
                      and coinbase_feed.covers(30.0))
    n_ticks_1s = coinbase_feed.trade_count(1.0) if cb_burst_fresh else None
    n_ticks_30s = coinbase_feed.trade_count(30.0) if cb_burst_fresh else None

    def _r(v: float | None, ndigits: int) -> float | None:
        return None if v is None else round(v, ndigits)

    return {
        "coinbase_cvd_60s": _r(cb_cvd, 4),
        "coinbase_taker_60s": _r(cb_taker, 4),
        "coinbase_taker_n": cb_taker_n,
        "cross_venue_gap": _r(gap, 2),
        "fast_realized_vol_60s": _r(fast_rv, 6),
        "n_ticks_1s": n_ticks_1s,
        "n_ticks_30s": n_ticks_30s,
    }

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


def _tercile(value: float | None, cuts: tuple[float, float]) -> str | None:
    if value is None:
        return None
    return "LO" if value < cuts[0] else ("MID" if value < cuts[1] else "HI")


def _regime_shadow_fields(atr: float, atr_long: float, atr_short_mean: float,
                          aux_signals: dict[str, Any], size: float,
                          bankroll: float, max_bankroll_pct: float) -> dict[str, Any]:
    """The shadow stamp block for a fill's trade_context. None buckets mean the
    input feed was cold at fire time (never 0.0 stand-ins); size_regime == 0
    means the regime-sized arm would have skipped the fill ($1 CLOB floor)."""
    n1, n30 = aux_signals.get("n_ticks_1s"), aux_signals.get("n_ticks_30s")
    burst = None
    if n1 is not None and n30:
        burst = "HOT" if (n1 / (n30 / 30.0)) >= _REGIME_BURST_HOT_RATIO else "COLD"
    lo, hi = _REGIME_MULT_CLAMP
    mult = max(lo, min(hi, _REGIME_MULT_TABLE.get(burst, 1.0)))
    size_regime = round(min(size * mult, bankroll * max_bankroll_pct), 2)
    if size_regime < 1.0:
        size_regime = 0.0
    return {
        "regime_buckets": {
            "atr_regime": _tercile(atr / atr_long if (atr > 0 and atr_long > 0) else None,
                                   _REGIME_CUTS_ATR_REGIME),
            "atr_short": _tercile(atr / atr_short_mean if (atr > 0 and atr_short_mean > 0) else None,
                                  _REGIME_CUTS_ATR_SHORT),
            "frv": _tercile(aux_signals.get("fast_realized_vol_60s"), _REGIME_CUTS_FRV),
            "session": ("ON", "DAY", "EVE")[datetime.now(ET).hour // 8],
            "burst": burst,
        },
        "regime_kelly_mult": mult,
        "size_flat": round(size, 2),
        "size_regime": size_regime,
    }


# ── Scar stamps + registry (core/scar_scan.py — the nightly learning loop) ───
# mtime-cached so the fire path never re-reads an unchanged registry; the file
# only changes when the nightly scan (or a git pull) rewrites it.
_scar_registry_cache: tuple[float, dict] = (-1.0, {"version": 1, "gates": []})

def _scar_registry() -> dict:
    global _scar_registry_cache
    try:
        mtime = SCAR_GATES_PATH.stat().st_mtime
    except OSError:
        return _scar_registry_cache[1]
    if mtime != _scar_registry_cache[0]:
        _scar_registry_cache = (mtime, scar_scan.load_registry(SCAR_GATES_PATH))
    return _scar_registry_cache[1]


def _scar_fields(cid: str, side: str, ask: float,
                 cb_move: float | None) -> dict[str, Any]:
    """Fire-time facts the scar dimensions need that nothing else stamps.

    refire_class: an earlier kill this window means the book repriced away;
    re-firing at/below that price means it came back. Stamp at fire time —
    reconstructing these facts post-hoc was irreducibly ambiguous."""
    kills: list[float] = []
    try:
        kills = _window_killed_asks.get(int(cid.rsplit("-", 1)[-1]), {}).get(side, [])
    except (ValueError, IndexError):
        pass
    if not kills:
        refire = "first_fire"
    elif ask <= min(kills) + 1e-9:
        refire = "refire_leq_kill"
    else:
        refire = "refire_above_kill"
    return {
        "scar_refire_class": refire,
        "scar_killed_n": len(kills),
        "scar_kill_min_ask": min(kills) if kills else None,
        "scar_cb_move": round(abs(cb_move), 2) if cb_move is not None else None,
    }


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
    _dist = ctx["btc_price"] - ctx["strike"]
    _phase = ctx["phase"]
    logger.info(
        f"{_C.YELLOW}{'=' * 60}{_C.RESET}\n"
        f"  {_C.YELLOW}{_C.BOLD}OPEN {ctx['side']}{_C.RESET} @{entry_price:.2f}  ${ctx['size']:.2f}  "
        f"{fee_str}  |  "
        f"{_slug_to_window(ctx['cid'])}{'' if _phase == 'normal' else f' [{_phase}]'}{slip_note}{prov}\n"
        f"  {_C.DIM}BTC {ctx['btc_price']:,.0f} ({_dist:+.0f} vs strike) · model {ctx['prob']:.0%} "
        f"edge {ctx['edge']:+.0%} · flow {ctx['flow']:+.2f} cvd {ctx['cvd']:+.2f} · "
        f"bank ${ctx['bankroll']:.2f}{_C.RESET}\n"
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
# SNIPE line once per (window, side) per 10s — the loop re-evaluates on every
# Coinbase tick while a burst holds, which spams the same line at tick rate.
_last_snipe_log: dict[tuple[int, str], float] = {}
_resolve_oracle_logged: set[str] = set()  # market_id — RESOLVE oracle line printed once
_SNIPER_ONLY_QUIET = True  # base entries are always suppressed (sniper-only), so their per-gate SKIP lines are noise -> DEBUG
_abandoned_scalp_positions: set[int] = set()  # position IDs too small to sell, hold to resolution

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
            f"{_fmt_secs(live.get('seconds_remaining', 0))}  |  Stale feeds"
        )
        logger.debug("stale-feed detail: %s", reason)


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


def _pregate_should_eval(now: float, last_eval_ts: float, sec_rem: float,
                         cb_move_abs: float, late_start_s: float,
                         move_thresh: float, sniper_on: bool) -> bool:
    """µs pre-gate: does this wake deserve the full 30-80ms evaluation?

    A fire-adjacent Coinbase move (≥60% of the sniper threshold, inside the
    late window) ALWAYS evaluates — no fire can be missed. Everything else is
    throttled (4Hz late-window, 1Hz otherwise): chained full evaluations on
    every burst book-tick were the 392ms queue in front of real signals.
    Ghost/skip records are per-(window, gate) deduped, so throttling changes
    their timestamp by <1s and their content not at all.
    """
    if sniper_on and sec_rem <= late_start_s and cb_move_abs >= 0.6 * move_thresh:
        return True
    return (now - last_eval_ts) >= (0.25 if sec_rem <= late_start_s else 1.0)


def _fmt_secs(s: float) -> str:
    """Seconds remaining formatted as M:SS — 298 → '4:58'. Easier to scan than '298s'."""
    s_int = max(0, int(s))
    return f"{s_int // 60}:{s_int % 60:02d}"


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

# Killed sniper FOKs this window: window_ts -> side -> [decision asks]. Feeds
# the scar_refire_class stamp (was this fire a re-attempt at/below a price the
# book already repriced away from?). Swept with the _strike_trusted 600s idiom.
_window_killed_asks: dict[int, dict[str, list[float]]] = {}

# Enforced scar vetoes already journaled this window: window_ts -> gate names.
# One journal line per (window, gate) — the veto repeats every evaluation tick,
# the evidence record must not.
_scar_vetoed: dict[int, set] = {}

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
_cf_check_task: Any = None  # in-flight guard — one background CF sweep at a time


def _build_signal_engine(signal_cfg: dict, config: dict) -> SignalEngine:
    """Construct SignalEngine from config — shared between pipeline and main."""
    return SignalEngine(
        min_edge=signal_cfg["min_edge"],
        kelly_fraction=config["math"]["kelly_fraction"],
        min_model_probability=signal_cfg["min_model_probability"],
        student_t_df=signal_cfg["student_t_df"],
        regime_lookback=signal_cfg["regime_lookback"],
        min_kelly=signal_cfg["min_kelly"],
        atr_sigma_ratio=signal_cfg["atr_sigma_ratio"],
        min_atr=signal_cfg["min_atr"],
        loss_cut_fraction=signal_cfg["loss_cut_fraction"],
        loss_cut_time_s=signal_cfg["loss_cut_time_s"],
        deep_loss_hold_threshold=signal_cfg["deep_loss_hold_threshold"],
        atr_regime_shift_threshold=signal_cfg["atr_regime_shift_threshold"],
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
        price_up: float, price_down: float,
        book_up: dict[str, Any], book_down: dict[str, Any],
        depth_usd_up: float, depth_usd_down: float,
        btc_price: float, strike: float, eval_window: int, last_eval_log_window: int,
        token_up: str, token_down: str, signal_config: dict[str, Any],
        max_bankroll_pct: float,
        bankroll: float = 0.0,
        depth_feed: Any = None,
        trades_feed: Any = None,
        coinbase_feed: Any = None,
        chainlink_feed: Any = None,
        ghost_tracker: Any = None) -> tuple[str | None, int]:
    """Compute indicators/flow/signal, check for entry, size the trade, execute."""

    # Coinbase tick this evaluation decides on — its delta to the pre-submit
    # moment stamps cb_tick_to_submit_ms, so decision latency is measured per
    # fill, not inferred. Sign + POST legs are recorded in latency_stats.
    _eval_tick_ts = coinbase_feed.state.updated_at if coinbase_feed is not None else 0.0
    # Same tick's feed-transit leg (Coinbase match → receipt) — captured here so
    # it can't be overwritten by a newer tick before the context is built.
    _eval_feed_delay_ms = (coinbase_feed.state.feed_delay_ms
                           if coinbase_feed is not None else None)
    _eval_clob_delay_ms = clob_ws.feed_delay_ms if clob_ws is not None else None

    # Stamped once per evaluation so ghosts and filled outcomes share one schema;
    # aux fields are a real value or None — never a 0.0 stand-in.
    aux_signals = _build_aux_signals(coinbase_feed, trades_feed)
    aux_signals.update(_clob_book_aux(clob_ws, token_up, token_down, book_up, book_down))

    # Neutral defaults so ghosts recorded BEFORE the adverse gate still stamp
    # the audit fields; the gate's real values rebind before downstream ghosts.
    adverse_kelly_mult = 1.0
    adverse_rate_at_30s = -1.0

    def _depth_usd_top20() -> float | None:
        """Binance top-20 depth, None when the feed is absent/stale/empty."""
        if depth_feed is None or getattr(depth_feed, "updated_at", 0.0) <= 0:
            return None
        if time.time() - depth_feed.updated_at >= 5:
            return None
        return depth_feed.get_depth_usd() or None

    def _ghost(gate: str, signal: Any, snap: dict) -> None:
        """Record a ghost trade when a downstream gate rejects a real BUY signal.

        Base trade_context comes from closure vars at gate-fire time; the
        caller's snap merges on top (caller wins on overlapping keys).
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
            "depth_usd_top20": _depth_usd_top20(),
            "adverse_rate_at_30s": adverse_rate_at_30s if adverse_rate_at_30s >= 0 else None,
            "adverse_kelly_mult": round(adverse_kelly_mult, 3),
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

    # Shared helper in core/aux_layers.py — entry and exit paths both call it
    # so the model math is identical.
    _vol_factor = regime_vol_factor(
        indicators.get("atr", {}).get("atr", 0.0), signal_engine.last_atr_long_term_mean)
    spot_flow_signal = compute_spot_flow_signal(
        aux_signals.get("coinbase_cvd_60s"),
        aux_signals.get("coinbase_taker_60s"),
        aux_signals.get("coinbase_taker_n", 0),
        vol_factor=_vol_factor,
    )
    # Cold-vs-real-zero split: the live model consumes a number (cold collapses
    # to 0.0), but the RECORDED value must be None when the feed is cold —
    # spot_flow cold when Coinbase CVD is None; book flow cold when neither
    # CLOB book nor any trade is present.
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

    # --- LATE-WINDOW SNIPER (behind sniper_enabled, the kill-bar safety) ----------
    # A sharp Coinbase move (the resolution venue) pushed price past strike but
    # the CLOB ask hasn't repriced — stale-book lag in OUR favor. Remaps to a
    # normal BUY so ALL sizing/exec/safety gates run unchanged; only max_edge
    # (-> sniper_max_edge) and the time penalty are bypassed.
    is_sniper = False
    lw_cfg = config.get("late_window", {})

    # Time multiplier, computed BEFORE the ghost below so a suppressed base
    # entry records its true entry_phase; the sniper overrides to late_sniper.
    timing_cfg = config.get("entry_timing", {})
    time_mult, phase = compute_time_multiplier(
        prob=signal.prob,
        seconds_remaining=contract["seconds_remaining"],
        normal_fraction=timing_cfg["normal_fraction"],
        late_max_penalty=timing_cfg["late_max_penalty"],
    )

    # The sniper is the SOLE capital-deploying strategy. Base BUYs never trade —
    # each becomes a "sniper_only" ghost, keeping the base strategy's zero-cost
    # evidence stream alive for the deployment gate.
    if signal.action in ("BUY_YES", "BUY_NO"):
        _ghost("sniper_only", signal, {})
        signal = TradeSignal("SKIP", signal.prob, signal.edge, 0,
                             "base entry suppressed — sniper is the only strategy (ghosted)",
                             side=signal.side)

    if (lw_cfg["sniper_enabled"]
            and signal.action not in ("BUY_YES", "BUY_NO")
            and coinbase_feed is not None
            and contract["seconds_remaining"] <= lw_cfg["sniper_late_start_s"]):
        # Capital only deploys on a TRUSTED strike (Gamma price_to_beat, or a
        # Chainlink boundary capture with no delivery hole). An untrusted strike
        # can be $35+ off Polymarket's, making move-past-strike a coin flip.
        try:
            _w_ts = int(cid.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            _w_ts = -1
        if not _strike_trusted.get(_w_ts, False):
            _emit_gate_skip(cid, "sniper_strike_unverified",
                            "sniper: strike unverified (RTDS boundary gap — value may differ "
                            "from Polymarket's price_to_beat)", quiet=_SNIPER_ONLY_QUIET)
            _snipe = TradeSignal("SKIP", signal.prob, signal.edge, 0,
                                 "sniper: strike unverified", side=signal.side)
        else:
            _cbm = coinbase_feed.cb_move(lw_cfg["sniper_move_window_s"])
            _snipe = signal_engine.evaluate_late_sniper(
                indicators, btc_price, strike, contract["seconds_remaining"],
                price_up, price_down, _cbm,
                lw_cfg["sniper_cb_move"],
                lw_cfg["sniper_ask_cap"],
                lw_cfg["sniper_min_edge"],
                fee_rate=fee_rate, closes=closes)
        if _snipe.action in ("LATE_SNIPE_YES", "LATE_SNIPE_NO"):
            _snipe.action = "BUY_YES" if _snipe.action == "LATE_SNIPE_YES" else "BUY_NO"
            signal = _snipe
            is_sniper = True
            _snipe_key = (_w_ts, signal.side)
            _snipe_now = time.time()
            if _snipe_now - _last_snipe_log.get(_snipe_key, 0.0) >= 10.0:
                if len(_last_snipe_log) > 64:
                    _last_snipe_log.clear()
                _last_snipe_log[_snipe_key] = _snipe_now
                logger.info(f"{_C.DIM}SNIPE {signal.side} — Coinbase {_cbm:+.0f} past strike | "
                            f"Model {signal.prob:.0%} Edge {signal.edge:+.0%}{_C.RESET}")

    if is_sniper:                       # the sniper bypasses the late-window time penalty
        time_mult, phase = 1.0, "late_sniper"

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
        _emit_gate_skip(cid, f"model_{_reason_type}", signal.reason,
                        quiet=_SNIPER_ONLY_QUIET)

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
                    # Schema parity with _ghost(): ghost and filled records share one schema.
                    "entry_phase": phase,
                    "flip_count": _st_flip_count,
                    "is_flip": _st_flip_count > 0,
                    **aux_signals,
                }},
            )
        return None, last_eval_log_window

    # --- ADVERSE SELECTION (sizing penalty + emergency hard-skip) ---
    if _adverse_monitor is not None:
        adverse_rate_at_30s = _adverse_monitor.get_adverse_rate(30.0)
        sig_cfg = config.get("signal", {})
        hard_skip_at = float(sig_cfg["adverse_selection_threshold"])
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
                    f"{_C.DIM}SKIP {signal.side} — {adverse_rate_at_30s:.0%} of recent "
                    f"fills faded after filling{_C.RESET}"
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
                f"Recent fills drifting against us ({recent_decay:+.3f} in 15s)"
            )
            return None, last_eval_log_window

    # --- EDGE CAP GATE ---
    # The cap dodges stale phantom prices, but can't tell a stale-against-us phantom
    # (bad) from the sniper's stale-in-our-favor lag (the edge) — so the sniper swaps
    # in its own wider sanity cap (sniper_max_edge) instead of the 0.20 entry cap.
    max_edge = config.get("signal", {}).get("max_edge", 0.20)
    if is_sniper:
        max_edge = lw_cfg["sniper_max_edge"]
    if signal.edge > max_edge:
        _record_skip("edge_cap")
        _ghost("edge_cap", signal, {})
        return None, last_eval_log_window

    side = "Up" if signal.action == "BUY_YES" else "Down"
    token_id = contract["token_id_up"] if side == "Up" else contract["token_id_down"]

    flip_state = _window_flip_state.setdefault(cid, {"flip_count": 0})
    flip_count = flip_state["flip_count"]
    if flip_count >= 1:
        # Flips 1–2 pay the base premium; +0.5pp per flip beyond the 2nd, unbounded.
        flip_premium_base = config.get("entry_timing", {})["flip_edge_premium"]
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
            # Same per-evaluation fee_rate as the Kelly gate and the booking —
            # this hurdle must price the round trip at the rate the trade pays.
            fee_impact_one_leg = fee_rate * side_price * (1.0 - side_price)
            spread_cost = spread_est + 2.0 * fee_impact_one_leg
        else:
            spread_cost = flip_premium
        flip_hurdle = signal_engine.min_edge + max(flip_premium, spread_cost)
        if signal.edge < flip_hurdle:
            _record_skip("flip_insufficient_edge")
            _ghost("flip_insufficient_edge", signal, {})
            return None, last_eval_log_window

    price = price_up if side == "Up" else price_down
    signal_ask = price   # executable ask the model decided on, before the FOK-limit chase pad
    if not bankroll:
        bankroll = await db.get_bankroll()
    kelly_mult = breaker.kelly_multiplier if breaker else 1.0


    raw_kelly_size = bankroll * signal.kelly_size
    size = round(raw_kelly_size * kelly_mult * time_mult, 2)

    size = round(size * adverse_kelly_mult, 2)

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

    # Book-depth fill cap. The upstream thin-CLOB gate passes if EITHER side has
    # depth ≥ min, so the chosen side can still be the empty leg of a one-sided
    # book — explicit skip rather than a full-Kelly order into 0 liquidity.
    side_depth = depth_usd_up if side == "Up" else depth_usd_down
    max_fill_pct = config.get("execution", {}).get("max_book_fill_pct", 0.50)
    # Same floor as the upstream both-sides-thin gate so the two can't drift apart.
    min_side_depth = market_scanner.min_book_depth_usd
    if side_depth < min_side_depth:
        _record_skip("thin_book_depth")
        # Ghost it — one-sided books land here (upstream passes on EITHER side
        # ≥ $50) and the thin-side fire population needs resolved evidence too.
        _ghost("thin_book_depth", signal, {})
        _emit_gate_skip(cid, "thin_book_depth",
                        f"Thin book on the {side} side (${side_depth:.0f} < ${min_side_depth:.0f})")
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
        _emit_gate_skip(cid, "net_edge_slippage", f"Edge gone after slippage ({net_edge:+.1%})")
        return None, last_eval_log_window

    # Final min-size check after all caps: Polymarket's CLOB rejects orders below
    # $1 notional. Paper mirrors the floor so paper and live behave identically.
    if size < 1.0:
        _record_skip("min_size")
        # Ghost it — the most active sniper veto at live bankrolls (anchored
        # Kelly on cheap asks lands under $1); the resolved ghosts are the
        # evidence for any future sizing-anchor change.
        _ghost("min_size", signal, {})
        _emit_gate_skip(cid, "min_size", f"Order below minimum (${size:.2f} < $1)")
        return None, last_eval_log_window

    # fee_rate already fetched before signal eval (used by Kelly). tick_size
    # is per-chosen-side so fetched here.
    tick_size = await market_scanner.fetch_tick_size(token_id, http_client)
    _loop_marks["m_tick"] = time.time()
    fresh_bba = clob_ws.best_bid_ask.get(token_id, {}) if clob_ws else {}
    _fresh_bba_ts = float(fresh_bba.get("ts", 0) or 0)
    fresh_ask = (float(fresh_bba.get("best_ask", 0) or 0)
                 if _fresh_bba_ts > 0 and (time.time() - _fresh_bba_ts) <= _WS_STALE_S
                 else 0.0)

    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    slip = slippage_pct(size, side_depth, impact)
    if is_sniper:
        # Sniper FOK limit: pad the decision ask by sniper_fok_slip (~one tick),
        # then die. The pad absorbs jitter; a genuine reprice KILLS the order,
        # and that kill IS the adverse-selection filter (repricing away = the
        # move is reverting). Never widen the pad: chased fills measured
        # −16¢/sh vs +9¢/sh clean. Gates ran at the decision ask; the pre-submit
        # VWAP re-check still vetoes books that lost the edge.
        _fok_slip = lw_cfg["sniper_fok_slip"]
        # Never chase above model_prob − min_edge: a fill there would carry less
        # than the pre-registered edge floor (e.g. a 0.93 limit on a 94% model).
        # The FOK fills at book prices, so the cap binds only when the book has
        # truly repriced — exactly when the chase should stop.
        _limit_cap = signal.prob - signal_engine.min_edge
        price = market_scanner.snap_to_tick(
            max(price, min(price + _fok_slip, _limit_cap)), tick_size)
    else:
        # Base entries keep a tight slip (no FOK-cross floor): a reject on
        # adverse movement is a feature — it stops buying post-reversal tops.
        # Exits use a loose floor instead — there we must fill to avoid lockout.
        price = market_scanner.snap_to_tick(price * (1 + slip), tick_size)

    snapshot = indicator_engine.get_snapshot(indicators)
    # Last two closes — an entry fact for the exit-value model / counterfactual replay.
    _closes_buf = binance_feed.buffer.get_closes()
    _closes_tail = (
        [float(_closes_buf[-2]), float(_closes_buf[-1])]
        if len(_closes_buf) >= 2 else None
    )

    _cl_age_at_fire = None
    _cl_px_at_fire = None
    if chainlink_feed is not None:
        _cl_age = getattr(chainlink_feed, "age_seconds", None)
        if _cl_age is not None and math.isfinite(_cl_age):
            _cl_age_at_fire = round(_cl_age, 3)
            _cl_px = getattr(chainlink_feed, "price", 0.0)
            # ≤5s fresh: staleness gates already passed at ≤60s, but a cross-
            # confirm read needs the report to be from the burst itself.
            if _cl_px > 0 and _cl_age <= 5.0:
                _cl_px_at_fire = _cl_px
    snapshot["trade_context"] = {
        # Entry-time facts — recorded for the counterfactual replay harness and exit-value model
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
        # Time-of-window telemetry ("late_sniper" on every sniper fill by construction)
        "entry_phase": phase,
        "flip_count": flip_count,
        "is_flip": flip_count > 0,
        # Microstructure aux, stamped from the once-per-evaluation `aux_signals`
        # dict (same schema as ghosts). None means "feed cold/stale", never 0.0.
        "depth_usd_top20": _depth_usd_top20(),
        **aux_signals,
        # Adverse-selection diagnostic — 30s is the post-fill checkpoint, not
        # the lookback. None when never computed — never a stand-in value.
        "adverse_rate_at_30s": adverse_rate_at_30s if adverse_rate_at_30s >= 0 else None,
        "adverse_kelly_mult": round(adverse_kelly_mult, 3),
        "cb_tick_to_submit_ms": (round((time.time() - _eval_tick_ts) * 1000.0, 1)
                                 if _eval_tick_ts > 0 else None),
        # Latency breakdown of the same span (observational, this iteration's
        # marks): tick receipt -> loop wake -> entry evaluation start.
        "lat_tick_to_wake_ms": (round((_loop_marks["wake"] - _eval_tick_ts) * 1000.0, 1)
                                if _eval_tick_ts > 0 and _loop_marks.get("wake", 0) >= _eval_tick_ts
                                else None),
        "lat_wake_to_eval_ms": (round((_loop_marks["pre_eval"] - _loop_marks["wake"]) * 1000.0, 1)
                                if _loop_marks.get("wake", 0) > 0
                                and _loop_marks.get("pre_eval", 0) >= _loop_marks.get("wake", 0)
                                else None),
        "lat_fast_path": bool(_loop_marks.get("fast")),
        "lat_cb_woke": bool(_loop_marks.get("cb_woke")),
        "lat_cb_feed_ms": _eval_feed_delay_ms,
        "lat_clob_feed_ms": _eval_clob_delay_ms,
        # Microscope: per-segment deltas (ms) through this iteration's pre-submit
        # path — wake → sched → pregate → discovery → prices → sizing-positions →
        # tick-size → ctx. Names the exact await that eats time under burst.
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
        # Regime-Kelly SHADOW stamps (frozen cuts; sizing untouched) — the
        # nightly counterfactual-D read and its gated SPRT consume these.
        **_regime_shadow_fields(
            indicators.get("atr", {}).get("atr", 0) or 0.0,
            signal_engine.last_atr_long_term_mean, signal_engine.last_atr_rolling_20,
            aux_signals, size, bankroll, max_bankroll_pct),
        # Scar stamps (fire-time facts for the nightly scan's dimensions), on
        # the DECISION ask (signal_ask) — `price` is already the padded FOK
        # limit here, and the limit cap binding would poison ask-vs-ask
        # comparisons. _cbm exists whenever is_sniper is True.
        **_scar_fields(cid, side, signal_ask, _cbm if is_sniper else None),
        # Burst-shape + oracle-confirm stamps (discovery-only scar dims; None on
        # cold, never 0.0): the 10s move brackets the 2s burst (isolated spike
        # vs extending — reverting spikes are the measured bleed), and the
        # resolution venue's own fresh report either confirms the strike cross
        # or the fire's premise rests on Coinbase alone.
        "scar_cb_move_10s": (coinbase_feed.cb_move(10.0)
                             if is_sniper and coinbase_feed else None),
        "chainlink_price_at_fire": _cl_px_at_fire,
        "chainlink_age_s_at_fire": _cl_age_at_fire,
        # Token IDs for both outcomes — required for startup reconciliation and dust sweeping.
        "token_id_up": contract.get("token_id_up", ""),
        "token_id_down": contract.get("token_id_down", ""),
    }
    # Scar-gate enforcement — learned vetoes, OFF by default. A gate vetoes only
    # if it GRADUATED its OOS SPRT AND the operator listed it in
    # late_window.scar_enforce (a shadow gate in the list is ignored). Journal
    # ONCE per (window, gate): the sniper re-evaluates every tick and duplicate
    # lines would tick-weight the nightly resolution. Fail-OPEN — a scar error
    # must never block trading (the kill rule owns shutoff, not this).
    _scar_enforce = lw_cfg.get("scar_enforce") or []
    if _scar_enforce:
        try:
            _scar_hits = [g for g in scar_scan.fire_time_matches(
                snapshot["trade_context"], side, datetime.now(ET).strftime("%a"),
                _scar_registry(), statuses=("graduated",)) if g in _scar_enforce]
        except Exception as e:
            logger.warning("scar enforce check failed (fail-open): %s", e)
            _scar_hits = []
        if _scar_hits:
            _gate = f"scar:{_scar_hits[0]}"
            _record_skip(_gate)
            try:
                _sw = int(cid.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                _sw = -1
            _now_ts = int(time.time())
            for k in [k for k in _scar_vetoed if _now_ts - k >= 600]:
                del _scar_vetoed[k]
            # EVERY matching gate journals — a co-firing gate whose vetoes only
            # counted when it fired alone would read better than reality.
            _first_new = True
            for _hit in _scar_hits:
                if _hit in _scar_vetoed.get(_sw, set()):
                    continue
                _scar_vetoed.setdefault(_sw, set()).add(_hit)
                if _first_new:
                    _ghost(_gate, signal, snapshot)
                    _emit_gate_skip(cid, _gate, f"SCAR Veto ({_scar_hits[0]})")
                    _first_new = False
                scar_scan.record_veto(SCAR_VETOES_PATH, _hit, cid, side,
                                      signal_ask, size)
            return None, last_eval_log_window
    # Pre-submit edge re-check: walk the ask ladder for the actual expected FOK
    # VWAP (the book is ground truth vs the modeled slip). Book unavailable/too
    # thin → fall back to the BBA-only fresh_ask gate, so this never tightens a
    # path the BBA gate would have passed.
    max_edge_live = config.get("signal", {}).get("max_edge", 0.20)
    if is_sniper:                       # mirror the EDGE CAP gate — the sniper's high edge is the point
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
        # scar_refire_class against it. Reason shapes: live "book moved so no
        # fill" / "price moved before fill after N attempts" / "unmatched
        # status … — cancelled, no fill"; paper "Price moved before fill".
        _rl = reason.lower()
        _killed = "no fill" in _rl or _rl.startswith("price moved")
        if is_sniper and _killed:
            _record_killed_ask(cid, side, signal_ask)
        _log_skip_once(
            cid, f"open_rejected_{reason}",
            f"{_C.DIM}OPEN {side} REJECTED  ${size:.2f} @ {price:.2f} — "
            f"{'Book repriced' if _killed else reason}{_C.RESET}"
        )
        return None, last_eval_log_window

    if result.success:
        # Drop the open-positions cache so the next tick sees this position immediately.
        _invalidate_open_positions_cache()
        if _window_recorder is not None:
            _window_recorder.mark_traded(cid)
        # Actual fill price (paper latency/book-walk or live FOK slippage may differ).
        fill_price = result.fill_price if result.fill_price > 0 else price
        shares_ordered = size / fill_price
        fee_shares = entry_fee_shares(shares_ordered, fill_price, fee_rate)
        fee_usd = fee_shares * fill_price
        bankroll_now = await db.get_bankroll()
        # signal = the ask the model decided on; posted = the (padded) FOK limit we
        # sent to the CLOB; filled = the realized fill. signal->posted is the chase
        # bubble (sniper_fok_slip / base slip pad); posted->filled is realized slippage.
        _banner_ctx = {
            "side": side, "size": size, "cid": cid, "phase": phase,
            "signal_ask": signal_ask, "posted": price,
            "btc_price": btc_price, "strike": strike,
            "prob": signal.prob, "edge": signal.edge,
            "flow": flow_score, "cvd": spot_flow_signal,
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
        if _adverse_monitor:
            # Baseline must live on the same axis as the post-fill checkpoints
            # (update_prices): the traded token's own mid. Falls back to the
            # fill price (same axis) when the WS book is stale.
            token_mid = _get_token_midprice(clob_ws)(token_id) if clob_ws else 0.0
            _adverse_monitor.record_fill(side=side, fill_price=fill_price, token_id=token_id,
                                         midprice=token_mid or fill_price,
                                         position_id=result.position_id)
        return cid, last_eval_log_window

    return None, last_eval_log_window


def _compute_strike_and_btc(cid: str, binance_feed: Any, window_strikes: dict[int, float],
                            eval_window: int,
                            last_eval_log_window: int,
                            chainlink_feed: Any = None,
                            coinbase_feed: Any = None,
                            contract: Any = None,
                            **kwargs) -> tuple[float | None, float | None, dict[int, float], int, str]:
    """Derive strike and BTC price for a window.

    Strike = Chainlink's first report at/after the boundary (the exact
    price_to_beat rule); Gamma's price_to_beat WINS whenever served."""
    now_ts = int(time.time())

    try:
        contract_window_ts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        contract_window_ts = int(now_ts // 300) * 300  # fallback

    # Decision strike = Chainlink's first report AT/AFTER the boundary — the
    # exact rule Polymarket's price_to_beat uses (capturing the LAST report
    # BEFORE it missed the official round by >$8 in fast opens). Gamma's
    # price_to_beat is the same value but served late/unreliably in-window.
    cl_strike = chainlink_feed.get_strike(contract_window_ts) if chainlink_feed else None
    ptb = (contract or {}).get("event_metadata") or {}
    ptb = ptb.get("price_to_beat") if isinstance(ptb, dict) else None
    if ptb and ptb > 0:
        # Gamma's price_to_beat is the RESOLVED truth — once served it WINS over
        # our own capture: an RTDS delivery hole can lock a first-received
        # report that is NOT Polymarket's (~1-2% of windows, $35+ off).
        prev = window_strikes.get(contract_window_ts)
        if prev is not None and abs(prev - ptb) > 0.005:
            logger.warning(f"Strike Corrected {_slug_to_window(cid)}: ${prev:,.2f} → ${ptb:,.2f}")
        window_strikes[contract_window_ts] = ptb
        _strike_trusted[contract_window_ts] = True
        if contract_window_ts not in _strike_logged:
            logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${ptb:,.2f} (Polymarket){_C.RESET}")
            _strike_logged.add(contract_window_ts)
    elif cl_strike and cl_strike > 0:
        window_strikes[contract_window_ts] = cl_strike     # the sniper reads this — set every loop
        _strike_trusted[contract_window_ts] = (
            chainlink_feed.strike_reliable(contract_window_ts) if chainlink_feed else False)
        # Log ONE line per window, when the boundary value LOCKS — before that
        # get_strike serves a cold-start fallback that ticks with the live price.
        locked = chainlink_feed.boundary_captured(contract_window_ts) if chainlink_feed else False
        if locked and contract_window_ts not in _strike_logged:
            logger.info(f"{_C.CYAN}NEW WINDOW {_slug_to_window(cid)} | Strike ${cl_strike:,.2f} (Chainlink){_C.RESET}")
            _strike_logged.add(contract_window_ts)
            _strike_logged.difference_update({k for k in _strike_logged if now_ts - k >= 600})

    window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}
    for k in [k for k in _strike_trusted if now_ts - k >= 600]:
        del _strike_trusted[k]

    strike = window_strikes.get(contract_window_ts, 0)
    if strike <= 0:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL {_slug_to_window(cid)} - No Polymarket strike captured yet")
        return None, None, window_strikes, last_eval_log_window, "none"

    # BTC price comes from Coinbase WS only (the venue Chainlink resolves against);
    # a stale Coinbase feed returns 0 here and skips the decision.
    trades_feed = kwargs.get("trades_feed")
    btc_price, _price_source = _fastest_btc_price(coinbase_feed, trades_feed, binance_feed)
    if btc_price <= 0:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL {_slug_to_window(cid)} - No Coinbase BTC price yet")
        return None, None, window_strikes, last_eval_log_window, "none"

    # Skip if candle data is stale (WebSocket may have disconnected)
    latest_candle_age = binance_feed.buffer.latest_age_s if binance_feed and binance_feed.buffer else float("inf")
    if latest_candle_age > 180:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.warning(f"Stale Binance Candle ({latest_candle_age:.0f}s old) — Skipping entry")
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
    # Union both watchlists: untraded ghost windows get no Gamma metadata from
    # anywhere else — sniper veto ghosts died unresolved (0 persisted live).
    watched = set(counterfactual_tracker.watched_markets)
    if ghost_tracker is not None:
        watched |= set(ghost_tracker.watched_markets)
    markets_to_fetch = [m for m in watched if m not in cf_event_metadata]
    if markets_to_fetch:
        # Look up each watched market by its exact slug — _get_contract_prices only checks
        # the current ±1 window, so it returns None for markets from 10+ minutes ago.
        async def _fetch_by_slug(slug: str) -> dict | None:
            try:
                data = await market_scanner.gamma_events_by_slug(http_client, slug)
                if data:
                    return market_scanner.parse_contract(data[0])
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
    # Loss-cut math (BTC vs strike + ATR) is independent of flow/Chainlink; only
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
            cl_str = f"  CL ${chainlink_feed.price:,.0f}" if chainlink_feed and chainlink_feed.price > 0 else ""
            logger.info(
                f"  {_C.DIM}HOLD {pos['side']}{_C.RESET}  {_fmt_secs(live['seconds_remaining'])}  |  "
                f"CB ${btc_now:,.0f} {_btc_src.replace('coinbase ', '')}{cl_str}  (No fresh bid)"
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

    # Same helper as the entry path — identical model math via aux_layers.
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
        # Log hold status every 30s so the operator knows the bot is alive
        now_ts = time.time()
        if now_ts - _last_hold_log.get(mid, 0) >= 30:
            _last_hold_log[mid] = now_ts
            if abs(holding_edge) < 0.005:
                edge_color, edge_str = _C.GREEN, "0%"
            else:
                edge_color = _C.GREEN if holding_edge > 0 else _C.RED
                edge_str = f"{holding_edge:+.0%}"
            cl_str = f"  CL ${chainlink_feed.price:,.0f}" if chainlink_feed and chainlink_feed.price > 0 else ""
            logger.info(
                f"  {_C.DIM}HOLD {pos['side']}{_C.RESET}  {_fmt_secs(live['seconds_remaining'])}  |  "
                f"model {model_prob:.0%}  mkt {market_price:.0%}  {edge_color}edge {edge_str}{_C.RESET}  |  "
                f"CB ${btc_now:,.0f} {_btc_src.replace('coinbase ', '')}{cl_str}")
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

        # Pre-sign the SELL FOK when a scalp is imminent — keeps the sign work
        # off the hot path; hasattr guards a trader without warm_sell_signature.
        if (hasattr(trader, 'warm_sell_signature')
                and -0.05 < holding_edge < -0.005):
            _sell_token = (live.get("token_id_up", "") if pos["side"] == "Up"
                           else live.get("token_id_down", ""))
            if _sell_token:
                _shares = pos.get("shares_held") or pos["size"] / pos["entry_price"]
                _wfee = pos.get("fee_rate") or DEFAULT_FEE_RATE
                # Sign for the same headroom-reduced quantity close_trade will
                # submit (base.py sell_fee_headroom) — a full-size signature is
                # immutable, passes the 5% drift gate, and would POST more
                # shares than the fee buffer authorizes.
                _headroom = max(max(_wfee * 0.25, 0.0) + 0.002, 0.005)
                _shares *= (1.0 - _headroom)
                # Approximate exit_fill = market_price × (1 − 8% cross floor);
                # _take_sell_warmup tolerates ±1¢ drift vs the actual exit_fill.
                _warm_price = round(market_price * 0.92, 4)
                asyncio.create_task(trader.warm_sell_signature(
                    _sell_token, _shares, _warm_price,
                    fee_rate=_wfee,
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
                    f"Bid {ws_bid:.2f} vs Price {verified_price:.2f} — Using real price"
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
                    f"  SCALP DEFERRED — Small position (${exit_size_usd:.2f} < $1.00), Monitoring"
                )
                _last_hold_log[mid] = now_ts
            elif now_ts - _last_hold_log.get(mid, 0) >= 30:
                _last_hold_log[mid] = now_ts
                logger.info(
                    f"  {_C.DIM}HOLD (Small) {pos['side']}{_C.RESET}  "
                    f"{_fmt_secs(live['seconds_remaining'])}  |  size ${exit_size_usd:.2f}  "
                    f"prob {model_prob:.0%}  mkt {market_price:.0%}  edge {holding_edge:+.0%}"
                )
            return day_wins, day_losses, day_fees

        # Size recovered above the $1 floor — clear the deferred flag and scalp.
        if pos["id"] in _abandoned_scalp_positions:
            _abandoned_scalp_positions.discard(pos["id"])
            logger.info(
                f"  SCALP RESUMED — Position recovered ${exit_size_usd:.2f}, Attempting exit"
            )

        # Emit the pre-scalp snapshot here (after size guard) so the price the
        # scalp triggers on is always visible, without spamming on deferred ticks.
        _cl_pre = f"  CL ${chainlink_feed.price:,.0f}" if chainlink_feed and chainlink_feed.price > 0 else ""
        logger.info(
            f"  {_C.DIM}PRE-SCALP {pos['side']}{_C.RESET}  {_fmt_secs(live['seconds_remaining'])}  |  "
            f"prob {model_prob:.0%}  mkt {market_price:.0%}  edge {holding_edge:+.0%}  |  "
            f"CB ${btc_now:,.0f} {_btc_src.replace('coinbase ', '')}{_cl_pre}"
        )

        result = await trader.close_trade(pos["id"], exit_fill, token_id=sell_token, position=pos)
        if not result.success:
            if "CLOB minimum" in (result.reason or ""):
                # Race: size was >= $1 at the pre-check but dropped by order time —
                # defer, monitor, retry next tick.
                _abandoned_scalp_positions.add(pos["id"])
                logger.info(
                    f"  SCALP DEFERRED — Below exchange minimum (${exit_size_usd:.2f}), Monitoring"
                )
                return day_wins, day_losses, day_fees
            _fr = (result.reason or "").lower()
            _fail_why = ("Book repriced" if ("no fill" in _fr or _fr.startswith("price moved"))
                         else (result.reason or "unknown"))
            logger.warning(f"  SCALP FAILED — {_fail_why}, retrying next tick")
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
                f"  {color}{_C.BOLD}SCALP {won} {pos['side']}{_C.RESET} {pos['entry_price']:.2f}→{exit_fill:.2f}  "
                f"{gain_pct:+.0%}  {color}${pnl:+.2f}{_C.RESET}  |  {_slug_to_window(pos['market_id'])}\n"
                f"  {_C.DIM}{reason} · day {day_wins}W/{day_losses}L · bank ${bankroll_after:.2f} · "
                f"fees {_fee_breakdown(result)}{_C.RESET}\n"
                f"{color}{'=' * 69}{_C.RESET}")
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
        db: Any, outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        day_wins: int, day_losses: int, day_fees: float,
        signal_engine: Any = None) -> tuple[bool, int, int, float]:
    """Resolve a position whose contract has expired (seconds_remaining <= 0)."""
    global _prev_resolution_margin
    # Chainlink oracle first (authoritative), then a coherent resolved CLOB book.
    exit_price, resolve_log = _resolved_exit_price(live, pos["side"], pos["market_id"])
    if exit_price is None:
        # Window hasn't resolved yet — wait for the next tick.
        now_ts = time.time()
        mid = pos["market_id"]
        if mid not in _last_resolve_wait_log:
            _last_resolve_wait_log[mid] = now_ts
            logger.info(f"{_C.DIM}WAITING FOR RESOLUTION {_slug_to_window(mid)}{_C.RESET}")
        return False, day_wins, day_losses, day_fees
    if resolve_log and pos["market_id"] not in _resolve_oracle_logged:
        # Log once per market — a pending winning redeem retries this path every
        # tick and would otherwise repeat the same RESOLVE line for minutes.
        _resolve_oracle_logged.add(pos["market_id"])
        logger.info(f"RESOLVED {resolve_log}")

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
        _abandoned_scalp_positions.discard(pos["id"])
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=total_fees)
        if counterfactual_tracker:
            counterfactual_tracker.record_hold_resolution(
                pos["market_id"], exit_price, pnl, gain_pct, position_id=pos["id"])
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
            logger.info(f"ORPHAN {_slug_to_window(pos['market_id'])} ({age:.0f}s old) — Waiting for resolution (no Chainlink strike captured)")
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
        f"Feeds: Coinbase {_f(coinbase_feed)} · Binance {_f(binance_feed)} · "
        f"Chainlink {_f(chainlink_feed)} · "
        f"CLOB WS {'OK' if clob_ws is not None else '--'} · "
        f"Discord {'OK' if alert_manager is not None else '--'}"
    )

    # Closure captures clob_ws once — reused across all book-update ticks.
    _midprice_fn = _get_token_midprice(clob_ws) if clob_ws else None

    _pg_lw = config.get("late_window", {})
    _pg_late_start = float(_pg_lw.get("sniper_late_start_s", 45))
    _pg_move = float(_pg_lw.get("sniper_cb_move", 8.0))
    _pg_mv_win = float(_pg_lw.get("sniper_move_window_s", 2.0))
    _last_full_eval = 0.0

    async def _entry_pass(positions: list) -> None:
        """The single entry evaluation — one copy, two call sites in the loop.

        A Coinbase-tick wake with no open position runs this FIRST (before
        position management); every other wake runs it last, as before.
        """
        nonlocal ws_subscribed_tokens, prev_contract_tokens, last_eval_log_window, window_strikes
        nonlocal _last_full_eval
        # µs pre-gate: only fire-adjacent wakes pay the full evaluation.
        _pg_now = time.time()
        _pg_mv = abs(coinbase_feed.cb_move(_pg_mv_win)) if coinbase_feed is not None else 0.0
        if not _pregate_should_eval(_pg_now, _last_full_eval, 300.0 - (_pg_now % 300.0),
                                    _pg_mv, _pg_late_start, _pg_move, _sniper_wake):
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

        strike, btc_price, window_strikes, last_eval_log_window, _ = \
            _compute_strike_and_btc(cid, binance_feed, window_strikes,
                                    eval_window, last_eval_log_window,
                                    chainlink_feed=chainlink_feed,
                                    coinbase_feed=coinbase_feed,
                                    trades_feed=trades_feed,
                                    contract=contract)
        if strike is None:
            return

        current_bankroll = await _get_bankroll_cached(db)
        _loop_marks["pre_eval"] = time.time()
        _, last_eval_log_window = await _evaluate_signal_and_enter(
            contract, cid, binance_feed, indicator_engine,
            signal_engine, market_scanner, http_client, clob_ws,
            trader, alert_manager, db, config, breaker,
            price_up, price_down,
            book_up, book_down, depth_usd_up, depth_usd_down,
            btc_price, strike, eval_window, last_eval_log_window,
            token_up, token_down, signal_config, max_bankroll_pct,
            bankroll=current_bankroll,
            depth_feed=depth_feed, trades_feed=trades_feed,
            coinbase_feed=coinbase_feed,
            chainlink_feed=chainlink_feed,
            ghost_tracker=ghost_tracker)

    while True:
        # Check if scheduler requested shutdown (auto-restart cycle after pipeline)
        if scheduler and getattr(scheduler, '_shutdown_requested', False):
            break

        # Late-window sniper (gated; default OFF): also wake on Coinbase ticks so a
        # stale-book move is acted on within one tick, not the 100ms housekeeping
        # fallback. No effect on the loop when the sniper is disabled.
        _sniper_wake = coinbase_feed is not None and bool(
            config.get("late_window", {})["sniper_enabled"])

        _cb_woke = False  # this wake was a Coinbase tick (set below)
        # Event-driven: react instantly to WebSocket book/resolution updates; short timeout for housekeeping
        if clob_ws:
            try:
                # Wake on book update OR market resolution — whichever comes first
                book_task = asyncio.create_task(clob_ws.book_updated.wait())
                resolve_task = asyncio.create_task(clob_ws.market_resolved.wait())
                _wait_set = {book_task, resolve_task}
                if _sniper_wake:
                    _wait_set.add(asyncio.create_task(coinbase_feed.price_event.wait()))
                done, pending = await asyncio.wait(
                    _wait_set, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if _sniper_wake:
                    _cb_woke = coinbase_feed.price_event.is_set()
                    coinbase_feed.price_event.clear()
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

            # --- FAST PATH: with nothing at risk, a Coinbase-tick wake OR any
            # wake during a fire-adjacent move runs the entry evaluation FIRST.
            # The mirror answers "anything open?" sync, so nothing — not even
            # the positions cache — precedes a hot evaluation.
            _fast_entry = False
            _loop_marks["cb_woke"] = 1.0 if _cb_woke else 0.0
            _now_fp = time.time()
            _hot_fp = (_sniper_wake and coinbase_feed is not None
                       and (300.0 - (_now_fp % 300.0)) <= _pg_late_start
                       and abs(coinbase_feed.cb_move(_pg_mv_win)) >= 0.6 * _pg_move)
            _open_n = db.open_market_count() if hasattr(db, "open_market_count") else None
            if (_cb_woke or _hot_fp) and _open_n == 0:
                _fast_entry = True
                _loop_marks["fast"] = 1.0
                await _entry_pass([])
            else:
                _loop_marks["fast"] = 0.0

            positions = await _get_open_positions_cached(db)
            if not _fast_entry and (_cb_woke or _hot_fp) \
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
            # Background task: the inline Gamma sweep blocked this wake ahead of
            # a potential sniper fire; nothing downstream needs its result.
            if counterfactual_tracker:
                global _last_cf_check_ts, _cf_check_task
                _now_cf = time.time()
                if (_now_cf - _last_cf_check_ts >= _CF_CHECK_INTERVAL
                        and (_cf_check_task is None or _cf_check_task.done())):
                    _last_cf_check_ts = _now_cf
                    _cf_check_task = asyncio.create_task(
                        _check_counterfactuals(counterfactual_tracker, ghost_tracker,
                                               market_scanner, http_client, binance_feed))

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
        discord_bot = create_bot(db=None, scanner=None, config=config)
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
        # The +8s chain audit reports the settled entry here → the OPEN banner
        # prints once, with the real fill (see _log_open_banner).
        trader.on_entry_settled = _on_entry_settled
    else:
        # Fallbacks match settings.yaml's calibrated values (one source of truth
        # for the realism constants; the fallbacks only fire if settings omit keys).
        trader = PaperTrader(db=db,
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"],
            paper_latency_scale=exec_cfg.get("paper_latency_scale", 1.0),
            paper_latency_floor_s=exec_cfg.get("paper_latency_floor_s", 0.41),
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
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
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
        counterfactual_tracker=counterfactual_tracker,
        ghost_tracker=ghost_tracker,
        alert_manager=alert_manager,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
    )
    scheduler._exit_edge_threshold = signal_cfg["exit_edge_threshold"]
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

    # Recorders: window-path stream (the kill-bar feed + pivot-research corpus)
    # + CLOB tape + micro-tape (event-true BBO/tick/report stream — the sub-5Hz
    # resolution the sampled recorder can't see). Write-behind; never block the loop.
    from polybot.recording import MicroTape, TapeRecorder, WindowPathRecorder
    tape_recorder = TapeRecorder()
    clob_ws.on_trade = tape_recorder.on_trade
    micro_tape = MicroTape()
    clob_ws.on_bba = micro_tape.on_bba
    coinbase_feed.on_tick = micro_tape.on_cb_tick
    chainlink_feed.on_report = micro_tape.on_cl_report
    window_recorder = WindowPathRecorder(
        db=db, clob_ws=clob_ws, coinbase_feed=coinbase_feed,
        chainlink_feed=chainlink_feed, market_scanner=market_scanner,
        http_client=http_client, binance_trades=trades_feed,
        binance_feed=binance_feed, binance_depth=depth_feed,
        # Dedicated instances (same config as live): compute_probability mutates
        # engine state, so the recorder must never share the trading loop's engine.
        indicator_engine=IndicatorEngine(params=indicator_params),
        signal_engine=_build_signal_engine(signal_cfg, config))
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

    from polybot.recording import recordings_cleanup_job
    scheduler.register_job("recordings_retention", recordings_cleanup_job())

    async def _sniper_health_job() -> dict:
        """Nightly sniper health: kill-bar read + post-live kill rule, pinged to Discord.

        Alert-only — never flips config (kill bars are operator authority).
        Reports the SIM corpus AND the realized fills with their gap; the
        kill-rule verdict is driven by the realized ledger once fills exist
        (the sim can't see live execution quality). Skipped when disabled."""
        if not config.get("late_window", {})["sniper_enabled"]:
            return {"skipped": "sniper disabled"}
        import importlib.util
        hp = Path(__file__).resolve().parent.parent / "scripts" / "analyze_late_window.py"
        spec = importlib.util.spec_from_file_location("analyze_late_window", hp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Read the SIM ceiling at the DEPLOYED config (settings.yaml), not the
        # harness's research defaults — apples-to-apples with realized fills.
        # RTT 0.34 = the box's measured POST p50 (Polymarket's 250ms crypto
        # taker delay is a policy floor — no sample beats it).
        _lw = config.get("late_window", {})
        # Each read individually guarded: the SIM corpus lives in a gitignored
        # sidecar DB that can be missing/corrupt while the realized ledger is
        # perfectly healthy — a dead sidecar must degrade the ping, never
        # suppress the kill-rule readout for live money (and vice versa).
        try:
            sim = await asyncio.to_thread(
                mod.health_read, 0.34, _lw["sniper_fok_slip"],
                _lw["sniper_cb_move"], _lw["sniper_ask_cap"])   # SIM corpus (window_paths.db, RO)
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
        # Burst-alive SPRT + regime-Kelly shadow accrual — alert-only reads over
        # the same realized ledger the kill rule uses. SPRT turns things ON;
        # the kill rule turns things OFF.
        _real_db = None if mode == "live" else mod.PAPER_DB
        try:
            sprt_burst = await asyncio.to_thread(
                mod.burst_sprt_read, _real_db, _lw.get("validation_epoch"))
            regime_d = await asyncio.to_thread(
                mod.regime_shadow_read, _real_db, _lw.get("validation_epoch"))
        except Exception as e:
            logger.debug("SPRT/regime shadow read failed: %s", e)
            sprt_burst, regime_d = None, None
        # The scar scan — the nightly learning loop (core/scar_scan.py): mines
        # the realized ledger for toxic cells, shadow-registers them, scores
        # each registered gate's OOS SPRT, resolves enforced vetoes. Alert-only
        # like everything here: enforcement is the operator's config flip.
        try:
            scars = await asyncio.to_thread(
                mod.scar_scan_read, _real_db, _lw.get("validation_epoch"),
                _lw.get("scar_enforce") or [], None, None, mode)
        except Exception as e:
            logger.warning("scar scan failed: %s", e)
            scars = None
        # Resolution-mechanism watch — the TWAP-rollout tripwire. Today every
        # window's official final_price equals the NEXT window's price_to_beat
        # bit-exact (same boundary report); the announced TWAP breaks that
        # equality, and the day it does the sniper's premise is gone.
        try:
            twap = await asyncio.to_thread(mod.resolution_snapshot_read, _real_db)
        except Exception as e:
            logger.warning("resolution snapshot read failed: %s", e)
            twap = None
        if sim is None and live is None:
            if alert_manager:
                await alert_manager.send_health("🎯 Sniper health: no data yet (sim corpus + live ledger both empty).")
            return {"health": "no data"}
        today = datetime.now(ET).strftime("%Y-%m-%d")
        # Kill-rule authority = the REALIZED ledger ONLY — the rule is defined on
        # realized fills, and the floor-blind SIM read (no L1 edge floor, mixed-era
        # corpus) must never trip it. An empty realized ledger is "still accruing",
        # not a verdict; the SIM line stays in the ping as context.
        kt = live["kill_rule_tripped"] if (live and live["n_fills"] > 0) else None
        status = ("⏳ STILL ACCRUING" if kt is None
                  else "⚠️ KILL RULE TRIPPED" if kt else "✅ HEALTHY")

        # ── Human-first daily ping: verdict up top, one line per fact, no
        # jargon the operator has to decode. The returned dict (below) keeps
        # the full numbers for tests/automation.
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
            base = f"Research sim (no capital): {sim['net_per_sh']*100:+.1f}¢/share"
            if live and live["n_fills"] > 0:
                gap = (sim["net_per_sh"] - live["net_per_sh"]) * 100
                if abs(gap) < 3:
                    base += " — real fills in line with it"
                elif gap > 0:
                    base += f" — real fills LAGGING it by {gap:.1f}¢ (possible execution leak)"
                else:
                    base += f" — real fills ahead of it by {-gap:.1f}¢ (normal: the sim reads low)"
            return base + "\n"

        def _experiments_line() -> str:
            parts = []
            if sprt_burst is not None:
                s = sprt_burst
                if s["state"] == "accruing_sigma":
                    parts.append(f"burst test warming up ({s['n_qualifying']}/{s['need']} baseline days)")
                elif s["state"] == "continue":
                    parts.append(f"burst test running (score {s['lam']:+.2f}; "
                                 f"graduates at +2.73, dies at −1.42)")
                elif s["state"] == "accept_h1":
                    parts.append("burst test ✅ GRADUATED — regime sizing unlocks its own trial")
                elif s["state"] == "accept_h0":
                    parts.append("burst test ❌ rejected — idea parked")
                elif s["state"] == "void":
                    parts.append("burst test ⚠️ voided (market regime shifted under it) — restarts")
                else:
                    parts.append("burst test hit its time limit — falling back to the fixed read")
            if regime_d is not None and regime_d["n_days"] > 0:
                parts.append(f"sizing shadow would have made {regime_d['total_d']:+.2f}$ vs flat")
            return ("Experiments: " + " · ".join(parts) + "\n") if parts else ""

        def _scars_line() -> str:
            if not scars:
                return ""
            parts = []
            for name in scars.get("registered", []):
                parts.append(f"🩹 new pocket quarantined: **{name}**")
            per_gate = (scars.get("vetoes") or {}).get("per_gate") or {}
            for g in scars.get("gates", []):
                if g["status"] == "retired":
                    # must precede the enforced branch: a retired gate no
                    # longer vetoes even if settings still lists it
                    parts.append(f"{g['name']} cleared by fresh data — retired"
                                 + (" — **REMOVE it from `scar_enforce`, its veto is dead**"
                                    if g.get("enforced") else ""))
                elif g.get("enforced"):
                    v = per_gate.get(g["name"]) or {}
                    avoided = (f", vetoes avoided {v['avoided_cs']:+.1f}¢/sh × "
                               f"{v['resolved']} (upper-bound, assumes fill)"
                               if v.get("avoided_cs") is not None else "")
                    parts.append(f"{g['name']} ENFORCED{avoided}")
                elif g["sprt_state"] == "paused_other_mode":
                    # must precede the graduated branch: a gate proven on the
                    # OTHER mode's ledger must never be advertised for
                    # enforcement in this one
                    parts.append(f"{g['name']} paused (evidence from the other mode)")
                elif g["status"] == "graduated":
                    parts.append(f"{g['name']} ✅ PROVEN toxic — add it to "
                                 f"`late_window.scar_enforce` to switch the veto on")
                elif g["sprt_state"] == "accruing_sigma":
                    parts.append(f"{g['name']} shadow-tracking "
                                 f"({g['n_oos']} fills since discovery, baseline forming)")
                elif g["sprt_state"] == "continue":
                    parts.append(f"{g['name']} shadow score {g['lam']:+.2f} "
                                 f"(proves at +2.73, clears at −1.42)")
                elif g["sprt_state"] == "truncated":
                    oe = f"{g['oos_ew']:+.1f}¢/sh" if g.get("oos_ew") is not None else "n/a"
                    parts.append(f"{g['name']} ⏱ 16 scored days, no verdict "
                                 f"(OOS {oe} over {g['n_oos']} fills) — operator call")
                elif g["sprt_state"] == "void":
                    parts.append(f"{g['name']} ⚠️ test voided (regime shifted) — "
                                 f"σ re-freezes on fresh days")
            # settings hygiene: an enforce entry with no live graduated gate is dead
            active = {g["name"] for g in scars.get("gates", [])
                      if g["status"] in ("graduated",)}
            for name in (_lw.get("scar_enforce") or []):
                if name not in active:
                    parts.append(f"⚠️ `scar_enforce` lists **{name}** but no graduated "
                                 f"gate matches — remove it")
            return ("Learned scars: " + " · ".join(parts) + "\n") if parts else ""

        def _twap_line() -> str:
            if not twap or not twap.get("checked"):
                return ""
            c, m = twap["checked"], twap["matched"]
            if m == c:
                return (f"Resolution watch: terminal snapshot intact — "
                        f"{m}/{c} boundary matches to the cent\n")
            return (f"🚨 **RESOLUTION MECHANISM SHIFT: {c - m}/{c} windows "
                    f"resolved OFF the terminal Chainlink snapshot (worst "
                    f"${twap['worst']:.2f} off)** — the announced TWAP rollout "
                    f"is the prime suspect. **Set `late_window.sniper_enabled: "
                    f"false` now** and verify a resolved market by hand before "
                    f"re-enabling.\n")

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
            f"{_shutoff_line(live)}"
            f"{_context_line()}"
            f"{_experiments_line()}"
            f"{_scars_line()}"
            f"{_twap_line()}"
            f"{action}"
        )
        if alert_manager:
            await alert_manager.send_health(msg)

        def _pick(r):
            if r is None:
                return None
            return {"net_per_sh": r["net_per_sh"], "t_day": round(r["t_day"], 2),
                    "n_fills": r["n_fills"], "n_days": r["n_days"],
                    "trailing4_mean": r["trailing4_mean"], "trailing8_t": r["trailing8_t"],
                    "kill_rule_tripped": r["kill_rule_tripped"]}
        return {"health": status, "kill_rule_tripped": kt,
                "live": _pick(live), "sim": _pick(sim),
                "sprt_burst": sprt_burst, "regime_shadow": regime_d,
                "scars": scars}
    scheduler.register_job("sniper_health", _sniper_health_job)

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
        binance_feed, market_scanner, indicator_engine, signal_engine,
        trader, alert_manager, db, config, outcome_reviewer,
        is_paused_fn=lambda: discord_bot.is_paused,
        scheduler=scheduler, clob_ws=clob_ws, breaker=breaker,
        counterfactual_tracker=counterfactual_tracker,
        ghost_tracker=ghost_tracker,
        http_client=http_client,
        depth_feed=depth_feed, trades_feed=trades_feed,
        chainlink_feed=chainlink_feed, coinbase_feed=coinbase_feed))
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
        await _stop(binance_feed.stop())
        await _stop(depth_feed.stop())
        await _stop(trades_feed.stop())
        await _stop(coinbase_feed.stop())
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
