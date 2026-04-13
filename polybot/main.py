# polybot/main.py
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any

from polybot.config.loader import load_config, get_secret
from polybot.execution.base import entry_fee_shares, slippage_pct, DEFAULT_FEE_RATE
from polybot.db.models import Database
from polybot.core.binance_feed import BinanceFeed
from polybot.core.market_scanner import BTCMarketScanner
from polybot.core.clob_ws import ClobWebSocket
from polybot.indicators.engine import IndicatorEngine, IndicatorNormalizer
from polybot.core.signal_engine import SignalEngine
from polybot.core.order_flow import compute_flow_signal
from polybot.brain.claude_client import ClaudeClient
from polybot.execution.paper_trader import PaperTrader
from polybot.execution.live_trader import LiveTrader, verify_auth
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.bias_detector import BiasDetector
from polybot.agents.ta_evolver import TAEvolver
from polybot.agents.weight_optimizer import WeightOptimizer
from polybot.agents.scheduler import AgentScheduler
from polybot.agents.counterfactual_tracker import CounterfactualTracker
from polybot.discord_bot.bot import create_bot
from polybot.discord_bot.alerts import AlertManager
from polybot.execution.circuit_breaker import CircuitBreaker
import math
from polybot.core.binance_depth import BinanceDepthFeed
from polybot.core.binance_trades import BinanceTradesFeed, BinanceTradeAccumulator
from polybot.core.bybit_feed import BybitFeed
from polybot.core.deribit_iv import DeribitIVFeed
from polybot.core.coinbase_feed import CoinbaseFeed
from polybot.core.kraken_feed import KrakenFeed
from polybot.core.bankroll_strategy import compute_kelly_tier, compute_uncertainty_discount, DrawdownVelocityTracker
from polybot.core.sprt import SPRTAccumulator
from polybot.core.regime import RegimeDetector
from polybot.core.alpha_decay import AlphaDecayTracker
from polybot.core.liquidation import compute_liquidation_pressure
from polybot.core.gamma_exposure import classify_gex
from polybot.core.signal_engine import compute_signal_consensus
from polybot.core.adverse_selection import AdverseSelectionMonitor
from polybot.core.crowd_bias import CrowdBiasTracker
from polybot.core.garch_vol import GarchPredictor

import numpy as np
import re
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')

class _StripAnsiFormatter(logging.Formatter):
    """Strips ANSI color codes so log files stay clean."""
    def format(self, record):
        result = super().format(record)
        return _ANSI_RE.sub('', result)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
_file_handler = logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=3, mode="a")
_file_handler.setFormatter(_StripAnsiFormatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
logging.basicConfig(
    level=logging.ERROR,
    handlers=[_console_handler, _file_handler],
)

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

# Throttled logging for hold evaluations and resolution waiting
_last_hold_log: dict[str, float] = {}  # market_id -> last log timestamp
_last_resolve_wait_log: dict[str, float] = {}  # market_id -> last log timestamp

# Trailing profit exit: track peak market price per held position
_peak_hold_price: dict[str, float] = {}  # market_id -> peak market_price_for_side during hold

# Previous window resolution margin for adjacent window momentum (D2)
_prev_resolution_margin: float = 0.0

_sprt: SPRTAccumulator | None = None
_alpha_decay: AlphaDecayTracker | None = None
_regime_detector: RegimeDetector | None = None
_garch: GarchPredictor | None = None
_cvd_normalizer: IndicatorNormalizer | None = None
_current_window_id: str = ""
_early_entry_fired: bool = False
_drawdown_tracker: DrawdownVelocityTracker | None = None
_adverse_monitor: AdverseSelectionMonitor | None = None
_crowd_bias: CrowdBiasTracker | None = None

_realized_edge_history: list[tuple[float, float]] = []  # (predicted_edge, realized_gain_pct)


def _build_signal_engine(signal_cfg: dict, config: dict) -> SignalEngine:
    """Construct SignalEngine from config — shared between pipeline and main."""
    return SignalEngine(
        min_edge=signal_cfg.get("entry_threshold", 0.04),
        kelly_fraction=config["math"].get("kelly_fraction", 0.15),
        momentum_weight=signal_cfg.get("momentum_weight", -0.02),
        weights=signal_cfg.get("weights", {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                            "obv": 0.15, "vwap": 0.20}),
        min_model_probability=signal_cfg.get("min_model_probability", 0.58),
        student_t_df=signal_cfg.get("student_t_df", 5),
        regime_weight=signal_cfg.get("regime_weight", 0.03),
        flow_weight=signal_cfg.get("flow_weight", 0.04),
        regime_lookback=signal_cfg.get("regime_lookback", 50),
        min_kelly=signal_cfg.get("min_kelly", 0.015),
        atr_sigma_ratio=signal_cfg.get("atr_sigma_ratio", 1.4),
        spot_flow_weight=signal_cfg.get("spot_flow_weight", 0.04),
        wall_weight=signal_cfg.get("wall_weight", 0.05),
        prev_margin_weight=signal_cfg.get("prev_margin_weight", 0.02),
        conviction_multiplier=config.get("bankroll_acceleration", {}).get("enabled", True),
        min_atr=signal_cfg.get("min_atr", 8.0),
        liquidation_weight=signal_cfg.get("liquidation_weight", 0.03),
        logit_scale=signal_cfg.get("logit_scale", 4.0),
        probability_compression=signal_cfg.get("probability_compression", 1.0),
        consensus_dead_zone=signal_cfg.get("consensus_dead_zone", 0.05),
        conviction_config=signal_cfg.get("conviction"),
        consensus_config=signal_cfg.get("consensus"),
        exit_config=signal_cfg.get("exit"),
    )


def compute_entry_phase(seconds_remaining: float, window_seconds: float = 300.0) -> dict:
    """Determine entry phase based on time elapsed in window.

    Phases (user requested 60s observe):
      0-60s elapsed (300-240s remaining): OBSERVE — collect data, no entry
      60-180s elapsed (240-120s remaining): NORMAL — full Kelly entry when gates pass
      180-240s elapsed (120-60s remaining): LATE — reduced Kelly (0.7x)
      240-300s elapsed (60-0s remaining): FINAL — only at >90% confidence, half Kelly
    """
    elapsed = window_seconds - seconds_remaining
    if elapsed < 60:
        return {"allowed": False, "kelly_multiplier": 1.0, "min_prob_override": None, "phase": "observe"}
    elif elapsed < 180:
        return {"allowed": True, "kelly_multiplier": 1.0, "min_prob_override": None, "phase": "normal"}
    elif elapsed < 240:
        return {"allowed": True, "kelly_multiplier": 0.7, "min_prob_override": None, "phase": "late"}
    else:
        return {"allowed": True, "kelly_multiplier": 0.5, "min_prob_override": 0.90, "phase": "final"}


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
            if http_client:
                resp = await http_client.get(f"{market_scanner.GAMMA_API}/events",
                                             params={"slug": slug})
            else:
                async with httpx.AsyncClient(timeout=3) as client:
                    resp = await client.get(f"{market_scanner.GAMMA_API}/events",
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
        if http_client:
            resp = await http_client.get(f"{market_scanner.GAMMA_API}/events",
                                         params={"slug": market_id})
        else:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{market_scanner.GAMMA_API}/events",
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
                          fees: float = 0.0) -> None:
    """Record a trade outcome for the learning pipeline."""
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
            weight_version=pos.get("weight_version", ""),
            category="crypto-5min",
            indicator_snapshot=json.loads(pos.get("indicator_snapshot", "{}")),
            exit_reason=exit_reason,
            size=pos.get("size", 0.0),
            pnl=pnl,
            fees=fees,
        )
    except Exception as e:
        logger.error(f"Failed to record outcome: {e}")


