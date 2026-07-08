# TODO — open work only

Completed items get deleted; history lives in git + memory. The sniper is the
sole strategy; the BINDING deployment gate is the paper-shadow's realized fills,
not the harness (CLAUDE.md §2) — never go live on the harness print alone.

## Operator — commit + restart to apply this session's fixes

- [ ] **Commit + restart** to pick up the live booking fix (`live_trader.py`
      `_FILL_PRICE_LOOKUP_RETRIES` 3→8 / `_DELAY` 0.12 — books the CLOB's true
      fill VWAP instead of the padded FOK limit; the old ledger ran ~1-3¢/sh
      pessimistic on sniper fills). Won't self-heal on the old code.
- [ ] Confirm the earlier restart already applied the **stale-strike fix**
      (`_compute_strike_and_btc` Chainlink-preferred) and the **Chainlink 429
      reconnect-storm fix** (`chainlink_feed.py`). Log should show every
      `NEW WINDOW … (Chainlink)`; if not, restart.
- [ ] Run `python scripts/smoke_order_test.py --confirm` before any live flip —
      one unfillable $1 FOK proving order POSTs clear Cloudflare.

## The re-validation gate (currently `mode: paper`, `sniper_enabled: true`)

- [ ] Accrue **≥ 8 clean ET days** of paper-shadow sniper fills on the fixed
      code, then gate on the REALIZED fills (`sniper_shadow_status.py` /
      `live_health_read`): equal-weight net **≥ +2¢/sh**, `t_day ≥ 2`, `p10 > 0`,
      ≥ 40 fills, ≥ 6/8 days positive, AND **shadow-vs-harness gap < 3¢**.
      Only if that clears does live re-deploy. Honest prior: ~break-even with a
      fat left tail — it may not clear.
- [ ] **Validate the god-trader config `ask_cap 0.80` + `cb_move 12`** (was 0.92/8) on
      the paper-shadow's REALIZED fills. It's the SIM robustness PEAK (14d grid): net
      +15.9¢/sh, t_day 6.10 (highest), p10 +0.124, 14/14 days positive, ~27 fills/day —
      both changes express one principle (fire only on the biggest, cheapest crossings;
      expensive favorites win often but bleed on the flip). Confirm the gain materializes
      live; if paper underperforms, decompose (revert cb_move 12 first, it's the newer
      change). Don't chase tighter (0.70 / $16) — t falls, days-positive slips = overfit.
- [ ] Do NOT apply `sniper_fok_slip` 0.05→0.02 (verified: loses net-positive fills,
      avg_fill flat = no real chase cost to recover). The exit engine is protective —
      never force hold-to-resolution.

## Optional / later (one change at a time)

- [ ] Wallet cleanup (21 resolved $0 losers lock nothing): set `POLYGON_RPC_URL`
      + keep a little POL in the EOA, then `python scripts/redeem_positions.py
      --confirm`; the nightly sweep then keeps future windows clean.
- [ ] The ONLY speculative upside lever left is geographic latency
      (`docs/DEPLOY_ORACLE_VPS.md`): pick the box by feed + order latency SUM
      (~90ms EU feed + ~40ms Dublin/Stockholm order). It fires the sniper earlier,
      before mean-reversion — the one thing that could lift the adverse-selection
      leak. Never change infra and anything else in the same move.
