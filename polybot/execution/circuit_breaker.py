"""Drawdown-based circuit breaker — scales Kelly proportionally to drawdown from peak bankroll."""
from __future__ import annotations

import logging

logger = logging.getLogger("polybot")


class CircuitBreaker:
    """Scales Kelly multiplier based on cumulative drawdown from peak bankroll.

    At 0% drawdown, Kelly multiplier is 1.0 (full sizing).
    At ``max_drawdown_pct`` or deeper, multiplier bottoms at ``min_multiplier``.
    Linear interpolation between. Recovery is automatic — as bankroll climbs
    back toward the high-water mark, drawdown shrinks and Kelly scales up.

    Streak tracking is kept for logging/Discord alerts but does NOT drive sizing.
    """

    def __init__(
        self,
        initial_bankroll: float = 1000.0,
        max_drawdown_pct: float = 0.15,
        min_multiplier: float = 0.25,
        # Legacy params accepted for backward compat (ignored for sizing)
        losses_to_reduce: int = 3,
        wins_to_restore: int = 2,
    ) -> None:
        # Drawdown tracking
        self.peak_bankroll: float = initial_bankroll
        self.current_bankroll: float = initial_bankroll
        self.max_drawdown_pct: float = max_drawdown_pct
        self.min_multiplier: float = min_multiplier

        # Streak tracking (for logging/Discord only)
        self.consecutive_losses: int = 0
        self.consecutive_wins: int = 0
        self.losses_to_reduce: int = losses_to_reduce
        self.wins_to_restore: int = wins_to_restore

    # ------------------------------------------------------------------
    # Core: drawdown-based Kelly scaling
    # ------------------------------------------------------------------

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown as fraction of peak bankroll (0.0 = at peak)."""
        if self.peak_bankroll <= 0:
            return 0.0
        dd = (self.peak_bankroll - self.current_bankroll) / self.peak_bankroll
        return max(dd, 0.0)

    @property
    def kelly_multiplier(self) -> float:
        """Kelly multiplier scaled linearly from 1.0 (no drawdown) to
        ``min_multiplier`` (at ``max_drawdown_pct`` or deeper)."""
        dd = self.drawdown_pct
        if dd <= 0:
            return 1.0
        if dd >= self.max_drawdown_pct:
            return self.min_multiplier
        # Linear interpolation: 1.0 at dd=0, min_multiplier at dd=max_drawdown_pct
        return 1.0 - (1.0 - self.min_multiplier) * (dd / self.max_drawdown_pct)

    def update_bankroll(self, amount: float) -> None:
        """Set the current bankroll and recalculate drawdown / Kelly multiplier.

        If the new amount exceeds the previous peak, the high-water mark is
        updated and drawdown becomes 0%.
        """
        self.current_bankroll = amount
        if amount > self.peak_bankroll:
            self.peak_bankroll = amount
            logger.info(
                f"CIRCUIT BREAKER: new high-water mark ${amount:,.2f}"
            )
        dd = self.drawdown_pct
        mult = self.kelly_multiplier
        logger.debug(
            f"CIRCUIT BREAKER: bankroll=${amount:,.2f} peak=${self.peak_bankroll:,.2f} "
            f"drawdown={dd:.1%} kelly_mult={mult:.2f}"
        )

    # ------------------------------------------------------------------
    # Streak tracking (for Discord alerts — does NOT affect sizing)
    # ------------------------------------------------------------------

    def record_win(self) -> str | None:
        """Record a winning trade. Returns an event string for Discord or None."""
        self.consecutive_losses = 0
        self.consecutive_wins += 1
        event = None
        if self.consecutive_wins >= self.wins_to_restore:
            event = "streak_wins"
            logger.info(
                f"CIRCUIT BREAKER: {self.consecutive_wins} consecutive wins — "
                f"kelly_mult={self.kelly_multiplier:.2f}"
            )
        return event

    def record_loss(self) -> str | None:
        """Record a losing trade. Returns an event string for Discord or None."""
        self.consecutive_wins = 0
        self.consecutive_losses += 1
        event = None
        if self.consecutive_losses >= self.losses_to_reduce:
            event = "streak_losses"
            logger.info(
                f"CIRCUIT BREAKER: {self.consecutive_losses} consecutive losses — "
                f"kelly_mult={self.kelly_multiplier:.2f}"
            )
        return event

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset streak counters for a new trading day.

        Bankroll / peak are NOT reset — drawdown persists across days.
        """
        self.consecutive_losses = 0
        self.consecutive_wins = 0