def _get_edge_realization_ratio() -> float:
    """Rolling ratio of realized gains to predicted edge. < 0.6 = model overconfident."""
    if len(_realized_edge_history) < 50:
        return 1.0  # insufficient data
    recent = _realized_edge_history[-50:]
    predicted = [abs(p) for p, _ in recent if p > 0]
    realized = [max(0, g) for _, g in recent]
    if not predicted or sum(predicted) == 0:
        return 1.0
    return sum(realized) / sum(predicted)


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
        depth_feed: Any = None,
        trades_feed: Any = None,
        bybit_feed: Any = None,
        deribit_feed: Any = None,
        coinbase_feed: Any = None,
        chainlink_feed: Any = None,
        kraken_feed: Any = None) -> tuple[str | None, int]:
    """Compute indicators/flow/signal, check for entry, size the trade, execute."""
    in_window = market_scanner.in_entry_window(contract["seconds_remaining"])

    # Dynamic entry timing — observe first 60s, then phased entry
    entry_phase = compute_entry_phase(contract["seconds_remaining"])
    if not entry_phase["allowed"]:
        # During observe phase: accumulate SPRT evidence + track alpha decay
        global _sprt, _alpha_decay, _current_window_id, _early_entry_fired
        window_id = contract.get("market_id", contract.get("slug", ""))
        if window_id != _current_window_id:
            _current_window_id = window_id
            if _sprt: _sprt.reset()
            if _alpha_decay: _alpha_decay.reset()
            _early_entry_fired = False

        prob_up_obs = 0.5
        if _sprt:
            indicators_obs = indicator_engine.compute_all(binance_feed.buffer)
            closes_obs = binance_feed.buffer.get_closes()
            prob_up_obs = signal_engine.compute_probability(
                btc_price, strike, contract["seconds_remaining"],
                indicators_obs.get("atr", {}).get("atr", 0), indicators_obs, closes_obs,
                prev_resolution_margin=_prev_resolution_margin)
            _sprt.update(prob_up_obs)
        if _alpha_decay:
            best_prob = max(prob_up_obs, 1 - prob_up_obs) if prob_up_obs != 0.5 else 0.5
            _alpha_decay.add_observation(time.time(), best_prob)

        # Adaptive alpha decay: if edge is decaying fast AND SPRT has strong evidence,
        # break out of observe phase early — but only once per window
        if (not _early_entry_fired
                and _alpha_decay and _alpha_decay.should_enter_now()
                and _sprt and _sprt.get_status() == "ENTER"):
            _early_entry_fired = True
            logger.info(f"EARLY ENTRY: alpha decay {_alpha_decay.get_decay_rate():+.4f}/s + SPRT ENTER — skipping remaining observe")
        else:
            return None, last_eval_log_window

    # Compute indicators and evaluate probability model
    indicators = indicator_engine.compute_all(binance_feed.buffer)

    # Compute order flow signal from CLOB data
    trades_up = clob_ws.get_trade_history(token_up) if clob_ws else []
    trades_down = clob_ws.get_trade_history(token_down) if clob_ws else []
    flow_data = compute_flow_signal(book_up, book_down, trades_up, trades_down)
    flow_score = flow_data["flow_score"]

    # --- New signals from extended feeds ---
    spot_flow_signal = 0.0
    wall_pressure_val = 0.0
    iv_ratio_val = 1.0

    if trades_feed and trades_feed.accumulator:
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

    if depth_feed:
        wall_pressure_val = depth_feed.get_wall_pressure(strike, btc_price)

    # Deribit IV: logged for pipeline analysis but NOT applied to CDF vol scaling.
    # 30-day IV is a regime mismatch for 5-min windows — ATR is the correct vol measure.
    if deribit_feed and deribit_feed.state.btc_iv > 0:
        atr_val = indicators.get("atr", {}).get("atr", 0)
        deribit_cfg = config.get("deribit", {})
        iv_ratio_val = deribit_feed.state.get_iv_ratio(
            atr_val, btc_price,
            iv_min=deribit_cfg.get("iv_ratio_min", 0.5),
            iv_max=deribit_cfg.get("iv_ratio_max", 3.0))
    # iv_ratio_val stays 1.0 for CDF — logged in trade_context for pipeline

    # CVD acceleration (first derivative of buying pressure)
    cvd_accel_val = 0.0
    if trades_feed and trades_feed.accumulator:
        cvd_accel_val = trades_feed.accumulator.get_cvd_acceleration(recent_s=15, baseline_s=45)

    # Liquidation pressure from Bybit OI changes
    liquidation_val = 0.0
    if bybit_feed and bybit_feed.state.open_interest > 0 and bybit_feed.state.open_interest_prev > 0:
        liquidation_val = compute_liquidation_pressure(
            bybit_feed.state.open_interest, bybit_feed.state.open_interest_prev,
            bybit_feed.state.price_at_oi, bybit_feed.state.price_at_oi_prev)

    # Gamma exposure regime
    gex_val = 0.0
    gex_info = {"regime": "neutral", "trade_bias": 1.0}
    if deribit_feed and deribit_feed.state.net_gex != 0:
        gex_val = deribit_feed.state.net_gex
        gex_info = classify_gex(gex_val)

    # Get closes array for regime detection
    closes = binance_feed.buffer.get_closes()

    signal = signal_engine.evaluate(
        indicators, has_position=False, in_entry_window=in_window,
        btc_price=btc_price, strike_price=strike,
        seconds_remaining=contract["seconds_remaining"],
        market_price_up=price_up, market_price_down=price_down,
        closes=closes, flow_signal=flow_score,
        spot_flow_signal=spot_flow_signal,
        wall_pressure=wall_pressure_val,
        prev_resolution_margin=_prev_resolution_margin,
        iv_ratio=1.0,  # ATR is the correct 5-min vol; Deribit 30-day IV not applied
        liquidation_pressure=liquidation_val,
        gex_signal=gex_val,
    )

    # Log signal evaluation once per window so we can see what the model sees
    if eval_window != last_eval_log_window:
        last_eval_log_window = eval_window
        phase_tag = entry_phase["phase"].upper()
        secs = contract["seconds_remaining"]
        dist = btc_price - strike
        action_color = _C.GREEN if signal.action in ("BUY_YES", "BUY_NO") else _C.DIM
        logger.info(
            f"{_C.CYAN}{'-' * 60}{_C.RESET}\n"
            f"  {action_color}EVAL  {signal.action:<8}{_C.RESET} | {contract.get('question', cid)}\n"
            f"  BTC   ${btc_price:,.0f}  strike ${strike:,.0f}  ({dist:+,.0f})  |  {secs:.0f}s left  [{phase_tag}]  src={price_source}\n"
            f"  MODEL prob {_C.BOLD}{signal.prob:.0%}{_C.RESET}  edge {signal.edge:+.0%}  |  mkt Up {price_up:.2f}  Dn {price_down:.2f}\n"
            f"  FLOW  clob {flow_score:+.3f}  spot {spot_flow_signal:+.3f}  wall {wall_pressure_val:+.3f}  iv {iv_ratio_val:.2f}\n"
            f"  SPRT {_sprt.get_status() if _sprt else 'N/A'} ({_sprt.get_confidence():.0%} conf)  |  liq {liquidation_val:+.2f}  gex {gex_val:+.2f}  cvd_a {cvd_accel_val:+.2f}\n"
            f"  {_C.DIM}{signal.reason}{_C.RESET}")

    if signal.action not in ("BUY_YES", "BUY_NO"):
        return None, last_eval_log_window

    # --- SPRT GATE: require accumulated evidence from observe phase ---
    if _sprt and _sprt.get_status() == "SKIP":
        logger.info(f"SKIP: SPRT rejected — insufficient evidence during observe phase")
        return None, last_eval_log_window

    # --- EDGE CAP GATE: large edge = model miscalibration, not real alpha ---
    max_edge = config.get("signal", {}).get("max_edge", 0.20)
    if signal.edge > max_edge:
        return None, last_eval_log_window

    # --- LAYER DISAGREEMENT GATE: momentum opposing trade direction ---
    side = "Up" if signal.action == "BUY_YES" else "Down"
    momentum_score = signal_engine.compute_momentum(indicators)
    # Account for negative momentum_weight: if weight < 0, model FADES indicators,
    # so raw indicator direction opposing the bet is actually AGREEMENT
    mw_sign = 1.0 if signal_engine.momentum_weight >= 0 else -1.0
    effective_momentum = momentum_score * mw_sign
    momentum_opposes = (
        (side == "Down" and effective_momentum > 0.5) or
        (side == "Up" and effective_momentum < -0.5)
    )
    if momentum_opposes and signal.edge * 0.5 < signal_engine.min_edge:
        logger.info(
            f"SKIP: layer disagreement — momentum={momentum_score:+.2f} opposes {side}, "
            f"penalized edge {signal.edge * 0.5:+.1%} < min {signal_engine.min_edge:.0%}")
        return None, last_eval_log_window

    price = price_up if side == "Up" else price_down
    token_id = contract["token_id_up"] if side == "Up" else contract["token_id_down"]
    bankroll = await db.get_bankroll()
    kelly_mult = breaker.kelly_multiplier if breaker else 1.0

    # Bankroll acceleration: ratchet Kelly up as track record grows (CLAUDE.md)
    trade_count = 0
    if config.get("bankroll_acceleration", {}).get("enabled", True):
        trade_count, win_rate = await db.get_trade_stats()
        dd_breach = _drawdown_tracker.is_velocity_breach() if _drawdown_tracker else False
        accel_kelly = compute_kelly_tier(trade_count, win_rate, signal_engine.kelly_fraction,
                                         drawdown_breach=dd_breach)
        if accel_kelly != signal_engine.kelly_fraction:
            signal_engine.kelly_fraction = accel_kelly

    # Uncertainty-adjusted Kelly: f* = f_kelly x (1 - sigma^2/edge^2) -- Thorp 2006
    if trade_count == 0:
        trade_count, _ = await db.get_trade_stats()
    avg_edge = await db.get_avg_edge()
    uncertainty_discount = compute_uncertainty_discount(trade_count, avg_edge)

    size = round(bankroll * signal.kelly_size * kelly_mult * uncertainty_discount, 2)

    # Dynamic entry timing: apply phase Kelly multiplier
    size = round(size * entry_phase["kelly_multiplier"], 2)

    # Regime-based Kelly adjustment
    regime_state = None
    if _regime_detector:
        atr_val = indicators.get("atr", {}).get("atr", 0)
        atr_history = [c.high - c.low for c in binance_feed.buffer.get_last_n(50)]
        cvd_now = trades_feed.accumulator.get_cvd(120) if trades_feed and trades_feed.accumulator else 0
        regime_state = _regime_detector.classify(closes, atr_val, atr_history, cvd_now)
        if regime_state.skip:
            logger.info(f"SKIP: regime={regime_state.name} — no edge in this market state")
            return None, last_eval_log_window
        # Regime: logged for pipeline, NOT applied to sizing (operates near noise at SE=0.14)

    # Signal consensus: logged for pipeline, NOT applied to sizing (weak signal correlations)
    consensus_signals = {
        "flow": flow_score,
        "spot_flow": spot_flow_signal,
        "wall": wall_pressure_val,
        "cvd_accel": cvd_accel_val,
    }
    consensus_mult = compute_signal_consensus(
        consensus_signals, side,
        dead_zone=signal_engine.consensus_dead_zone,
        consensus_config=signal_engine.consensus_config)

    # GEX: logged for pipeline ablation, NOT applied to sizing
    gex_bias = gex_info.get("trade_bias", 1.0)

    # Vol ratio: logged for pipeline, NOT applied to sizing (just added, no data)
    garch_adj = 1.0
    if _garch and deribit_feed and deribit_feed.state.btc_iv:
        log_returns = np.diff(np.log(closes)) if closes is not None and len(closes) > 20 else np.array([])
        garch_adj = _garch.compute_sizing_adjustment(log_returns, deribit_feed.state.btc_iv)

    # Oracle divergence: logged for pipeline, NOT applied to sizing (just added, no data)
    oracle_divergence = 0.0
    oracle_discount = 1.0
    if chainlink_feed:
        cl_price = chainlink_feed.price if hasattr(chainlink_feed, 'price') else 0
        if cl_price > 0 and btc_price > 0:
            oracle_divergence = abs(btc_price - cl_price)
            atr_val = indicators.get("atr", {}).get("atr", 0)
            if atr_val > 0 and oracle_divergence > atr_val:
                divergence_ratio = min(oracle_divergence / atr_val, 3.0)
                oracle_discount = max(0.3, 1.0 - (divergence_ratio - 1.0) * 0.3)

    logger.debug(
        f"  REGIME {regime_state.name if regime_state else 'N/A'} ({regime_state.kelly_mult if regime_state else 1.0:.1f}x)  |  "
        f"SPRT {_sprt.get_status() if _sprt else 'N/A'} ({_sprt.get_confidence():.0%})  |  "
        f"consensus {consensus_mult:.1f}x  |  gex {gex_bias:.1f}x  |  vol {garch_adj:.1f}x  |  oracle {oracle_discount:.1f}x")

    # Phase-based probability override (final phase needs >90%)
    if entry_phase["min_prob_override"] and signal.prob < entry_phase["min_prob_override"]:
        logger.info(f"SKIP: final phase prob {signal.prob:.0%} < {entry_phase['min_prob_override']:.0%}")
        return None, last_eval_log_window

    # Concurrent windows: correlated position discount (ρ≈0.75 for adjacent BTC windows)
    # Portfolio Kelly with correlation: ~0.35 per position, not naive 0.50
    open_positions = await db.get_open_positions()
    if len(open_positions) > 0:
        concurrent_discount = config.get("execution", {}).get("concurrent_position_discount", 0.45)
        size = round(size * concurrent_discount, 2)

    if size > bankroll * max_bankroll_pct:
        size = round(bankroll * max_bankroll_pct, 2)

    # Cap single position to prevent concentration risk
    max_pos_pct = config.get("execution", {}).get("max_single_position_pct", 0.12)
    max_single = round(bankroll * max_pos_pct, 2)
    if size > max_single:
        size = max_single

    # Cap size to fraction of book depth (realistic fill constraint)
    side_depth = depth_usd_up if side == "Up" else depth_usd_down
    max_fill_pct = config.get("execution", {}).get("max_book_fill_pct", 0.50)
    if side_depth > 0:
        max_fill = side_depth * max_fill_pct
        if size > max_fill:
            size = round(max_fill, 2)
            if size < 0.10:
                logger.info(f"SKIP: size capped to ${size:.2f} by book depth ${side_depth:.0f} — too small")
                return None, last_eval_log_window

    # Net-edge gate: reject if slippage eats the edge below threshold.
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    est_slip = slippage_pct(size, side_depth, impact)
    net_edge = signal.edge - price * est_slip
    if net_edge < signal_engine.min_edge:
        logger.info(
            f"SKIP: net edge {net_edge:+.1%} < min {signal_engine.min_edge:.0%} "
            f"after {est_slip:.2%} slippage (gross {signal.edge:+.1%})")
        return None, last_eval_log_window

    # Final minimum size check — after all caps have been applied
    if size < 0.10:
        logger.info(f"SKIP: size ${size:.2f} < $0.10 after caps")
        return None, last_eval_log_window

    # Fetch fee rate and tick size from Polymarket API
    fee_rate = await market_scanner.fetch_fee_rate(token_id, http_client)
    # Simulate maker/FOK blend: ~65% of orders fill as maker (0% fee),
    # ~35% fall back to FOK (full taker fee). Randomize per trade.
    if config.get("execution", {}).get("use_maker_orders", False):
        import random
        if random.random() < 0.65:
            fee_rate = 0.0  # maker fill
    tick_size = await market_scanner.fetch_tick_size(token_id, http_client)

    # Apply slippage to the execution price already fetched in _fetch_market_prices
    # (price already comes from GET /price?side=BUY — no need to refetch)
    impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
    slip = slippage_pct(size, side_depth, impact)
    price = market_scanner.snap_to_tick(price * (1 + slip), tick_size)

    snapshot = indicator_engine.get_snapshot(indicators)
    snapshot["trade_context"] = {
        "btc_price": btc_price,
        "strike_price": strike,
        "seconds_remaining": contract["seconds_remaining"],
        "market_price_up": price_up,
        "market_price_down": price_down,
        "model_probability_raw": signal.prob,
        "model_probability": signal.prob,
        "edge": signal.edge,
        "momentum_score": signal_engine.compute_momentum(indicators),
        "atr": indicators.get("atr", {}).get("atr", 0),
        "size": size,
        "flow_score": flow_score,
        "flow_book_imbalance": flow_data.get("book_imbalance", 0),
        "flow_trade_count": flow_data.get("trade_count", 0),
        "spot_flow_signal": spot_flow_signal,
        "wall_pressure": wall_pressure_val,
        "iv_ratio": iv_ratio_val,
        "prev_resolution_margin": _prev_resolution_margin,
        "bybit_perp_price": bybit_feed.state.perp_price if bybit_feed and bybit_feed.state else 0,
        "funding_rate": bybit_feed.state.funding_rate if bybit_feed and bybit_feed.state else 0,
        "depth_usd_top20": depth_feed.get_depth_usd() if depth_feed else 0,
        "entry_phase": entry_phase.get("phase", "unknown"),
        "cvd_120s": trades_feed.accumulator.get_cvd(120) if trades_feed and trades_feed.accumulator else 0,
        "taker_ratio_60s": trades_feed.accumulator.get_taker_ratio(60) if trades_feed and trades_feed.accumulator else 0,
        "volume_surge": trades_feed.accumulator.is_volume_surge() if trades_feed and trades_feed.accumulator else False,
        "liquidation_pressure": liquidation_val,
        "gex_signal": gex_val,
        "gex_regime": gex_info.get("regime", "neutral"),
        "cvd_acceleration": cvd_accel_val,
        "clob_velocity_up": clob_ws.get_price_velocity(token_up) if clob_ws else 0,
        "clob_velocity_down": clob_ws.get_price_velocity(token_down) if clob_ws else 0,
        "coinbase_btc": coinbase_feed.state.price if coinbase_feed and coinbase_feed.state.price > 0 else 0,
        "regime_state": regime_state.name if regime_state else "unknown",
        "regime_kelly_mult": regime_state.kelly_mult if regime_state else 1.0,
        "sprt_confidence": _sprt.get_confidence() if _sprt else 0,
        "sprt_status": _sprt.get_status() if _sprt else "N/A",
        "signal_consensus": consensus_mult,
        "adverse_selection_30s": _adverse_monitor.get_adverse_rate(30.0) if _adverse_monitor else 0.5,
        "alpha_decay_rate": _alpha_decay.get_decay_rate() if _alpha_decay else 0,
        "garch_vol_ratio": _garch.compute_vol_ratio(np.diff(np.log(closes)) if closes is not None and len(closes) > 20 else np.array([]), deribit_feed.state.btc_iv if deribit_feed and deribit_feed.state.btc_iv else 0) if _garch else 1.0,
        "crowd_bias": _crowd_bias.compute_composite(price_up, price_down, strike) if _crowd_bias else {},
        "oracle_divergence": oracle_divergence,
        "edge_realization_ratio": _get_edge_realization_ratio(),
    }
    snapshot_str = json.dumps(snapshot)
    result = await trader.open_trade(
        market_id=cid,
        question=contract["question"],
        side=side,
        price=price,
        size=size,
        signal_score=signal.prob,
        signal_strength=f"edge={signal.edge:.0%}",
        ev_at_entry=signal.edge,
        exit_target=1.0,
        stop_loss=0.0,
        weight_version=signal_config.get("active_weights_version", "weights_v001"),
        indicator_snapshot=snapshot_str,
        token_id=token_id,
        fee_rate=fee_rate,
    )

    if result.success:
        shares_ordered = size / price
        fee_shares = entry_fee_shares(shares_ordered, price, fee_rate)
        fee_usd = fee_shares * price
        # Log fill vs last actual trade for realism validation
        last_trade_info = ""
        if clob_ws:
            lt = clob_ws.last_trade.get(token_id, {})
            if lt.get("price"):
                last_trade_info = f" last_trade={lt['price']}"
        bankroll_now = await db.get_bankroll()
        logger.info(
            f"{_C.GREEN}{'=' * 60}{_C.RESET}\n"
            f"  {_C.GREEN}{_C.BOLD}OPEN {side}{_C.RESET}  @ {price:.3f}  |  ${size:.2f}  |  fee ${fee_usd:.2f}\n"
            f"  {contract.get('question', cid)}  [{entry_phase['phase']}]\n"
            f"  {_C.DIM}Bankroll ${bankroll_now:.2f}  |  {signal.reason}{_C.RESET}")
        if _adverse_monitor:
            mkt_mid = (price_up + price_down) / 2 if price_up + price_down > 0 else price
            _adverse_monitor.record_fill(side=side, fill_price=price, token_id=token_id, midprice=mkt_mid)
        if alert_manager:
            mkt_price = price_up if side == "Up" else price_down
            await alert_manager.send_trade_opened(
                question=contract["question"], side=side, size=size,
                entry_price=price, ev=signal.edge, exit_target=1.0,
                model_prob=signal.prob, market_price=mkt_price,
                fee=fee_usd, flow=flow_score, bankroll=bankroll_now)
        return cid, last_eval_log_window

    return None, last_eval_log_window


