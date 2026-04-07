# polybot/main.py
import argparse
import asyncio
import json
import logging
import logging.handlers
import time
from pathlib import Path

from polybot.config.loader import load_config, get_secret
from polybot.execution.paper_trader import _taker_fee
from polybot.db.models import Database
from polybot.core.binance_feed import BinanceFeed
from polybot.core.market_scanner import BTCMarketScanner
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine
from polybot.brain.claude_client import ClaudeClient
from polybot.execution.paper_trader import PaperTrader
from polybot.execution.live_trader import LiveTrader
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.bias_detector import BiasDetector
from polybot.agents.ta_evolver import TAEvolver
from polybot.agents.weight_optimizer import WeightOptimizer
from polybot.agents.scheduler import AgentScheduler
from polybot.discord_bot.bot import create_bot
from polybot.discord_bot.alerts import AlertManager

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


async def _get_contract_prices(market_scanner, market_id: str, http_client=None) -> dict | None:
    """Fetch current Up/Down prices for an active contract via Gamma API."""
    import httpx

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
                    return contract
        except httpx.TimeoutException:
            continue
        except Exception as e:
            logger.warning(f"Price fetch error for {slug}: {e}")
            continue
    return None


async def _record_outcome(outcome_reviewer, pos, exit_price, log_return, gain_pct,
                          exit_reason="resolution"):
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
        )
    except Exception as e:
        logger.error(f"Failed to record outcome: {e}")


