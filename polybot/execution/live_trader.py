"""Live trader stub — polymarket.com CLOB trading requires EIP-712 signed orders.

Paper mode works with real CLOB order book prices. Live .com trading is future work.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LATENCY OPTIMIZATION NOTES (for live implementation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Paper trader fills in ~2ms. Real execution is ~200-500ms. On 5-min
# contracts, that gap can erase your edge. Target: <120ms signal-to-fill.
#
# --- BIGGEST WINS (save 100-200ms) ---
#
# 1. PRE-SIGN ORDERS
#    EIP-712 signing is pure CPU. Keep the signing key hot in memory
#    and pre-compute partial signatures. Don't do crypto ops after
#    the signal fires.
#
# 2. PERSISTENT HTTP CONNECTIONS
#    Don't open a new TCP+TLS handshake per order. Use aiohttp
#    connection pooling with a keep-alive session to Polymarket's
#    CLOB endpoint. Saves ~80-150ms per request.
#
# 3. PRE-BUILD ORDER TEMPLATES
#    Have the order JSON 90% assembled before the signal. When signal
#    fires, just slot in price/size/side and send.
#
# --- MEDIUM WINS (save 30-80ms) ---
#
# 4. COLOCATE NEAR POLYMARKET
#    A small VPS in the same AWS region as Polymarket (likely us-east-1)
#    cuts network round-trip vs running from a home PC.
#
# 5. OPTIMISTIC ORDER PREP
#    If Layer 1 (CDF) alone shows a big edge, start preparing the
#    order while Layers 2-4 compute. Cancel if they disagree.
#
# 6. SKIP /price HTTP CALL AT ENTRY
#    You already have the book from WebSocket. Compute the walk-through
#    price locally from in-memory book state instead of another HTTP
#    round-trip. Save that ~100-200ms.
#
# --- SMALLER WINS ---
#
# 7. Check if Polymarket supports WebSocket order submission (avoids
#    HTTP overhead entirely for order placement).
#
# 8. Ensure no pre-checks (tick size, fee rate, min order size) trigger
#    a synchronous HTTP fetch at order time — they're already cached.
#
# --- TARGET ARCHITECTURE ---
#
#   WebSocket update arrives
#     → signal_engine computes edge (~5ms)
#     → if edge passes gates:
#         → grab pre-built order template (0ms)
#         → slot in price/size from local book state (no HTTP)
#         → sign with hot key (~10-20ms)
#         → fire on persistent connection (~50-100ms to server)
#     → realistic floor: ~60-120ms signal-to-fill
#
# The bot's 10%+ min-edge requirement helps here — you're not chasing
# tiny arbs that vanish in milliseconds. Structural mispricings from
# the fat-tail model persist longer than speed arbs. But order-flow
# signals (Layer 3) decay fast, so speed still matters.
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