def _compute_strike_and_btc(cid: str, binance_feed: Any, window_strikes: dict[int, float],
                            eval_window: int,
                            last_eval_log_window: int,
                            chainlink_feed: Any = None,
                            coinbase_feed: Any = None,
                            **kwargs) -> tuple[float | None, float | None, dict[int, float], int, str]:
    """Derive strike and BTC price, preferring Chainlink (resolution source) over Binance."""
    now_ts = int(time.time())

    try:
        contract_window_ts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        contract_window_ts = int(now_ts // 300) * 300  # fallback

    if contract_window_ts not in window_strikes:
        # Prefer Chainlink boundary price (matches Polymarket's priceToBeat)
        if chainlink_feed:
            cl_strike = chainlink_feed.get_strike(contract_window_ts)
            if cl_strike:
                window_strikes[contract_window_ts] = cl_strike
                logger.info(f"STRIKE: using Chainlink ${cl_strike:,.2f} for window {contract_window_ts}")

        # Fall back to Binance candle if Chainlink didn't capture it
        if contract_window_ts not in window_strikes:
            target_ms = contract_window_ts * 1000
            candles = binance_feed.buffer.get_last_n(10)
            for c in reversed(candles):
                if c.timestamp == target_ms:
                    window_strikes[contract_window_ts] = c.open
                    break
                elif c.timestamp < target_ms <= c.timestamp + 60_000:
                    window_strikes[contract_window_ts] = c.close
                    break
            else:
                latest = binance_feed.buffer.latest()
                if latest and now_ts - contract_window_ts < 10:
                    window_strikes[contract_window_ts] = latest.close

    # Clean old strikes
    window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}

    strike = window_strikes.get(contract_window_ts, 0)
    if strike <= 0:
        buf_len = len(binance_feed.buffer) if binance_feed.buffer else 0
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL: no strike for window {contract_window_ts} — candle buffer has {buf_len} candles")
        return None, None, window_strikes, last_eval_log_window, "none"

    # BTC price priority: Coinbase (fastest) > Kraken (Chainlink source) > Binance (fallback)
    _price_source = "none"
    kraken_feed_ref = kwargs.get("kraken_feed")
    if coinbase_feed and coinbase_feed.state.price > 0 and coinbase_feed.state.age_seconds < 5:
        btc_price = coinbase_feed.state.price
        _price_source = f"coinbase ({coinbase_feed.state.age_seconds:.1f}s)"
    elif kraken_feed_ref and kraken_feed_ref.state.price > 0 and kraken_feed_ref.state.age_seconds < 5:
        btc_price = kraken_feed_ref.state.price
        _price_source = f"kraken ({kraken_feed_ref.state.age_seconds:.1f}s)"
    else:
        btc_price = binance_feed.buffer.latest().close if binance_feed.buffer.latest() else 0
        _price_source = f"binance (fallback)"
    if btc_price <= 0:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL: no BTC price — Binance feed not ready")
        return None, None, window_strikes, last_eval_log_window, "none"

    # Skip if candle data is stale (WebSocket may have disconnected)
    latest_candle_age = (time.time() * 1000 - binance_feed.buffer.latest().timestamp) / 1000
    if latest_candle_age > 180:
        logger.warning(f"Stale candle data: {latest_candle_age:.0f}s old, skipping entry")
        return None, None, window_strikes, last_eval_log_window, "none"

    return strike, btc_price, window_strikes, last_eval_log_window, _price_source


