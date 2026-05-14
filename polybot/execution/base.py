"""Base execution layer: shared dataclasses, fee math, and BaseTrader ABC."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from polybot.db.models import Database
from polybot.core.returns import log_return

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    success: bool
    position_id: int | None = None
    reason: str = ""
    log_return: float | None = None
    pnl: float = 0.0
    entry_fee_usd: float = 0.0
    exit_fee_usd: float = 0.0
    gain_pct: float = 0.0
    shares: float = 0.0
    fill_price: float = 0.0

@dataclass
class FillResult:
    filled: bool
    fill_price: float = 0.0
    fill_size: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Fee math (canonical — imported by paper_trader, live_trader, main)
# ---------------------------------------------------------------------------

DEFAULT_FEE_RATE = 0.018  # Polymarket Dynamic taker fee model: 1.8% peak


def slippage_pct(order_size_usd: float, book_depth_usd: float,
                 impact_factor: float = 0.03) -> float:
    """Convex market impact: deeper book consumption costs disproportionately more.

    Returns a percentage (0.015 = 1.5%) to add (buys) or subtract (sells).
    Uses fill_pct * impact * (1 + fill_pct) so cost accelerates as the order
    walks through price levels.  At 50% depth the cost is 50% higher than a
    naive linear model; at 100% it is 2x.  Conservative for negRisk markets
    where cross-matching creates deeper real liquidity than the raw book shows.
    """
    if book_depth_usd <= 0:
        return 0.0
    fill_pct = min(order_size_usd / book_depth_usd, 1.0)
    return fill_pct * impact_factor * (1.0 + fill_pct)


def taker_fee(shares: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Polymarket fee: feeRate x shares x p x (1-p). Zero at extremes, max at p=0.50."""
    return round(fee_rate * shares * price * (1.0 - price), 6)


