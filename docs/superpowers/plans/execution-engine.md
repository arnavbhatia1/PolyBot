Trade Execution Engine
What it covers:

Maker order → FOK fallback flow (60s timeout)
snap_to_tick() implementation correctness
Convex slippage model: fill_pct * impact * (1 + fill_pct)
65/35 maker/FOK blend in paper mode realism
Entry fee in shares vs exit fee in USDC (asymmetry handling)
GET /price vs raw book (negRisk cross-matching)
50% max book depth cap mechanics
Three rejection gates before exchange interaction
TradeResult / FillResult contract boundaries
Paper vs live behavioral parity (what can diverge?)
EIP-712 signed order flow in live mode

Why it's #13: Execution is where theoretical edge meets real slippage. Paper/live divergence here means paper results are fiction.