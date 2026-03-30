from dataclasses import dataclass


@dataclass
class TradeResult:
    success: bool
    position_id: int | None = None
    reason: str = ""
    log_return: float | None = None
