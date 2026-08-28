# CLAUDE.md claim ledger (Phase 0)

Source: `C:\Users\abhat\Personal\PolyBot\CLAUDE.md` as on disk 2026-08-27 (329 lines). Extraction only; no claim is evaluated here.

## Header

- Total claims: **357**
- By type: architecture 194, calibration 41, refutation 7, invariant 48, performance 2, prohibition 21, procedure 25, history 19
- Quantitative (carries a number, date, count, or threshold): **112**
- No evidence pointer stated: **298**
- By section: Preamble (# PolyBot) 26, Quick Start 14, Quick Start > Secrets 3, 1. The market + the two modes 45, 2. The two legs 93, 3. Sizing (every leg) 16, 4. Orders 31, 5. Resolution 13, 6. Recorders + nightly 49, 7. Hard rules 21, 8. Project layout 13, 9. Data sources 9, 10. Running + invariants 15, 11. Discord 9

Conventions: ids are document order. "Evidence pointer" is exactly what CLAUDE.md itself cites for the claim (file, function, script, date, N, sibling doc); an in-text code identifier counts as a pointer to code, a sibling-doc name counts as a deferral. A fact restated in two sections appears twice, once per location, so each occurrence can be disposed where it sits.

## Ledger

| id | section | claim | type | evidence pointer (as stated) | quantitative |
|---|---|---|---|---|---|
| C001 | Preamble (# PolyBot) | PolyBot is a 5-min BTC Up/Down trader for Polymarket | architecture | none stated | yes |
| C002 | Preamble (# PolyBot) | The only feeds the STRATEGY reads are Chainlink (RTDS) + the Polymarket CLOB + Gamma | architecture | none stated | no |
| C003 | Preamble (# PolyBot) | Every position holds to resolution | invariant | none stated | no |
| C004 | Preamble (# PolyBot) | There is no other model and no exit path | invariant | none stated | no |
| C005 | Preamble (# PolyBot) | The edge is the projection — the mostly-written 60s average the book cannot price because it prices off spot | performance | none stated | yes |
| C006 | Preamble (# PolyBot) | We are demonstrably not the fast participant | performance | none stated | no |
| C007 | Preamble (# PolyBot) | The book reprices 0.33s after Binance | calibration | none stated | yes |
| C008 | Preamble (# PolyBot) | The book reprices 2.5s before our oracle receipt | calibration | none stated | yes |
| C009 | Preamble (# PolyBot) | The projection's information is harvested at prices that match its confidence | architecture | none stated | no |
| C010 | Preamble (# PolyBot) | The sign record and its out-of-fit bounds live in RESEARCH.md — quote those, not an in-sample count | procedure | RESEARCH.md | no |
| C011 | Preamble (# PolyBot) | Since 2026-08-14 00:00 UTC Polymarket resolves on the official 60-second TWAP stream | history | none stated | yes |
| C012 | Preamble (# PolyBot) | The resolution stream is RTDS topic `crypto_prices_twap_sixty` | architecture | none stated | no |
| C013 | Preamble (# PolyBot) | Strike = the sixty stream's value at the open | architecture | none stated | no |
| C014 | Preamble (# PolyBot) | Final = the sixty stream's value at the close | architecture | none stated | no |
| C015 | Preamble (# PolyBot) | Strike and final are verified bit-exact against served price_to_beat/final_price | calibration | live probe 08-18 | yes |
| C016 | Preamble (# PolyBot) | A live probe on 08-18 was included in the bit-exact verification | history | live probe 08-18 | yes |
| C017 | Preamble (# PolyBot) | The switch from the 30s stream was SILENT | history | RESEARCH.md | no |
| C018 | Preamble (# PolyBot) | The bot traded the wrong stream for 4 days | history | RESEARCH.md | yes |
| C019 | Preamble (# PolyBot) | The full incident, and why both watchers missed it, is in RESEARCH.md | history | RESEARCH.md | no |
| C020 | Preamble (# PolyBot) | The nightly ping carries a SOURCE watch (`mechanism_read`: served values vs our own captured boundaries) | architecture | none stated | no |
| C021 | Preamble (# PolyBot) | The SOURCE watch turns red the same night a silent source switch ever happens again | invariant | none stated | no |
| C022 | Preamble (# PolyBot) | On any mechanism alarm: set `trading_enabled: false`, then run `scripts/research/ws1_boundary_autopsy.py` before anything else | procedure | scripts/research/ws1_boundary_autopsy.py | no |
| C023 | Preamble (# PolyBot) | `REFUTATIONS.md` is the graveyard — binding; killed lanes + methodology bans | architecture | REFUTATIONS.md | no |
| C024 | Preamble (# PolyBot) | `RESEARCH.md` holds ranked open problems, the frozen-measurement register with reopening conditions, and the 08-14 incident record | architecture | RESEARCH.md | yes |
| C025 | Preamble (# PolyBot) | `WALLETS.md` is the counterparty census (who is extracting, how, era-split) | architecture | WALLETS.md | no |
| C026 | Preamble (# PolyBot) | CLAUDE.md is the single source of truth for how the bot works and must update in the same commit as any behavioral change | procedure | none stated | no |
| C027 | Quick Start | `pip install -r requirements.txt` installs the dependencies (a root requirements.txt exists) | procedure | none stated | no |
| C028 | Quick Start | `cp polybot/config/.env.example polybot/config/.env` — an `.env.example` exists at `polybot/config/` and `.env` is read from there | procedure | none stated | no |
| C029 | Quick Start | `DISCORD_BOT_TOKEN` is required (monitoring) | procedure | none stated | no |
| C030 | Quick Start | Live mode also needs `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER` | procedure | none stated | no |
| C031 | Quick Start | `python -m polybot.main --mode paper` runs paper trading | procedure | none stated | no |
| C032 | Quick Start | `python -m polybot.main --mode live` trades real USDC and needs allowance | procedure | none stated | no |
| C033 | Quick Start | `python -m polybot.main --run-pipeline` runs one nightly cycle with no trading | procedure | none stated | no |
| C034 | Quick Start | `python -m pytest polybot/tests/` runs the full test suite | procedure | none stated | no |
| C035 | Quick Start | The full suite also runs in CI on every push | architecture | none stated | no |
| C036 | Quick Start | `scripts/run_polybot.sh` runs the daily cycle: trade -> nightly jobs -> commit -> restart, VPS only | procedure | none stated | no |
| C037 | Quick Start | The live recipe applies only after the per-leg bar passes AND `smoke_gtc_test.py --confirm` passes | procedure | smoke_gtc_test.py | no |
| C038 | Quick Start | The live recipe is: `settings.yaml` -> `mode: live` + `late_window.trading_enabled: true` + a fresh `validation_epoch` | procedure | none stated | no |
| C039 | Quick Start | That recipe is the complete switch to live | invariant | none stated | no |
| C040 | Quick Start | Paper and live share every decision path | invariant | none stated | no |
| C041 | Quick Start > Secrets | `DISCORD_BOT_TOKEN` is needed always (monitoring) | procedure | none stated | no |
| C042 | Quick Start > Secrets | `POLYMARKET_PRIVATE_KEY` is needed in live mode, for EIP-712 signing | architecture | none stated | no |
| C043 | Quick Start > Secrets | `POLYMARKET_FUNDER` is needed in live mode and is the USDC funding address | architecture | none stated | no |
| C044 | 1. The market + the two modes | Every 5 min, Polymarket runs a market | architecture | none stated | yes |
| C045 | 1. The market + the two modes | The market question: will BTC's 60-second TWAP at the window close be >= its value at the open | architecture | none stated | yes |
| C046 | 1. The market + the two modes | A tie resolves Up | invariant | none stated | no |
| C047 | 1. The market + the two modes | Up/Down tokens are ERC-1155 | architecture | none stated | no |
| C048 | 1. The market + the two modes | Tokens trade $0-$1 | architecture | none stated | yes |
| C049 | 1. The market + the two modes | The winner pays $1/share | architecture | none stated | yes |
| C050 | 1. The market + the two modes | The resolution source is Chainlink's official BTC/USD 60s-TWAP stream (`crypto_prices_twap_sixty`) | architecture | none stated | yes |
| C051 | 1. The market + the two modes | The sixty stream ticks ~1Hz on integer seconds | calibration | none stated | yes |
| C052 | 1. The market + the two modes | The sixty stream is delivered ~1.6-1.8s behind observation | calibration | none stated | yes |
| C053 | 1. The market + the two modes | Gamma mirrors the resolution stream for discovery | architecture | none stated | no |
| C054 | 1. The market + the two modes | The decision strike is the sixty stream's first report at/after the window boundary | architecture | chainlink_feed._record_boundary | no |
| C055 | 1. The market + the two modes | Boundary capture is implemented in `chainlink_feed._record_boundary` | architecture | chainlink_feed._record_boundary | no |
| C056 | 1. The market + the two modes | Gamma's served `price_to_beat` WINS over our capture when present | architecture | none stated | no |
| C057 | 1. The market + the two modes | Boundary trust runs on the payload clock | architecture | none stated | no |
| C058 | 1. The market + the two modes | A capture is trusted iff its report's OWN timestamp is within 0.5s of the boundary | invariant | none stated | yes |
| C059 | 1. The market + the two modes | The topic ticks on integer seconds, so the true boundary report carries ts == boundary exactly | calibration | none stated | no |
| C060 | 1. The market + the two modes | Delivery lag (rx - ts) never enters the trust comparison, so normal delivery cannot veto a capture | architecture | none stated | no |
| C061 | 1. The market + the two modes | Only a genuine hole can veto a boundary capture | invariant | none stated | no |
| C062 | 1. The market + the two modes | The RAW ~1Hz stream (`crypto_prices_chainlink`) is NOT the strike source | architecture | none stated | yes |
| C063 | 1. The market + the two modes | The raw stream feeds the running reconstruction `running_avg` | architecture | none stated | no |
| C064 | 1. The market + the two modes | `running_avg` uses rx-clock ZOH | architecture | none stated | no |
| C065 | 1. The market + the two modes | `running_avg` matches the served 60s final at median $0.028 | calibration | none stated | yes |
| C066 | 1. The market + the two modes | `running_avg` matches the served 60s final at p90 $0.22 | calibration | none stated | yes |
| C067 | 1. The market + the two modes | The raw stream feeds the projection `projected_final_twap` | architecture | none stated | no |
| C068 | 1. The market + the two modes | The projection horizon is 60s | architecture | none stated | yes |
| C069 | 1. The market + the two modes | A boundary capture landing > 0.5s past the boundary is UNTRUSTED | invariant | none stated | yes |
| C070 | 1. The market + the two modes | No leg deploys capital on OUR untrusted capture (`_strike_trusted`) | invariant | _strike_trusted | no |
| C071 | 1. The market + the two modes | A served Gamma `price_to_beat` is the resolution source itself | architecture | none stated | no |
| C072 | 1. The market + the two modes | A served Gamma `price_to_beat` restores trust when it arrives | architecture | none stated | no |
| C073 | 1. The market + the two modes | The projection refuses spot older than 3s | invariant | none stated | yes |
| C074 | 1. The market + the two modes | The projection refuses any raw delivery hole > 10s inside the averaging span (`RAW_GAP_MAX_S`) | invariant | RAW_GAP_MAX_S | yes |
| C075 | 1. The market + the two modes | A 68s hole once projected a $24 error onto a $0.14 photo-finish behind a perfectly fresh spot | history | none stated | yes |
| C076 | 1. The market + the two modes | There are two modes, paper and live, sharing one engine | architecture | none stated | no |
| C077 | 1. The market + the two modes | Paper uses real CLOB books | architecture | none stated | no |
| C078 | 1. The market + the two modes | Paper uses FOK semantics | architecture | none stated | no |
| C079 | 1. The market + the two modes | Paper latency is sampled from the live ledger's measured POST-RTT distribution | architecture | none stated | no |
| C080 | 1. The market + the two modes | Paper has a network-fail sim | architecture | none stated | no |
| C081 | 1. The market + the two modes | Paper does tick snapping | architecture | none stated | no |
| C082 | 1. The market + the two modes | Paper maker fills are print-through conservative (see section 2) | architecture | none stated | no |
| C083 | 1. The market + the two modes | Live uses `py-clob-client-v2` against the real CLOB | architecture | none stated | no |
| C084 | 1. The market + the two modes | Live verifies balance + allowance at boot | architecture | none stated | no |
| C085 | 1. The market + the two modes | Decision parity is a CI invariant | invariant | test_decision_parity.py | no |
| C086 | 1. The market + the two modes | `test_decision_parity.py` replays real recorded windows through both traders | architecture | test_decision_parity.py | no |
| C087 | 1. The market + the two modes | The parity test asserts bit-identical gates, signals, sizing, and order intents | invariant | test_decision_parity.py | no |
| C088 | 1. The market + the two modes | The parity fixture regenerates via `scripts/research/parity_fixture_gen.py` | procedure | scripts/research/parity_fixture_gen.py | no |
| C089 | 2. The two legs | Margin tables live in `signal_engine.TWAP_MARGIN_P995/_MAX` | architecture | signal_engine.TWAP_MARGIN_P995/_MAX | no |
| C090 | 2. The two legs | Margin tables were re-fit 2026-08-27 | history | none stated | yes |
| C091 | 2. The two legs | Re-fit corpus: 3,695 real-final 60s-rule windows | calibration | none stated | yes |
| C092 | 2. The two legs | The real-final corpus spans 15 ET days | calibration | none stated | yes |
| C093 | 2. The two legs | Re-fit corpus adds 1,651 synthetic windows | calibration | none stated | yes |
| C094 | 2. The two legs | Synthetic windows enter via max-union only | calibration | none stated | no |
| C095 | 2. The two legs | Synthetic finals are our own a60 reconstruction re-targeted onto pre-rule tape | calibration | none stated | no |
| C096 | 2. The two legs | Re-targeting makes low-k errors self-referentially SMALL | calibration | none stated | no |
| C097 | 2. The two legs | Synthetic windows may only ever WIDEN the max knots, never tighten anything | invariant | none stated | no |
| C098 | 2. The two legs | Estimator = rx-clock ZOH + coverage guard | calibration | none stated | no |
| C099 | 2. The two legs | MAX knots come from per-tick interval maxima | calibration | none stated | no |
| C100 | 2. The two legs | p99.5 at k=6 is $4.0 | calibration | none stated | yes |
| C101 | 2. The two legs | p99.5 at k=25 is $28.5 | calibration | none stated | yes |
| C102 | 2. The two legs | The 08-18 freeze's $8 (k=25) was exceeded on 11% of k=25 samples | history | none stated | yes |
| C103 | 2. The two legs | The 08-18 freeze was fit on one calm week | history | none stated | yes |
| C104 | 2. The two legs | Knots run to k=58 | calibration | none stated | yes |
| C105 | 2. The two legs | Re-fit on a bigger corpus is re-measurement, not bar-relaxing | prohibition | RESEARCH.md | no |
| C106 | 2. The two legs | Tuning the margin tables to make a window fire IS bar-relaxing | prohibition | none stated | no |
| C107 | 2. The two legs | The deep-projection maker ladder (`signal_leg="deep_proj"`) is the business | architecture | none stated | no |
| C108 | 2. The two legs | Ladder rungs sit at 0.80/0.65/0.50/0.35/0.20 | architecture | none stated | yes |
| C109 | 2. The two legs | Each rung is 20% of `maker_bankroll_frac` | architecture | none stated | yes |
| C110 | 2. The two legs | `maker_bankroll_frac` = 0.15 | architecture | none stated | yes |
| C111 | 2. The two legs | Rungs rest on the projection-favored side | architecture | none stated | no |
| C112 | 2. The two legs | Rungs rest while the BRIDGED projection's displacement clears `need` x p99.5(k) | architecture | none stated | no |
| C113 | 2. The two legs | `need` = 1.0 | architecture | none stated | yes |
| C114 | 2. The two legs | need 1.0 is the interim floor from the 08-18 walk-forward audit | history | 08-18 walk-forward audit | yes |
| C115 | 2. The two legs | The in-sample 0.5 grid could not be validated out-of-fit | refutation | RESEARCH.md #1 | yes |
| C116 | 2. The two legs | The >=14-day re-fit re-decides `need` | procedure | RESEARCH.md #1 | yes |
| C117 | 2. The two legs | Placement k is in [6,25] | architecture | none stated | yes |
| C118 | 2. The two legs | The k>25 flow is REFUTED as harvestable | refutation | REFUTATIONS.md | yes |
| C119 | 2. The two legs | Sweeps traverse the whole ladder inside ~1s and outrun any cancel | refutation | REFUTATIONS.md | yes |
| C120 | 2. The two legs | Flip-race loss probability exceeds every rung's price margin | refutation | REFUTATIONS.md | no |
| C121 | 2. The two legs | The same `need` x p99.5(k) floor cancels resting rungs when it breaks | architecture | none stated | no |
| C122 | 2. The two legs | Post-close hold is 60s | architecture | none stated | yes |
| C123 | 2. The two legs | The post-close hold is gated on the boundary-verified winner (`certain_winner`) | architecture | certain_winner | no |
| C124 | 2. The two legs | `certain_winner` fails closed | invariant | certain_winner | no |
| C125 | 2. The two legs | The bridge: spot_est = latest raw report + Binance movement since that report's payload ts (`spot_bridge_delta`) | architecture | spot_bridge_delta | no |
| C126 | 2. The two legs | Every bridge failure mode collapses to the plain projection | invariant | none stated | no |
| C127 | 2. The two legs | Fills book through `book_maker_fill` as ONE blended position | architecture | book_maker_fill | no |
| C128 | 2. The two legs | The paper fill rule is live-calibrated and conservative | calibration | none stated | no |
| C129 | 2. The two legs | Strictly-below prints fill a rung in FULL | architecture | none stated | no |
| C130 | 2. The two legs | At-price prints credit only volume beyond `AT_PRICE_QUEUE_SH` | architecture | AT_PRICE_QUEUE_SH | no |
| C131 | 2. The two legs | `AT_PRICE_QUEUE_SH` = 135 sh | calibration | none stated | yes |
| C132 | 2. The two legs | Snapshot queue models are BANNED | prohibition | REFUTATIONS.md | no |
| C133 | 2. The two legs | Paper pays a GTC round trip on place and cancel | architecture | none stated | no |
| C134 | 2. The two legs | The paper GTC round trip is not measured | calibration | none stated | no |
| C135 | 2. The two legs | Paper's GTC round trip is 56ms/rung | calibration | none stated | yes |
| C136 | 2. The two legs | ~500ms GTC round trip was reconstructed from the one live ladder | calibration | none stated | yes |
| C137 | 2. The two legs | Paper's rungs become matchable about twice as fast as the real ones | calibration | RESEARCH.md | yes |
| C138 | 2. The two legs | Maker fills are fee-free | calibration | 08-18 re-verification | no |
| C139 | 2. The two legs | Fee-free maker fills re-verified on post-rule fills 08-18: 274/274 USDC deltas exact | history | 08-18: 274/274 | yes |
| C140 | 2. The two legs | Bar (unchanged): >=6 clean ET days | invariant | none stated | yes |
| C141 | 2. The two legs | Bar: >=20 filled windows | invariant | none stated | yes |
| C142 | 2. The two legs | Bar: EW >= +5 cents/sh | invariant | none stated | yes |
| C143 | 2. The two legs | Bar: `usd_per_day > 0` | invariant | none stated | yes |
| C144 | 2. The two legs | The bar is computed on realized paper fills since `validation_epoch` | procedure | none stated | no |
| C145 | 2. The two legs | The lock-dip taker (`lock_dip`) is DORMANT: `taker_enabled: false` since 08-18 | architecture | none stated | yes |
| C146 | 2. The two legs | The taker's whipsaw supply died with the 60s rule | refutation | none stated | no |
| C147 | 2. The two legs | 4 winner-side max-lock dips were observed in 1,184 windows | calibration | none stated | yes |
| C148 | 2. The two legs | One of those dips was FOK-reachable | calibration | none stated | yes |
| C149 | 2. The two legs | The taker supply bar is >=1 dip per 3 days | invariant | none stated | yes |
| C150 | 2. The two legs | A 60s average moves too slowly to produce panicked max-lock asks | calibration | none stated | yes |
| C151 | 2. The two legs | The taker is not refuted: the code stays | architecture | none stated | no |
| C152 | 2. The two legs | The taker signal still evaluates and logs would-be fires | architecture | none stated | no |
| C153 | 2. The two legs | The taker re-arm condition is in RESEARCH.md | procedure | RESEARCH.md | no |
| C154 | 2. The two legs | When armed, the taker fires on max tier ONLY (`require_max_tier`) | architecture | require_max_tier | no |
| C155 | 2. The two legs | p99.5 tiers realize breaches | calibration | none stated | no |
| C156 | 2. The two legs | The 60s sim's one loss was a p99.5 fire | history | none stated | yes |
| C157 | 2. The two legs | Taker requires k >= 6s | invariant | none stated | yes |
| C158 | 2. The two legs | Taker uses the PLAIN projection | architecture | none stated | no |
| C159 | 2. The two legs | Taker requires ask <= tier_prob - `sniper_min_edge` | architecture | none stated | no |
| C160 | 2. The two legs | Taker applies a one-tick FOK pad | architecture | none stated | yes |
| C161 | 2. The two legs | Taker sizes with market-anchored Kelly | architecture | none stated | no |
| C162 | 2. The two legs | Taker applies all section-1 gates | architecture | none stated | no |
| C163 | 2. The two legs | Taker booking is chain-truth via the +8s audit | architecture | none stated | yes |
| C164 | 2. The two legs | The `fees` column is share-denominated, NOT the charged taker fee | architecture | none stated | no |
| C165 | 2. The two legs | Takers pay the documented curve via the USDC debit | calibration | none stated | no |
| C166 | 2. The two legs | Taker fee re-verified post-rule 08-18: 326 rows at fee/model median 1.000 | history | 08-18: 326 rows | yes |
| C167 | 2. The two legs | No sell path exists in the codebase | invariant | none stated | no |
| C168 | 2. The two legs | Both legs' edges were measured hold-to-resolution | calibration | REFUTATIONS.md: exits | no |
| C169 | 2. The two legs | Kill rules are implemented in `live_health_read.kill_rule_tripped` | architecture | live_health_read.kill_rule_tripped | no |
| C170 | 2. The two legs | Kill rules are armed at any go-live | architecture | none stated | no |
| C171 | 2. The two legs | Any `lock_dip` loss trips the kill rule on ONE occurrence | invariant | none stated | yes |
| C172 | 2. The two legs | Every lock_dip fire is max-tier, so a loss IS a breach | invariant | none stated | no |
| C173 | 2. The two legs | Otherwise the kill rule trips on trailing-4-day mean DOLLARS < 0 | invariant | none stated | yes |
| C174 | 2. The two legs | The trailing rule is judged only once the window holds >=4 ET days AND >=5 fills | invariant | none stated | yes |
| C175 | 2. The two legs | Sparse fills keep accruing toward the trailing window | architecture | none stated | no |
| C176 | 2. The two legs | One -$4.50 rung loss after three quiet days must not halt a leg that is up on the week (measured 08-18) | history | measured 08-18 | yes |
| C177 | 2. The two legs | `trading_enabled: false` is the shared emergency brake for every leg | architecture | none stated | no |
| C178 | 2. The two legs | The per-window SOURCE gate (section 6) flips `trading_enabled` in-process on a resolution-source mismatch | architecture | none stated | no |
| C179 | 2. The two legs | Never deploy on a harness print alone | prohibition | none stated | no |
| C180 | 2. The two legs | The paper shadow's realized fills are the binding gate | invariant | none stated | no |
| C181 | 2. The two legs | Capital deploys ONLY through these two legs | invariant | none stated | no |
| C182 | 3. Sizing (every leg) | size = bankroll * kelly * circuit_breaker_mult | architecture | none stated | no |
| C183 | 3. Sizing (every leg) | size *= concurrent_multiplier(side, market, opens), which is correlation-aware | architecture | none stated | no |
| C184 | 3. Sizing (every leg) | size = min(size, bankroll * max_bankroll_deployed) | architecture | none stated | no |
| C185 | 3. Sizing (every leg) | `max_bankroll_deployed` = 0.80 | architecture | none stated | yes |
| C186 | 3. Sizing (every leg) | size = min(size, side_depth * max_book_fill_pct) | architecture | none stated | no |
| C187 | 3. Sizing (every leg) | `max_book_fill_pct` = 0.50 | architecture | none stated | yes |
| C188 | 3. Sizing (every leg) | If size < 1.0 the trade is skipped (CLOB $1 floor) | invariant | none stated | yes |
| C189 | 3. Sizing (every leg) | `kelly` = fee-aware Kelly on the market-anchored defended edge | architecture | none stated | no |
| C190 | 3. Sizing (every leg) | Kelly is scaled by `kelly_fraction` | architecture | none stated | no |
| C191 | 3. Sizing (every leg) | `kelly_fraction` = 0.08 | architecture | none stated | yes |
| C192 | 3. Sizing (every leg) | Circuit breaker: tier-locked floor at $100/150/200... milestones | architecture | none stated | yes |
| C193 | 3. Sizing (every leg) | Circuit breaker floor = tier x 0.85 | architecture | none stated | yes |
| C194 | 3. Sizing (every leg) | Circuit breaker uses sqrt interpolation to 0.40x | architecture | none stated | yes |
| C195 | 3. Sizing (every leg) | The circuit breaker tier never resets down | invariant | none stated | no |
| C196 | 3. Sizing (every leg) | The circuit breaker persists via `peak_bankroll` | architecture | peak_bankroll | no |
| C197 | 3. Sizing (every leg) | The ladder budget is a flat fraction, not Kelly | architecture | none stated | no |
| C198 | 4. Orders | FOK orders go via `py-clob-client-v2` | architecture | none stated | no |
| C199 | 4. Orders | `py-clob-client-v2` is pinned <1.1.0 | architecture | none stated | yes |
| C200 | 4. Orders | 1.1.0 wraps post_order in a blocking 30s hash-poll | history | none stated | yes |
| C201 | 4. Orders | FOK orders get 3 attempts | architecture | none stated | yes |
| C202 | 4. Orders | Only provably-unposted failures retry | invariant | none stated | no |
| C203 | 4. Orders | Order-POST RTT p50 was ~410-436ms as last measured (pre-08-13) | calibration | pre-08-13 measurement | yes |
| C204 | 4. Orders | The RTT table embeds Polymarket's DELIBERATE taker hold on crypto up/down markets (`itode: true`) | architecture | none stated | no |
| C205 | 4. Orders | The Polymarket changelog cut the taker hold from 250ms to 50ms on 08-17 11:00 UTC | history | Polymarket changelog | yes |
| C206 | 4. Orders | The paper RTT table is stale by route change | calibration | none stated | no |
| C207 | 4. Orders | The RTT table re-derives from the next measured POST samples (`smoke_order_test.py --confirm`), never by hand | procedure | smoke_order_test.py | no |
| C208 | 4. Orders | EIP-712 sign takes 17.5ms pure-python on the box | calibration | none stated | yes |
| C209 | 4. Orders | coincurve on Linux is ~10x faster than pure-python signing | calibration | none stated | yes |
| C210 | 4. Orders | Dev boxes skip coincurve | architecture | none stated | no |
| C211 | 4. Orders | SELL signatures are pre-armed | architecture | none stated | no |
| C212 | 4. Orders | BUY pre-signs concurrently | architecture | none stated | no |
| C213 | 4. Orders | Book pre-check is WS-only | architecture | none stated | no |
| C214 | 4. Orders | HTTP/2 connection pool is kept warm | architecture | none stated | no |
| C215 | 4. Orders | gc.freeze() runs post-boot | architecture | none stated | no |
| C216 | 4. Orders | GTC rungs pass `legal_price` (round DOWN to tick, clamp [tick, 1-tick]) | architecture | legal_price | no |
| C217 | 4. Orders | GTC rungs pass the 5-share exchange minimum | architecture | none stated | yes |
| C218 | 4. Orders | A rung that cannot be rested logs `MAKER BID REJECTED` at ERROR, refusal and POST failure alike | architecture | none stated | no |
| C219 | 4. Orders | A rung the budget cannot afford logs `MAKER RUNG SKIPPED` at INFO | architecture | none stated | no |
| C220 | 4. Orders | `cl_report_to_submit_ms` + `lat_*` stamps measure the race per fill | architecture | none stated | no |
| C221 | 4. Orders | GTC place/cancel RTTs stamp per rung (`gtc_place_ms`/`gtc_cancel_ms`) | architecture | none stated | no |
| C222 | 4. Orders | `latency_stats.json` has a gtc section | architecture | latency_stats.json | no |
| C223 | 4. Orders | `smoke_gtc_test.py --samples` also feeds the gtc section | architecture | smoke_gtc_test.py | no |
| C224 | 4. Orders | A fill whose owned segments exceed 1.5x the 25ms budget logs LATENCY BUDGET at WARNING | architecture | none stated | yes |
| C225 | 4. Orders | Live boot checks key+funder | architecture | none stated | no |
| C226 | 4. Orders | Live boot runs a balance/allowance preflight | architecture | none stated | no |
| C227 | 4. Orders | Allowance is rechecked every 10 fills | architecture | none stated | yes |
| C228 | 4. Orders | `fill.fill_size` is always USDC notional | invariant | none stated | no |
| C229 | 5. Resolution | The TWAP oracle decides resolution | architecture | none stated | no |
| C230 | 5. Resolution | Winner $1 / loser $0 is credited atomically | architecture | none stated | yes |
| C231 | 5. Resolution | Exit price is oracle-first (Gamma `event_metadata`) | architecture | none stated | no |
| C232 | 5. Resolution | Exit price falls back to a coherent resolved CLOB book | architecture | none stated | no |
| C233 | 5. Resolution | Exit price never uses Binance | prohibition | none stated | no |
| C234 | 5. Resolution | The orphan fallback resolves ONLY from genuine boundary captures | invariant | none stated | no |
| C235 | 5. Resolution | The orphan fallback waits and pages rather than fabricate | architecture | none stated | no |
| C236 | 5. Resolution | Our tape prints a TAPE VERDICT before Gamma serves | architecture | none stated | no |
| C237 | 5. Resolution | Per-window RESOLUTION DRIFT warns (log-level) when Gamma disagrees with a reliable capture | architecture | none stated | no |
| C238 | 5. Resolution | The nightly SOURCE watch is the systematic net for resolution drift | architecture | none stated | no |
| C239 | 5. Resolution | Winner payouts book via Polymarket auto-redeem | architecture | none stated | no |
| C240 | 5. Resolution | Losing $0 stubs sit inert on the wallet; redemption is deliberately not automated | architecture | none stated | no |
| C241 | 5. Resolution | CLOB orders are the only on-chain thing the bot signs | invariant | none stated | no |
| C242 | 6. Recorders + nightly | The window-path recorder samples at 1 Hz | architecture | none stated | yes |
| C243 | 6. Recorders + nightly | The window-path recorder samples at 5 Hz in the final 45s | architecture | none stated | yes |
| C244 | 6. Recorders + nightly | It records both tokens' BBO/depth + Chainlink price + strike | architecture | none stated | no |
| C245 | 6. Recorders + nightly | It records `strike_trusted`, since `get_strike` also serves untrusted captures | architecture | get_strike | no |
| C246 | 6. Recorders + nightly | It records EVERY window | invariant | none stated | no |
| C247 | 6. Recorders + nightly | Output goes to `window_paths` (gitignored sidecar DB) / `window_labels` | architecture | none stated | no |
| C248 | 6. Recorders + nightly | window_paths retention is 90 days | architecture | none stated | yes |
| C249 | 6. Recorders + nightly | Labels are the kill-bar ground truth | invariant | none stated | no |
| C250 | 6. Recorders + nightly | The tape recorder writes every CLOB print (+ exchange ts, fee bps) to `memory/recordings/tape_*.jsonl` | architecture | none stated | no |
| C251 | 6. Recorders + nightly | Tape recordings are gitignored | architecture | none stated | no |
| C252 | 6. Recorders + nightly | The micro-tape records every CLOB BBO change in the final 90s | architecture | none stated | yes |
| C253 | 6. Recorders + nightly | The micro-tape records every raw report as "l" | architecture | none stated | no |
| C254 | 6. Recorders + nightly | The micro-tape records the official 60s stream as "t" | architecture | none stated | no |
| C255 | 6. Recorders + nightly | The micro-tape records the RETIRED 30s stream as "t3", recorded only | architecture | none stated | no |
| C256 | 6. Recorders + nightly | The t3 record is A/B evidence for the next silent source swap | architecture | none stated | no |
| C257 | 6. Recorders + nightly | RTDS resumed serving the 30s stream by 08-27 | history | none stated | yes |
| C258 | 6. Recorders + nightly | The nightly SOURCE line states the t3 count | architecture | none stated | no |
| C259 | 6. Recorders + nightly | The micro-tape records the Binance relay as "s"/src "bz" | architecture | none stated | no |
| C260 | 6. Recorders + nightly | Micro-tape records carry payload+receipt ts and go to `micro_*.jsonl` | architecture | none stated | no |
| C261 | 6. Recorders + nightly | Micro-tape is gzipped nightly at ~39x | architecture | none stated | yes |
| C262 | 6. Recorders + nightly | Micro-tape readers take .jsonl(.gz) | architecture | none stated | no |
| C263 | 6. Recorders + nightly | `trade_context` is recorded on fills AND ghosts | architecture | none stated | no |
| C264 | 6. Recorders + nightly | `signal_leg` is the per-leg ledger key | architecture | none stated | no |
| C265 | 6. Recorders + nightly | None-vs-0.0 is load-bearing: cold inputs record None, never 0.0 | invariant | none stated | no |
| C266 | 6. Recorders + nightly | Ladder fills carry `print_gap` = 1 when the CLOB feed reconnected while the rungs rested | architecture | none stated | yes |
| C267 | 6. Recorders + nightly | Paper's fill count is short where `print_gap` = 1 | calibration | none stated | no |
| C268 | 6. Recorders + nightly | The per-window SOURCE hard gate is `recording._check_resolution_source` | architecture | recording._check_resolution_source | no |
| C269 | 6. Recorders + nightly | Every labeled window's served strike/final is compared against our TRUSTED stream captures | architecture | none stated | no |
| C270 | 6. Recorders + nightly | A >$0.005 mismatch flips `trading_enabled` false in-process | invariant | none stated | yes |
| C271 | 6. Recorders + nightly | A source mismatch pages Discord | architecture | none stated | no |
| C272 | 6. Recorders + nightly | On a source mismatch, settings on disk remain unchanged | architecture | none stated | no |
| C273 | 6. Recorders + nightly | The operator re-arms by restart after re-pointing the feed | procedure | none stated | no |
| C274 | 6. Recorders + nightly | The SOURCE gate is the one wired exception to "watches never flip config" | invariant | none stated | no |
| C275 | 6. Recorders + nightly | NightlyScheduler runs at 23:45 ET | architecture | none stated | yes |
| C276 | 6. Recorders + nightly | Nightly does rollups + retention + the sniper health ping | architecture | none stated | no |
| C277 | 6. Recorders + nightly | `_sniper_health_job` posts to Discord `#polybot-daily` | architecture | _sniper_health_job | no |
| C278 | 6. Recorders + nightly | The ping carries the realized per-leg ledger + kill-rule verdict (realized-only authority) | architecture | none stated | no |
| C279 | 6. Recorders + nightly | The ping carries a SIM ceiling read | architecture | none stated | no |
| C280 | 6. Recorders + nightly | The ping carries a regime line: trailing gaps p25/50/75 + photo-finish share <$1 | architecture | none stated | yes |
| C281 | 6. Recorders + nightly | HOSTILE = p50 < $6 or photo > 15% | calibration | none stated | yes |
| C282 | 6. Recorders + nightly | HOSTILE thresholds were percentile-ported to the 60s rule on 08-18 | history | none stated | yes |
| C283 | 6. Recorders + nightly | HOSTILE predicts zero fills, not losses | calibration | none stated | no |
| C284 | 6. Recorders + nightly | The ping carries a chain watch (final == next strike) | architecture | none stated | no |
| C285 | 6. Recorders + nightly | The ping carries the nightly SOURCE summary (`mechanism_read`) | architecture | mechanism_read | no |
| C286 | 6. Recorders + nightly | Ops watch: POST RTT p50 vs the 436ms table +/-25% | architecture | none stated | yes |
| C287 | 6. Recorders + nightly | Ops watch: trailing-7d sweep-consumed deep-queue p75 vs the 135-sh at-price constant | architecture | none stated | yes |
| C288 | 6. Recorders + nightly | Ops watch: measured GTC place p50 vs paper's 56ms table +/-25%, dark until samples exist | architecture | none stated | yes |
| C289 | 6. Recorders + nightly | Ops watch: owned-latency budget breaches | architecture | none stated | no |
| C290 | 6. Recorders + nightly | The nightly ping is alert-only | invariant | none stated | no |
| C291 | 7. Hard rules | No ML/feature-stack entry-side prediction | prohibition | none stated | no |
| C292 | 7. Hard rules | The CLOB price wins everywhere our arithmetic doesn't | refutation | none stated | no |
| C293 | 7. Hard rules | The ONE sanctioned exception to the no-prediction rule is the TWAP-lock projection (an already-observed average) | prohibition | none stated | yes |
| C294 | 7. Hard rules | Measurement of observed quantities is always in scope; prediction of unobserved ones is not | prohibition | none stated | no |
| C295 | 7. Hard rules | No deployment before a kill bar passes | prohibition | none stated | no |
| C296 | 7. Hard rules | Never relax a bar to pass it | prohibition | none stated | no |
| C297 | 7. Hard rules | Re-measurement on a bigger corpus/better estimator is not relaxing | prohibition | RESEARCH.md register | no |
| C298 | 7. Hard rules | No symmetric market-making | prohibition | none stated | no |
| C299 | 7. Hard rules | No oracle-cadence trading | prohibition | none stated | no |
| C300 | 7. Hard rules | No expansion past btc-5m | prohibition | none stated | no |
| C301 | 7. Hard rules | What is actually refuted is post-close camping on the sibling markets (30s era) | refutation | REFUTATIONS.md | yes |
| C302 | 7. Hard rules | The siblings' in-window deep flow is unmeasured and low priority | history | RESEARCH.md | no |
| C303 | 7. Hard rules | Scaling is SIZE on this one book | prohibition | none stated | no |
| C304 | 7. Hard rules | No mid-price edge math; executable CLOB BBO only | prohibition | none stated | no |
| C305 | 7. Hard rules | Never skip the fee: fee = rate*shares*p*(1-p) | invariant | none stated | no |
| C306 | 7. Hard rules | Fee rate is 0.07 | calibration | none stated | yes |
| C307 | 7. Hard rules | Flat-additive gates use 0.0175 | calibration | none stated | yes |
| C308 | 7. Hard rules | Never mix the curve fee and the flat-additive fee | prohibition | none stated | no |
| C309 | 7. Hard rules | `gain_pct = pnl/size`, never log_return | invariant | none stated | no |
| C310 | 7. Hard rules | Don't bypass the circuit breaker | prohibition | none stated | no |
| C311 | 7. Hard rules | Don't delete `polybot/db/polybot_*.db` | prohibition | none stated | no |
| C312 | 8. Project layout | `polybot/main.py` holds the trading loop, gates, ladder hook, and nightly health job | architecture | none stated | no |
| C313 | 8. Project layout | `polybot/config/` holds `settings.yaml` (THE single config source) and `loader.py` | architecture | none stated | no |
| C314 | 8. Project layout | `polybot/core/signal_engine.py` holds the margin tables (60s-rule freeze 08-18) + lock math | architecture | none stated | yes |
| C315 | 8. Project layout | `polybot/feeds/` holds chainlink_feed (sixty topic, strike, projection, bridge, coverage guard), clob_ws, market_scanner | architecture | none stated | no |
| C316 | 8. Project layout | `polybot/recording.py` holds WindowPathRecorder + TapeRecorder + MicroTape | architecture | none stated | no |
| C317 | 8. Project layout | `polybot/execution/` holds base (fee math), paper_trader, live_trader, maker_bid (deep_proj ladder), circuit_breaker | architecture | none stated | no |
| C318 | 8. Project layout | `polybot/` also holds agents/, memory/, discord_bot/, and db/models.py (per-mode SQLite + labels) | architecture | none stated | no |
| C319 | 8. Project layout | `scripts/run_polybot.sh` is the daily supervisor (systemd unit: polybot) | architecture | none stated | no |
| C320 | 8. Project layout | `scripts/analyze_twap_lock.py` is the lock replay harness (60s) + bit-exact mechanism check | architecture | none stated | yes |
| C321 | 8. Project layout | `scripts/analyze_late_window.py` holds realized-ledger readers + resolution/SOURCE watches | architecture | none stated | no |
| C322 | 8. Project layout | `scripts/` contains sniper_shadow_status.py, verify_keys.py, smoke_order_test.py, smoke_gtc_test.py, reset_paper_clean.py | architecture | none stated | no |
| C323 | 8. Project layout | `scripts/research/` is offline analysis tooling with its own README; its data/ is gitignored; covers census, error tables, engine-true grid | architecture | scripts/research/README | no |
| C324 | 8. Project layout | REFUTATIONS.md, RESEARCH.md, WALLETS.md live at the repo root | architecture | REFUTATIONS.md, RESEARCH.md, WALLETS.md | no |
| C325 | 9. Data sources | Polymarket CLOB is accessed via WS + `GET /price /book /spread /tick-size` | architecture | none stated | no |
| C326 | 9. Data sources | The CLOB supplies books, tape, executable prices | architecture | none stated | no |
| C327 | 9. Data sources | Polymarket Gamma is accessed via `GET /events?slug=` with fallback `/events/slug/{slug}` | architecture | none stated | no |
| C328 | 9. Data sources | Gamma supplies discovery + resolution + labels | architecture | none stated | no |
| C329 | 9. Data sources | Chainlink RTDS WS endpoint is `wss://ws-live-data.polymarket.com` | architecture | none stated | no |
| C330 | 9. Data sources | RTDS topics consumed: `crypto_prices_twap_sixty` + raw `crypto_prices_chainlink` + Binance `crypto_prices` | architecture | none stated | no |
| C331 | 9. Data sources | The sixty topic supplies strike + resolution | architecture | none stated | no |
| C332 | 9. Data sources | The raw topic feeds the projection | architecture | none stated | no |
| C333 | 9. Data sources | Binance feeds ONLY the bridge delta | invariant | none stated | no |
| C334 | 10. Running + invariants | The bot runs ONLY on the VPS (Oracle Stockholm, systemd `polybot`) | invariant | none stated | no |
| C335 | 10. Running + invariants | The bot starts at 12:01 AM ET | architecture | none stated | yes |
| C336 | 10. Running + invariants | The bot stops at 11:30 PM ET | architecture | none stated | yes |
| C337 | 10. Running + invariants | Nightly jobs run at 11:45 PM ET | architecture | none stated | yes |
| C338 | 10. Running + invariants | The supervisor commits + pushes `origin main` on clean exit | procedure | none stated | no |
| C339 | 10. Running + invariants | The supervisor pulls + restarts at midnight | procedure | none stated | yes |
| C340 | 10. Running + invariants | A mid-day crash restarts after 60s | architecture | none stated | yes |
| C341 | 10. Running + invariants | Never run the bot on a workstation | prohibition | none stated | no |
| C342 | 10. Running + invariants | The instance lock is single-host only | architecture | none stated | no |
| C343 | 10. Running + invariants | Live preflight is `verify_keys.py` then `smoke_order_test.py --confirm` | procedure | verify_keys.py, smoke_order_test.py | no |
| C344 | 10. Running + invariants | Storage uses UTC; ET is used only for date-bucketing + trading windows | invariant | none stated | no |
| C345 | 10. Running + invariants | Recordings are gitignored | architecture | none stated | no |
| C346 | 10. Running + invariants | `memory/` records + per-mode DBs + settings.yaml commit nightly | procedure | none stated | no |
| C347 | 10. Running + invariants | Heavy analysis never runs on the box; scp the tape local | prohibition | none stated | no |
| C348 | 10. Running + invariants | Kill bars are the deployment authority | invariant | none stated | no |
| C349 | 11. Discord | Discord command `!status` exists | architecture | none stated | no |
| C350 | 11. Discord | Discord command `!history [n]` exists | architecture | none stated | no |
| C351 | 11. Discord | Discord command `!pause` exists | architecture | none stated | no |
| C352 | 11. Discord | Discord command `!resume` exists | architecture | none stated | no |
| C353 | 11. Discord | Discord command `!clear [trades\|control\|all] confirm` exists | architecture | none stated | no |
| C354 | 11. Discord | Discord command `!session` exists | architecture | none stated | no |
| C355 | 11. Discord | Discord command `!pipeline` exists | architecture | none stated | no |
| C356 | 11. Discord | Discord command `!commands` exists | architecture | none stated | no |
| C357 | 11. Discord | `!pause` halts new entries only | architecture | none stated | no |

## Claims that delegate to sibling docs

- **REFUTATIONS.md** (8): C023, C118, C119, C120, C132, C168, C301, C324
- **RESEARCH.md** (13): C010, C017, C018, C019, C024, C105, C115, C116, C137, C153, C297, C302, C324
- **WALLETS.md** (2): C025, C324

Per-claim detail:

- C010 -> RESEARCH.md: The sign record and its out-of-fit bounds live in RESEARCH.md — quote those, not an in-sample count
- C017 -> RESEARCH.md: The switch from the 30s stream was SILENT
- C018 -> RESEARCH.md: The bot traded the wrong stream for 4 days
- C019 -> RESEARCH.md: The full incident, and why both watchers missed it, is in RESEARCH.md
- C023 -> REFUTATIONS.md: `REFUTATIONS.md` is the graveyard — binding; killed lanes + methodology bans
- C024 -> RESEARCH.md: `RESEARCH.md` holds ranked open problems, the frozen-measurement register with reopening conditions, and the 08-14 incident record
- C025 -> WALLETS.md: `WALLETS.md` is the counterparty census (who is extracting, how, era-split)
- C105 -> RESEARCH.md: Re-fit on a bigger corpus is re-measurement, not bar-relaxing
- C115 -> RESEARCH.md: The in-sample 0.5 grid could not be validated out-of-fit (pointer as stated: RESEARCH.md #1)
- C116 -> RESEARCH.md: The >=14-day re-fit re-decides `need` (pointer as stated: RESEARCH.md #1)
- C118 -> REFUTATIONS.md: The k>25 flow is REFUTED as harvestable
- C119 -> REFUTATIONS.md: Sweeps traverse the whole ladder inside ~1s and outrun any cancel
- C120 -> REFUTATIONS.md: Flip-race loss probability exceeds every rung's price margin
- C132 -> REFUTATIONS.md: Snapshot queue models are BANNED
- C137 -> RESEARCH.md: Paper's rungs become matchable about twice as fast as the real ones
- C153 -> RESEARCH.md: The taker re-arm condition is in RESEARCH.md
- C168 -> REFUTATIONS.md: Both legs' edges were measured hold-to-resolution (pointer as stated: REFUTATIONS.md: exits)
- C297 -> RESEARCH.md: Re-measurement on a bigger corpus/better estimator is not relaxing (pointer as stated: RESEARCH.md register)
- C301 -> REFUTATIONS.md: What is actually refuted is post-close camping on the sibling markets (30s era)
- C302 -> RESEARCH.md: The siblings' in-window deep flow is unmeasured and low priority
- C324 -> REFUTATIONS.md, RESEARCH.md, WALLETS.md: REFUTATIONS.md, RESEARCH.md, WALLETS.md live at the repo root (pointer as stated: REFUTATIONS.md, RESEARCH.md, WALLETS.md)

## Internal cross-references for the verifier (noted, not evaluated)

- C090-C104 (section 2: margin tables re-fit 2026-08-27; p99.5 k=6 $4.0, k=25 $28.5) vs C314 (section 8: `signal_engine.py` described as "60s-rule freeze 08-18"): same object, two dates stated.
- C203 and C286 (POST RTT table ~410-436ms; ops watch anchored at 436ms) vs C205 and C206 (taker hold cut 250ms -> 50ms on 08-17; table declared stale): the doc itself flags the anchor as stale.
- C255 ("RETIRED 30s stream") vs C257 (RTDS resumed serving it by 08-27): both stated in the same bullet.
- C084 and C226: balance/allowance preflight stated in sections 1 and 4.
- C105 and C297: re-measurement-is-not-relaxing stated in sections 2 and 7.
- C067 and C332: raw stream feeds the projection, stated in sections 1 and 9.
- C029 and C041: DISCORD_BOT_TOKEN requirement stated in the Quick Start comment and the Secrets table.
