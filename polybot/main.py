# polybot/main.py
import asyncio
import json
import logging
import logging.handlers
from pathlib import Path

from polybot.config.loader import load_config, get_secret
from polybot.db.models import Database
from polybot.core.binance_feed import BinanceFeed
from polybot.core.market_scanner import BTCMarketScanner
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine
from polybot.math_engine.decision_table import DecisionTable
from polybot.brain.claude_client import ClaudeClient
from polybot.execution.paper_trader import PaperTrader
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
        logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=0, mode="w"),
    ],
)
# Only polybot and discord bot loggers show INFO. Everything else (httpx, discord.client, websockets) is silent.
logger = logging.getLogger("polybot")
logger.setLevel(logging.INFO)
logging.getLogger("polybot.discord_bot.bot").setLevel(logging.INFO)


async def _get_contract_prices(market_scanner, condition_id: str) -> dict | None:
    """Fetch current Up/Down prices for an active contract via Gamma API."""
    try:
        import httpx
        # Re-fetch the event to get latest prices
        async with httpx.AsyncClient(timeout=5) as client:
            # Use the cached slug or reconstruct
            import time
            window_ts = int(time.time() // 300) * 300
            for ts in [window_ts, window_ts + 300, window_ts - 300]:
                slug = market_scanner._make_slug(ts)
                resp = await client.get(f"{market_scanner.GAMMA_API}/events",
                                        params={"slug": slug})
                data = resp.json()
                if data:
                    event = data[0] if isinstance(data, list) else data
                    contract = market_scanner.parse_contract(event)
                    if contract and contract["condition_id"] == condition_id:
                        return contract
    except Exception:
        pass
    return None


async def trading_loop(binance_feed, market_scanner, indicator_engine, signal_engine,
                       decision_table, trader, alert_manager, db, config, outcome_reviewer, is_paused_fn):
    math_config = config["math"]
    signal_config = config["signal"]
    scalp_config = config.get("scalping", {})
    take_profit_pct = scalp_config.get("take_profit_pct", 0.10)
    stop_loss_pct = scalp_config.get("stop_loss_pct", 0.08)

    while True:
        try:
            if is_paused_fn():
                await asyncio.sleep(1)
                continue

            # --- SCALP EXIT CHECK: monitor open positions for profit/loss ---
            positions = await db.get_open_positions()
            for pos in positions:
                live = await _get_contract_prices(market_scanner, pos["condition_id"])
                if not live:
                    continue

                side = pos["side"]
                entry_price = pos["entry_price"]
                if side == "Up":
                    current_price = live["price_up"]
                else:
                    current_price = live["price_down"]

                gain_pct = (current_price - entry_price) / entry_price

                if gain_pct >= take_profit_pct:
                    # Take profit
                    result = await trader.close_trade(pos["id"], current_price)
                    if result.success:
                        logger.info(f"SCALP TAKE PROFIT: {pos['question'][:50]} | "
                                    f"entry={entry_price:.3f} exit={current_price:.3f} "
                                    f"gain={gain_pct:.1%}")
                        if alert_manager:
                            await alert_manager.send_trade_closed(
                                question=pos["question"], exit_price=current_price,
                                log_return=result.log_return or 0, hold_hours=0)
                        # Record outcome for learning pipeline
                        outcome_reviewer.record_outcome(
                            position_id=pos["id"],
                            market_id=pos["condition_id"],
                            question=pos["question"],
                            side=pos["side"],
                            predicted_probability=abs(pos["signal_score"]),
                            actual_outcome=gain_pct > 0,
                            entry_price=entry_price,
                            exit_price=current_price,
                            log_return=result.log_return or 0,
                            prompt_version=pos.get("weight_version", ""),
                            category="crypto-5min",
                            indicator_snapshot=json.loads(pos.get("indicator_snapshot", "{}"))
                        )

                elif gain_pct <= -stop_loss_pct:
                    # Stop loss
                    result = await trader.close_trade(pos["id"], current_price)
                    if result.success:
                        logger.info(f"SCALP STOP LOSS: {pos['question'][:50]} | "
                                    f"entry={entry_price:.3f} exit={current_price:.3f} "
                                    f"loss={gain_pct:.1%}")
                        if alert_manager:
                            await alert_manager.send_trade_closed(
                                question=pos["question"], exit_price=current_price,
                                log_return=result.log_return or 0, hold_hours=0)
                        # Record outcome for learning pipeline
                        outcome_reviewer.record_outcome(
                            position_id=pos["id"],
                            market_id=pos["condition_id"],
                            question=pos["question"],
                            side=pos["side"],
                            predicted_probability=abs(pos["signal_score"]),
                            actual_outcome=gain_pct > 0,
                            entry_price=entry_price,
                            exit_price=current_price,
                            log_return=result.log_return or 0,
                            prompt_version=pos.get("weight_version", ""),
                            category="crypto-5min",
                            indicator_snapshot=json.loads(pos.get("indicator_snapshot", "{}"))
                        )

            # --- ENTRY: find contract and evaluate signal ---
            contract = await market_scanner.find_active_contract()
            if not contract:
                await asyncio.sleep(1)
                continue

            in_window = market_scanner.in_entry_window(contract["seconds_remaining"])
            has_position = await db.has_position_for_market(contract["condition_id"])

            # Compute indicators from Binance candle buffer
            indicators = indicator_engine.compute_all(binance_feed.buffer)
            signal = signal_engine.evaluate(indicators, has_position, in_window)

            logger.debug(
                f"Signal: {signal.action} score={signal.score:.3f} "
                f"contract={contract['question'][:50]} "
                f"in_window={in_window} has_pos={has_position}"
            )

            if signal.action in ("BUY_YES", "BUY_NO"):
                side = "Up" if signal.action == "BUY_YES" else "Down"
                price = contract["price_up"] if side == "Up" else contract["price_down"]
                bankroll = await db.get_bankroll()
                size = decision_table.position_size(abs(signal.score), price, bankroll)
                if size < 1.0:
                    await asyncio.sleep(1)
                    continue

                snapshot_str = json.dumps(indicator_engine.get_snapshot(indicators))
                result = await trader.open_trade(
                    market_id=contract["condition_id"],
                    question=contract["question"],
                    side=side,
                    price=price,
                    size=size,
                    signal_score=abs(signal.score),
                    signal_strength="high",
                    ev_at_entry=signal.score,
                    exit_target=math_config["exit_target"],
                    stop_loss=price * (1 - math_config["stop_loss_pct"]),
                    weight_version=signal_config.get("active_weights_version", "weights_v001"),
                    indicator_snapshot=snapshot_str,
                )

                if result.success and alert_manager:
                    await alert_manager.send_trade_opened(
                        question=contract["question"], side=side, size=size,
                        entry_price=price, ev=signal.score,
                        exit_target=math_config["exit_target"])

        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            if alert_manager:
                await alert_manager.send_error(str(e))

        await asyncio.sleep(1)  # 1-second decision cycle


async def main():
    config = load_config()
    base_dir = Path(__file__).parent

    # Database
    db = Database(config["database"]["path"])
    await db.initialize()
    if await db.get_bankroll() == 0:
        await db.set_bankroll(config["execution"]["initial_bankroll"])

    # Math
    math_cfg = config["math"]
    decision_table = DecisionTable(ev_threshold=math_cfg["ev_threshold"],
        kelly_fraction=math_cfg["kelly_fraction"], entry_discount=math_cfg["entry_discount"],
        exit_target=math_cfg["exit_target"], stop_loss_pct=math_cfg["stop_loss_pct"])
    decision_table.build()

    # Binance feed
    binance_cfg = config.get("binance", {})
    binance_feed = BinanceFeed(
        symbol=binance_cfg.get("symbol", "btcusdt"),
        buffer_size=binance_cfg.get("candle_buffer_size", 200),
        ws_url=binance_cfg.get("ws_url", "wss://stream.binance.com:9443/ws"),
        rest_url=binance_cfg.get("rest_url", "https://api.binance.com/api/v3"),
    )

    # BTC market scanner
    market_cfg = config.get("market", {})
    market_scanner = BTCMarketScanner(
        entry_window_seconds=market_cfg.get("entry_window_seconds", 120),
        min_time_remaining=market_cfg.get("min_time_remaining_seconds", 30),
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
                "low_pct": ind_cfg.get("atr", {}).get("low_percentile", 25),
                "high_pct": ind_cfg.get("atr", {}).get("high_percentile", 90),
                "history": ind_cfg.get("atr", {}).get("history_periods", 100)},
    }
    indicator_engine = IndicatorEngine(
        weights_dir=weights_dir,
        active_version=signal_cfg.get("active_weights_version", "weights_v001"),
        params=indicator_params,
    )

    # Signal engine
    signal_engine = SignalEngine(
        entry_threshold=signal_cfg.get("entry_threshold", 0.60),
        weights=signal_cfg.get("weights", {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                            "obv": 0.15, "vwap": 0.20}),
    )

    # Brain (Claude client kept for TA evolver analysis calls)
    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model="claude-sonnet-4-6")

    # Execution
    exec_cfg = config["execution"]
    trader = PaperTrader(db=db, max_slippage=exec_cfg["max_slippage"],
        max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
        max_concurrent_positions=exec_cfg["max_concurrent_positions"])

    # Agents
    agents_cfg = config["agents"]
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    ta_evolver = TAEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"))
    weight_optimizer = WeightOptimizer(
        weights_dir=weights_dir,
        scores_path=str(base_dir / "memory" / "weight_scores.json"),
        min_improvement=agents_cfg.get("prompt_optimizer_min_improvement", 0.03),
    )
    scheduler = AgentScheduler(
        outcome_reviewer=outcome_reviewer,
        bias_detector=bias_detector,
        ta_evolver=ta_evolver,
        weight_optimizer=weight_optimizer,
        outcome_interval_seconds=agents_cfg["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=agents_cfg["daily_pipeline_hour"],
        math_config=math_cfg,
    )

    # Discord
    discord_bot = create_bot(db, trader, market_scanner, scheduler, config)
    alert_manager = AlertManager(bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"])

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
            decision_table, trader, alert_manager, db, config, outcome_reviewer,
            is_paused_fn=lambda: discord_bot.is_paused)),
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(run_discord()),
    ]
    logger.info("PolyBot started — all systems running (1-second TA decision loop)")

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
