"""Tiered floor circuit breaker — locks in a protected floor at each bankroll milestone.

Every time the bankroll crosses a tier (100, 150, 200, 300, ...) the floor is
raised to floor_pct of that tier. The floor never moves down. Kelly scales concavely
from 1.0 at the locked tier to min_multiplier at the floor, so shallow drawdowns
(noise in a 58% model) penalize lightly while deep drawdowns (real regime break)
penalize aggressively.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger("polybot")

# Milestone tiers in USD. The bot locks in whichever tier the bankroll last crossed.
_TIERS = [100, 150, 200, 300, 400, 600, 800, 1000,
          1500, 2000, 3000, 4000, 6000, 8000, 10_000]


def _locked_tier(bankroll: float) -> float:
    """Highest tier at or below the given bankroll."""
    result = _TIERS[0]
    for t in _TIERS:
        if bankroll >= t:
            result = t
        else:
            break
    return float(result)


class CircuitBreaker:
    """Tiered floor protection for Kelly sizing.

    Kelly multiplier:
      - 1.0x at or above locked_tier
      - min_multiplier at or below floor (= locked_tier × floor_pct)
      - concave (sqrt) interpolation between floor and locked_tier

    When bankroll crosses a higher tier the floor ratchets up. It never resets down.

    Streak tracking feeds Discord alerts only; it does NOT drive sizing.
    """

    def __init__(
        self,
        initial_bankroll: float = 100.0,
        floor_pct: float = 0.85,
        min_multiplier: float = 0.40,
        # Streak tracking — Discord alerts only, no sizing effect
        losses_to_reduce: int = 3,
        wins_to_restore: int = 3,
    ) -> None:
        self.floor_pct: float = floor_pct
        self.min_multiplier: float = min_multiplier
        self.current_bankroll: float = initial_bankroll

        # Derive locked tier from starting bankroll (or persisted peak set later)
        self.locked_tier: float = _locked_tier(initial_bankroll)
        self.floor: float = round(self.locked_tier * self.floor_pct, 2)

        # Peak tracked for logging (high-water mark)
        self.peak_bankroll: float = initial_bankroll

        # Streak tracking (for Discord alerts only)
        self.consecutive_losses: int = 0
        self.consecutive_wins: int = 0
        self.losses_to_reduce: int = losses_to_reduce
        self.wins_to_restore: int = wins_to_restore

    # ------------------------------------------------------------------
    # Core: tiered floor Kelly scaling
    # ------------------------------------------------------------------

    @property
    def drawdown_pct(self) -> float:
        """How far below the locked tier we've fallen (0 if at or above tier)."""
        if self.locked_tier <= 0:
            return 0.0
        dd = (self.locked_tier - self.current_bankroll) / self.locked_tier
        return max(dd, 0.0)

    @property
    def kelly_multiplier(self) -> float:
        """1.0 at/above tier, min_multiplier at/below floor, concave (sqrt) between.

        Shallow drawdowns are mostly noise in a 58% model (3 losses in a row is ~7%
        probability per window). Concave curve keeps Kelly near 1.0 for small
        drawdowns and reserves the aggressive cut for deep drawdowns that are more
        likely to reflect a real regime break.

        Comparison at midpoint of a $100 tier / $85 floor:
          Linear:  0.40 + 0.60 × 0.50        = 0.70
          Concave: 0.40 + 0.60 × √0.50 ≈ 0.82
        """
        b = self.current_bankroll
        if b >= self.locked_tier:
            return 1.0
        if b <= self.floor:
            return self.min_multiplier
        span = self.locked_tier - self.floor
        ratio = (b - self.floor) / span
        return self.min_multiplier + (1.0 - self.min_multiplier) * math.sqrt(ratio)

    def update_bankroll(self, amount: float) -> None:
        """Update current bankroll; ratchet the floor up if a new tier is crossed."""
        self.current_bankroll = amount
        if amount > self.peak_bankroll:
            self.peak_bankroll = amount
            logger.info(f"New high-water mark ${amount:,.2f}")

        new_tier = _locked_tier(amount)
        if new_tier > self.locked_tier:
            self.locked_tier = new_tier
            self.floor = round(new_tier * self.floor_pct, 2)
            logger.info(
                f"Tier locked ${new_tier:,.0f} -> floor ${self.floor:,.2f}"
            )

    def restore_from_peak(self, peak: float, current: float) -> None:
        """Restart restore: lock the tier/floor from the historical ``peak`` while
        leaving ``current_bankroll`` at the real (post-drawdown) balance.

        Encapsulates the two-step dance so callers can't get the ordering wrong:
        ``update_bankroll(peak)`` ratchets the tier up to the high-water mark, then
        we set ``current_bankroll`` back to the live balance so the drawdown Kelly
        multiplier applies. e.g. peak=$1000, restart at $700 → floor stays $850,
        ``kelly_multiplier`` reflects the $700 drawdown rather than resetting to 1.0.
        """
        self.peak_bankroll = max(self.peak_bankroll, peak)
        self.update_bankroll(peak)      # ratchet locked_tier/floor from the peak
        self.current_bankroll = current  # but size against the live balance

    # ------------------------------------------------------------------
    # Streak tracking (Discord alerts only — no effect on sizing)
    # ------------------------------------------------------------------

    def record_win(self) -> str | None:
        self.consecutive_losses = 0
        self.consecutive_wins += 1
        if self.consecutive_wins >= self.wins_to_restore:
            logger.info(
                f"{self.consecutive_wins} consecutive Ws — "
                f"kelly mult={self.kelly_multiplier:.2f}"
            )
            return "streak_wins"
        return None

    def record_loss(self) -> str | None:
        self.consecutive_wins = 0
        self.consecutive_losses += 1
        if self.consecutive_losses >= self.losses_to_reduce:
            logger.info(
                f"{self.consecutive_losses} consecutive Ls — "
                f"kelly mult={self.kelly_multiplier:.2f}"
            )
            return "streak_losses"
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset streak counters for a new trading day.
        Bankroll, locked_tier, and floor persist across days.
        """
        self.consecutive_losses = 0
        self.consecutive_wins = 0
