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
from polybot.execution.base import taker_fee, entry_fee_shares, exit_fee_usdc, slippage_pct, DEFAULT_FEE_RATE
from polybot.db.models import Database
from polybot.core.binance_feed import BinanceFeed
from polybot.core.market_scanner import BTCMarketScanner
from polybot.core.clob_ws import ClobWebSocket
from polybot.indicators.engine import IndicatorEngine
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

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=3, mode="a"),
    ],
)
# Only polybot and discord bot loggers show INFO. Everything else (httpx, discord.client, websockets) is silent.
logger = logging.getLogger("polybot")
logger.setLevel(logging.INFO)
logging.getLogger("polybot.discord_bot.bot").setLevel(logging.INFO)


def _compute_pnl(pos: dict[str, Any], exit_price: float) -> tuple[float, float, float, float]:
    """Compute realistic PnL using shares-based entry fee and USDC exit fee.

    Returns (shares, entry_fee_usd, exit_fee_usd, pnl).
    """
    fee_rate = pos.get("fee_rate") or DEFAULT_FEE_RATE
    shares = pos.get("shares_held") or pos["size"] / pos["entry_price"]
    # Entry fee was already deducted in shares — express as USD for logging
    shares_ordered = pos["size"] / pos["entry_price"]
    entry_fee_in_shares = shares_ordered - shares
    entry_fee_usd = entry_fee_in_shares * pos["entry_price"]
    # Exit fee in USDC
    exit_fee_usd = exit_fee_usdc(shares, exit_price, fee_rate)
    # Revenue = sell shares at exit_price minus USDC fee
    revenue = shares * exit_price - exit_fee_usd
    pnl = revenue - pos["size"]  # size = USDC originally spent
    return shares, entry_fee_usd, exit_fee_usd, pnl


