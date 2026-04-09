"""Live trader — real Polymarket CLOB orders via py-clob-client SDK."""
import asyncio
import logging
import os

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.execution.paper_trader import (
    DEFAULT_FEE_RATE,
    entry_fee_shares,
    exit_fee_usdc,
)
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)

# Poll interval and timeout for fill confirmation
_FILL_POLL_INTERVAL = 0.5  # seconds
_FILL_TIMEOUT = 5.0  # seconds


class LiveTrader:
    """Real Polymarket CLOB trading. Same interface as PaperTrader."""

    def __init__(self, db: Database, **kwargs):
        self.db = db
        self.max_slippage = kwargs.get("max_slippage", 0.02)
        self.max_bankroll_deployed = kwargs.get("max_bankroll_deployed", 0.80)
        self.max_concurrent_positions = kwargs.get("max_concurrent_positions", 1)

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            raise ValueError("Missing required secret: POLYMARKET_PRIVATE_KEY")
        funder = os.environ.get("POLYMARKET_FUNDER", "")

        self.client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            signature_type=2,  # GNOSIS_SAFE — proxy wallet deployed via Polymarket
            funder=funder,    # NOTE: if auth fails, try signature_type=1 (POLY_PROXY)
        )
        creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)
        logger.info("LiveTrader authenticated with Polymarket CLOB")

    async def get_balance(self) -> float:
        """Fetch USDC balance from Polymarket. Returns float in dollars."""
        result = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance_wei = int(result.get("balance", "0"))
        return balance_wei / 1e6  # USDC has 6 decimals

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    async def open_trade(
        self, market_id: str, question: str, side: str, price: float,
        size: float, signal_score: float, signal_strength: str,
        ev_at_entry: float, exit_target: float, stop_loss: float,
        weight_version: str, indicator_snapshot: str = "",
        token_id: str = "", fee_rate: float = DEFAULT_FEE_RATE,
    ) -> TradeResult:
        raise NotImplementedError("open_trade pending — Task 5")

    async def close_trade(
        self, position_id: int, exit_price: float, token_id: str = "",
    ) -> TradeResult:
        raise NotImplementedError("close_trade pending — Task 6")

    async def resolve_position(self, position_id: int, exit_price: float) -> TradeResult:
        raise NotImplementedError("resolve_position pending — Task 7")
