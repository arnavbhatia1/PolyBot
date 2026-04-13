PolyBot — Kelly Sizing & Bankroll Management: Critical Review + Implementation Spec
Honest Assessment
Is it good enough as-is? No. The concepts are sound. The implementation spec has 4 genuine bugs and 1 critical spec gap that will cause real money loss or silent miscalibration. The rest is solid — don't touch it.

Critical Issues Only (5 Real Problems)

BUG 1: Cap Chain Ordering — Sub-Economic Positions Can Execute
Current order (from CLAUDE.md):
Copysize < $0.10 → REJECT
size > 80% bankroll → cap
size > 12% bankroll → cap  
size > 50% depth → cap
The bug: Minimum size check runs before caps. A $0.25 position passes the $0.10 gate, then depth-cap reduces it to $0.06, then it executes at $0.06 — buying a position that can never recoup its entry fee.
Fix: Minimum size check runs last, after all caps are applied.

BUG 2: Uncertainty Discount Blows Up for Early Trades
Current formula: f* = f × (1 - σ²_edge / edge²), floor=0.50
The math: σ²_edge = p(1-p)/n ≈ 0.25/n for binary outcomes.
At n=10, edge=0.06: ratio = (0.25/10) / 0.0036 = 6.94
1 - 6.94 = -5.94 — a negative multiplier.
What the floor=0.50 probably intends: max(0.50, multiplier) — but if the formula returns -5.94 and you clamp to [0,1] before the floor, you get 0.0x then floor to 0.50x. If you don't clamp, behavior is undefined/implementation-dependent.
The deeper issue: At n<15, σ²/edge² > 1 always, meaning the formula is always below zero. The floor saves you but makes the formula meaningless in the early regime — it's just "bet 50% of Kelly for first ~15 trades regardless of anything." That's actually fine and conservative, but it needs to be intentional and documented, not an accidental clamp.
Fix: Explicit clamp to [0.0, 1.0] before applying floor. Document that floor=0.50 is the early-trade minimum, not a safety net for a formula gone negative.

BUG 3: Wilson CI Win Rate Threshold Is Unspecified
What CLAUDE.md says: Kelly ratchets 0.15 → 0.18/0.22/0.25 at 200/400/750 trades using Wilson score 95% CI lower bound.
What's missing: What win rate must the Wilson lower bound exceed to advance a tier?
Without this threshold, compute_kelly_tier() either:

Advances any time you hit the trade count (Wilson CI is decorative)
Has a hardcoded threshold buried in code that nobody can tune

This is a spec gap that makes the ratchet logic unverifiable and untunable by the pipeline.

BUG 4: Ratchet Has No Downward Path Except Velocity Trigger
Current downward protection: Drawdown velocity (rolling 25-trade PnL < -15%) resets to base Kelly.
What it misses: Slow edge decay. If win rate drifts from 58% → 51% over 300 trades, the velocity trigger never fires (no single 25-trade window drops hard enough). But the strategy is now running at the top Kelly tier (0.25) on a deteriorated edge.
The edge half-life tracker catches this at the strategy level, but doesn't feed back into compute_kelly_tier().
Fix: compute_kelly_tier() evaluates downward demotion at each call using the same Wilson lower bound logic. If current tier's win rate threshold is no longer met by the Wilson lower bound, step down one tier. Add tier_demotion_cooldown_trades: 50 to prevent thrashing.

BUG 5: fill_pct Definition Ambiguity in Convex Slippage
Current formula: slippage = fill_pct × impact × (1 + fill_pct)
The ambiguity: fill_pct is referenced but never defined in CLAUDE.md. Two plausible interpretations:

A: position_size_dollars / book_depth_dollars (fraction of available liquidity consumed)
B: position_size_shares / total_available_shares_at_best_price (fraction of top-of-book consumed)

At $50 book depth, $10 position: interpretation A = 0.20, interpretation B could be 0.60 if best bid is thin.
With impact=0.03: interpretation A slippage = 0.20 × 0.03 × 1.20 = 0.72%, interpretation B = 0.60 × 0.03 × 1.60 = 2.88%
A 4x difference means the net-edge gate is either rejecting good trades or approving bad ones.

What Is Solid — Don't Touch

The uncertainty discount concept (even with the clamp fix, the math is correct)
The drawdown velocity trigger (25-trade window is well-calibrated for 5-min contracts)
The conviction multipliers (applied, thresholds pipeline-tunable — correct)
The concurrent position discount (0.45x is conservative vs theoretical 0.524x — appropriate for unvalidated ρ assumption)
The bankroll percentage caps (80% total, 12% single — correct order relative to each other)
The maker/FOK fee blend (65/35 with 0% or full taker fee — correctly captures uncertainty)