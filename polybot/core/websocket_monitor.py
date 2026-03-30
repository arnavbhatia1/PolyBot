import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ExitMonitor:
    def __init__(self, time_stop_hours: int = 24, time_stop_min_gain: float = 0.02):
        self.time_stop_hours = time_stop_hours
        self.time_stop_min_gain = time_stop_min_gain

    def check_exit(self, position: dict, current_price: float) -> str:
        entry_price = position["entry_price"]
        exit_target = position["exit_target"]
        stop_loss = position["stop_loss"]

        if current_price >= exit_target:
            return "take_profit"
        if current_price <= stop_loss:
            return "stop_loss"

        entry_time = datetime.fromisoformat(position["entry_timestamp"])
        now = datetime.now(timezone.utc)
        hours_held = (now - entry_time).total_seconds() / 3600
        gain_pct = (current_price - entry_price) / entry_price

        if hours_held >= self.time_stop_hours and gain_pct < self.time_stop_min_gain:
            return "time_stop"

        return "hold"

    async def monitor_positions(self, positions: list[dict], get_price, on_exit):
        for position in positions:
            try:
                current_price = await get_price(position["market_id"])
                action = self.check_exit(position, current_price)
                if action != "hold":
                    logger.info(
                        f"Exit signal '{action}' for position {position['id']} at price {current_price}"
                    )
                    await on_exit(position["id"], current_price, action)
            except Exception as e:
                logger.error(f"Error monitoring position {position['id']}: {e}")
