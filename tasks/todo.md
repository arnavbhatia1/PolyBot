# POLYBOT ROADMAP — LEAN KILLING MACHINE (BTC ONLY)

**Open work only.** Completed phases and dated history live in git + memory, not
here. This file is the forward roadmap and kill-bar status.

**Scope:** BTC 5-min markets only. Multi-asset expansion is the single TODO after
this goal completes — do not start it early.
**Constraint:** No phase ships to live capital before its kill bar passes. Zero
exceptions.
**Authority:** kill bars are the deployment authority. For exit-policy questions
the CounterfactualTracker records (both arms per scalp/hold) are ground truth —
score via `actual − cf` / `scalp_was_optimal`, never a naive signed sum of the
stored `delta_pnl`.

---

## ESTABLISHED FACTS (constrain all future work)

1. **No entry-side information advantage EARLY/MID-window.** The CLOB price beats
   the model at entry (k=0, 44/44 segments, day-clustered t≈3.7–4.4 against).
   Entry is inventory sourcing; never rebuild early/mid entry prediction. The
   final-seconds late-window sniper is the one sanctioned exception, gated by its
   own kill bar.
2. **The base (entry + exit-engine) strategy has NO proven edge — measured, not
   pending.** Binding ≥10-clean-day read landed 2026-07-01 and FAILED on every
   defensible cut (act−cf +$32/day, t_day +1.07; strict 9d t +1.48; per-$ t −0.20;
   p10 −$4.14; lean concentrated in two adjacent days, drop-best t +0.51). ~35
   clean days needed at current SNR just to reach t≥2, and the clean series is
   closed (06-30 mixed-regime; 07-01+ new fill-realism regime). **The base
   strategy never deploys live** — `sniper_only: true` suppresses it in the live
   recipe while its evidence stream keeps accruing as ghosts. Realized paper P&L
   swings are BTC-vol variance, not edge.
3. **Depth ceiling is real.** Top-5 book is $50–2k/side; per-scalp size caps
   ~$100–300; bankroll stops mattering past ~$5–10k. The only multiplier is
   parallel markets (post-goal).

---

## THE BINDING GO-LIVE GATE: the late-window sniper kill bar

### Late-window directional sniper — BUILT, shadow-validating, blocked ONLY by n_days

The one bot-formable edge: when a Coinbase move over ~2s pushes price past strike
in the final 45s and the chosen side's ask hasn't repriced (~350ms half-life),
buy that side before the CLOB reprices. Entry path built + adversarially reviewed
+ unit-tested; live in PAPER SHADOW since 06-30 (`sniper_enabled: true`,
`mode: paper`).

**Kill bar (all legs, at the host's MEASURED RTT):** momentum `t_day ≥ 2.0` AND
block-bootstrap `p10 > 0` over **≥ 8 clean ET days, ≥ 6 positive, ≥ 40 fills**,
net of the 0.07 fee, executable asks, CONTROL (spot-side@ask) ~0. PLUS a
paper-shadow span whose realized fills track the harness (live trades the
higher-conviction `sniper_min_edge` subset, so the harness is a conservative
directional gate).

**Read 2026-07-01 ~18:15 ET (7 ET days 06-25→07-01, 07-01 partial):**
- Lenient (`--max-slip 0.05`): 416 fills, win 77.9%, net +0.0753/sh,
  **t_day +3.67, p10 +0.0460**, 6/7 days positive.
- Strict (`--max-slip 0.005`): 376 fills, win 76.1%, net +0.0561/sh,
  **t_day +2.26, p10 +0.0233**, 6/7 days positive.
- Control ~0 both limits. Every statistical leg PASSES; the sole blocker is
  **n_days = 7 < 8**.
- Shadow: 3 fills, 3/3 wins, +$82.47; sides agree 3/3 with the harness; fill
  prices track ≤2¢.
- **Watch — per-day decay:** lenient +0.0722 → +0.0641 → +0.0453 (declining,
  decelerating; recent days well below the 06-26/27 peak ~+0.125). The mechanism
  is publicly documented (Feb–Mar 2026 articles) and the Jun-1 CLOB rate-limit
  raise lowers the barrier for competing flow — if 07-02 prints below ~+0.03/sh
  lenient the downtrend is confirmed. Let the bar bind honestly; the answer to a
  failed bar is "not yet," never "lower the bar."

**Next reads:**
- [ ] **2026-07-03 (morning, after the 07-02 ET day completes):** re-run
      `python scripts/analyze_late_window.py --rtt-sweep 0.135 --max-slip 0.05`
      (and `--max-slip 0.005`). First read that can satisfy n_days ≥ 8. If it
      passes and the operator waives/short-cuts the shadow-span leg, go live per
      the runbook below; otherwise
- [ ] **~2026-07-08:** shadow-span leg (`python scripts/sniper_shadow_status.py`
      vs the harness at 0.135) — ~10–15 expected shadow fills; fidelity tracking,
      not an independent t-test. Operator's call on span sufficiency.

**Go-live runbook once the bar passes (all operator-run, in order):**
1. `python scripts/verify_keys.py` — GET-auth + balance/allowance preflight
   (07-01 check: authenticated OK, $130.63 USDC, unlimited allowance — top up if
   a larger live stake is intended).
2. `python scripts/smoke_order_test.py --confirm` — proves the ORDER-POST path
   (EIP-712 sign + POST through Cloudflare) with one unfillable $1 FOK;
   verify_keys covers GETs only. Can be run any day before launch.
3. Note the smoke test + first live FOK success rate vs the
   `paper_network_fail_rate` 0.03 estimate (calibrate it from live volume).
4. Stop the bot. Optional fresh baseline: `python scripts/reset_paper_clean.py`
   (operator-run, bot STOPPED).
5. settings.yaml: `mode: live` + `late_window.sniper_only: true`
   (`sniper_enabled` is already true). That is the complete flip — paper and
   live share every decision path (verified 07-01: zero mode-conditional
   decision branches).
6. Relaunch via `.\scripts\run_polybot.ps1`; watch the first fills vs the
   harness (`scripts/sniper_shadow_status.py`) and the `latency_stats` sign/post
   percentiles.

**Known quirks (documented, deliberate non-blockers — revisit only if they bind):**
- First live boot computes the allowance-preflight floor from the pre-sync DB
  bankroll (`initial_bankroll` → $160 floor), not the on-chain balance; moot with
  an unlimited allowance. Same for the circuit-breaker seed (tier ratchets up on
  the first close; sizing unaffected).
- Paper lacks live's $1-notional order-time re-check (decision-path $1 gates are
  shared; only a rare pre-check→order-time race diverges — paper slightly
  optimistic there).
