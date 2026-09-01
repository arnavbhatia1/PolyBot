# Venue truth — btc-updown-5m, as of 2026-08-31

Charter §1 (Phase 0). Sources: docs.polymarket.com (changelog, fees, rewards,
rate-limits, matching-engine pages), Gamma/CLOB API live reads 08-31,
status.polymarket.com, X/@PolymarketDevs, third-party coverage. Full citations
in the session transcript; every load-bearing claim was read from a primary
page or a live API response on 08-31. Tags: [measured] fetched/quoted,
[inferred] computed from documented formula, [spec] unconfirmed.

## 1.1 Rebates — the program pays our pattern $0.00; no DEAD ENDS entry flips

- Liquidity rewards ARE live on btc-5m today [measured]: the live window's
  `clob.polymarket.com/rewards/markets/{cid}` shows `rewards_daily_rate 10000`
  (USDC.e), i.e. ~$34.7/window prorated ≈ the published $300k/August BTC-5m
  pool. Only that endpoint shows it — Gamma `clobRewards` is empty on live 5m
  windows, CLOB `/markets` mirrors `rates: null`, `total_rewards: 0` still lies.
- Scoring [measured, docs verbatim]: `S = ((v−s)/v)²·b`, per-minute random
  sampling, pro-rata across makers. Midpoint outside [0.10, 0.90] → one-sided
  liquidity scores **exactly zero**; inside, one-sided scores /3. Params on the
  live window: `min_size 50` shares, `max_spread` 1.5¢ (4.5¢ on the CLOB
  mirror/pre-live records — binding live value 1.5¢ until re-measured).
- Our ladder against that [inferred]: at deployment the mid is ≥~0.90 by
  construction → one-sided Q ≡ 0; four of five rungs (< 50 sh at $60 budget)
  fail min_size; every rung sits 10–70¢ from mid vs a 1.5–4.5¢ max_spread.
  Zero on three independent clauses. Maker rebate (20% of crypto taker fees,
  `rebateRate 0.2` [measured]): a full 5-rung sweep ≈ $0.52 rebate [inferred]
  — under the $1 daily payout floor, which forfeits (no rollover) [measured].
- Two-sided compliant quoting taps a real ~$10k/day family pool but pro-rata
  against multi-thousand-share incumbent queues under closeness-squared
  weighting → $10–100/day [spec] bought with all-day two-sided inventory —
  the refuted symmetric-MM position (08-11: 1-tick spread, touch = 5×
  bankroll). **Verdict: rebates flip none of symmetric MM (08-11), mid-window
  touch-bid (08-21), post-close camping (08-13).** Those died on queue
  position and inventory risk, not on missing subsidy; the subsidy our
  pattern can actually collect is $0.
- **Expiry risk TODAY**: the program is published "through the month of
  August" [measured]; no September announcement either way. The whole-window
  walls (pocket C, plausibly reward-farming [data h1_report.md]) may thin from
  09-01 → the at-price queue constant (135 sh) and wall structure should be
  re-read next week. Re-read `rewards/markets/{cid}` on 09-01.

## 1.2 Rule risk — 60s TWAP confirmed stable; tripwire identified