async def _fetch_market_prices(contract: dict[str, Any], token_up: str, token_down: str,
                               market_scanner: Any,
                               http_client: Any, clob_ws: Any, max_spread: float,
                               last_eval_log_window: int) -> tuple[dict[str, Any] | None, int]:
    """Read order books, fetch negRisk prices, apply sanity/depth/spread gates."""
    now_ts = int(time.time())

    # Read order books — WebSocket state (instant) with HTTP fallback
    if clob_ws and clob_ws.connected:
        book_up = clob_ws.get_book(token_up)
        book_down = clob_ws.get_book(token_down)
        # Fall back to HTTP if WS book is empty or has no asks
        if not book_up or not book_up.get("asks"):
            book_up = await market_scanner.fetch_clob_book(token_up, http_client)
        if not book_down or not book_down.get("asks"):
            book_down = await market_scanner.fetch_clob_book(token_down, http_client)
    else:
        book_up = await market_scanner.fetch_clob_book(token_up, http_client)
        book_down = await market_scanner.fetch_clob_book(token_down, http_client)

    # NegRisk execution prices: GET /price accounts for cross-matching
    exec_up = await market_scanner.fetch_market_price(token_up, "BUY", http_client)
    exec_down = await market_scanner.fetch_market_price(token_down, "BUY", http_client)

    if exec_up > 0 or exec_down > 0:
        price_up = exec_up if exec_up > 0 else contract["price_up"]
        price_down = exec_down if exec_down > 0 else contract["price_down"]
        price_source = "clob"
    else:
        price_up = contract["price_up"]
        price_down = contract["price_down"]
        price_source = "gamma"

    # Price sanity gate
    price_sum = price_up + price_down
    if price_source == "clob" and (price_sum < 0.98 or price_sum > 1.02):
        eval_window = int(now_ts // 300) * 300
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL: stale prices | Up={price_up:.2f} + Dn={price_down:.2f} = {price_sum:.2f} — skipping")
        return None, last_eval_log_window

    # Raw book depth
    ask_up, depth_up = market_scanner.clob_best_ask(book_up)
    ask_down, depth_down = market_scanner.clob_best_ask(book_down)

    eval_window = int(now_ts // 300) * 300

    # Book depth in USD
    depth_usd_up = depth_up * ask_up if ask_up > 0 else 0
    depth_usd_down = depth_down * ask_down if ask_down > 0 else 0

    # Skip if no real depth to fill against
    if price_source == "clob":
        min_depth = market_scanner.min_book_depth_usd
        if depth_usd_up < min_depth and depth_usd_down < min_depth:
            if eval_window != last_eval_log_window:
                last_eval_log_window = eval_window
                logger.info(f"EVAL: thin CLOB depth Up=${depth_usd_up:.0f} Dn=${depth_usd_down:.0f} — skipping window")
            return None, last_eval_log_window

    # Skip if spread too wide
    if price_source == "clob":
        spread_val = -1.0
        if clob_ws:
            bba_up = clob_ws.best_bid_ask.get(token_up, {})
            if bba_up.get("spread"):
                spread_val = float(bba_up["spread"])
        if spread_val < 0:
            spread_val = await market_scanner.get_spread(token_up, http_client)
        if spread_val >= 0 and spread_val > max_spread:
            logger.debug(f"Wide spread {spread_val:.3f} > {max_spread} — skipping")
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
                                           ) -> tuple[dict[str, Any] | None, str | None, dict[str, int], list[str], list[str]]:
    """Find an active contract and subscribe its WebSocket tokens. Returns (contract, cid, ..., prev_tokens)."""
    if prev_contract_tokens is None:
        prev_contract_tokens = []
    contract = await market_scanner.find_active_contract()
    if not contract:
        return None, None, traded_contracts, ws_subscribed_tokens, prev_contract_tokens

    cid = contract["slug"]  # Use slug as market_id — US API needs marketSlug, not condition_id

    # Clean old entries
    now_ts = int(time.time())
    traded_contracts = {k: v for k, v in traded_contracts.items() if now_ts - v < 600}

    # One trade per contract
    if cid in traded_contracts:
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

    return contract, cid, traded_contracts, ws_subscribed_tokens, current_tokens


async def _check_counterfactuals(counterfactual_tracker: Any, market_scanner: Any,
                                 http_client: Any, binance_feed: Any) -> None:
    """Pre-fetch Gamma metadata for watched scalps and check resolutions."""
    cf_event_metadata = {}
    for cf_mid in counterfactual_tracker.watched_markets:
        cf_live = await _get_contract_prices(market_scanner, cf_mid, http_client)
        if cf_live and cf_live.get("event_metadata"):
            cf_event_metadata[cf_mid] = cf_live["event_metadata"]
    cf_resolved = counterfactual_tracker.check_resolutions(
        binance_feed, _btc_at_expiry, event_metadata=cf_event_metadata
    )
    for cf in cf_resolved:
        verdict = "CORRECT" if cf["scalp_was_optimal"] else "MISSED +${:.2f}".format(cf["delta_pnl"])
        logger.info(f"COUNTERFACTUAL resolved: {cf['side']} {cf['market_id']} — {verdict}")


async def _evaluate_and_exit_position(
        pos: dict[str, Any], live: dict[str, Any], binance_feed: Any,
        indicator_engine: Any, signal_engine: Any, market_scanner: Any,
        http_client: Any, clob_ws: Any, trader: Any, alert_manager: Any, db: Any,
        outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        config: dict[str, Any], scheduler: Any, default_exit_threshold: float,
        day_wins: int, day_losses: int, day_fees: float,
        depth_feed: Any = None, trades_feed: Any = None,
        bybit_feed: Any = None, deribit_feed: Any = None,
        coinbase_feed: Any = None,
        chainlink_feed: Any = None,
        kraken_feed: Any = None) -> tuple[int, int, float, str | None]:
    """Re-evaluate an active position and exit (scalp) if holding edge is gone."""
    # BTC price priority: Coinbase > Kraken > Binance
    kraken_feed_ref = kraken_feed
    if coinbase_feed and coinbase_feed.state.price > 0 and coinbase_feed.state.age_seconds < 5:
        btc_now = coinbase_feed.state.price
    elif kraken_feed_ref and kraken_feed_ref.state.price > 0 and kraken_feed_ref.state.age_seconds < 5:
        btc_now = kraken_feed_ref.state.price
    else:
        btc_now = binance_feed.buffer.latest().close if binance_feed.buffer.latest() else 0
    if btc_now <= 0:
        return day_wins, day_losses, day_fees, None

    # Don't make exit decisions on stale data — hold until fresh
    candle_age = (time.time() * 1000 - binance_feed.buffer.latest().timestamp) / 1000
    if candle_age > 180:
        return day_wins, day_losses, day_fees, None

    # Get strike from the position's stored trade_context (correct for this contract)
    pos_ctx = json.loads(pos.get("indicator_snapshot", "{}")).get("trade_context", {})
    strike_now = pos_ctx.get("strike_price", 0)
    if strike_now <= 0:
        return day_wins, day_losses, day_fees, None

    indicators = indicator_engine.compute_all(binance_feed.buffer)

    # NegRisk execution sell price via /price endpoint
    hold_token = live.get("token_id_up", "") if pos["side"] == "Up" else live.get("token_id_down", "")
    exec_sell = await market_scanner.fetch_market_price(hold_token, "SELL", http_client)
    gamma_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]
    market_price = exec_sell if exec_sell > 0 else gamma_price

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

    # New signals for hold evaluation
    hold_spot_flow = 0.0
    hold_wall_pressure = 0.0
    hold_iv_ratio = 1.0

    if trades_feed and trades_feed.accumulator:
        acc = trades_feed.accumulator
        cvd = acc.get_cvd(window_s=120)
        taker = acc.get_taker_ratio(window_s=60)
        trade_count = acc.trade_count
        cvd_z = _cvd_normalizer.normalize("cvd_hold", cvd) if _cvd_normalizer else 0.0
        cvd_comp = math.tanh(cvd_z) * 0.8
        taker_comp = (taker - 0.5) * 2 * 0.2 if trade_count >= 5 else 0.0
        hold_spot_flow = max(-1.0, min(1.0, cvd_comp + taker_comp))

    if depth_feed:
        hold_wall_pressure = depth_feed.get_wall_pressure(strike_now, btc_now)

    if deribit_feed and deribit_feed.state.btc_iv > 0:
        atr_val = indicators.get("atr", {}).get("atr", 0)
        deribit_cfg = config.get("deribit", {})
        hold_iv_ratio = deribit_feed.state.get_iv_ratio(
            atr_val, btc_now,
            iv_min=deribit_cfg.get("iv_ratio_min", 0.5),
            iv_max=deribit_cfg.get("iv_ratio_max", 3.0))

    # Liquidation pressure for hold evaluation
    hold_liquidation = 0.0
    if bybit_feed and bybit_feed.state.open_interest > 0 and bybit_feed.state.open_interest_prev > 0:
        hold_liquidation = compute_liquidation_pressure(
            bybit_feed.state.open_interest, bybit_feed.state.open_interest_prev,
            bybit_feed.state.price_at_oi, bybit_feed.state.price_at_oi_prev)

    # GEX signal for hold evaluation
    hold_gex = 0.0
    if deribit_feed and deribit_feed.state.net_gex != 0:
        hold_gex = deribit_feed.state.net_gex

    action, model_prob, holding_edge, reason = signal_engine.evaluate_hold(
        indicators, btc_now, strike_now, live["seconds_remaining"],
        market_price, pos["side"], exit_threshold,
        entry_price=pos["entry_price"],
        fee_rate=pos.get("fee_rate") or DEFAULT_FEE_RATE,
        closes=closes, flow_signal=hold_flow["flow_score"],
        spot_flow_signal=hold_spot_flow,
        wall_pressure=hold_wall_pressure,
        prev_resolution_margin=_prev_resolution_margin,
        iv_ratio=1.0,  # ATR is the correct 5-min vol; Deribit 30-day IV not applied
        liquidation_pressure=hold_liquidation,
        gex_signal=hold_gex)

    # --- TRAILING PROFIT EXIT: don't ride cheap winners to zero ---
    mid = pos["market_id"]
    prev_peak = _peak_hold_price.get(mid, 0.0)
    if market_price > prev_peak:
        _peak_hold_price[mid] = market_price
    peak = _peak_hold_price.get(mid, 0.0)
    if (action == "HOLD"
            and pos["entry_price"] < 0.50
            and peak >= 0.65
            and market_price < peak * 0.85):
        action = "EXIT"
        reason = (
            f"Trailing profit exit {pos['side']}: entry={pos['entry_price']:.2f} "
            f"peak={peak:.2f} now={market_price:.2f} (dropped {(1 - market_price/peak):.0%} from peak)")
        logger.info(f"TRAILING EXIT triggered: {reason}")

    if action == "HOLD":
        # Log hold status every 30s so the operator knows the bot is alive
        now_ts = time.time()
        if now_ts - _last_hold_log.get(mid, 0) >= 30:
            _last_hold_log[mid] = now_ts
            secs = live['seconds_remaining']
            edge_color = _C.GREEN if holding_edge > 0 else (_C.RESET if holding_edge == 0 else _C.RED)
            edge_str = f"{abs(holding_edge):.0%}" if holding_edge == 0 else f"{holding_edge:+.0%}"
            cl_str = f"  cl ${chainlink_feed.price:,.0f}" if chainlink_feed and chainlink_feed.price > 0 else ""
            logger.info(
                f"  {_C.DIM}HOLD {pos['side']}{_C.RESET}  {secs:.0f}s  |  "
                f"prob {model_prob:.0%}  {edge_color}edge {edge_str}{_C.RESET}  |  "
                f"BTC ${btc_now:,.0f}{cl_str}  mkt {market_price:.2f}")
        if counterfactual_tracker:
            counterfactual_tracker.track_hold_moment(pos["market_id"], pos, {
                "holding_edge": holding_edge, "model_prob": model_prob,
                "market_price": market_price, "seconds_remaining": live["seconds_remaining"],
                "exit_threshold": exit_threshold, "strike_price": strike_now,
                "btc_price": btc_now,
            })

    traded_market_id = None
    if action == "EXIT":
        sell_token = live.get("token_id_up", "") if pos["side"] == "Up" else live.get("token_id_down", "")

        # Apply slippage to sell price (worse fill for seller)
        hold_book = clob_ws.get_book(hold_token) if clob_ws else {}
        bid_depth_usd = sum(
            float(b.get("size", 0)) * float(b.get("price", 0))
            for b in (hold_book or {}).get("bids", [])
        )
        shares_held = pos.get("shares_held") or pos["size"] / pos["entry_price"]
        exit_size_usd = shares_held * market_price
        impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
        slip = slippage_pct(exit_size_usd, bid_depth_usd, impact)
        exit_fill = round(market_price * (1 - slip), 4)

        result = await trader.close_trade(pos["id"], exit_fill, token_id=sell_token)
        if result.success:
            pnl = result.pnl
            gain_pct = result.gain_pct
            total_fees = result.entry_fee_usd + result.exit_fee_usd
            won = "WIN" if pnl > 0 else "LOSS"
            if pnl > 0: day_wins += 1
            else: day_losses += 1
            day_fees += total_fees
            color = _C.GREEN if pnl >= 0 else _C.RED
            bankroll_after = await db.get_bankroll()
            logger.info(
                f"{color}{'=' * 60}{_C.RESET}\n"
                f"  {color}{_C.BOLD}SCALP {won} {pos['side']}{_C.RESET}  |  {pos['entry_price']:.3f} -> {exit_fill:.3f}  |  {gain_pct:+.1%}  |  {color}${pnl:+.2f}{_C.RESET}\n"
                f"  {pos.get('question', pos['market_id'])}  |  fees ${total_fees:.2f}\n"
                f"  {_C.DIM}Day: {day_wins}W/{day_losses}L  |  Bankroll ${bankroll_after:.2f}{_C.RESET}")
            if breaker:
                breaker.update_bankroll(await db.get_bankroll())
                await db.set_peak_bankroll(breaker.peak_bankroll)
                cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
                if cb_event and alert_manager:
                    await alert_manager.send_circuit_breaker(cb_event, breaker)
            if alert_manager:
                await alert_manager.send_trade_closed(
                    question=pos.get("question", ""), exit_price=exit_fill, log_return=0, hold_hours=0,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                    gain_pct=gain_pct, reason=f"scalp {won.lower()}", fees=total_fees,
                    bankroll=bankroll_after, day_wins=day_wins, day_losses=day_losses)
            await _record_outcome(outcome_reviewer, pos, exit_fill, result.log_return or 0, gain_pct,
                                  exit_reason="scalp", pnl=pnl, fees=total_fees)
            _realized_edge_history.append((pos.get("ev_at_entry", 0), gain_pct))
            if len(_realized_edge_history) > 500:
                _realized_edge_history[:] = _realized_edge_history[-500:]
            if _drawdown_tracker:
                _drawdown_tracker.record_trade(gain_pct)
            traded_market_id = pos["market_id"]
            _peak_hold_price.pop(traded_market_id, None)
            if counterfactual_tracker:
                counterfactual_tracker.watch(pos, {
                    "exit_fill": exit_fill, "pnl": pnl, "gain_pct": gain_pct,
                    "holding_edge": holding_edge, "model_prob": model_prob,
                    "market_price": market_price, "seconds_remaining": live["seconds_remaining"],
                    "exit_threshold": exit_threshold, "strike_price": strike_now,
                    "btc_price": btc_now,
                })

    return day_wins, day_losses, day_fees, traded_market_id