- Paper's warm-SELL latency discount constant (0.15s, floor-bounded to ≤ ~15ms
  effective) overstates live's measured ~3–5ms sign cost — retune only AFTER the
  shadow-span read (never perturb paper realism mid-measurement; the 06-30
  mixed-regime day is the standing lesson).
- settings.yaml says "Ireland-VPN" where docs/DEPLOY_ORACLE_VPS.md says "Atlanta
  VPN" for the same measured ~118ms — operator to reconcile the label; the
  measured RTT (warm 119–123ms re-probed 07-01) matches calibration either way.

---

## WATCH ITEMS (no action until they move)

- **Gamma `/events` deprecation:** the endpoint is 2 months past its Sunset
  header; all single-slug lookups auto-fall-back to the undeprecated
  `GET /events/slug/{slug}` (`gamma_events_by_slug`, latched after first
  enforcement). Open residual: the calibration monitor's offset paging
  (`calibration/discovery.py::discover`) has no keyset fallback —
  measurement-only, fails loud in its own log; migrate to `/events/keyset`
  only if Gamma enforces.
- **CFTC investigation into Polymarket (reported 06-26):** marketing-conduct
  scope, no market-structure implication — but a real custody tail risk. Keep
  on-platform bankroll at a level whose total loss is acceptable.
- **Rumored 1-minute windows** (unconfirmed, March press): would shift bot/
  liquidity attention if launched. Watch only.
- **VPS option** (`docs/DEPLOY_ORACLE_VPS.md`): Stockholm free box ≈ ~40ms RTT
  (~3× better than the current ~120ms) — strengthens the latency-sensitive
  sniper. Do not change infra and flip to live in the same move.

---

## ONE REMAINING TODO (post-goal)

Expand to ETH, SOL, XRP 5-min markets. Architecture is parameterized; execution
is a symbol loop. Do not start until the BTC goal is complete and all kill bars
have held in production ≥7 days.

---

## WHAT YOU ARE NOT ALLOWED TO DO

- Add any EARLY/MID-window entry-side prediction logic (ML or rules) — dead, G-M
  holds. (The final-45s late-window sniper is the one sanctioned exception, and
  only through its kill bar.)
- Deploy the base strategy live — its gate FAILED on the binding 07-01 read.
- Expand to non-BTC markets before this goal is fully complete.
- Deploy any phase to live capital before its kill bar passes.
- Relax a kill bar to pass it — the answer to a failed bar is "not yet," never
  "lower the bar."
- Preserve deleted code in comments or dead branches.
- Rebuild symmetric market-making, the wide-quote maker sleeve, or passive-exit
  resting (measured −$62/day, t_day −2.03), under any name.
- Treat the oracle cadence or Chainlink heartbeat as a tradeable signal.
- Fill-weight any analysis where within-window outcomes are correlated — one bet
  per window, day-clustered.