async def trading_loop(binance_feed, market_scanner, indicator_engine, signal_engine,
                       trader, alert_manager, db, config, outcome_reviewer, is_paused_fn,
                       scheduler=None):
    import httpx
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    signal_config = config["signal"]
    max_bankroll_pct = config["execution"]["max_bankroll_deployed"]
    default_exit_threshold = signal_config.get("exit_edge_threshold", -0.10)

    # Trading schedule in ET (handles EST/EDT automatically)
    sched = config.get("schedule", {})
    sched_start_et = (sched.get("trading_start_hour_et", 8), sched.get("trading_start_minute", 0))
    sched_end_et = (sched.get("trading_end_hour_et", 16), sched.get("trading_end_minute", 30))

    traded_contracts: dict[str, int] = {}      # condition_id -> timestamp (one trade per contract)
    window_strikes: dict[int, float] = {}      # window_ts -> BTC price at window open

    # Shared HTTP client — one connection pool for all Gamma API calls
    http_client = httpx.AsyncClient(timeout=5)

    # Day tracking for open/close banners
    current_trading_day: str | None = None
    day_open_bankroll: float = 0.0
    day_wins: int = 0
    day_losses: int = 0

    while True:
        await asyncio.sleep(0.25)  # 250ms tick — fast detection, ~4 req/sec to Gamma API
        try:
            if is_paused_fn():
                await asyncio.sleep(0.5)
                continue

            # --- DAY OPEN / CLOSE ---
            now_et = datetime.now(ET)
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
                    await alert_manager.send_day_close(bankroll, day_pnl, day_wins, day_losses)
                current_trading_day = today_str
                day_open_bankroll = await db.get_bankroll()
                day_wins = 0
                day_losses = 0
                if alert_manager:
                    await alert_manager.send_day_open(config.get("mode", "paper"), day_open_bankroll)

            if not in_trading_hours and current_trading_day is not None:
                # Trading hours ended — send day close banner
                if alert_manager:
                    bankroll = await db.get_bankroll()
                    day_pnl = bankroll - day_open_bankroll
                    await alert_manager.send_day_close(bankroll, day_pnl, day_wins, day_losses)
                current_trading_day = None

            # --- POSITION MANAGEMENT: resolution check + active re-evaluation ---
            positions = await db.get_open_positions()
            for pos in positions:
                live = await _get_contract_prices(market_scanner, pos["market_id"], http_client)
                if not live:
                    continue

                if live["seconds_remaining"] <= 0:
                    # Contract expired — check if Polymarket has resolved it.
                    # Resolved contracts show outcomePrices ["1","0"] or ["0","1"].
                    # This is the actual on-chain Chainlink resolution — no guessing.
                    if live.get("closed") and (live["price_up"] >= 0.99 or live["price_up"] <= 0.01):
                        # Polymarket has resolved: use the actual outcome prices
                        exit_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]
                    else:
                        # Not resolved yet — wait before re-checking (avoid hammering Gamma API)
                        await asyncio.sleep(5)
                        continue

                    result = await trader.resolve_position(pos["id"], exit_price)
                    if result.success:
                        shares = pos["size"] / pos["entry_price"]
                        entry_fee = _taker_fee(shares, pos["entry_price"])
                        exit_fee = _taker_fee(shares, exit_price)
                        pnl = shares * exit_price - pos["size"] - entry_fee - exit_fee
                        gain_pct = pnl / pos["size"] if pos["size"] > 0 else 0
                        won = "WIN" if pnl > 0 else "LOSS"
                        if pnl > 0: day_wins += 1
                        else: day_losses += 1
                        logger.info(f"RESOLVED {won} {pos['side']} | {pos['entry_price']:.3f}->{exit_price:.3f} | {gain_pct:+.1%} | ${pnl:+.2f} | fees=${entry_fee + exit_fee:.2f}")
                        if alert_manager:
                            await alert_manager.send_trade_closed(
                                question="", exit_price=exit_price, log_return=0, hold_hours=0,
                                side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                                gain_pct=gain_pct, reason=won.lower())
                        await _record_outcome(outcome_reviewer, pos, exit_price, result.log_return or 0, gain_pct,
                                              exit_reason="resolution")
                        traded_contracts[pos["market_id"]] = int(time.time())
                else:
                    # Active position — re-evaluate using probability model
                    btc_now = binance_feed.buffer.latest().close if binance_feed.buffer.latest() else 0
                    if btc_now <= 0:
                        continue
                    # Don't make exit decisions on stale data — hold until fresh
                    candle_age = (time.time() * 1000 - binance_feed.buffer.latest().timestamp) / 1000
                    if candle_age > 180:
                        continue

                    # Get strike from the position's stored trade_context (correct for this contract)
                    pos_ctx = json.loads(pos.get("indicator_snapshot", "{}")).get("trade_context", {})
                    strike_now = pos_ctx.get("strike_price", 0)
                    if strike_now <= 0:
                        continue

                    indicators = indicator_engine.compute_all(binance_feed.buffer)
                    market_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]

                    exit_threshold = (scheduler._exit_edge_threshold if scheduler and scheduler._exit_edge_threshold is not None
                                      else default_exit_threshold)
                    action, model_prob, holding_edge, reason = signal_engine.evaluate_hold(
                        indicators, btc_now, strike_now, live["seconds_remaining"],
                        market_price, pos["side"], exit_threshold)

                    if action == "EXIT":
                        sell_token = live.get("token_id_up", "") if pos["side"] == "Up" else live.get("token_id_down", "")
                        result = await trader.close_trade(pos["id"], market_price, token_id=sell_token)
                        if result.success:
                            shares = pos["size"] / pos["entry_price"]
                            entry_fee = _taker_fee(shares, pos["entry_price"])
                            exit_fee = _taker_fee(shares, market_price)
                            pnl = shares * market_price - pos["size"] - entry_fee - exit_fee
                            gain_pct = pnl / pos["size"] if pos["size"] > 0 else 0
                            won = "WIN" if pnl > 0 else "LOSS"
                            if pnl > 0: day_wins += 1
                            else: day_losses += 1
                            logger.info(f"SCALP {won} {pos['side']} | {pos['entry_price']:.3f}->{market_price:.3f} | {gain_pct:+.1%} | ${pnl:+.2f} | fees=${entry_fee + exit_fee:.2f} | {reason}")
                            if alert_manager:
                                await alert_manager.send_trade_closed(
                                    question="", exit_price=market_price, log_return=0, hold_hours=0,
                                    side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                                    gain_pct=gain_pct, reason=f"scalp {won.lower()}")
                            await _record_outcome(outcome_reviewer, pos, market_price, result.log_return or 0, gain_pct,
                                                  exit_reason="scalp")
                            traded_contracts[pos["market_id"]] = int(time.time())

            # --- ENTRY: find contract and evaluate for edge ---
            # Skip new entries outside trading hours (positions still managed above)
            if not in_trading_hours:
                continue

            # Skip if we already have an open position (one at a time)
            if await db.get_open_position_count() > 0:
                continue

            contract = await market_scanner.find_active_contract()
            if not contract:
                await asyncio.sleep(1)
                continue

            cid = contract["slug"]  # Use slug as market_id — US API needs marketSlug, not condition_id

            # Clean old entries
            now_ts = int(time.time())
            traded_contracts = {k: v for k, v in traded_contracts.items() if now_ts - v < 600}

            # One trade per contract
            if cid in traded_contracts:
                continue

            # Don't enter if market already decided (extreme prices)
            price_up = contract["price_up"]
            price_down = contract["price_down"]
            if price_up < 0.15 or price_up > 0.85:
                continue

            # Compute strike: BTC price at the 5-min window boundary
            # Use the candle that opened at the window start
            window_ts = int(now_ts // 300) * 300
            if window_ts not in window_strikes:
                # Find the candle closest to the 5-min window boundary
                candles = binance_feed.buffer.get_last_n(10)
                for c in candles:
                    if abs(c.timestamp / 1000 - window_ts) < 60:
                        window_strikes[window_ts] = c.open
                        break
                # No fallback — if we can't find the window-open candle, skip this window.
                # Using current price as strike creates distance=0 and false edge.
            # Clean old strikes
            window_strikes = {k: v for k, v in window_strikes.items() if now_ts - k < 600}

            strike = window_strikes.get(window_ts, 0)
            if strike <= 0:
                continue

            btc_price = binance_feed.buffer.latest().close if binance_feed.buffer.latest() else 0
            if btc_price <= 0:
                continue

            # Skip if candle data is stale (WebSocket may have disconnected)
            # Threshold 180s: candle timestamps are open-time, so a normal 1-min candle
            # can be up to ~120s old by design. 180s catches real outages without false triggers.
            latest_candle_age = (time.time() * 1000 - binance_feed.buffer.latest().timestamp) / 1000
            if latest_candle_age > 180:
                logger.warning(f"Stale candle data: {latest_candle_age:.0f}s old, skipping entry")
                continue

            in_window = market_scanner.in_entry_window(contract["seconds_remaining"])

            # Compute indicators and evaluate probability model
            indicators = indicator_engine.compute_all(binance_feed.buffer)
            signal = signal_engine.evaluate(
                indicators, has_position=False, in_entry_window=in_window,
                btc_price=btc_price, strike_price=strike,
                seconds_remaining=contract["seconds_remaining"],
                market_price_up=price_up, market_price_down=price_down,
            )

            if signal.action in ("BUY_YES", "BUY_NO"):
                side = "Up" if signal.action == "BUY_YES" else "Down"
                price = price_up if side == "Up" else price_down
                bankroll = await db.get_bankroll()
                size = round(bankroll * signal.kelly_size, 2)
                if size < 1.0:
                    continue  # Kelly says don't trade — respect it
                if size > bankroll * max_bankroll_pct:
                    size = round(bankroll * max_bankroll_pct, 2)

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
                }
                snapshot_str = json.dumps(snapshot)
                token_id = contract["token_id_up"] if side == "Up" else contract["token_id_down"]
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
                )

                if result.success:
                    traded_contracts[cid] = now_ts
                    entry_fee = _taker_fee(size / price, price)
                    logger.info(f"OPEN {side} @ {price:.3f} | ${size:.2f} | fee=${entry_fee:.2f} | {signal.reason}")
                    if alert_manager:
                        await alert_manager.send_trade_opened(
                            question=contract["question"], side=side, size=size,
                            entry_price=price, ev=signal.edge, exit_target=1.0)

        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            if alert_manager:
                await alert_manager.send_error(str(e))