- Today's markets carry `resolutionSource:
  data.chain.link/streams/btc-usd-twap-60s-streams` [measured, Gamma live
  read]; description text = "TWAP … greater than or equal to" (tie → Up,
  matches code). 15m and 4h siblings cite the SAME 60s stream [measured] —
  deep_proj's projection math transfers to btc-15m unchanged (see verdict memo).
- Change history: the 08-14 30s→60s move exists ONLY in the docs changelog —
  zero X posts, zero press [measured]. Rule changes land silently; the
  changelog is the one public record.
- **Tripwire (proposal, ~zero marginal cost): the per-market Gamma
  `resolutionSource` field.** It is machine-readable, names the stream in the
  URL, and flipped at exactly the first affected window on 08-14 (verified on
  the 1786665300/1786665600 pair [measured]). The bot's discovery call already
  fetches this event record — compare `resolutionSource` (+ `feeSchedule`,
  `orderMinSize`, `orderPriceMinTickSize`, `negRisk`) against expected
  constants per new window; any mismatch → the existing `_on_source_mismatch`
  path (in-process `trading_enabled=False` + CRITICAL + Discord). The
  `description` text alone is a BLIND tripwire — it did not change on 08-14.
  Second layer: daily changelog poll in the nightly (one GET).
- No announced future resolution/oracle changes found [measured searches].

## 1.3 Mechanics — fee model exact; two changes to log

- Taker fee: `crypto_fees_v2 {rate 0.07, exponent 1, takerOnly, rebateRate
  0.2}` live [measured]; docs formula `C·feeRate·p·(1−p)`; makers never pay.
  Our model is exactly the schedule. Tick 0.01 (0.001 past the extreme flip),
  min order 5 shares, negRisk false — all unchanged [measured].
- Matching: price-time; the one 2026 matching change is the crypto taker
  delay 250→50ms (08-17 11:00 UTC) [measured] — already in RESEARCH.md; it
  ages the dormant taker's 436ms POST table further (the table embeds the
  250ms hold).
- New per-signer rate limits (standard 40 orders/s burst 60) [measured] —
  irrelevant at 5 GTC posts/window.
- `postOnly` order type exists (GTC/GTD; rejected instead of crossing)
  [measured]. Ops note, not edge: rungs placed postOnly can never
  accidentally cross on a stale book; a matching-engine restart has a ~2-min
  window accepting ONLY cancels and postOnly orders [measured] — plain GTC
  rungs cannot be re-placed in that window.
- **Venue reliability regression [measured]: three CLOB "delayed open order
  read" incidents in ~37h — 08-30 17:36–18:29Z, 08-31 00:40–01:12Z, 08-31
  06:23–10:47Z (4h24m, cancel-only before resolution); no root cause
  published.** Order placement halts while RTDS keeps streaming — a state
  paper never simulates (paper would keep "filling" while live could not
  place). Live GTC rungs resting when placement halts can be cancelled but
  not replaced. Any live ladder needs this failure priced in
  (fail-closed = existing cancel path; acceptable).

## 1.4 Flow — the counterparty pool is shrinking and our fills' supply with it

- No public bot implements our late-window resting-bid maker pattern; the
  public 5-min bots are late-window TAKERS (jmazzini 38★; AllAboutAI, 200k+
  subscriber channel, self-reported unprofitable) [measured]. But the barrier
  is collapsing: agent platforms (Simmer), $99/mo book-depth APIs (Polydepth),
  a free CC0 tick dataset (Kacho), and an 08-14 TradoxVPS piece that names
  "forecast the moving average" as the surviving edge [measured]. Expect
  copycats in weeks–months, not years.
- Offshore Polymarket volume: July −26% MoM, August −60% MoM (site-wide)
  [measured]; US retail is migrating to the separate CFTC-regulated US book we
  cannot quote; perps (public 08-14) compete in-app for the same audience;
  the mid-July manipulation exposé branded these exact markets "retail exit
  liquidity" [measured]. Structural, not seasonal.
- **08-20 same-minute stop of 0xAAAAA + JetFadil: no public explanation
  exists** (enforcement, incident, deprecation all searched — nothing dated
  08-20) [measured]. Most consistent with one operator behind both pseudonyms
  [spec], as the census suggested. Not a market-structure event.
- Own-data corroboration (r7, 18 ET days of tape): deep winner-side sell flow
  in our resting span ran **$923/day (08-28..30) vs $2,651/day (08-21..27)
  and $1,415/day (08-14..20)** [data r7_supply_by_day.json]; tape coverage on
  the new days is complete (284–289 windows/day, no material holes), so the
  decline is behavioral, not a recording artifact.

## What reprices §2–§3 of the charter

1. Rebates: nothing — candidate lane closed with numbers (above).
2. Rule: stable; add the `resolutionSource` tripwire (config-free, rides
   discovery).
3. Supply: the ceiling and days-to-validation math must use the POST-08-27
   supply level (~⅓ of the prior week), not the era mean — done in
   `ceiling_2026-08-31.md`.
4. Rewards expiry (09-01) may remove the reward-farming walls → re-run the
   census and at-price queue read next week before trusting the 135-sh
   constant or wall-derived refutations directionally.
5. Venue outages: paper fills during CLOB placement halts are fantasy;
   verdict arithmetic treats 08-30/31 realized-fill evidence as censored.
