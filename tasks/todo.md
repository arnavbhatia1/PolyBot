# TODO — open work only

Completed items get deleted; history lives in git + memory. The sniper is the
sole strategy; the BINDING deployment gate is the paper-shadow's realized fills,
not the harness (CLAUDE.md §2) — never go live on the harness print alone.

## Operator — commit + restart cleanly to apply this session's fixes

- [ ] **Commit + restart.** The bot won't self-heal on the old code. This session
      shipped the 7/6 sniper regression fix: `sniper_fok_slip` 0.05→0.01 (the pad
      was letting the FOK chase reverting books — realized paper: clean fills
      +9.1¢/sh at 70% win vs chased fills −15.8¢/sh), and reverted `cb_move`→8 /
      `ask_cap`→0.92 to the realized-profitable 7/3-7/5 values (the 12/0.80 were
      SIM-grid values that never ran). Also removed the on-chain redemption
      subsystem (you claim winners manually; $0 loser stubs sit inert).
- [ ] After restart, confirm the log shows `NEW WINDOW … (Chainlink)` (strike fix
      active) and no Chainlink 429 reconnect storm.
- [ ] Before any live flip: `python scripts/smoke_order_test.py --confirm`
      (one unfillable $1 FOK proving order POSTs clear Cloudflare).

## The re-validation gate (currently `mode: paper`, `sniper_enabled: true`)

- [ ] Accrue **≥ 8 clean ET days** of paper-shadow fills ON THE FIXED CODE, then
      gate on the REALIZED fills (`sniper_shadow_status.py`): equal-weight net
      **≥ +2¢/sh**, `t_day ≥ 2`, `p10 > 0`, ≥ 40 fills, ≥ 6/8 days positive, AND
      **shadow-vs-harness gap < 3¢**. Only if that clears does live re-deploy.
      Honest prior: ~break-even with a fat left tail — it may not clear.
- [ ] Watch avg fill-slippage stays ≤ ~one tick. A drift back toward +0.03 means
      the FOK is chasing reverting books again — the exact 7/6 failure mode.

## Optional / later (one change at a time)

- [ ] The one speculative upside lever left is geographic latency
      (`docs/DEPLOY_ORACLE_VPS.md`): pick the box by feed + order latency SUM
      (~90ms EU feed + ~40ms Dublin/Stockholm order). It fires the sniper earlier,
      before mean-reversion — the one thing that could lift the adverse-selection
      leak. Never change infra and anything else in the same move.