async def _resolve_expired_position(
        pos: dict[str, Any], live: dict[str, Any], trader: Any, alert_manager: Any,
        db: Any, outcome_reviewer: Any, breaker: Any, counterfactual_tracker: Any,
        day_wins: int, day_losses: int, day_fees: float) -> tuple[bool, int, int, float, str | None]:
    """Resolve a position whose contract has expired (seconds_remaining <= 0)."""
    global _prev_resolution_margin
    if live.get("closed") and (live["price_up"] >= 0.99 or live["price_up"] <= 0.01):
        # Polymarket has resolved: use the actual outcome prices
        exit_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]
    elif live.get("event_metadata"):
        # Gamma has Chainlink oracle prices but outcome prices not yet clear
        meta = live["event_metadata"]
        up_won = meta["final_price"] >= meta["price_to_beat"]
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        logger.info(f"RESOLVE via eventMetadata: priceToBeat={meta['price_to_beat']:,.2f} final={meta['final_price']:,.2f} → {'Up' if up_won else 'Down'}")
    else:
        # Gamma hasn't resolved yet — wait for next tick (polls every 2s)
        now_ts = time.time()
        mid = pos["market_id"]
        if now_ts - _last_resolve_wait_log.get(mid, 0) >= 60:
            _last_resolve_wait_log[mid] = now_ts
            logger.info(f"WAITING for Gamma resolution: {mid} | closed={live.get('closed')} "
                        f"meta={'yes' if live.get('event_metadata') else 'no'} "
                        f"prices=Up:{live.get('price_up', 0):.2f}/Dn:{live.get('price_down', 0):.2f}")
        return False, day_wins, day_losses, day_fees, None

    result = await trader.resolve_position(pos["id"], exit_price)
    traded_market_id = None
    if result.success:
        pnl = result.pnl
        gain_pct = result.gain_pct
        total_fees = result.entry_fee_usd + result.exit_fee_usd
        won = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0: day_wins += 1
        else: day_losses += 1
        day_fees += total_fees
        color = _C.GREEN if pnl >= 0 else _C.RED
        bankroll_after = await db.get_bankroll()
        logger.info(
            f"{color}{'=' * 60}{_C.RESET}\n"
            f"  {color}{_C.BOLD}RESOLVED {won} {pos['side']}{_C.RESET}  |  {pos['entry_price']:.3f} -> {exit_price:.3f}  |  {gain_pct:+.1%}  |  {color}${pnl:+.2f}{_C.RESET}\n"
            f"  {pos.get('question', pos['market_id'])}  |  fees ${total_fees:.2f}\n"
            f"  {_C.DIM}Day: {day_wins}W/{day_losses}L  |  Bankroll ${bankroll_after:.2f}{_C.RESET}")
        if breaker:
            breaker.update_bankroll(await db.get_bankroll())
            await db.set_peak_bankroll(breaker.peak_bankroll)
            cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
            if cb_event and alert_manager:
                await alert_manager.send_circuit_breaker(cb_event, breaker)
        if alert_manager:
            await alert_manager.send_trade_closed(
                question=pos.get("question", ""), exit_price=exit_price, log_return=0, hold_hours=0,
                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                gain_pct=gain_pct, reason=won.lower(), fees=total_fees,
                bankroll=bankroll_after, day_wins=day_wins, day_losses=day_losses)
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=total_fees)
        _realized_edge_history.append((pos.get("ev_at_entry", 0), gain_pct))
        if len(_realized_edge_history) > 500:
            _realized_edge_history[:] = _realized_edge_history[-500:]
        if _drawdown_tracker:
            _drawdown_tracker.record_trade(gain_pct)
        if counterfactual_tracker:
            cf_hold = counterfactual_tracker.record_hold_resolution(
                pos["market_id"], exit_price, pnl, gain_pct)
            if cf_hold:
                verdict = "CORRECT" if cf_hold["hold_was_optimal"] else "MISSED scalp +${:.2f}".format(-cf_hold["delta_pnl"])
                logger.info(f"COUNTERFACTUAL HOLD resolved: {pos['side']} {pos['market_id']} — {verdict}")
        traded_market_id = pos["market_id"]
        _peak_hold_price.pop(traded_market_id, None)
        # Track resolution margin for adjacent window momentum (D2)
        meta = live.get("event_metadata")
        if meta and meta.get("final_price") and meta.get("price_to_beat"):
            _prev_resolution_margin = meta["final_price"] - meta["price_to_beat"]
        # Record winning side for crowd bias recency tracking
        if _crowd_bias:
            winning_side = pos["side"] if gain_pct > 0 else ("Down" if pos["side"] == "Up" else "Up")
            _crowd_bias.record_resolution(winning_side)
    return True, day_wins, day_losses, day_fees, traded_market_id


