Risk Management: Critical Issues Only
Overall: The system is well-designed. Three real problems, rest is fine.

🔴 Critical #1 — Circuit Breaker Uses Initial Principal, Not Peak
Problem: After bankroll doubles, you lose all drawdown protection on gains. Bot grows $100→$200, crashes to $140 — circuit breaker never fires because $140 > $100 initial.
Fix: Track high_water_mark. Drawdown from the higher of (initial principal, peak). Use a blended floor: max(initial_principal, peak * 0.70) as the reference point.

🔴 Critical #2 — Asymmetry Is Backwards
losses_to_reduce: 3, wins_to_restore: 2 means you recover faster than you protect. In a choppy regime you'll oscillate between reduced/normal Kelly rapidly.
Fix: Flip it. losses_to_reduce: 2, wins_to_restore: 4. Fast to cut, slow to restore.