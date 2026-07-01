"""Long-horizon crypto calibration harness (measurement-only, no capital, no trading).

Standalone research surface that tests whether Polymarket's longer-horizon crypto
price-target markets (daily up/down ladders, weekly/monthly/yearly "hit $X" touch
ladders) carry a tradeable calibration edge. Built because the >1-week zone is NOT
retrospectively measurable (closed-market CLOB price-history is truncated to ~the
last week), so the edge must be recorded FORWARD.

Three things it measures, gated behind a pre-registered kill bar (see analysis.py):
  1. Forward-recorded calibration: market ask vs eventual resolution, event-clustered.
  2. The Deribit options cross-check: PM ask-implied prob vs option-IV-implied
     (one-touch / terminal-digital) probability — the instant "is it already arbed"
     test that can kill the edge in a single snapshot without waiting for resolution.

Decoupled from the trading engine: its own sqlite DB (polybot/db/calibration.db,
gitignored, local-only), no order placement, no WS, never touches polybot_*.db.
"""
from __future__ import annotations