async def _manage_orphaned_position(
        pos: dict[str, Any], market_scanner: Any, http_client: Any, trader: Any,
        alert_manager: Any, db: Any, outcome_reviewer: Any, breaker: Any,
        day_wins: int, day_losses: int, day_fees: float) -> tuple[bool, int, int, float, str | None]:
    """Resolve positions where the contract can no longer be found via Gamma API."""
    from datetime import datetime, timezone

    try:
        entry_dt = datetime.fromisoformat(pos.get("entry_timestamp", ""))
        age = (datetime.now(timezone.utc) - entry_dt).total_seconds()
    except (ValueError, TypeError):
        age = 0
    if age < 600:
        return True, day_wins, day_losses, day_fees, None  # too young, skip
    # Try direct Gamma fetch for eventMetadata (Chainlink oracle)
    direct = await _get_contract_prices(market_scanner, pos["market_id"], http_client)
    if direct and direct.get("event_metadata"):
        meta = direct["event_metadata"]
        up_won = meta["final_price"] >= meta["price_to_beat"]
        exit_price = 1.0 if (pos["side"] == "Up") == up_won else 0.0
        logger.info(f"RESOLVE orphan via eventMetadata: priceToBeat={meta['price_to_beat']:,.2f} final={meta['final_price']:,.2f} → {'Up' if up_won else 'Down'}")
    elif direct and direct.get("closed") and (direct["price_up"] >= 0.99 or direct["price_up"] <= 0.01):
        exit_price = direct["price_up"] if pos["side"] == "Up" else direct["price_down"]
    else:
        # No official resolution data yet — keep waiting.
        # Never guess from Binance: Chainlink oracle can differ by $20-200.
        if age > 3600:
            logger.error(f"ORPHANED >1hr: {pos['market_id']} — no Gamma resolution data. Waiting for Chainlink oracle.")
            if alert_manager:
                await alert_manager.send_trade_closed(
                    question=pos.get("question", ""), exit_price=0, log_return=0, hold_hours=age / 3600,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=0,
                    gain_pct=0, reason="orphaned — awaiting resolution", fees=0)
        else:
            logger.info(f"Orphan {pos['market_id']} age={age:.0f}s — waiting for Gamma resolution data")
        return True, day_wins, day_losses, day_fees, None  # still waiting
    result = await trader.resolve_position(pos["id"], exit_price)
    traded_market_id = None
    if result.success:
        pnl = result.pnl
        gain_pct = result.gain_pct
        total_fees = result.entry_fee_usd + result.exit_fee_usd
        won = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0: day_wins += 1
        else: day_losses += 1
        day_fees += total_fees
        color = _C.GREEN if pnl >= 0 else _C.RED
        bankroll_after = await db.get_bankroll()
        logger.info(
            f"{color}{'=' * 60}{_C.RESET}\n"
            f"  {color}{_C.BOLD}RESOLVED {won} {pos['side']} (orphan){_C.RESET}  |  {pos['entry_price']:.3f} -> {exit_price:.3f}  |  {gain_pct:+.1%}  |  {color}${pnl:+.2f}{_C.RESET}\n"
            f"  {pos.get('question', pos['market_id'])}\n"
            f"  {_C.DIM}Day: {day_wins}W/{day_losses}L  |  Bankroll ${bankroll_after:.2f}{_C.RESET}")
        if breaker:
            breaker.update_bankroll(await db.get_bankroll())
            await db.set_peak_bankroll(breaker.peak_bankroll)
            cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
            if cb_event and alert_manager:
                await alert_manager.send_circuit_breaker(cb_event, breaker)
        if alert_manager:
            await alert_manager.send_trade_closed(
                question=pos.get("question", ""), exit_price=exit_price, log_return=0, hold_hours=0,
                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                gain_pct=gain_pct, reason=won.lower(), fees=total_fees,
                bankroll=bankroll_after, day_wins=day_wins, day_losses=day_losses)
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=total_fees)
        _realized_edge_history.append((pos.get("ev_at_entry", 0), gain_pct))
        if len(_realized_edge_history) > 500:
            _realized_edge_history[:] = _realized_edge_history[-500:]
        if _drawdown_tracker:
            _drawdown_tracker.record_trade(gain_pct)
        traded_market_id = pos["market_id"]
        _peak_hold_price.pop(traded_market_id, None)
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
        # Restore from DB in case of mid-day restart
        day_wins, day_losses, day_fees = await db.get_day_stats(today_str)
        if breaker:
            breaker.reset()
        if alert_manager:
            await alert_manager.send_day_open(config.get("mode", "paper"), day_open_bankroll)

    if not in_trading_hours and current_trading_day is not None:
        # Trading hours ended — send day close banner
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
                       http_client: Any = None,
                       depth_feed: Any = None,
                       trades_feed: Any = None,
                       bybit_feed: Any = None,
                       deribit_feed: Any = None,
                       chainlink_feed: Any = None,
                       coinbase_feed: Any = None,
                       kraken_feed: Any = None) -> None:
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
        http_client = httpx.AsyncClient(timeout=5)

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
        logger.info("Fresh day start (scheduled restart)")
    else:
        _db_wins, _db_losses, _db_fees = await db.get_day_stats(_today_et)
        current_trading_day = _today_et if (_db_wins + _db_losses) > 0 else None
        day_open_bankroll = await db.get_bankroll()
        day_wins = _db_wins
        day_losses = _db_losses
        day_fees = _db_fees
        if _db_wins + _db_losses > 0:
            logger.info(f"Mid-day restart: restored {_db_wins}W/{_db_losses}L from DB")

    while True:
        # Check if scheduler requested shutdown (auto-restart cycle after pipeline)
        if scheduler and getattr(scheduler, '_shutdown_requested', False):
            logger.info("Scheduler requested shutdown — exiting trading loop")
            break

        # Event-driven: react instantly to WebSocket book/resolution updates, timeout 1s for housekeeping
        if clob_ws:
            try:
                # Wake on book update OR market resolution — whichever comes first
                book_task = asyncio.create_task(clob_ws.book_updated.wait())
                resolve_task = asyncio.create_task(clob_ws.market_resolved.wait())
                done, pending = await asyncio.wait(
                    {book_task, resolve_task}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if clob_ws.book_updated.is_set():
                    clob_ws.book_updated.clear()
                if clob_ws.market_resolved.is_set():
                    clob_ws.market_resolved.clear()
                    # Invalidate price cache — Gamma should have resolution data now
                    _contract_price_cache.clear()
                    logger.info("WS market_resolved — cache cleared, checking resolution")
            except asyncio.TimeoutError:
                pass  # housekeeping tick — contract discovery, day banners
        else:
            await asyncio.sleep(0.25)  # fallback polling if no WebSocket
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
            for pos in positions:
                live = await _get_contract_prices(market_scanner, pos["market_id"], http_client)

                if not live:
                    handled, day_wins, day_losses, day_fees, traded_mid = \
                        await _manage_orphaned_position(
                            pos, market_scanner, http_client, trader,
                            alert_manager, db, outcome_reviewer, breaker,
                            day_wins, day_losses, day_fees)
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
                            day_wins, day_losses, day_fees)
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
                            bybit_feed=bybit_feed, deribit_feed=deribit_feed,
                            coinbase_feed=coinbase_feed,
                            chainlink_feed=chainlink_feed, kraken_feed=kraken_feed)
                    if traded_mid:
                        traded_contracts[traded_mid] = int(time.time())

            # --- COUNTERFACTUAL: check watched scalps for resolution ---
            if counterfactual_tracker:
                await _check_counterfactuals(counterfactual_tracker, market_scanner,
                                             http_client, binance_feed)

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
                    prev_contract_tokens)
            if not contract:
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
                                        kraken_feed=kraken_feed)
            if strike is None:
                continue

            traded_cid, last_eval_log_window = await _evaluate_signal_and_enter(
                contract, cid, binance_feed, indicator_engine,
                signal_engine, market_scanner, http_client, clob_ws,
                trader, alert_manager, db, config, breaker,
                price_up, price_down, price_source,
                book_up, book_down, depth_usd_up, depth_usd_down,
                btc_price, strike, eval_window, last_eval_log_window,
                token_up, token_down, signal_config, max_bankroll_pct,
                now_ts,
                depth_feed=depth_feed, trades_feed=trades_feed,
                bybit_feed=bybit_feed, deribit_feed=deribit_feed,
                coinbase_feed=coinbase_feed,
                chainlink_feed=chainlink_feed,
                kraken_feed=kraken_feed)
            if traded_cid:
                traded_contracts[traded_cid] = now_ts

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
    return parser.parse_args()


