# polybot/main.py
import asyncio
import json
import logging
import logging.handlers
import time
from pathlib import Path

from polybot.config.loader import load_config, get_secret
from polybot.db.models import Database
from polybot.core.binance_feed import BinanceFeed
from polybot.core.market_scanner import BTCMarketScanner
from polybot.indicators.engine import IndicatorEngine
from polybot.core.signal_engine import SignalEngine
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


async def _get_contract_prices(market_scanner, market_id: str) -> dict | None:
    """Fetch current Up/Down prices for an active contract via Gamma API."""
    import httpx
    import time as _time

    window_ts = int(_time.time() // 300) * 300
    for ts in [window_ts, window_ts + 300, window_ts - 300]:
        slug = market_scanner._make_slug(ts)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{market_scanner.GAMMA_API}/events",
                                        params={"slug": slug})
                resp.raise_for_status()
                data = resp.json()
                if data:
                    event = data[0] if isinstance(data, list) else data
                    contract = market_scanner.parse_contract(event)
                    if contract and contract["condition_id"] == market_id:
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
        )
    except Exception as e:
        logger.error(f"Failed to record outcome: {e}")


async def trading_loop(binance_feed, market_scanner, indicator_engine, signal_engine,
                       trader, alert_manager, db, config, outcome_reviewer, is_paused_fn):
    signal_config = config["signal"]
    max_bankroll_pct = config["execution"]["max_bankroll_deployed"]
    exit_threshold = signal_config.get("exit_edge_threshold", -0.05)

    traded_contracts: dict[str, int] = {}      # condition_id -> timestamp (one trade per contract)
    window_strikes: dict[int, float] = {}      # window_ts -> BTC price at window open

    while True:
        try:
            if is_paused_fn():
                await asyncio.sleep(1)
                continue

            # --- POSITION MANAGEMENT: resolution check + active re-evaluation ---
            positions = await db.get_open_positions()
            for pos in positions:
                live = await _get_contract_prices(market_scanner, pos["market_id"])
                if not live:
                    continue

                if live["seconds_remaining"] <= 0:
                    # Contract resolved — close at final market price
                    exit_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]
                    result = await trader.close_trade(pos["id"], exit_price)
                    if result.success:
                        gain_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] > 0 else 0
                        shares = pos["size"] / pos["entry_price"]
                        pnl = shares * exit_price - pos["size"]
                        won = "WIN" if pnl > 0 else "LOSS"
                        logger.info(f"RESOLVED {won} {pos['side']} | {pos['entry_price']:.3f}->{exit_price:.3f} | {gain_pct:+.1%} | ${pnl:+.2f}")
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

                    # Get strike from the position's stored trade_context (correct for this contract)
                    pos_ctx = json.loads(pos.get("indicator_snapshot", "{}")).get("trade_context", {})
                    strike_now = pos_ctx.get("strike_price", 0)
                    if strike_now <= 0:
                        continue

                    indicators = indicator_engine.compute_all(binance_feed.buffer)
                    market_price = live["price_up"] if pos["side"] == "Up" else live["price_down"]

                    action, model_prob, holding_edge, reason = signal_engine.evaluate_hold(
                        indicators, btc_now, strike_now, live["seconds_remaining"],
                        market_price, pos["side"], exit_threshold)

                    if action == "EXIT":
                        result = await trader.close_trade(pos["id"], market_price)
                        if result.success:
                            gain_pct = (market_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] > 0 else 0
                            shares = pos["size"] / pos["entry_price"]
                            pnl = shares * market_price - pos["size"]
                            won = "WIN" if pnl > 0 else "LOSS"
                            logger.info(f"SCALP {won} {pos['side']} | {pos['entry_price']:.3f}->{market_price:.3f} | {gain_pct:+.1%} | ${pnl:+.2f} | {reason}")
                            if alert_manager:
                                await alert_manager.send_trade_closed(
                                    question="", exit_price=market_price, log_return=0, hold_hours=0,
                                    side=pos["side"], entry_price=pos["entry_price"], pnl=pnl,
                                    gain_pct=gain_pct, reason=f"scalp {won.lower()}")
                            await _record_outcome(outcome_reviewer, pos, market_price, result.log_return or 0, gain_pct,
                                                  exit_reason="scalp")
                            traded_contracts[pos["market_id"]] = int(time.time())

            # --- ENTRY: find contract and evaluate for edge ---
            # Skip if we already have an open position (one at a time)
            if await db.get_open_position_count() > 0:
                await asyncio.sleep(0)
                continue

            contract = await market_scanner.find_active_contract()
            if not contract:
                await asyncio.sleep(1)
                continue

            cid = contract["condition_id"]

            # Clean old entries
            now_ts = int(time.time())
            traded_contracts = {k: v for k, v in traded_contracts.items() if now_ts - v < 600}

            # One trade per contract
            if cid in traded_contracts:
                await asyncio.sleep(0)
                continue

            # Don't enter if market already decided (extreme prices)
            price_up = contract["price_up"]
            price_down = contract["price_down"]
            if price_up < 0.15 or price_up > 0.85:
                await asyncio.sleep(0)
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
                await asyncio.sleep(0)
                continue

            btc_price = binance_feed.buffer.latest().close if binance_feed.buffer.latest() else 0
            if btc_price <= 0:
                await asyncio.sleep(0)
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
                size = max(size, 1.0)
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
                )

                if result.success:
                    traded_contracts[cid] = now_ts
                    logger.info(f"OPEN {side} @ {price:.3f} | ${size:.2f} | {signal.reason}")
                    if alert_manager:
                        await alert_manager.send_trade_opened(
                            question=contract["question"], side=side, size=size,
                            entry_price=price, ev=signal.edge, exit_target=1.0)

        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            if alert_manager:
                await alert_manager.send_error(str(e))

        await asyncio.sleep(0)  # Yield control, then loop immediately — speed limited only by API latency


async def main():
    config = load_config()
    base_dir = Path(__file__).parent

    # Database — fresh start every run
    db_path = Path(config["database"]["path"])
    if db_path.exists():
        db_path.unlink()
        logger.info("Deleted old database — fresh start")
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
        min_edge=signal_cfg.get("entry_threshold", 0.10),
        kelly_fraction=config["math"].get("kelly_fraction", 0.15),
        momentum_weight=signal_cfg.get("momentum_weight", 0.08),
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
        control_channel_name=config["discord"]["control_channel_name"])
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
        math_config=math_cfg,
    )
    discord_bot.scheduler = scheduler
    discord_bot.initial_bankroll = config["execution"]["initial_bankroll"]

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
            is_paused_fn=lambda: discord_bot.is_paused)),
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
