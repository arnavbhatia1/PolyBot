"""Base execution layer: shared dataclasses, fee math, and BaseTrader ABC."""
from __future__ import annotations

import json as _json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from polybot.db.models import Database
from polybot.core.returns import log_return

logger = logging.getLogger(__name__)

try:
    import orjson as _orjson
    def _dumps_snapshot(obj: Any) -> str:
        return _orjson.dumps(obj).decode("utf-8")
except ImportError:
    def _dumps_snapshot(obj: Any) -> str:
        return _json.dumps(obj)

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
    pending: bool = False  # resolution not final yet (e.g. on-chain redeem in flight) — retry next tick
    maker_fill: bool = False  # close filled as a resting maker (zero taker fee) vs a taker FOK
    maker_rebate_usd: float = 0.0  # expected daily-pUSD maker rebate on a maker_fill close (0 for taker)

@dataclass
class FillResult:
    filled: bool
    fill_price: float = 0.0
    fill_size: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Fee math (canonical — imported by paper_trader, live_trader, main)
# ---------------------------------------------------------------------------

# Polymarket's `feeRate` coefficient for Crypto markets (docs.polymarket.com/trading/fees).
# It goes INSIDE the formula `fee = feeRate x shares x p x (1-p)` — a coefficient, not a flat
# percentage. Peak effective fee is feeRate x 0.25 = 1.75% of payout at p=0.50.
DEFAULT_FEE_RATE = 0.07

# Flat per-share effective-fee proxy (= feeRate x 0.25, the p=0.50 peak). Use ONLY where the fee
# is a flat additive cost term (spread/exec-cost gates), never inside the p(1-p) formula.
EFFECTIVE_FEE_PEAK = round(DEFAULT_FEE_RATE * 0.25, 5)  # 0.0175

# Polymarket Maker Rebates Program: makers earn a daily pUSD rebate funded by taker fees —
# 20% of collected taker fees on Crypto markets (docs.polymarket.com/market-makers/maker-rebates).
# Automatic on any resting order that gets filled; no scoring/two-sided/dwell requirement (that is
# the SEPARATE liquidity-rewards program, which excludes short-horizon crypto). This coefficient is
# the sole-maker CEILING — Polymarket pays it pro-rata by your share of the market's maker
# fee-equivalent, so the realized live credit is a fraction of it. Not an edge: it is dwarfed by
# the adverse-selection cost on the same fills; it only trims the cost of maker exits.
DEFAULT_MAKER_REBATE_RATE = 0.20


