# TODO — open work only

Completed items get deleted; history lives in git + memory. Kill bars are the
deployment authority — never relax one to pass it.

## Operator

- [ ] Run `python scripts/smoke_order_test.py --confirm` — one unfillable $1 FOK
      proving order POSTs clear Cloudflare (`verify_keys.py` covered GETs only).

## Scheduled reads

- [ ] ~07-07 — delta-lead OOS re-read on `late_window_collect.db`
      (bar: OOS day-clustered t ≥ 2 AND p10 > 0 AND +300ms column positive).
      The DB exists only for this read — delete it after.
- [ ] ~07-08 — shadow-span fidelity read: `python scripts/sniper_shadow_status.py`
      vs the harness at 0.135; weight the post-07-03 (sniper_only) fills.

## After the first live fills

- [ ] Capture one real `get_order` JSON → verify the `_order_fully_filled` field
      names (resting-exit path only, currently disabled).
- [ ] Re-read `latency_stats.json` after a day of kill-RTT recording (07-05 fix)
      and nudge `paper_latency_*` if the live distribution disagrees.
- [ ] Revisit `paper_network_fail_rate` (0.03) at ~100 live POSTs
      (0 network errors in the first 29 — consistent so far).

## Later (one change at a time)

- [ ] VPS move (`docs/DEPLOY_ORACLE_VPS.md`): pick the box by feed + order
      latency SUM (Coinbase ~90ms from EU + order ~40ms Stockholm/Dublin) — it
      fixes both legs at once. Never change infra and anything else in the same
      move.
- [ ] Post-goal: expand to ETH/SOL/XRP 5-min markets — only after the BTC kill
      bar has held in production ≥ 7 days.

Candidate edges ($20/10s move overlay; cbm 5 / cap 0.96 loosening) deploy only
through their own forward kill bar — never off discovery data.