# Cache for _get_contract_prices — avoid hammering Gamma API every tick
_contract_price_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # market_id -> (timestamp, contract)
_CONTRACT_CACHE_TTL = 5.0  # seconds — re-fetch at most every 5s per contract
_CONTRACT_RESOLUTION_TTL = 2.0  # faster polling when contract might be resolving


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
        now_ts: int) -> tuple[str | None, int]:
    """Compute indicators/flow/signal, check for entry, size the trade, execute."""
    in_window = market_scanner.in_entry_window(contract["seconds_remaining"])

    # Compute indicators and evaluate probability model
    indicators = indicator_engine.compute_all(binance_feed.buffer)

    # Compute order flow signal from CLOB data
    trades_up = clob_ws.get_trade_history(token_up) if clob_ws else []
    trades_down = clob_ws.get_trade_history(token_down) if clob_ws else []
    flow_data = compute_flow_signal(book_up, book_down, trades_up, trades_down)
    flow_score = flow_data["flow_score"]

    # Get closes array for regime detection
    closes = binance_feed.buffer.get_closes()

    signal = signal_engine.evaluate(
        indicators, has_position=False, in_entry_window=in_window,
        btc_price=btc_price, strike_price=strike,
        seconds_remaining=contract["seconds_remaining"],
        market_price_up=price_up, market_price_down=price_down,
        closes=closes, flow_signal=flow_score,
    )

    # Log signal evaluation once per window so we can see what the model sees
    if eval_window != last_eval_log_window:
        last_eval_log_window = eval_window
        buf_len = len(binance_feed.buffer) if binance_feed.buffer else 0
        logger.info(
            f"EVAL: {signal.action} | {cid} | BTC={btc_price:,.0f} strike={strike:,.0f} "
            f"d={btc_price-strike:+,.0f} | mkt Up={price_up:.2f} Dn={price_down:.2f} "
            f"| prob={signal.prob:.0%} edge={signal.edge:+.0%} | {contract['seconds_remaining']:.0f}s left "
            f"| flow={flow_score:+.2f} | buf={buf_len} | {signal.reason}")

    if signal.action not in ("BUY_YES", "BUY_NO"):
        return None, last_eval_log_window

    side = "Up" if signal.action == "BUY_YES" else "Down"
    price = price_up if side == "Up" else price_down
    token_id = contract["token_id_up"] if side == "Up" else contract["token_id_down"]
    bankroll = await db.get_bankroll()
    kelly_mult = breaker.kelly_multiplier if breaker else 1.0
    size = round(bankroll * signal.kelly_size * kelly_mult, 2)
    if size < 1.0:
        logger.info(f"SKIP: Kelly size ${size:.2f} < $1 — edge too small to trade")
        return None, last_eval_log_window
    if size > bankroll * max_bankroll_pct:
        size = round(bankroll * max_bankroll_pct, 2)

    # Cap size to fraction of book depth (realistic fill constraint)
    side_depth = depth_usd_up if side == "Up" else depth_usd_down
    max_fill_pct = config.get("execution", {}).get("max_book_fill_pct", 0.50)
    if side_depth > 0:
        max_fill = side_depth * max_fill_pct
        if size > max_fill:
            size = round(max_fill, 2)
            if size < 1.0:
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

    # Fetch fee rate and tick size from Polymarket API
    fee_rate = await market_scanner.fetch_fee_rate(token_id, http_client)
    tick_size = await market_scanner.fetch_tick_size(token_id, http_client)

    # Execution price: /price endpoint gives the real negRisk cross-matched price
    exec_price = await market_scanner.fetch_market_price(token_id, "BUY", http_client)
    if exec_price > 0:
        impact = config.get("execution", {}).get("slippage_impact_pct", 0.03)
        slip = slippage_pct(size, side_depth, impact)
        price = market_scanner.snap_to_tick(exec_price * (1 + slip), tick_size)

    snapshot = indicator_engine.get_snapshot(indicators)
    snapshot["trade_context"] = {
        "btc_price": btc_price,
        "strike_price": strike,
        "seconds_remaining": contract["seconds_remaining"],
        "market_price_up": price_up,
        "market_price_down": price_down,
        "model_probability": signal.prob,
        "edge": signal.edge,
        "momentum_score": signal_engine.compute_momentum(indicators),
        "atr": indicators.get("atr", {}).get("atr", 0),
        "size": size,
        "flow_score": flow_score,
        "flow_book_imbalance": flow_data.get("book_imbalance", 0),
        "flow_trade_count": flow_data.get("trade_count", 0),
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
        logger.info(f"OPEN {side} @ {price:.3f} | ${size:.2f} | fee=${fee_usd:.2f} ({fee_rate:.1%}) | src={price_source}{last_trade_info} | {signal.reason}")
        if alert_manager:
            mkt_price = price_up if side == "Up" else price_down
            await alert_manager.send_trade_opened(
                question=contract["question"], side=side, size=size,
                entry_price=price, ev=signal.edge, exit_target=1.0,
                model_prob=signal.prob, market_price=mkt_price,
                fee=fee_usd, flow=flow_score)
        return cid, last_eval_log_window

    return None, last_eval_log_window


def _compute_strike_and_btc(cid: str, binance_feed: Any, window_strikes: dict[int, float],
                            eval_window: int,
                            last_eval_log_window: int) -> tuple[float | None, float | None, dict[int, float], int]:
    """Derive strike from slug, get current BTC price, check staleness."""
    now_ts = int(time.time())

    try:
        contract_window_ts = int(cid.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        contract_window_ts = int(now_ts // 300) * 300  # fallback
    if contract_window_ts not in window_strikes:
        # Find the candle closest to the 5-min window boundary
        candles = binance_feed.buffer.get_last_n(10)
        for c in candles:
            if abs(c.timestamp / 1000 - contract_window_ts) <= 60:
                window_strikes[contract_window_ts] = c.open
                break
    # Clean old strikes
    window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}

    strike = window_strikes.get(contract_window_ts, 0)
    if strike <= 0:
        buf_len = len(binance_feed.buffer) if binance_feed.buffer else 0
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL: no strike for window {contract_window_ts} — candle buffer has {buf_len} candles")
        return None, None, window_strikes, last_eval_log_window

    btc_price = binance_feed.buffer.latest().close if binance_feed.buffer.latest() else 0
    if btc_price <= 0:
        if eval_window != last_eval_log_window:
            last_eval_log_window = eval_window
            logger.info(f"EVAL: no BTC price — Binance feed not ready")
        return None, None, window_strikes, last_eval_log_window

    # Skip if candle data is stale (WebSocket may have disconnected)
    latest_candle_age = (time.time() * 1000 - binance_feed.buffer.latest().timestamp) / 1000
    if latest_candle_age > 180:
        logger.warning(f"Stale candle data: {latest_candle_age:.0f}s old, skipping entry")
        return None, None, window_strikes, last_eval_log_window

    return strike, btc_price, window_strikes, last_eval_log_window


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
                                           clob_ws: Any) -> tuple[dict[str, Any] | None, str | None, dict[str, int], list[str]]:
    """Find an active contract and subscribe its WebSocket tokens. Returns (contract, cid) or None."""
    contract = await market_scanner.find_active_contract()
    if not contract:
        return None, None, traded_contracts, ws_subscribed_tokens

    cid = contract["slug"]  # Use slug as market_id — US API needs marketSlug, not condition_id

    # Clean old entries
    now_ts = int(time.time())
    traded_contracts = {k: v for k, v in traded_contracts.items() if now_ts - v < 600}

    # One trade per contract
    if cid in traded_contracts:
        return None, None, traded_contracts, ws_subscribed_tokens

    # Subscribe WebSocket to this contract's tokens (idempotent)
    token_up = contract["token_id_up"]
    token_down = contract["token_id_down"]
    new_tokens = [t for t in [token_up, token_down] if t and t not in ws_subscribed_tokens]
    if new_tokens and clob_ws:
        await clob_ws.subscribe(new_tokens)
        ws_subscribed_tokens.extend(new_tokens)

    return contract, cid, traded_contracts, ws_subscribed_tokens


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
        day_wins: int, day_losses: int, day_fees: float) -> tuple[int, int, float, str | None]:
    """Re-evaluate an active position and exit (scalp) if holding edge is gone."""
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

    action, model_prob, holding_edge, reason = signal_engine.evaluate_hold(
        indicators, btc_now, strike_now, live["seconds_remaining"],
        market_price, pos["side"], exit_threshold,
        entry_price=pos["entry_price"],
        fee_rate=pos.get("fee_rate") or DEFAULT_FEE_RATE,
        closes=closes, flow_signal=hold_flow["flow_score"])

    if action == "HOLD" and counterfactual_tracker:
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
            shares, entry_fee, exit_fee, pnl = _compute_pnl(pos, exit_fill)
            gain_pct = pnl / pos["size"] if pos["size"] > 0 else 0
            won = "WIN" if pnl > 0 else "LOSS"
            if pnl > 0: day_wins += 1
            else: day_losses += 1
            day_fees += entry_fee + exit_fee
            logger.info(f"SCALP {won} {pos['side']} | {pos['entry_price']:.3f}->{exit_fill:.3f} | {gain_pct:+.1%} | ${pnl:+.2f} | fees=${entry_fee + exit_fee:.2f} | slip={slip:.2%} | {reason}")
            if breaker:
                breaker.update_bankroll(await db.get_bankroll())
                cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
                if cb_event and alert_manager:
                    await alert_manager.send_circuit_breaker(cb_event, breaker)
            if alert_manager:
                await alert_manager.send_trade_closed(
                    question="", exit_price=exit_fill, log_return=0, hold_hours=0,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                    gain_pct=gain_pct, reason=f"scalp {won.lower()}", fees=entry_fee + exit_fee)
            await _record_outcome(outcome_reviewer, pos, exit_fill, result.log_return or 0, gain_pct,
                                  exit_reason="scalp", pnl=pnl, fees=entry_fee + exit_fee)
            traded_market_id = pos["market_id"]
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
        return False, day_wins, day_losses, day_fees, None

    result = await trader.resolve_position(pos["id"], exit_price)
    traded_market_id = None
    if result.success:
        shares, entry_fee, exit_fee, pnl = _compute_pnl(pos, exit_price)
        gain_pct = pnl / pos["size"] if pos["size"] > 0 else 0
        won = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0: day_wins += 1
        else: day_losses += 1
        day_fees += entry_fee + exit_fee
        logger.info(f"RESOLVED {won} {pos['side']} | {pos['entry_price']:.3f}->{exit_price:.3f} | {gain_pct:+.1%} | ${pnl:+.2f} | fees=${entry_fee + exit_fee:.2f}")
        if breaker:
            breaker.update_bankroll(await db.get_bankroll())
            cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
            if cb_event and alert_manager:
                await alert_manager.send_circuit_breaker(cb_event, breaker)
        if alert_manager:
            await alert_manager.send_trade_closed(
                question="", exit_price=exit_price, log_return=0, hold_hours=0,
                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                gain_pct=gain_pct, reason=won.lower(), fees=entry_fee + exit_fee)
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=entry_fee + exit_fee)
        if counterfactual_tracker:
            cf_hold = counterfactual_tracker.record_hold_resolution(
                pos["market_id"], exit_price, pnl, gain_pct)
            if cf_hold:
                verdict = "CORRECT" if cf_hold["hold_was_optimal"] else "MISSED scalp +${:.2f}".format(-cf_hold["delta_pnl"])
                logger.info(f"COUNTERFACTUAL HOLD resolved: {pos['side']} {pos['market_id']} — {verdict}")
        traded_market_id = pos["market_id"]
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
                    question="", exit_price=0, log_return=0, hold_hours=age / 3600,
                    side=pos["side"], entry_price=pos["entry_price"], pnl=0,
                    gain_pct=0, reason="orphaned — awaiting resolution", fees=0)
        else:
            logger.info(f"Orphan {pos['market_id']} age={age:.0f}s — waiting for Gamma resolution data")
        return True, day_wins, day_losses, day_fees, None  # still waiting
    result = await trader.resolve_position(pos["id"], exit_price)
    traded_market_id = None
    if result.success:
        shares, entry_fee, exit_fee, pnl = _compute_pnl(pos, exit_price)
        gain_pct = pnl / pos["size"] if pos["size"] > 0 else 0
        won = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0: day_wins += 1
        else: day_losses += 1
        day_fees += entry_fee + exit_fee
        logger.info(f"RESOLVED {won} {pos['side']} (orphaned) | {pos['entry_price']:.3f}->{exit_price:.3f} | {gain_pct:+.1%} | ${pnl:+.2f}")
        if breaker:
            breaker.update_bankroll(await db.get_bankroll())
            cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
            if cb_event and alert_manager:
                await alert_manager.send_circuit_breaker(cb_event, breaker)
        if alert_manager:
            await alert_manager.send_trade_closed(
                question="", exit_price=exit_price, log_return=0, hold_hours=0,
                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                gain_pct=gain_pct, reason=won.lower(), fees=entry_fee + exit_fee)
        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                              exit_reason="resolution", pnl=pnl, fees=entry_fee + exit_fee)
        traded_market_id = pos["market_id"]
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
        day_wins = 0
        day_losses = 0
        day_fees = 0.0
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
                       counterfactual_tracker: Any = None) -> None:
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
    sched_start_et = (sched.get("trading_start_hour_et", 8), sched.get("trading_start_minute", 0))
    sched_end_et = (sched.get("trading_end_hour_et", 16), sched.get("trading_end_minute", 30))

    traded_contracts: dict[str, int] = {}      # condition_id -> timestamp (one trade per contract)
    window_strikes: dict[int, float] = {}      # window_ts -> BTC price at window open
    ws_subscribed_tokens: list[str] = []       # currently subscribed token_ids
    last_eval_log_window: int = 0              # track which window we last logged eval for

    # Shared HTTP client — one connection pool for all API calls
    http_client = httpx.AsyncClient(timeout=5)

    # Day tracking for open/close banners
    current_trading_day: str | None = None
    day_open_bankroll: float = 0.0
    day_wins: int = 0
    day_losses: int = 0
    day_fees: float = 0.0

    while True:
        # Event-driven: react instantly to WebSocket book updates, timeout 1s for housekeeping
        if clob_ws:
            try:
                await asyncio.wait_for(clob_ws.book_updated.wait(), timeout=1.0)
                clob_ws.book_updated.clear()
            except asyncio.TimeoutError:
                pass  # housekeeping tick — contract discovery, day banners
        else:
            await asyncio.sleep(0.25)  # fallback polling if no WebSocket
        try:
            if is_paused_fn():
                await asyncio.sleep(0.5)
                continue

            # --- DAY OPEN / CLOSE ---
            now_et = datetime.now(ET)
            in_trading_hours, current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees = \
                await _check_trading_schedule(
                    now_et, scheduler, sched_start_et, sched_end_et,
                    current_trading_day, day_open_bankroll, day_wins, day_losses, day_fees,
                    alert_manager, db, config, breaker)

            # --- POSITION MANAGEMENT: resolution check + active re-evaluation ---
            positions = await db.get_open_positions()
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
                    day_wins, day_losses, day_fees, traded_mid = \
                        await _evaluate_and_exit_position(
                            pos, live, binance_feed, indicator_engine,
                            signal_engine, market_scanner, http_client,
                            clob_ws, trader, alert_manager, db,
                            outcome_reviewer, breaker, counterfactual_tracker,
                            config, scheduler, default_exit_threshold,
                            day_wins, day_losses, day_fees)
                    if traded_mid:
                        traded_contracts[traded_mid] = int(time.time())

            # --- COUNTERFACTUAL: check watched scalps for resolution ---
            if counterfactual_tracker:
                await _check_counterfactuals(counterfactual_tracker, market_scanner,
                                             http_client, binance_feed)

            # --- ENTRY: find contract and evaluate for edge ---
            # Skip new entries outside trading hours (positions still managed above)
            if not in_trading_hours:
                continue

            # Skip if we already have an open position (one at a time)
            if await db.get_open_position_count() > 0:
                continue

            contract, cid, traded_contracts, ws_subscribed_tokens = \
                await _discover_contract_and_subscribe(
                    market_scanner, traded_contracts, ws_subscribed_tokens, clob_ws)
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

            strike, btc_price, window_strikes, last_eval_log_window = \
                _compute_strike_and_btc(cid, binance_feed, window_strikes,
                                        eval_window, last_eval_log_window)
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
                now_ts)
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
    return parser.parse_args()


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
    signal_engine = SignalEngine(
        min_edge=signal_cfg.get("entry_threshold", 0.20),
        kelly_fraction=config["math"].get("kelly_fraction", 0.15),
        momentum_weight=signal_cfg.get("momentum_weight", 0.04),
        weights=signal_cfg.get("weights", {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                            "obv": 0.15, "vwap": 0.20}),
        min_model_probability=signal_cfg.get("min_model_probability", 0.65),
        student_t_df=signal_cfg.get("student_t_df", 4),
        regime_weight=signal_cfg.get("regime_weight", 0.05),
        flow_weight=signal_cfg.get("flow_weight", 0.06),
        regime_lookback=signal_cfg.get("regime_lookback", 20),
    )

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
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
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
        max_drawdown_pct=cb_cfg.get("max_drawdown_pct", 0.15),
        min_multiplier=cb_cfg.get("min_multiplier", 0.25),
        losses_to_reduce=cb_cfg.get("losses_to_reduce", 3),
        wins_to_restore=cb_cfg.get("wins_to_restore", 2),
    )

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
    discord_bot.scheduler = scheduler
    if mode == "live":
        # Sync DB bankroll with real Polymarket balance (fetched during preflight)
        await db.set_bankroll(live_balance)

    # CLOB WebSocket — real-time order book feed
    clob_ws_url = market_cfg.get("clob_ws_url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    clob_ws = ClobWebSocket(url=clob_ws_url)
    await clob_ws.start()

    await scheduler.start()
    await binance_feed.start()

    async def run_discord():
        try:
            await discord_bot.start(get_secret("DISCORD_BOT_TOKEN"))
        except Exception as e:
            logger.error(f"Discord bot error: {e}")

    tasks = [
        asyncio.create_task(trading_loop(
            binance_feed, market_scanner, indicator_engine, signal_engine,
            trader, alert_manager, db, config, outcome_reviewer,
            is_paused_fn=lambda: discord_bot.is_paused,
            scheduler=scheduler, clob_ws=clob_ws, breaker=breaker,
            counterfactual_tracker=counterfactual_tracker)),
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(run_discord()),
    ]
    logger.info("PolyBot started — all systems running (WebSocket + event-driven)")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        await clob_ws.close()
        await scheduler.stop()
        await binance_feed.stop()
        await db.close()
        await discord_bot.close()


if __name__ == "__main__":
    asyncio.run(main())