def slippage_pct(order_size_usd: float, book_depth_usd: float,
                 impact_factor: float = 0.03) -> float:
    """Convex market impact: deeper book consumption costs disproportionately more.

    Returns a percentage (0.015 = 1.5%) to add (buys) or subtract (sells). Cost
    accelerates via fill_pct * impact * (1 + fill_pct): 1.5x a linear model at
    50% depth, 2x at 100%. Conservative for negRisk markets where cross-matching
    creates deeper real liquidity than the raw book shows.
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


def maker_rebate(shares: float, price: float,
                 rebate_rate: float = DEFAULT_MAKER_REBATE_RATE,
                 fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """Expected maker rebate on a resting fill = rebate_rate × the taker fee the lifting
    counterparty pays (taker_fee = feeRate × shares × p × (1-p)). Zero at price extremes,
    max at p=0.50. Sole-maker ceiling — pro-rata diluted live (see DEFAULT_MAKER_REBATE_RATE)."""
    return round(rebate_rate * taker_fee(shares, price, fee_rate), 6)


def compute_buy_vwap(book: dict[str, Any] | None, size_usd: float) -> float | None:
    """Expected BUY fill VWAP from walking the asks ladder for ``size_usd`` notional.

    ``None`` when the book is missing, asks are empty/unparseable, or depth is
    below the requested size (caller falls back to a best-ask-only gate). Same
    walk math as ``LiveTrader._estimate_fok_walk`` / ``PaperTrader._precheck_rejects``
    so the gate and the actual FOK see the same book.
    """
    if not book or size_usd <= 0:
        return None
    levels_raw = book.get("asks") or []
    if not levels_raw:
        return None
    try:
        parsed = [(float(l["price"]), float(l["size"])) for l in levels_raw
                  if l.get("price") and l.get("size")]
    except (TypeError, ValueError, KeyError):
        return None
    if not parsed:
        return None
    parsed.sort(key=lambda ps: ps[0])  # asks ascending — best (lowest) first
    spent = 0.0
    consumed = 0.0
    remaining = size_usd
    for px, sz in parsed:
        if remaining <= 0:
            break
        level_usd = px * sz
        take_usd = min(remaining, level_usd)
        spent += take_usd
        consumed += take_usd / px
        remaining -= take_usd
    if remaining > 1e-6 or consumed <= 0:
        return None
    return spent / consumed


def _entry_fee_usd_from_position(position: dict[str, Any], shares_held: float) -> float:
    """USD value of the entry fee (paid in shares): (size/entry_price − shares_held)
    × entry_price. Logging only — bankroll math doesn't double-count the fee.
    """
    entry_price = position["entry_price"]
    shares_ordered = position["size"] / entry_price
    return (shares_ordered - shares_held) * entry_price


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
        max_bankroll_deployed: float = 0.80,
        max_concurrent_positions: int = 1,
    ) -> None:
        self.db: Database = db
        self.max_bankroll_deployed: float = max_bankroll_deployed
        self.max_concurrent_positions: int = max_concurrent_positions
        self._clob_ws: Any = None

    def set_clob_ws(self, clob_ws: Any) -> None:
        """Attach the CLOB WebSocket — paper: book snapshots; live: WS-derived
        fill-price fast path in ``_submit_fok_order``."""
        self._clob_ws = clob_ws

    # -- abstract hooks --------------------------------------------------

    @abstractmethod
    async def _execute_buy(
        self, token_id: str, price: float, size: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Execute a buy order. ``fee_rate`` lets live convert a WS gross VWAP
        into the net-shares-based fill_price; paper ignores it."""

    @abstractmethod
    async def _execute_sell(
        self, token_id: str, shares: float, price: float,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> FillResult:
        """Execute a sell order. Returns FillResult with actual fill details."""

    async def _sellable_shares(self, token_id: str, fallback_shares: float) -> float:
        """Shares actually available to sell. LiveTrader overrides to query the
        on-chain balance (avoids DB-vs-chain drift); paper keeps DB authoritative."""
        return fallback_shares

    def _scalp_residual_credit(self, residual_shares: float, fill_price: float,
                               fee_rate: float) -> float:
        """USDC to credit the bankroll for the fee-headroom shares held back from
        a scalp's FOK. Live returns 0 — its on-chain residual is swept by
        ``_sweep_residual`` and surfaces in the next absolute balance sync. Paper
        overrides to credit the simulated sweep, since paper bankroll is
        delta-only and would otherwise leak ~2% of exit notional per scalp."""
        return 0.0

    def _maker_rebate_credit(self, rebate_usd: float) -> float:
        """USDC to credit the bankroll for the maker rebate on a passive-exit fill.
        Live returns 0 — Polymarket pays the 20% crypto maker rebate as a SEPARATE daily
        pUSD credit that surfaces in the next absolute balance sync, so crediting it
        per-fill would double-count. Paper overrides to credit the simulated rebate, since
        paper bankroll is delta-only and would otherwise never reflect what live earns."""
        return 0.0

    @abstractmethod
    async def _resolve_bankroll(self, position: dict[str, Any], exit_price: float) -> float | None:
        """New bankroll after resolution (does NOT route through _execute_sell/
        close_trade, so handles fees itself). Paper: bankroll + revenue net of fee.
        Live: real Polymarket USDC balance. Returns None when the credit hasn't
        settled — resolve_position reports pending and the caller retries next tick.
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
        indicator_snapshot: str | dict[str, Any] = "",
        token_id: str = "",
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> TradeResult:
        # --- Rejection gates ---
        # Single composite query: one round trip on aiosqlite's serialized
        # connection, and an atomic snapshot — no race between sub-reads.
        has_pos, pos_count, bankroll, deployed = await self.db.get_open_trade_preflight(market_id)
        if has_pos:
            return TradeResult(success=False, reason="Duplicate market — already have position")
        if pos_count >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")
        # `bankroll` is free cash (open positions already debited); add deployed
        # cost back so the cap means "≤ max_bankroll_deployed of total equity
        # across all positions" rather than shrinking with each open position.
        max_deployable = (bankroll + deployed) * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(
                success=False,
                reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}",
            )

        # --- Execute buy ---
        fill = await self._execute_buy(token_id, price, size, fee_rate=fee_rate)
        if not fill.filled:
            return TradeResult(success=False, reason=fill.reason or "Buy not filled")

        # --- Fee math (entry fee collected in SHARES) ---
        shares_ordered = fill.fill_size / fill.fill_price
        fee_in_shares = entry_fee_shares(shares_ordered, fill.fill_price, fee_rate)
        shares_received = shares_ordered - fee_in_shares

        # Lazy serialization — a dict defers the JSON dump off the path-to-submit;
        # a rejected entry never dumps at all.
        if isinstance(indicator_snapshot, dict):
            indicator_snapshot = _dumps_snapshot(indicator_snapshot)

        # --- Persist atomically: position insert + bankroll debit in one transaction
        # (a crash mid-write can't leave one without the other). Debit = USDC spent
        # only — entry fee is paid in shares, not USDC.
        try:
            pos_id = await self.db.open_position_and_debit_bankroll(
                new_bankroll=bankroll - fill.fill_size,
                market_id=market_id,
                question=question,
                side=side,
                entry_price=fill.fill_price,
                size=fill.fill_size,
                signal_score=signal_score,
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
        exit_reason: str = "scalp",
        maker_fill: bool = False,
    ) -> TradeResult:
        """``maker_fill=True`` (Phase 1 passive exit): the resting SELL was
        already lifted at ``exit_price`` (a tape print strictly through the
        level), so there is no taker leg to execute and no taker fee."""
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
        fallback_shares = position.get("shares_held") or position["size"] / position["entry_price"]
        # _sellable_shares sells what we really own (on-chain), not what the DB
        # thinks — eliminates dust from drift.
        sellable_shares = await self._sellable_shares(token_id, fallback_shares)
        fee_rate = position.get("fee_rate") or DEFAULT_FEE_RATE

        if maker_fill:
            # No taker leg: the resting SELL already lifted at the level, exit fee
            # is zero. Size off the position's shares (fallback), NOT a fresh
            # _sellable_shares read — live's on-chain balance is ~0 right after the
            # maker fill executed, which would zero the recorded close. For paper
            # _sellable_shares == fallback_shares, so this is a no-op there.
            sell_fee_headroom = 0.005
            shares = fallback_shares * (1.0 - sell_fee_headroom)
            fill_price = exit_price
            exit_fee_rate = 0.0
        else:
            # Share buffer so Polymarket's per-share fee deduction (fee_rate × shares
            # × p × (1-p), peak fee_rate × 0.25) doesn't push the FOK above available
            # balance; the 0.005 floor also covers 1-tick mismatches at zero fee_rate.
            sell_fee_headroom = max(fee_rate * 0.25, 0.0) + 0.002
            sell_fee_headroom = max(sell_fee_headroom, 0.005)
            shares = sellable_shares * (1.0 - sell_fee_headroom)

            # --- Execute sell ---
            fill = await self._execute_sell(token_id, shares, exit_price, fee_rate=fee_rate)
            if not fill.filled:
                return TradeResult(success=False, reason=fill.reason or "Sell not filled")
            fill_price = fill.fill_price
            exit_fee_rate = fee_rate

        # --- Fee math and revenue ---
        lr = log_return(position["entry_price"], fill_price)
        fee_usdc = exit_fee_usdc(shares, fill_price, exit_fee_rate)
        revenue = shares * fill_price - fee_usdc
        # Entry fee = the at-open share haircut; derive it from the entry-held shares
        # (fallback_shares), not the headroom-reduced sell qty, so held-back maker
        # headroom (credited back via _scalp_residual_credit) isn't booked as fee.
        # Mirrors resolve_position.
        entry_fee_usd = _entry_fee_usd_from_position(position, fallback_shares)
        pnl = revenue - position["size"]
        gain_pct = pnl / position["size"] if position["size"] > 0 else 0.0

        # Maker rebate: the taker who lifted our resting SELL paid taker_fee at the FULL
        # fee_rate even though our exit fee is 0, so a maker close earns rebate_rate × that
        # fee back (Polymarket pays makers 20% of crypto taker fees, daily in pUSD). Booked
        # like the residual credit (paper simulates the daily credit; live returns 0 — the
        # real pUSD credit lands in the next absolute balance sync, so per-fill crediting
        # would double-count). Kept OUT of pnl so paper/live records stay comparable and the
        # counterfactual/go-live-gate pnl is untouched.
        rebate_usd = maker_rebate(shares, fill_price, fee_rate=fee_rate) if maker_fill else 0.0

        # --- Persist to DB (atomic: close + bankroll credit in one transaction) ---
        # Headroom shares held back from the FOK are credited via
        # _scalp_residual_credit (paper simulates the sweep; live returns 0 — its
        # residual lands in the next absolute balance sync). Deliberately NOT in
        # pnl: live's recorded pnl excludes the swept residual too, so paper/live
        # trade records stay comparable.
        residual_credit = self._scalp_residual_credit(
            sellable_shares - shares, fill_price, exit_fee_rate)
        rebate_credit = self._maker_rebate_credit(rebate_usd)
        total_fees = entry_fee_usd + fee_usdc
        await self.db.close_position(
            position_id, exit_price=fill_price,
            bankroll_delta=revenue + residual_credit + rebate_credit, pnl=pnl,
            fees=total_fees, exit_reason=exit_reason, maker_fill=maker_fill,
            maker_rebate=rebate_usd,
        )

        return TradeResult(success=True, position_id=position_id, log_return=lr,
                           pnl=pnl, entry_fee_usd=entry_fee_usd, exit_fee_usd=fee_usdc,
                           gain_pct=gain_pct, shares=shares, fill_price=fill_price,
                           maker_fill=maker_fill, maker_rebate_usd=rebate_usd)

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
        entry_fee_usd = _entry_fee_usd_from_position(position, shares)
        exit_fee_usd_val = exit_fee_usdc(shares, exit_price, fee_rate)
        revenue = shares * exit_price - exit_fee_usd_val
        pnl = revenue - position["size"]
        gain_pct = pnl / position["size"] if position["size"] > 0 else 0.0

        # --- Compute log return, then close + set bankroll atomically ---
        lr = log_return(position["entry_price"], exit_price)
        total_fees = entry_fee_usd + exit_fee_usd_val
        new_bankroll = await self._resolve_bankroll(position, exit_price)
        if new_bankroll is None:
            return TradeResult(success=False, pending=True, position_id=position_id,
                               reason="awaiting on-chain redeem")
        await self.db.close_position(
            position_id, exit_price=exit_price,
            new_bankroll=new_bankroll, pnl=pnl, fees=total_fees, exit_reason="resolution",
        )

        return TradeResult(success=True, position_id=position_id, log_return=lr,
                           pnl=pnl, entry_fee_usd=entry_fee_usd, exit_fee_usd=exit_fee_usd_val,
                           gain_pct=gain_pct, shares=shares)
