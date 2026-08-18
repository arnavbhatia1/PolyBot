# REFUTATIONS.md — the graveyard

Everything here was killed by measurement, most of it by live evidence.
Binding: do not re-hunt, do not rebuild on paper evidence, do not soften into
"worth revisiting". If new live evidence contradicts an entry, that evidence
goes to RESEARCH.md first — the entry only moves after the new measurement
survives its own pre-registered bar. Dates are 2026.

## Strategy lanes

- **Post-close 0.99/0.999 camping** (08-13, live probe): 102 real placements,
  13 sh each, ZERO fills while 21,691 shares printed at exactly 0.990 during
  our own rests. ~290k-share wall at 0.99 queued from k≈43s BEFORE the close;
  61k-237k at 0.999 after the tick flip; the whole post-close pie ≈ $431/day
  across ALL participants. Time priority at a shared price is unwinnable at
  any join time we can reach.
- **Hairline-underdog inverse** (08-13, 14,897 windows): label-space richness
  (+9.2¢/sh) inverts to −2.2¢/sh when conditioned on the only tradable
  trigger (our displacement small at fire time); negative in every disp band.
  The book's final-seconds conviction is calibrated even on windows our
  arithmetic calls a coin flip.
- **Burst sniper under TWAP scoring** (08-07): −17.5¢/sh. Ripped out.
- **Open head-start leg** (08-10, 749 windows): decision rule anti-predictive
  (best cell = "model says don't buy"); Gamma never serves the strike first
  (0/757). Birthplace of the standing monotonicity bar (below).
- **Continuous P(win) body trading** (08-10): calibration excellent OOS and
  STILL loses to the CLOB ask on log score in 100% of splits; claimed-edge
  gradient inverts. The book wins the body; only the tail lock survives.
- **Vol-conditional margins** (08-10, 739 windows): introduce 6 windows where
  the LOSING side clears P≥0.995 vs 0 under frozen tables. Vol is not
  persistent at this timescale; both breach windows were dead-calm first.
- **Latency race / oracle-lag head start** (08-10, 464 sharp-move races): the
  book is spot-synchronised — reprices +0.33s after Binance, 2.5s BEFORE our
  oracle receipt, wins 97-100% of races. We are structurally not the fast
  participant; the edge is settled-outcome computation, not speed.
- **Multi-market expansion** (08-12, 24 windows/family): btc-15m 7.3
  post-close sh/window, eth-5m 1.8, xrp-5m 0.6, sol-5m 0.0 vs btc-5m's 151.5.
  All four siblings combined ceiling ~$16/day at impossible 100% capture.
  Scaling is SIZE on this one book.
- **Symmetric market-making at our capital** (08-11): 1-tick spread,
  4,416-share touch = 5× bankroll.
- **Dutch book / strike-ladder / negRisk arb** (07-02): zero fee-clearing
  arbs; "hit $X" is touch-either-direction (hump-shaped, not nested) — the
  pressure to find an edge manufactures fake arbs.
- **Entry-side feature/ML prediction** (multiple closures 06-09..07-16, and
  Hard Rule 1): all six lenses dead, G-M re-derives everywhere;
  filled-outcome records are poisoned for entry research. The census (see
  WALLETS.md Candidate B) shows wallets that DO have mid-window information —
  observed, out of mandate, not a license.
- **Exit engines** (07-01 passive exit −2.1¢/sh t −2.03; night-one scalp sold
  a winner at 0.05 seconds before it paid $1.00): every leg's edge was
  measured hold-to-resolution; there is deliberately NO sell path in the
  codebase. Any exit needs its own measured evidence AND new code.
- **deep_proj regime gate** (08-18, this session): the gate's target
  population is EMPTY under the corrected 60s-rule engine — engine-true
  replay of 08-14..17 (991 windows; the exact tape that motivated the gate)
  shows zero chop-regime full-sweep losses at every needs level from 2.0 down
  to 0.5 with k_place ≤ 25 (the only grid losses anywhere came from k>25
  arms). The 08-14..15 "massacre" losses the gate was designed to remove were
  wrong-rule artifacts: the bot traded the 30s stream for 4 days after the
  60s switch (wrong strikes med $0.3/p90 $3, wrong projection horizon, wrong
  post-close winner checks). The trailing-4-day dollars kill rule therefore
  no longer contradicts expected leg behavior; it stands unmodified.

## Methodology bans (they fake edges)

- **Snapshot queue_ahead modeling** (08-13): paper "validated" 77 fills/day
  the live book filled 0 of. Paper maker fills count ONLY prints strictly
  below the rung, plus at-price volume beyond the measured queue constant.
- **Per-print floor conventions in maker backtests** (08-17): the retired
  +33.8¢/4.5-day read tested each print against small late-k floors — trades
  the engine can never take. Ladder backtests arm at the configured placement
  window and REST (engine-true), or they are fiction.
- **Fill-weighting when within-window outcomes are correlated** (06-28):
  fakes significance; one bet per window is the honest unit.
- **Scoring fills vs 1Hz BBO** (06-30): fakes edges that survive t/LOO/
  bootstrap; event-true books only.
- **Blind counterfactual replays** (06-14): replays must re-decide BOTH
  directions branch-faithfully; exactly-0.0 deltas mean the replay is blind
  (an asymmetric replay nearly shipped −$370 as +$1,293).
- **The standing monotonicity bar** (08-11): every new leg must show net
  ¢/sh rising monotonically across model-edge buckets against an edge<0
  control. A candidate whose best cell is the control is anti-predictive no
  matter its aggregate.

## Superseded eras (not refuted — replaced by rule changes)

- **The 30s-TWAP resolution era** (08-07..08-13) and everything calibrated to
  it: the 564-window margin tables, the "2× floor is the regime filter"
  doctrine, the 08-17 engine-true grid ("k [6,25] arms the same windows as
  [6,8]"), the k≥6 breach history as stated. Polymarket moved resolution to
  the 60s stream at 08-14 00:00 UTC (see CLAUDE.md); 30s-era ¢/sh numbers are
  historical context only. The breach MECHANISM lessons carry (low-k tail
  bounds are unpinnable; p99.5 tiers break; max tier is the capital tier).
- **1723 as the deep_proj answer key** (frozen 08-14, superseded 08-18): its
  realized edge collapsed 20× at the rule change (WALLETS.md). The leg's
  evidence base is now its own engine-true record under the 60s rule.