def parse_args():
    parser = argparse.ArgumentParser(description="PolyBot — 5-min BTC Up/Down trader")
    parser.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Trading mode (overrides settings.yaml)")
    return parser.parse_args()


async def main():
    args = parse_args()
    config = load_config()
    mode = args.mode or config.get("mode", "paper")
    config["mode"] = mode
    base_dir = Path(__file__).parent

    # Database — persistent across sessions (both paper and live)
    db = Database(config["database"]["path"])
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
        momentum_weight=signal_cfg.get("momentum_weight", 0.08),
        weights=signal_cfg.get("weights", {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                            "obv": 0.15, "vwap": 0.20}),
        min_model_probability=signal_cfg.get("min_model_probability", 0.65),
    )

    # Brain (Claude client kept for TA evolver analysis calls)
    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model="claude-sonnet-4-6")

    # Execution — route based on mode
    exec_cfg = config["execution"]
    if mode == "live":
        from polybot.execution.polymarket_us import PolymarketUSClient
        us_client = PolymarketUSClient(
            api_key=get_secret("POLYMARKET_API_KEY"),
            secret_key=get_secret("POLYMARKET_SECRET"),
        )
        trader = LiveTrader(db=db, us_client=us_client,
            max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
        # Verify API credentials before trading real money
        logger.info("LIVE MODE — verifying Polymarket US API credentials...")
        try:
            live_balance = await trader.get_balance()
            logger.info(f"LIVE MODE — API connection: OK")
            logger.info(f"LIVE MODE — balance: ${live_balance:,.2f}")
            if live_balance > 0:
                await db.set_bankroll(live_balance)
            else:
                logger.warning("LIVE MODE — balance is $0.00. Fund your account before trading.")
        except Exception as e:
            logger.error(f"LIVE MODE — API verification FAILED: {e}")
            logger.error("LIVE MODE — cannot trade with invalid credentials. Exiting.")
            return
    else:
        trader = PaperTrader(db=db, max_slippage=exec_cfg["max_slippage"],
            max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
            max_concurrent_positions=exec_cfg["max_concurrent_positions"])
        logger.info("PAPER MODE — simulated trading")

    # Agents
    agents_cfg = config["agents"]
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
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
    )
    scheduler._exit_edge_threshold = signal_cfg.get("exit_edge_threshold", -0.10)
    scheduler._min_time_remaining = market_cfg.get("min_time_remaining_seconds", 20)
    discord_bot.scheduler = scheduler
    if mode == "live":
        live_balance = await trader.get_balance()
        await db.set_bankroll(live_balance)
        logger.info(f"USDC balance: ${live_balance:,.2f}")

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
            scheduler=scheduler)),
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(run_discord()),
    ]
    logger.info("PolyBot started — all systems running")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        await scheduler.stop()
        await binance_feed.stop()
        await db.close()
        await discord_bot.close()


if __name__ == "__main__":
    asyncio.run(main())
