# polybot/main.py
import asyncio
import logging
import logging.handlers
from pathlib import Path

from polybot.config.loader import load_config, get_secret
from polybot.db.models import Database
from polybot.core.filters import MarketFilter
from polybot.core.scanner import MarketScanner
from polybot.core.websocket_monitor import ExitMonitor
from polybot.math_engine.decision_table import DecisionTable
from polybot.brain.claude_client import ClaudeClient
from polybot.brain.prompt_builder import PromptBuilder
from polybot.execution.paper_trader import PaperTrader
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.bias_detector import BiasDetector
from polybot.agents.strategy_evolver import StrategyEvolver
from polybot.agents.prompt_optimizer import PromptOptimizer
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

async def trading_loop(scanner, claude, prompt_builder, decision_table, trader,
                       exit_monitor, alert_manager, db, config, is_paused_fn):
    brain_config = config["brain"]
    math_config = config["math"]
    scan_interval = config["scanner"]["interval_seconds"]

    while True:
        try:
            if is_paused_fn():
                await asyncio.sleep(10)
                continue

            # Exit monitoring for open positions
            positions = await db.get_open_positions()
            if positions:
                async def get_price(market_id):
                    import httpx
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(f"{scanner.CLOB_BASE_URL}/markets/{market_id}")
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

            # Scan for new opportunities
            markets = await scanner.fetch_and_filter()
            logger.info(f"Found {len(markets)} markets after filtering")

            for market in markets:
                if is_paused_fn():
                    break
                if await db.has_position_for_market(market["condition_id"]):
                    continue

                prompt = prompt_builder.build(version=brain_config["active_prompt_version"],
                                              category=market.get("category", ""))
                try:
                    analysis = await claude.analyze_market(
                        question=market["question"], price=market["price_yes"],
                        volume=market["volume_24h"], liquidity=market["liquidity"],
                        spread=market["spread"], days_to_expiry=market["days_to_expiry"],
                        prompt=prompt)
                except Exception as e:
                    logger.error(f"Claude analysis failed for {market['question'][:50]}: {e}")
                    continue

                if not analysis.passes_gate(min_confidence=brain_config["min_confidence"],
                                            min_probability=brain_config["min_probability"]):
                    continue

                if not decision_table.should_buy(analysis.probability, market["price_yes"]):
                    continue

                bankroll = await db.get_bankroll()
                size = decision_table.position_size(analysis.probability, market["price_yes"], bankroll)
                if size < 1.0:
                    continue

                decision = decision_table.lookup(analysis.probability)
                ev = decision_table.calculate_ev(analysis.probability, market["price_yes"])

                result = await trader.open_trade(
                    market_id=market["condition_id"], question=market["question"], side="YES",
                    price=market["price_yes"], size=size, claude_probability=analysis.probability,
                    claude_confidence=analysis.confidence, ev_at_entry=ev,
                    exit_target=decision["exit_price"],
                    stop_loss=market["price_yes"] * (1 - math_config["stop_loss_pct"]),
                    prompt_version=brain_config["active_prompt_version"])

                if result.success and alert_manager:
                    await alert_manager.send_trade_opened(
                        question=market["question"], side="YES", size=size,
                        entry_price=market["price_yes"], ev=ev, exit_target=decision["exit_price"])

        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            if alert_manager:
                await alert_manager.send_error(str(e))

        await asyncio.sleep(scan_interval)

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

    # Core
    filter_cfg = config["filters"]
    market_filter = MarketFilter(min_volume_24h=filter_cfg["min_volume_24h"],
        min_liquidity=filter_cfg["min_liquidity"], min_days_to_expiry=filter_cfg["min_days_to_expiry"],
        max_days_to_expiry=filter_cfg["max_days_to_expiry"], max_spread=filter_cfg["max_spread"],
        category_whitelist=filter_cfg["category_whitelist"], category_blacklist=filter_cfg["category_blacklist"])
    scanner = MarketScanner(filter=market_filter, max_markets=config["scanner"]["max_markets_per_cycle"])
    exit_monitor = ExitMonitor(time_stop_hours=math_cfg["time_stop_hours"],
                               time_stop_min_gain=math_cfg["time_stop_min_gain"])

    # Brain
    claude = ClaudeClient(api_key=get_secret("ANTHROPIC_API_KEY"), model=config["brain"]["model"])
    prompt_builder = PromptBuilder(prompts_dir=str(base_dir / "brain" / "prompts"),
        biases_path=str(base_dir / "memory" / "biases.json"),
        lessons_path=str(base_dir / "memory" / "lessons.json"))

    # Execution
    exec_cfg = config["execution"]
    trader = PaperTrader(db=db, max_slippage=exec_cfg["max_slippage"],
        max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
        max_concurrent_positions=exec_cfg["max_concurrent_positions"])

    # Agents
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    strategy_evolver = StrategyEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"))
    prompt_optimizer = PromptOptimizer(prompts_dir=str(base_dir / "brain" / "prompts"),
        scores_path=str(base_dir / "memory" / "prompt_scores.json"),
        min_improvement=config["agents"]["prompt_optimizer_min_improvement"])
    scheduler = AgentScheduler(outcome_reviewer=outcome_reviewer, bias_detector=bias_detector,
        strategy_evolver=strategy_evolver, prompt_optimizer=prompt_optimizer,
        outcome_interval_seconds=config["agents"]["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=config["agents"]["daily_pipeline_hour"], math_config=math_cfg)

    # Discord
    discord_bot = create_bot(db, trader, scanner, scheduler, config)
    alert_manager = AlertManager(bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"])

    await scheduler.start()

    async def run_discord():
        try:
            await discord_bot.start(get_secret("DISCORD_BOT_TOKEN"))
        except Exception as e:
            logger.error(f"Discord bot error: {e}")

    tasks = [
        asyncio.create_task(trading_loop(scanner, claude, prompt_builder, decision_table, trader,
            exit_monitor, alert_manager, db, config, is_paused_fn=lambda: discord_bot.is_paused)),
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
        await db.close()
        await discord_bot.close()

if __name__ == "__main__":
    asyncio.run(main())
