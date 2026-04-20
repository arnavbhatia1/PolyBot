"""Deribit BTC options implied volatility feed.

Provides forward-looking volatility from the BTC options market to complement
the backward-looking ATR used by the signal engine. When the options market
expects higher volatility than recent ATR suggests, the model should widen
its sigma estimate (more conservative probabilities).

Key concepts:
- IV ratio = Deribit ATM IV / ATR-derived annualized vol
- Ratio > 1.0: market expects MORE vol than ATR shows (widen sigma)
- Ratio < 1.0: market expects LESS vol than ATR shows (tighten sigma)
- Clamped to [0.5, 2.0] to prevent extreme adjustments
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# Minutes per year (365.25 days * 24 hours * 60 minutes)
MINUTES_PER_YEAR = 525600

# Default clamp bounds for IV ratio (overridden by config)
IV_RATIO_MIN = 0.5
IV_RATIO_MAX = 3.0

# ATM proximity threshold: option strike within 5% of underlying
ATM_THRESHOLD_PCT = 0.05

DERIBIT_BOOK_SUMMARY_URL = (
    "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
)


def compute_iv_ratio(current_iv: float, historical_iv: float,
                     iv_min: float = IV_RATIO_MIN,
                     iv_max: float = IV_RATIO_MAX) -> float:
    """Compute clamped ratio of current IV to historical IV.

    Args:
        current_iv: Forward-looking implied volatility (annualized, decimal).
        historical_iv: Backward-looking realized/historical volatility (annualized, decimal).
        iv_min: Floor for the ratio (pipeline-tunable).
        iv_max: Cap for the ratio (pipeline-tunable).

    Returns:
        Ratio clamped to [iv_min, iv_max]. Returns 1.0 if historical_iv is zero or negative.
    """
    if historical_iv <= 0.0:
        return 1.0
    ratio = current_iv / historical_iv
    return max(iv_min, min(iv_max, ratio))


@dataclass
class IVState:
    """Holds the latest Deribit ATM implied volatility for BTC.

    Attributes:
        btc_iv: Annualized implied volatility as a decimal (e.g. 0.80 = 80%).
                 None when no data has been received yet.
        updated_at: Unix timestamp of last successful update.
    """
    btc_iv: float | None = None
    updated_at: float = 0.0
    net_gex: float = 0.0

    # Max age before IV is treated as stale and the ratio defaults to 1.0 (no scaling).
    # Two poll intervals plus buffer — covers a single failed poll but flags a sustained
    # outage as "use ATR-only vol" rather than silently feeding week-old IV into L1.
    STALE_SECONDS: float = 180.0

    def get_iv_ratio(self, atr: float, btc_price: float,
                     iv_min: float = IV_RATIO_MIN,
                     iv_max: float = IV_RATIO_MAX) -> float:
        """Compare Deribit IV to ATR-derived annualized volatility.

        Converts ATR (1-minute) to annualized vol:
            annualized_vol = (atr / btc_price) * sqrt(525600)

        Returns 1.0 (neutral — defer to ATR-only vol) when IV is missing, stale, or
        inputs are invalid. Staleness matters because a failed ATM extraction leaves
        ``btc_iv`` pinned at its last value; we must not silently feed an hour-old IV
        into the L1 CDF.
        """
        if self.btc_iv is None:
            return 1.0
        if btc_price <= 0.0 or atr <= 0.0:
            return 1.0
        if self.updated_at > 0 and (time.time() - self.updated_at) > self.STALE_SECONDS:
            return 1.0
        historical_iv = (atr / btc_price) * math.sqrt(MINUTES_PER_YEAR)
        return compute_iv_ratio(self.btc_iv, historical_iv, iv_min, iv_max)


class DeribitIVFeed:
    """REST poller for Deribit BTC options ATM implied volatility.

    Polls the public book summary endpoint every 60 seconds. No authentication
    required. Extracts the ATM option's mark_iv and stores it in IVState.

    Usage:
        feed = DeribitIVFeed()
        asyncio.create_task(feed.start())
        # Later:
        ratio = feed.state.get_iv_ratio(atr=25.0, btc_price=73000.0)
    """

    def __init__(self, poll_interval: float = 60.0) -> None:
        self.poll_interval = poll_interval
        self.state = IVState()
        self._running = False

    async def start(self) -> None:
        """Start the polling loop. Runs until stop() is called."""
        self._running = True
        logger.info("DeribitIVFeed starting (poll every %.0fs)", self.poll_interval)
        while self._running:
            try:
                await self._poll()
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                logger.warning("DeribitIVFeed: network timeout (%s), retrying in %.0fs", type(e).__name__, self.poll_interval)
            except Exception:
                logger.exception("DeribitIVFeed poll failed")
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._running = False
        logger.debug("DeribitIVFeed stopped")

    async def _poll(self) -> None:
        """Fetch book summaries and extract ATM IV."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                DERIBIT_BOOK_SUMMARY_URL,
                params={"currency": "BTC", "kind": "option"},
            )
            resp.raise_for_status()
            data = resp.json()

        summaries = data.get("result", [])
        if not summaries:
            logger.warning("DeribitIVFeed: empty result from book summary")
            return

        iv = self._extract_atm_iv(summaries)
        if iv is not None:
            self.state.btc_iv = iv
            self.state.updated_at = time.time()
            logger.debug("DeribitIVFeed: ATM IV = %.4f", iv)
        else:
            logger.warning("DeribitIVFeed: no ATM option found")

        # Compute net gamma exposure from the full options chain
        from polybot.core.gamma_exposure import compute_net_gex
        options = []
        underlying_price = 0.0
        for s in summaries:
            name = s.get("instrument_name", "")
            parts = name.split("-")
            if len(parts) >= 4:
                try:
                    strike = float(parts[2])
                    opt_type = "call" if parts[3] == "C" else "put"
                    iv_val = s.get("mark_iv", 0)
                    oi_val = s.get("open_interest", 0)
                    underlying = s.get("underlying_price", 0)
                    if underlying:
                        underlying_price = float(underlying)
                    if iv_val and oi_val and underlying:
                        options.append({
                            "strike": strike,
                            "type": opt_type,
                            "oi": float(oi_val),
                            "iv": float(iv_val) / 100.0,
                            "expiry_hours": 24,
                        })
                except (ValueError, IndexError):
                    continue
        if options and underlying_price > 0:
            self.state.net_gex = compute_net_gex(options, spot_price=underlying_price)
            logger.debug("Deribit GEX: %.4f", self.state.net_gex)

    @staticmethod
    def _extract_atm_iv(summaries: list[dict]) -> float | None:
        """Find the nearest ATM option and return its mark_iv.

        Filters for options within 5% of underlying_price with mark_iv > 0.
        Returns mark_iv / 100.0 (Deribit reports IV as percentage).

        Args:
            summaries: List of book summary dicts from Deribit API.

        Returns:
            Annualized IV as decimal, or None if no suitable option found.
        """
        best_iv: float | None = None
        best_distance = float("inf")

        for s in summaries:
            mark_iv = s.get("mark_iv")
            underlying = s.get("underlying_price")
            # Deribit includes a mid_price or mark_price from which we can
            # infer the strike.  The instrument_name encodes the strike, but
            # underlying_price + mark_iv is sufficient for ATM selection.
            if mark_iv is None or underlying is None:
                continue
            if mark_iv <= 0 or underlying <= 0:
                continue

            # Extract strike from instrument name: BTC-DDMMMYY-STRIKE-C/P
            instrument = s.get("instrument_name", "")
            parts = instrument.split("-")
            if len(parts) < 3:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue

            distance = abs(strike - underlying) / underlying
            if distance > ATM_THRESHOLD_PCT:
                continue

            if distance < best_distance:
                best_distance = distance
                best_iv = mark_iv / 100.0  # Deribit reports as percentage

        return best_iv