async def run_pipeline() -> None:
    """Run the daily learning pipeline once and exit. No trading, no WebSockets, no Discord."""
    config = load_config()
    base_dir = Path(__file__).parent

    # Logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    signal_cfg = config.get("signal", {})
    market_cfg = config.get("market", {})
    sched_cfg = config.get("schedule", {})
    weights_dir = str(base_dir / "memory" / "weights")
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
                "high_pct": ind_cfg.get("atr", {}).get("high_percentile", 95),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(weights_dir=weights_dir,
        active_version=signal_cfg.get("active_weights_version", "weights_v001"),
        params=indicator_params)

    signal_engine = _build_signal_engine(signal_cfg, config)

    from polybot.core.calibrator import PlattCalibrator
    calibrator = PlattCalibrator()
    _cal_path = Path(base_dir) / "memory" / "calibration" / "platt_params.json"
    calibrator.load(_cal_path)
    signal_engine.calibrator = calibrator

    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model="claude-sonnet-4-6")

    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    ta_evolver = TAEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"),
                          claude_client=claude)
    weight_optimizer = WeightOptimizer(
        weights_dir=weights_dir,
        scores_path=str(base_dir / "memory" / "weight_scores.json"),
        min_improvement=0.03,
    )

    agents_cfg = config["agents"]
    scheduler = AgentScheduler(
        outcome_reviewer=outcome_reviewer,
        bias_detector=bias_detector,
        ta_evolver=ta_evolver,
        weight_optimizer=weight_optimizer,
        indicator_engine=indicator_engine,
        signal_engine=signal_engine,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        daily_pipeline_minute=agents_cfg.get("daily_pipeline_minute", 0),
        math_config=config["math"],
        config=config,
        counterfactual_tracker=counterfactual_tracker,
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", -0.10)
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    scheduler._trading_start = (sched_cfg.get("trading_start_hour_et", 0), sched_cfg.get("trading_start_minute", 15))
    scheduler._trading_end = (sched_cfg.get("trading_end_hour_et", 23), sched_cfg.get("trading_end_minute", 59))

    logger.info("Running daily learning pipeline (manual trigger)...")
    await scheduler.run_daily_pipeline()
    logger.info("Pipeline complete.")


async def main() -> None:
    args = parse_args()
    config = load_config()
    mode = args.mode or config.get("mode", "paper")
    config["mode"] = mode
    base_dir = Path(__file__).parent

    # Database — separate files for paper and live (no cross-contamination)
    db_path = config["database"]["path"]
    if mode == "live":
        db_path = db_path.replace(".db", "_live.db")
    db = Database(db_path)
    await db.initialize()
    if await db.get_bankroll() == 0:
        await db.set_bankroll(config["execution"]["initial_bankroll"])

    # Math config
    math_cfg = config["math"]

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
    )

    # Indicator engine
    signal_cfg = config.get("signal", {})
    weights_dir = str(base_dir / "memory" / "weights")
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
                "high_pct": ind_cfg.get("atr", {}).get("high_percentile", 95),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(
        weights_dir=weights_dir,
        active_version=signal_cfg.get("active_weights_version", "weights_v001"),
        params=indicator_params,
    )

    # Signal engine — probability model with edge-based entry
    from polybot.core.calibrator import PlattCalibrator

    signal_engine = _build_signal_engine(signal_cfg, config)

    # Load Platt calibrator (identity if file doesn't exist)
    calibrator = PlattCalibrator()
    _cal_path = Path(base_dir) / "memory" / "calibration" / "platt_params.json"
    calibrator.load(_cal_path)
    signal_engine.calibrator = calibrator

    # Brain (Claude client kept for TA evolver analysis calls)
    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model="claude-sonnet-4-6")

    # Execution — route based on mode
    exec_cfg = config["execution"]
    if mode == "live":
        ok, msg, live_balance = verify_auth()
        if not ok:
            logger.error(f"LIVE MODE preflight failed: {msg}")
            return
        logger.info(f"LIVE MODE — {msg}")
        trader = LiveTrader(db=db, max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"],
            use_maker_orders=exec_cfg.get("use_maker_orders", False),
            maker_timeout_s=exec_cfg.get("maker_timeout_s", 60.0))
    else:
        trader = PaperTrader(db=db, max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
        logger.info("PAPER MODE — simulated trading")

    # Circuit breaker (drawdown-based Kelly scaling)
    cb_cfg = config.get("circuit_breaker", {})
    init_bankroll = await db.get_bankroll()
    breaker = CircuitBreaker(
        initial_bankroll=init_bankroll,
        max_drawdown_pct=cb_cfg.get("max_drawdown_pct", 0.30),
        min_multiplier=cb_cfg.get("min_multiplier", 0.40),
        losses_to_reduce=cb_cfg.get("losses_to_reduce", 3),
        wins_to_restore=cb_cfg.get("wins_to_restore", 2),
    )
    persisted_peak = await db.get_peak_bankroll()
    if persisted_peak is not None and persisted_peak > init_bankroll:
        breaker.peak_bankroll = persisted_peak
        logger.info(f"CIRCUIT BREAKER: restored persisted peak ${persisted_peak:,.2f} (current ${init_bankroll:,.2f}, drawdown={breaker.drawdown_pct:.1%})")
    else:
        await db.set_peak_bankroll(init_bankroll)

    # Agents
    agents_cfg = config["agents"]
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    counterfactual_tracker = CounterfactualTracker(memory_dir=str(base_dir / "memory"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    ta_evolver = TAEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"),
                          claude_client=claude)
    weight_optimizer = WeightOptimizer(
        weights_dir=weights_dir,
        scores_path=str(base_dir / "memory" / "weight_scores.json"),
        min_improvement=0.03,
    )

    # Discord (created before scheduler so alert_manager can be passed in)
    discord_bot = create_bot(db, trader, market_scanner, None, config)
    alert_manager = AlertManager(bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"],
        daily_channel_name=config["discord"].get("daily_channel_name", "polybot-daily"))
    discord_bot.alert_manager = alert_manager

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
        math_config=math_cfg,
        market_scanner=market_scanner,
        config=config,
        counterfactual_tracker=counterfactual_tracker,
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", -0.10)
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    scheduler._auto_shutdown = args.auto_restart
    discord_bot.scheduler = scheduler
    if mode == "live":
        # Sync DB bankroll with real Polymarket balance (fetched during preflight)
        await db.set_bankroll(live_balance)

    # CLOB WebSocket — real-time order book feed
    clob_ws_url = market_cfg.get("clob_ws_url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    clob_ws = ClobWebSocket(url=clob_ws_url)
    await clob_ws.start()

    # Give LiveTrader access to CLOB WS for fast maker fill detection
    if hasattr(trader, "set_clob_ws"):
        trader.set_clob_ws(clob_ws)

    # --- New data feeds ---
    depth_cfg = config.get("binance_depth", {})
    depth_feed = BinanceDepthFeed(
        ws_url=depth_cfg.get("ws_url", "wss://stream.binance.us:9443/ws"),
        rest_url=depth_cfg.get("rest_url", "https://api.binance.us/api/v3"),
        rest_interval=depth_cfg.get("poll_interval_s", 5.0),
    )
    trades_cfg = config.get("binance_trades", {})
    trades_accumulator = BinanceTradeAccumulator(max_age_s=trades_cfg.get("max_age_s", 300))
    trades_feed = BinanceTradesFeed(
        accumulator=trades_accumulator,
        ws_url=trades_cfg.get("ws_url", "wss://stream.binance.us:9443/ws"),
    )
    bybit_cfg = config.get("bybit", {})
    bybit_feed_inst = BybitFeed(
        ws_url=bybit_cfg.get("ws_url", "wss://stream.bybit.com/v5/public/linear"),
        rest_url=bybit_cfg.get("rest_url", "https://api.bybit.com/v5/market/tickers"),
    )
    deribit_cfg = config.get("deribit", {})
    deribit_feed = DeribitIVFeed(
        poll_interval=deribit_cfg.get("poll_interval_s", 60.0),
    )

    # Coinbase feed — faster BTC price (leads Binance.US by 0.5-2s)
    coinbase_cfg = config.get("coinbase", {})
    coinbase_feed = CoinbaseFeed(
        ws_url=coinbase_cfg.get("ws_url", "wss://ws-feed.exchange.coinbase.com"),
        product_id=coinbase_cfg.get("product_id", "BTC-USD"),
    )

    # Kraken feed — Chainlink oracle data source, secondary fast price
    kraken_feed = KrakenFeed()

    # SPRT, regime detector, alpha decay — module-level state for trading loop
    global _sprt, _alpha_decay, _regime_detector
    _sprt = SPRTAccumulator(
        alpha=config.get("sprt", {}).get("alpha", 0.05),
        beta=config.get("sprt", {}).get("beta", 0.10),
        min_interval_s=config.get("sprt", {}).get("observation_interval_s", 10.0),
    )
    regime_cfg = config.get("regime", {})
    _regime_detector = RegimeDetector(
        lookback=regime_cfg.get("lookback", 50),
        vol_high_pct=regime_cfg.get("vol_high_percentile", 75),
        vol_low_pct=regime_cfg.get("vol_low_percentile", 25),
        autocorr_threshold=regime_cfg.get("autocorr_threshold", 0.25),
    )
    _alpha_decay = AlphaDecayTracker()

    global _adverse_monitor
    _adverse_monitor = AdverseSelectionMonitor()

    global _drawdown_tracker
    _drawdown_tracker = DrawdownVelocityTracker()

    global _crowd_bias
    _crowd_bias = CrowdBiasTracker()

    global _garch
    _garch = GarchPredictor()

    global _cvd_normalizer
    _cvd_normalizer = IndicatorNormalizer(alpha=0.02, warmup=50)

    await scheduler.start()
    await binance_feed.start()
    await depth_feed.start()
    await trades_feed.start()
    await bybit_feed_inst.start()
    await coinbase_feed.start()
    await kraken_feed.start()
    # DeribitIVFeed.start() is a blocking loop — run as background task
    deribit_task = asyncio.create_task(deribit_feed.start())

    # Chainlink oracle feed — resolution price source (Polymarket uses this, not Binance)
    from polybot.core.chainlink_feed import ChainlinkFeed
    chainlink_feed = ChainlinkFeed()
    await chainlink_feed.start()

    # Shared HTTP client — lifecycle managed here in main()
    import httpx
    http_client = httpx.AsyncClient(timeout=5)

    async def run_discord():
        try:
            await discord_bot.start(get_secret("DISCORD_BOT_TOKEN"))
        except Exception as e:
            logger.error(f"Discord bot error: {e}")

    trading_task = asyncio.create_task(trading_loop(
        binance_feed, market_scanner, indicator_engine, signal_engine,
        trader, alert_manager, db, config, outcome_reviewer,
        is_paused_fn=lambda: discord_bot.is_paused,
        scheduler=scheduler, clob_ws=clob_ws, breaker=breaker,
        counterfactual_tracker=counterfactual_tracker,
        http_client=http_client,
        depth_feed=depth_feed, trades_feed=trades_feed,
        bybit_feed=bybit_feed_inst, deribit_feed=deribit_feed,
        chainlink_feed=chainlink_feed, coinbase_feed=coinbase_feed,
        kraken_feed=kraken_feed))
    background_tasks = [
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(run_discord()),
    ]
    logger.info("PolyBot started — all systems running (WebSocket + event-driven)")

    try:
        # Wait for trading loop — it exits after pipeline sets _shutdown_requested
        await trading_task
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        # Pipeline already completed before trading_loop exited — cancel lingering tasks
        for t in background_tasks:
            t.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await http_client.aclose()
        await clob_ws.close()
        await scheduler.stop()
        await binance_feed.stop()
        await depth_feed.stop()
        await trades_feed.stop()
        await bybit_feed_inst.stop()
        await chainlink_feed.stop()
        deribit_feed.stop()
        deribit_task.cancel()
        await db.close()
        await discord_bot.close()


if __name__ == "__main__":
    args = parse_args()
    if args.run_pipeline:
        asyncio.run(run_pipeline())
    else:
        asyncio.run(main())