def entry_fee_shares(shares_ordered: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """On buys, Polymarket collects fee in shares. Returns shares deducted."""
    fee_dollars = taker_fee(shares_ordered, price, fee_rate)
    return fee_dollars / price if price > 0 else 0.0


def exit_fee_usdc(shares: float, price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """On sells, Polymarket collects fee in USDC. Returns USDC deducted."""
    return taker_fee(shares, price, fee_rate)


# ---------------------------------------------------------------------------
# BaseTrader ABC
# ---------------------------------------------------------------------------

class BaseTrader(ABC):
    """Abstract base for PaperTrader and LiveTrader.

    Subclasses implement only:
      _execute_buy  — how to fill a buy (mock vs CLOB order)
      _execute_sell — how to fill a sell
      _resolve_bankroll — how to compute new bankroll on resolution
    """

    def __init__(
        self,
        db: Database,
        max_slippage: float = 0.02,
        max_bankroll_deployed: float = 0.80,
        max_concurrent_positions: int = 1,
    ) -> None:
        self.db: Database = db
        self.max_slippage: float = max_slippage
        self.max_bankroll_deployed: float = max_bankroll_deployed
        self.max_concurrent_positions: int = max_concurrent_positions

    # -- deployed capital ------------------------------------------------

    async def _get_deployed_capital(self) -> float:
        """Sum of USDC size across all open positions."""
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    # -- abstract hooks --------------------------------------------------

    @abstractmethod
    async def _execute_buy(self, token_id: str, price: float, size: float) -> FillResult:
        """Execute a buy order. Returns FillResult with actual fill details."""

    @abstractmethod
    async def _execute_sell(self, token_id: str, shares: float, price: float) -> FillResult:
        """Execute a sell order. Returns FillResult with actual fill details."""

    @abstractmethod
    async def _resolve_bankroll(self, position: dict[str, Any], exit_price: float) -> float:
        """Compute new bankroll after market resolution.

        resolve_position does NOT route through _execute_sell/close_trade,
        so this method is responsible for fee calculation if applicable.

        Paper: current bankroll + revenue (shares * exit_price - fee).
        Live: fetch real USDC balance from Polymarket (auto-credited).

        Returns the new bankroll value to set in DB.
        """

    # -- open_trade ------------------------------------------------------

    async def open_trade(
        self,
        market_id: str,
        question: str,
        side: str,
        price: float,
        size: float,
        signal_score: float,
        signal_strength: str,
        ev_at_entry: float,
        exit_target: float,
        stop_loss: float,
        indicator_snapshot: str = "",
        token_id: str = "",
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> TradeResult:
        # --- Rejection gates ---
        has_pos, pos_count, bankroll, deployed = await asyncio.gather(
            self.db.has_position_for_market(market_id),
            self.db.get_open_position_count(),
            self.db.get_bankroll(),
            self._get_deployed_capital(),
        )
        if has_pos:
            return TradeResult(success=False, reason="Duplicate market — already have position")
        if pos_count >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")
        max_deployable = bankroll * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(
                success=False,
                reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}",
            )

        # --- Execute buy ---
        fill = await self._execute_buy(token_id, price, size)
        if not fill.filled:
            return TradeResult(success=False, reason=fill.reason or "Buy not filled")

        # --- Fee math (entry fee collected in SHARES) ---
        shares_ordered = fill.fill_size / fill.fill_price
        fee_in_shares = entry_fee_shares(shares_ordered, fill.fill_price, fee_rate)
        shares_received = shares_ordered - fee_in_shares

        # --- Persist atomically: position insert + bankroll debit in one transaction.
        # Either both writes happen or neither, so a crash mid-write cannot leave
        # the DB with a position record but no bankroll debit (or vice versa).
        # Bankroll debit = USDC spent only (entry fee is paid in shares, not USDC).
        try:
            pos_id = await self.db.open_position_and_debit_bankroll(
                new_bankroll=bankroll - fill.fill_size,
                market_id=market_id,
                question=question,
                side=side,
                entry_price=fill.fill_price,
                size=fill.fill_size,
                signal_score=signal_score,
                signal_strength=signal_strength,
                ev_at_entry=ev_at_entry,
                exit_target=exit_target,
                stop_loss=stop_loss,
                indicator_snapshot=indicator_snapshot,
                fee_rate=fee_rate,
                shares_held=shares_received,
            )
        except Exception as e:
            # Buy already filled on Polymarket but we couldn't persist locally.
            # Loud error so the operator can manually reconcile from chain state.
            logger.error(
                "CRITICAL: buy filled (size=$%.2f, fill=$%.4f) but DB write failed: %s. "
                "Polymarket has the position; reconcile manually before next trade.",
                fill.fill_size, fill.fill_price, e,
            )
            return TradeResult(success=False, reason=f"DB write failed after fill: {e}")

        return TradeResult(success=True, position_id=pos_id, fill_price=fill.fill_price)

    # -- close_trade -----------------------------------------------------

    async def close_trade(
        self,
        position_id: int,
        exit_price: float,
        token_id: str = "",
        position: dict[str, Any] | None = None,
    ) -> TradeResult:
        # --- Lookup position ---
        if position is None:
            positions = await self.db.get_open_positions()
            position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(
                success=False,
                reason=f"Position {position_id} not found or already closed",
            )

        # --- Shares and fee rate from position ---
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE

        # --- Execute sell ---
        fill = await self._execute_sell(token_id, shares, exit_price)
        if not fill.filled:
            return TradeResult(success=False, reason=fill.reason or "Sell not filled")

        # --- Fee math and revenue ---
        lr = log_return(position["entry_price"], fill.fill_price)
        fee_usdc = exit_fee_usdc(shares, fill.fill_price, fee_rate)
        revenue = shares * fill.fill_price - fee_usdc

        # Entry fee breakdown (already deducted in shares at entry — express as USD for logging)
        shares_ordered = position["size"] / position["entry_price"]
        entry_fee_in_shares = shares_ordered - shares
        entry_fee_usd = entry_fee_in_shares * position["entry_price"]

        pnl = revenue - position["size"]
        gain_pct = pnl / position["size"] if position["size"] > 0 else 0.0

        # --- Persist to DB (atomic: close + bankroll credit in one transaction) ---
        total_fees = entry_fee_usd + fee_usdc
        await self.db.close_position_and_credit_bankroll(
            position_id, exit_price=fill.fill_price, log_return=lr,
            bankroll_delta=revenue, pnl=pnl, fees=total_fees,
        )

        return TradeResult(success=True, position_id=position_id, log_return=lr,
                           pnl=pnl, entry_fee_usd=entry_fee_usd, exit_fee_usd=fee_usdc,
                           gain_pct=gain_pct, shares=shares, fill_price=fill.fill_price)

    # -- resolve_position ------------------------------------------------

    async def resolve_position(
        self,
        position_id: int,
        exit_price: float,
    ) -> TradeResult:
        """Resolution: exit at $1 or $0. Delegates bankroll logic to subclass."""
        # --- Lookup position ---
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(
                success=False,
                reason=f"Position {position_id} not found or already closed",
            )

        # --- Fee breakdown for logging ---
        shares = position.get("shares_held") or position["size"] / position["entry_price"]
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE
        shares_ordered = position["size"] / position["entry_price"]
        entry_fee_in_shares = shares_ordered - shares
        entry_fee_usd = entry_fee_in_shares * position["entry_price"]
        exit_fee_usd_val = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - exit_fee_usd_val
        pnl = revenue - position["size"]
        gain_pct = pnl / position["size"] if position["size"] > 0 else 0.0

        # --- Compute log return, then close + set bankroll atomically ---
        lr = log_return(position["entry_price"], exit_price)
        total_fees = entry_fee_usd + exit_fee_usd_val
        new_bankroll = await self._resolve_bankroll(position, exit_price)
        await self.db.close_position_and_set_bankroll(
            position_id, exit_price=exit_price, log_return=lr,
            new_bankroll=new_bankroll, pnl=pnl, fees=total_fees,
        )

        return TradeResult(success=True, position_id=position_id, log_return=lr,
                           pnl=pnl, entry_fee_usd=entry_fee_usd, exit_fee_usd=exit_fee_usd_val,
                           gain_pct=gain_pct, shares=shares)
