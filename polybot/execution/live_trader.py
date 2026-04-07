"""Live trader stub — polymarket.com CLOB trading requires EIP-712 signed orders.

Paper mode works with real CLOB order book prices. Live .com trading is future work.
"""
import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult

logger = logging.getLogger(__name__)


class LiveTrader:
    def __init__(self, db: Database, **kwargs):
        raise NotImplementedError(
            "Live trading on polymarket.com requires EIP-712 signed orders. "
            "Use --mode paper. Live .com trader is future work."
        )

    async def open_trade(self, **kwargs) -> TradeResult:
        raise NotImplementedError

    async def close_trade(self, position_id: int, exit_price: float, **kwargs) -> TradeResult:
        raise NotImplementedError

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        raise NotImplementedError
