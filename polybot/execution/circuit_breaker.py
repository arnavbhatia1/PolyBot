"""Streak-based circuit breaker — halves Kelly after consecutive losses."""

import logging

logger = logging.getLogger("polybot")


class CircuitBreaker:
    def __init__(self, losses_to_reduce: int = 3, wins_to_restore: int = 2):
        self.losses_to_reduce = losses_to_reduce
        self.wins_to_restore = wins_to_restore
        self.consecutive_losses = 0
        self.wins_since_reduction = 0
        self.reduced = False

    @property
    def kelly_multiplier(self) -> float:
        return 0.5 if self.reduced else 1.0

    def record_win(self) -> str | None:
        self.consecutive_losses = 0
        if self.reduced:
            self.wins_since_reduction += 1
            if self.wins_since_reduction >= self.wins_to_restore:
                self.reduced = False
                self.wins_since_reduction = 0
                logger.info("CIRCUIT BREAKER: restored to full Kelly")
                return "restored"
        return None

    def record_loss(self) -> str | None:
        self.consecutive_losses += 1
        self.wins_since_reduction = 0
        if self.consecutive_losses >= self.losses_to_reduce and not self.reduced:
            self.reduced = True
            logger.info(f"CIRCUIT BREAKER: {self.consecutive_losses} consecutive losses — Kelly halved")
            return "reduced"
        return None

    def reset(self):
        self.consecutive_losses = 0
        self.wins_since_reduction = 0
        self.reduced = False
