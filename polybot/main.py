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
from polybot.core.websocket_monitor import ExitMonitor
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
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("polybot.log", maxBytes=5_000_000, backupCount=3),
    ],
)
logger = logging.getLogger("polybot")


async def trading_loop(binance_feed, market_scanner, indicator_engine, signal_engine,
                       decision_table, trader, exit_monitor, alert_manager, db, config, is_paused_fn):
    math_config = config["math"]
    signal_config = config["signal"]

    while True:
        try:
            if is_paused_fn():
                await asyncio.sleep(1)
                continue

            # Exit monitoring for open positions
            positions = await db.get_open_positions()
            if positions:
                async def get_price(market_id):
                    import httpx
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            f"{BTCMarketScanner.CLOB_BASE_URL}/markets/{market_id}")
                        data = resp.json()
                        for t in data.get("tokens", []):
                            if t.get("outcome", "").lower() == "yes":
                                return float(t.get("price", 0))
                    return 0.0

                async def on_exit(position_id, exit_price, reason):
                    result = await trader.close_trade(position_id, exit_price)
                    if result.success and alert_manager:
                        pos = next((p for p in positions if p["id"] == position_id), {})
                        await alert_manager.send_trade_closed(
                            question=pos.get("question", ""), exit_price=exit_price,
                            log_return=result.log_return or 0, hold_hours=0)

                await exit_monitor.monitor_positions(positions, get_price, on_exit)

            # Find active BTC 5-min contract
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
                side = "YES" if signal.action == "BUY_YES" else "NO"
                price = contract["price_yes"] if side == "YES" else contract["price_no"]
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
                    claude_probability=abs(signal.score),
                    claude_confidence="high",
                    ev_at_entry=signal.score,
                    exit_target=math_config["exit_target"],
                    stop_loss=price * (1 - math_config["stop_loss_pct"]),
                    prompt_version=signal_config.get("active_weights_version", "weights_v001"),
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
    indicator_engine = IndicatorEngine(
        weights_dir=weights_dir,
        active_version=signal_cfg.get("active_weights_version", "weights_v001"),
    )

    # Signal engine
    signal_engine = SignalEngine(
        entry_threshold=signal_cfg.get("entry_threshold", 0.60),
        weights=signal_cfg.get("weights", {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                            "obv": 0.15, "vwap": 0.20}),
    )

    # Exit monitor
    exit_monitor = ExitMonitor(time_stop_hours=math_cfg["time_stop_hours"],
                               time_stop_min_gain=math_cfg["time_stop_min_gain"])

    # Brain (Claude client kept for TA evolver analysis calls)
    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model=config["brain"]["model"])

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
            decision_table, trader, exit_monitor, alert_manager, db, config,
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
