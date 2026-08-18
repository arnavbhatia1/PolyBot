# RESEARCH.md — open problems, ranked

The living successor to each session's charter. Every entry carries a
hypothesis, a pre-registered bar, and a status. Every frozen measurement in
the codebase is registered at the bottom with its reopening condition —
re-measuring against a bigger corpus or better estimator is NOT relaxing a
bar; lowering a threshold to make a trade fire is. REFUTATIONS.md is binding;
WALLETS.md is the census. Dates 2026.

## The 08-14 rule-change incident (context for everything below)

Polymarket silently moved resolution from the 30s to the 60s TWAP stream at
08-14 00:00 UTC (market descriptions now cite btc-usd-twap-60s-streams; RTDS
topic `crypto_prices_twap_sixty` bit-matches served finals — live-verified
08-18). The bot traded the wrong stream for 4 days. Both watchers missed it:
the chain invariant (final == next strike) is source-internal, and the
bit-exact tape check existed only inside the heavy sim read that was timing
out nightly, dropped by the ping formatter, plus a per-window drift WARNING
that only ran on held positions and only logged. Fixed on branch
research/60s-rule: feed re-pointed, 60s projection + re-frozen tables, raw
coverage guard, and a dedicated nightly SOURCE watch (mechanism_read) that
turns red the same night. Detection lesson: **an invariant both sides of
which come from the counterparty cannot detect the counterparty changing.**

## Ranked queue

1. **deep_proj paper validation under the corrected engine** — config staged
   08-18: need 0.5, k_place [6,25], 60s tables. Hypothesis: the corrected
   sign (873/873 armed windows) + rung-price cushion turns deep winner-side
   panic into ~2 fills/day at 100%-ish win. Bar (§2, unchanged): ≥6 clean ET
   days, ≥20 filled windows, EW ≥ +5¢/sh, usd_per_day > 0, judged on realized
   paper fills since the re-pinned epoch. At the observed 1.8 fills/day this
   needs ~11 days. Status: waiting on deploy + tape.
2. **60s margin-table re-fit at ≥14 real-final days** — the 08-18 freeze
   stands on 970 real-final windows (3.3 days; p99.5 ≈ 5th-from-top order
   stat) + synthetic max-union. Reopen automatically when the corpus reaches
   ~14 real days (~4,000 windows); re-fit with ws1_measure60 conventions
   (rx-clock ZOH + 10s coverage guard, one sample per window per k). Also
   re-derive `need` for the ladder from the fresh tables.
3. **Taker under the 60s rule** — the harness (new tables, zone 60) reads 14
   fills / 93% win / EW +4.18¢/sh / 579 kills on 08-14..17 tape — a CEILING,
   and one losing fill whose tier must be identified before trusting max-tier
   semantics (if that loss was max-tier, the never-breach premise needs the
   k-region mapped out before any live thought). Status: fire-level dump in
   flight 08-18; then the paper shadow accrues alongside deep_proj.
4. **Bridged projection for the taker** — 08-18 measurement: bz-bridge p99.5
   ≤ plain at 12/13 knots (one ~tie), max never wider on real-final days;
   kline-sim (validated ≡ live bz at p99 $0.000) tighter at ALL knots on the
   full corpus but max WIDER at k=25/35 on synthetic days. Bar to adopt: on
   ≥14 real-final days, paired, bz p99.5 ≤ plain at EVERY k ≥ 6 AND bz max ≤
   plain max at every k ≥ 6. Until then the taker stays plain (tables are
   plain-measured). Status: measured, provisionally positive, not shipped.
5. **Candidate A — cushion dip-buyer** (WALLETS.md): both-sides deep rungs
   0.10-0.35, no sign filter. Bar written in WALLETS.md (win ≥ price+8pp per
   rung ≥10 fills, positive dollars in two disjoint 3-day splits, and the
   sign-gated variant must not dominate). Status: observed in three wallets
   through both eras; not implemented.
6. **k_place_max under the 60s rule** — 79% of deep winner-side volume prints
   at k>25 but it is contested flow (99.9% prints before ANY 2×-floor clear;
   the only 08-18 grid losses came from k>25 arms at need 0.5). Reopen only
   with a mechanism that prices contested flow (e.g. Candidate A), not by
   extending the sign-gated ladder.
7. **twap_k_min_s 6.0** — carried from the 30s era by charter decision. At
   ≥14 real days, measure k ∈ [2,6) knots on the 60s rule (08-18 read:
   p99.5 ≈ $0.7-0.9, max ≈ $1.5-1.6 gated — pinnable-looking but the 30s era
   taught exactly this overconfidence at low k). Proposal-only either way.
8. **Queue-constant estimator discrepancy** — sweep-consumed depth at deep
   levels is stable (med 19-46 sh, p75 62-120, pooled med 31, NO trend across
   11 days) while the book-resting watch grew 55→135. The shipped 135
   over-states typical queues → paper under-credits at-price fills →
   conservative, correct direction. Reopen only if live deep fills land that
   paper refuses to credit (recalibrate from `filled_at_px` live/paper
   attribution, never from book snapshots).
9. **Maker rebates at scale** — proven real (~0.4%/day of maker notional on
   1723's ledger). An adder after a bar passes, never a strategy.
10. **Census cadence** — re-run WALLETS.md census weekly during validation
    and after any regime break; the 08-14 leaderboard reshuffle was visible
    in one day of counterparty data.

## Frozen-measurement register

| constant | value | frozen | corpus / estimator | reopening condition |
|---|---|---|---|---|
| TWAP_MARGIN_P995/_MAX | signal_engine.py | 08-18 | 970 real-final windows (08-14..17) + 1,651 synthetic max-union; rx-clock ZOH + 10s coverage guard | ≥14 real-final days, or any resolution-rule change (mechanism_read red) |
| ladder need | 0.5 | 08-18 | pre-registered grid, 08-14..17 engine-true (7/7 wins +$81, ANTI −$17k) | the §2 paper bar verdict; any fill-conditional loss cluster |
| k_place [6,25] | settings | 08-18 | same grid (k>25 arms = the only losses) | queue-6 above |
| AT_PRICE_QUEUE_SH | 135 sh | 08-17 | live book watch (49 windows) vs sweep-consumed med 31 (11 days, stable) | live at-price fills paper refuses to credit |
| twap_k_min_s | 6.0 | 08-12 scar (30s era) | k=1.1 realized max-tier breach | queue-7 above |
| bz relay lag | p50 0.421s | 08-18 | 74,184 bz records rx−ts | new relay behavior |
| GTC/taker latency tables | paper_trader | 08-07..08 | box-measured | any Polymarket pipeline change (smoke tests) |
| kelly_fraction 0.08, maker_bankroll_frac 0.15 | settings | pre-era | post-gate playbook | after a §2 bar pass |
| fee model 0.07 / 0.0175 | base.py | 07-22 | 1,751 live fills vs documented curve | Polymarket fee change |

## Tooling (session 08-18, scratchpad scripts — rebuild from these names)

`ws1_reduce.py` (micro-tape → per-window streams), `ws1_measure60.py` (error
tables, all estimators), `ws1_freeze_tables.py`, `ws1_boundary_autopsy.py`
(which stream instant equals the served final — run this FIRST on any future
mechanism alarm), `ws2_ladder_replay.py` (engine-true grid + ANTI controls),
`ws2_supply*.py` (panic-supply attribution), `ws3_census.py` /
`ws3_behavior.py` / `ws3_queue.py` (WALLETS.md), `klines_download.py` /
`pm_trades_download.py` (Binance 1s mirror; data-api both-counterparty pull,
offset caps at 3,500 rows/window).
