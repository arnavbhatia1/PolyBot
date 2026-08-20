# AUDIT — total correctness verification (2026-08-20)

Adversarial audit against the charter of 2026-08-19. Frame: assume the system is
wrong until proven right; docs describe intent, only code/tape/ledgers describe
reality. Every section ends VERIFIED (with the executed check named), FINDING
(with evidence), or UNVERIFIABLE (with the missing datum named).

**Scope note.** The charter text was truncated mid-Part-4.3 (restart matrix).
That section was completed on its evident intent — process death at each
critical state — and is marked accordingly.

**Corpus.** Code at `e4e19a10`. Ledgers: box snapshots taken 08-19 19:30Z and
08-20 10:18Z (`polybot_paper.db` 7 post-migration fills; `polybot_live.db` 337
rows, complete — live halted 08-15). Tape: `micro_2026-08-14..18.jsonl.gz` and
`tape_2026-08-05..18.jsonl.gz` pulled from the box (301 MB, gitignored).
Epochs: migration deploy 2026-08-18T16:45Z, `validation_epoch` 2026-08-19T13:00Z.

**Disposition.** Every finding below with a code correction is **fixed and
merged** (branch `audit/fixes-0820`, 28 commits, one per finding, each with a
regression test that fails without it; suite 424 → 465 green). Exactly two
findings are deliberately NOT fixed in code — **MARGIN-K25** and
**GTC-LATENCY** — because their correction is a *measurement*, not a code
change; picking a number for either would be the bar-tuning the project
forbids. Both are now ranked open problems in RESEARCH.md (#2 and #2b) with
their unblocking conditions stated. `validation_epoch` was re-pinned to
2026-08-21T04:30Z because these fixes change decision behavior and the bar
must never average across a change.

---

## Findings summary

| ID | Sev | Site | One line |
|---|---|---|---|
| F-9 | **S0** | `maker_bid.py:290-321` | Shutdown cancellation loses accrued ladder fills — real shares, no DB row |
| ORPHAN-TRUST | **S0** | `main.py:1702-1707` | Orphan resolution accepts an untrusted boundary capture |
| KILL-WINDOW | S1 | `analyze_late_window.py:140-143` | "Trailing 4 days" is 4 *fill-bearing* days, spanning any calendar range |
| KILL-AGGREGATION | S1 | `analyze_late_window.py:140-158` | Dollars verdict sums all legs; a bleeding leg hides behind a winning one |
| GTC-LATENCY | S1 | `paper_trader.py:293-295` | Paper's GTC round trip is 56 ms and unmeasured; replay implies ~500 ms |
| MARGIN-K25 | S1 | `signal_engine.py:31-36` | Adding 08-18 puts p99.5(k=25) at ~$10.0 vs frozen $8.0 |
| BRIDGE-VALUE | S1 | `chainlink_feed.py:170-190` | No sanity band on the Binance relay value feeding the bridge |
| BRIDGE-ANCHOR | S1 | `chainlink_feed.py:182-190` | No anchor-age guard; a ring hole double-counts movement |
| SOURCE-SILENT | S1 | `recording.py:326-330` | Bare `except: return`, no log — a slug change blinds the SOURCE gate |
| CLOB-GAP | S1 | `clob_ws.py:157-182` | Prints during a reconnect are never delivered or replayed |
| PAPER-RESTART | S1 | `main.py:2394` | Paper has no orphan recovery; a restart deletes a kill-bar sample |
| ZONE-58 | S1 | `settings.yaml` | `twap_zone_s: 60` thins margin in (58,60], past the last knot |
| F-7 | S2→S0 path | `scheduler.py:61` | `wait_for` cannot stop a `to_thread` job; zombies starve the executor |
| F-1 | S2 | `chainlink_feed.py:297-317` | One rescaled timestamp evicts every boundary capture, silently |
| F-3 | S2 | `chainlink_feed.py:497` | `twap_frozen` evaded by last-digit jitter |
| F-2 | S2 | `chainlink_feed.py:182,453` | Binance ring assumes ordering nothing enforces |
| F-5 | S2 | `chainlink_feed.py:122-132` | `get_strike` serves untrusted captures into `window_paths` |
| F-8 | S2 | `main.py:2934` | Shutdown watchdog armed after the calls that can hang |
| T3-ABSENT | S2 | `recording.py:588-602` | The `t3` A/B tape has zero records — the documented net does not exist |
| WS1-MAX | S2 | `ws1_freeze_tables.py:33-38` | Freeze script fits MAX at grid points, not interval maxima |
| DEPLOY-CAP | S2 | `base.py:400` | `book_maker_fill` checks free cash, not `max_bankroll_deployed` |
| LADDER-OPEN | S2 | `main.py:910-913` | Ladder places without checking open positions |
| LATENCY-DARK | S2 | `main.py:2643` | `latency_stats.json` n=2, 7 d old — the POST-RTT watch has never fired |
| SPOT-CLOCK | S2 | `chainlink_feed.py:209` | Spot-age gate ignores `now=`; replay-only impact |

S3 items are listed in full in the S3 section below.

---

## Part 1 — Money-path correctness

**1.1 Fill booking — VERIFIED.** All 7 post-migration paper fills and the last
20 live fills recomputed from first principles (shares × entry, maker blending
via `book_maker_fill`, chain-truth corrections) reproduce booked pnl and fees to
1e-9 through the real `exit_fee_usdc` / `_entry_fee_usd_from_position`. The
bankroll identity holds exactly: 150 + Σpnl = 182.545571. The +8s audit's four
edge cases each VERIFIED: `sync_entry_booking` refuses post-close rewrites; a
failed chain lookup stays provisional and writes nothing; duplicate-market
preflight covers two fills in one window; a partial rung books only the filled
part. The +8s audit is taker-only (`base.py:371`) and never touches maker fills.

**1.2 Fee math — VERIFIED.** `EFFECTIVE_FEE_PEAK` (0.0175) exists at exactly two
sites — its definition and the single flat-additive gate at `main.py:1425`. No
gate mixes the additive and multiplicative forms. Every read of
`trade_history.fees` was traced: the kill rule and health read never touch it
(`analyze_late_window.py:117` documents "pnl already nets all fees — never
subtract `fees` again"); `get_day_stats` is display-only.

**1.3 Sizing chain — VERIFIED.** Adversarial unit tests against the real
pipeline: tier boundaries inclusive (`>=`), floor = tier × 0.85, sqrt
interpolation exact, never resets down, `restore_from_peak` ordering correct.
Depth exactly $50 passes (strict `<`); size exactly $1.00 passes. `ask + edge`
cannot cross 1.0 — bounded by the edge gate at `signal_engine.py:130`. The
5-share floor kills the 0.80 rung first at bankroll < $133.33, then
108.33 / 83.33 / 58.33 / 33.33; the drop **is** logged at INFO
(`maker_bid.py:156`) and the budget deliberately does not redistribute, so the
settings.yaml "silently drops" comment is imprecise but the behavior is sound.

**1.4 Resolution booking — VERIFIED except ORPHAN-TRUST (below).** `close_position`
is a single transaction and a re-resolve returns "already closed", so a crash
between resolution and bankroll sync cannot double-credit; boot re-derives the
live bankroll from the wallet (`main.py:2388`). Tie → Up is implemented as `>=`
at all six winner sites (`main.py:1524/1586/1715`, `maker_bid.py:233`,
`recording.py:310`, `signal_engine.py:113`) and behaviorally tested at
final == strike.

> **FINDING ORPHAN-TRUST · S0 · `main.py:1702-1707`.** The orphan fallback gates
> on `chainlink_feed.boundary_captured()`, which is presence-only
> (`chainlink_feed.py:268`: `window_ts in self._boundary_prices`). The trust
> predicate is `strike_reliable()` (`:270-284`), which adds the 0.5 s payload-ts
> check. So a delivery-hole capture — a later second's average — can decide a
> real position's winner. The sibling tape-verdict path at `main.py:1571-1574`
> checks trust correctly, and the comment directly above the defect
> (`:1695-1697`) shows the author guarding this exact "fake Up win" risk with
> the wrong function. Graded S0, above the subagent's S1: the ladder grades
> wrong *resolution* as S0 by kind, not by probability of reaching the path.
> **Staged:** `strike_reliable()` at both lines.

**1.5 Kill rule — TWO FINDINGS.** The lock_dip one-loss rule, the epoch filter
(kill rule and health read share one reader, `main.py:2599-2604`), and dormancy
are all VERIFIED: a dormant-taker would-be fire is rewritten to SKIP at
`main.py:891` before any order exists, and ghosts never reach `trade_history`.

> **FINDING KILL-WINDOW · S1 · `analyze_late_window.py:140-143`.** `per_day`
> contains only fill-bearing ET days, so `usd_daily[-4:]` and `fills_trailing4`
> mean "the last 4 days that had fills" — a window that can span an arbitrary
> calendar range. A leg quiet for three weeks is still judged on its last four
> active days. The documented rule is a trailing-4-**ET-day** window.
> **Staged:** calendar ET window, zero-filled.
>
> Related, **not** staged: the ≥5-fills-in-4-days threshold is unreachable for
> any leg filling ≤1×/day, so such a leg can bleed indefinitely without a
> verdict (demonstrated: 30 losing days, −$90, verdict `None`). A calendar
> window does not fix that. Changing the threshold is a policy decision for the
> operator, not an audit correction. Note the currently-dormant `lock_dip` leg
> is covered separately by its one-loss rule; the exposure is `deep_proj` in a
> quiet regime.

> **FINDING KILL-AGGREGATION · S1 · `analyze_late_window.py:140-158`.** The
> dollars verdict sums every leg into one series, so a leg losing $9/day hides
> behind a leg making $10/day — even though per-leg stats are computed
> separately at `:159` and CLAUDE.md §2 defines the bar per leg.
> **Staged:** per-`signal_leg` verdict.

---

## Part 2 — Signal-path correctness

**2.1 Strike — VERIFIED.** 24-case property matrix (payload ts ∈
{B−1, B, B+0.4, B+0.5, B+0.6, B+1} × rx lag ∈ {0, 1.7, 5} s): strike is the
first at/after report by payload ts; trust is `≤ 0.5 s` inclusive; delivery lag
never flips trust (zero rx terms at `chainlink_feed.py:284-287`); a pre-boundary
hole does not veto; `setdefault` ordering holds; a genuine hole reads UNTRUSTED.
Post-deploy mechanism check: 80/80 finals and 80/81 strikes bit-exact, the one
miss being `ep 1787070000`, the deploy window itself.

**2.2 Projection — VERIFIED.** An independent re-derivation from the definition
diffed against the shipped code over **823 (window, k) pairs across 120
windows: worst |Δ| = 0.000000000**. Estimator vs served final is p50 $0.0286 /
p90 $0.196, matching the documented $0.028/$0.22. The coverage guard, 3 s
spot-age refusal, trailing-gap guard and zone bounds each genuinely veto, and
`None` propagates to SKIP / no-place / retire. Zero surviving `30`-second
literals in feed, signal, ladder or execution code.

**2.3 Bridge — VERIFIED as to collapse, TWO FINDINGS as to inputs.** `bridged ==
plain` exactly on all five collapse modes; negative delta time is unreachable;
all four `projected_final_twap` call sites enumerated, with the taker and the
dormant would-be-fire log seeing plain only; `twap_proj` and `twap_proj_plain`
are both stamped, None never 0.0.

> **FINDING BRIDGE-VALUE · S1 · `chainlink_feed.py:170-190`.** No sanity band on
> the relay value: a 10× decimal slip in one Binance tick produced a **−$58,509**
> delta that flowed into `projected_final_twap(bridged=True)` — the value that
> picks the ladder's side (`main.py:908-925`) and that the cancel floor rechecks
> each tick (`maker_bid.py:260-273`). Nothing logs. The documented "collapses to
> plain, never to a guess" contract is unreachable for a bad *value*.
> **Staged:** bound the delta, collapse to plain, warn once.

> **FINDING BRIDGE-ANCHOR · S1 · `chainlink_feed.py:182-190`.** No anchor-age
> guard, so a hole in the ring makes the delta double-count movement the
> Chainlink report already contains. Live-semantics replay over 40,601
> evaluations: p99 0.00 s but max 6.0 s stale anchor, worst injected error
> −$10.02 at `ep 1787090400` (~$2 projection shift at k=12 against a $3.50
> floor). **Staged:** `if obs_ts - anchor_ts > 2.0: return 0.0`.

**2.4 Margin tables — 15/16 knots VERIFIED, ONE FINDING.** An independent
implementation (own ZOH + per-tick interval maxima, not importing `ws1_*`)
reproduces 15 of 16 p99.5 knots exactly on the freeze span; the k=10 frozen knot
is wider than computed, i.e. conservative. The union logic was audited at code
level: synthetic windows enter MAX only and never touch p99.5, as documented.
The lookup clamps at both ends and the low-side clamp is conservative and chosen.

> **FINDING MARGIN-K25 · S1 · `signal_engine.py:31-36`.** Adding 08-18 — the day
> the freeze excluded — puts p99.5 outside the frozen envelope at several knots,
> most importantly **k=25: $10.0 computed vs $8.0 frozen (+25%)**; also k=40
> ($24 vs $18), k=12 ($4.5 vs $3.5), and k=8/20/29/35 thin. k=25 is exactly
> `maker_k_place_max`, and **5 of the 7 realized paper fills armed at k ≈ 24.99–25.0**,
> so live arming sits precisely where the table is thinnest.
> **Reported, not staged.** The correction is the scheduled ≥14-real-final-day
> re-fit; editing knots here would be measurement by fiat. The actionable
> conclusion is that the re-fit should be expected to **widen**, and that
> `need 1.0` is currently absorbing an under-stated error at the arming edge.

> **FINDING ZONE-58 · S1 (latent) · `settings.yaml late_window.twap_zone_s`.**
> The tables stop at k=58 but the decision zone is 60, so (58,60] clamps to the
> k=58 margin where the true error is strictly larger — margin thinning at
> unfitted k. Reaches capital only through the dormant taker.
> **Staged:** `twap_zone_s: 58.0`.

**2.5 Ladder state machine — VERIFIED.** Arm and cancel boundaries are both
inclusive-at-equal and mutually consistent (`maker_bid.py:145` vs `:268` vs
`main.py:921`); sign flip, noise and cold inputs all cancel; k<6 keeps resting;
k>25 with rungs resting is unreachable; the post-close hold consults only
`certain_winner` and never re-enters the projection; a 5 s grace then fails
closed; hold expiry correct at 59.9/60.1; one ladder at a time; taker suppressed
while rungs rest; paper pays the GTC round trip on place **and** cancel.

---

## Part 3 — Paper realism (the binding gate's own correctness)

**3.5 The deep-rung asymmetry — HYPOTHESIS REFUTED.** During this audit the lead
auditor raised a hypothesis: that paper's at-price rule structurally suppresses
deep-rung fills, and since the deep rungs are what lost real money in the live
probe, paper would be filtering out losers and inflating the measured edge. It
is recorded here as a **documented negative result**.

Driving the real `MakerBidManager.on_print` over the real tape across each
ladder's actual rest interval reproduces live **exactly** on live fills 333
($22.55), 335 ($4.88), 336 ($1.51) and 337 ($1.50), over-crediting only 334
($11.21 vs $3.74). Paper books $41.65 against live's $34.18 — it books **more**
loss, not less. Paper credited the 0.20 rung in three of the five windows from
strictly-below prints; the at-price arm suppressed nothing (deep-level at-price
volume 30/0/0 shares against the 135 threshold). The harness was validated by
reproducing paper fill 9584 bit-for-bit (34.4 shares, $18.00 vs $17.99 booked).

The observed "3 of 5 live fills hit the 0.20 rung" pattern is a **bankroll
artifact**: at the live probe's bankroll the per-rung budget was $1.50–$4.51, so
`MIN_SHARES = 5` stripped the shallow rungs *before placement* — the same
mechanism Part 1.3 measured from the other direction (the 0.80 rung dies first,
below $133.33). Every rung actually placed filled in full in 4 of 5 windows.

Separately: under the shipped 60 s table at need 1.0, **4 of the 5 live losers
would not arm today** (headroom 0.88 / 0.78 / 1.19 / 0.87 / 0.92). Weakly
powered — n=5, one confounded era — but it points the right way.

> **FINDING GTC-LATENCY · S1 · `paper_trader.py:293-295`.** The replay that
> refuted the above surfaced a real defect in its place. Paper credits live 334
> from prints landing 0.03 s and 0.07 s after placement. Sweeping the placement
> offset: 0.000/0.224/0.300 s all yield $41.65, and only 0.500 s reproduces the
> live $32.67. Root cause: `_GTC_LATENCY_QUANTILES` p50 is **56 ms per rung**
> while the empirical reconstruction needs ~500 ms. Paper's ladder becomes
> matchable roughly twice as fast as the real one, buying fills in the tenths of
> a second while the triggering sweep is still printing — +$7.47 on a $34.18
> sample. The error is two-sided (it helps winners and hurts losers), so it
> distorts the gate in an unknown direction. The constant's own commit is titled
> "Paper pays the MEASURED GTC round trip" but `latency_stats.json` has **no
> `gtc` section at all** — the docstring's claim to be measured is unsupported.
> **Reported, not staged**: the correction is a measurement, and silently
> retuning a constant that moves the deployment gate is not an auditor's call.
> Unblocks with 3.3 below.

**3.1 Live-fill reproduction — see 3.5.** Paper credits every live maker fill,
one of them over-generously; no live fill is missed. Conservative on fill count,
non-conservative on timing (GTC-LATENCY).

**3.2 `AT_PRICE_QUEUE_SH` — VERIFIED.** Recomputed with the ops watch's own
definition via the real `queue_depth_read`: 14-day p75 = **99.0 sh** (median 31,
n = 56,333), 7-day 84.2 — that is **0.73×** the 135 constant, i.e. drifted in the
safe direction (paper under-credits at-price fills). The watch was proven to
fire by driving the real predicate with a synthetic 400-sh day (`main.py:2745`
True). *S3:* the watch is one-sided and stays silent on the shrink the tape
actually shows, which makes paper under-credit fills on a gate that counts fills.

**3.3 GTC place/cancel RTT — UNVERIFIABLE.** No such measurement exists: the 337
live rows carry `cb_tick_to_submit_ms` / `lat_*` only, the five `deep_proj` rows
carry none, and `latency_stats.json` has no `gtc` section. **Unblocks on** either
`smoke_gtc_test.py --confirm` persisting its samples, or a
`t_post_return − t_post_start` stamp in the ladder snapshot. This is the gating
datum for GTC-LATENCY above.

**3.4 FOK latency sampling — FINDING.** `latency_stats.json` holds **n = 2**,
p50 302.9 ms, last updated 2026-08-13. The sampler over 20k draws gives p50
414 ms / p75 644 / p99 1567; a KS test is meaningless at n=2.

> **FINDING LATENCY-DARK · S2 · `main.py:2643`.** The ops line requires
> `n >= 10 and age <= 7d`, so the POST-RTT watch has **never surfaced**, and its
> read is a bare `except: pass`. The one watch for `paper_latency_scale` drift is
> dark while the file itself sits 31% below the 436 ms anchor. *S3:* the 0.95
> scale is unsupported by any current measurement; it feeds only the FOK/warm-SELL
> path, so no capital touches it while `lock_dip` is dormant — re-measure before
> re-arming.

---

## Part 4 — Silent-failure sweep

**4.1 Clock discipline — VERIFIED.** Every timestamp comparison in `feeds/`,
`core/`, `recording.py` and the time-gating parts of `main.py` /
`agents/scheduler.py` was classified payload / rx / wall / mixed. The 3 s
spot-age gate is rx-anchored, so it does **not** eat the 1.6–1.8 s delivery lag;
`RAW_GAP_MAX_S` is an rx-gap as documented; boundary trust is payload-only with
zero rx terms; the one mixed comparison (`clob_ws.py:255-261`) is deliberate
delivery-lag telemetry that no decision consumes. ET/UTC handling is DST-safe
throughout (`ZoneInfo`; no `pytz`, `utcnow`, or fixed offsets anywhere) and
storage stays UTC.

Micro-tape ordering: the file is **rx-sorted**, and `ts` is not one clock —
`l`/`t`/`t3`/`s·bz` carry payload in `ts` plus a separate `rx`, but `b` rows
carry rx *in* `ts` (`recording.py:557`) and `s·cb` rows have no `ts` key at all
(`:614`). Every current reader discriminates correctly. *S3 latent:* a future
reader merging `b` and `l` on one `ts` key inherits a systematic ~1.7 s
book/oracle skew, and `r["ts"]` on an `s` row would `KeyError`.

**4.2 Feed pathology matrix — 30 cells, guards named or findings filed.** The
findings that came out of it:

> **FINDING F-7 · S2 with an S0 escalation path · `scheduler.py:61`.** This is
> the root cause of every nightly pathology in the current log.
> `asyncio.wait_for(job(), 600.0)` cannot stop the work, because every job body
> is `asyncio.to_thread`: cancelling the awaiting coroutine leaves the **thread
> running**. So `"timed out at 600s — skipped"` means the bot stopped *waiting*,
> not that the work stopped. Registration order is `maker_ladder` →
> `recordings_retention` → `sniper_health` → `compress_recordings`;
> `maker_ladder` reads ~2.5 GB of tape on a 1-core box and overruns, the
> scheduler moves on, and `sniper_health` starts its own read of the same day
> *concurrently with the zombie* — both thrash, both time out. That is exactly
> 08-15/16/17, and the same property on the inner 240 s `wait_for`
> (`main.py:2586`) explains `sim: None` on 08-18/19/20. The documented ordering
> guarantee at `main.py:2808-2809` ("compress only after they are done with the
> tape") is violated: `compress` unlinks `micro_*.jsonl` while a zombie may hold
> it open. The default executor is `min(32, cpu+4)` = **5 workers**, so
> accumulated zombies can starve the money path's own `to_thread` calls
> (`place_gtc_bid`, `cancel_gtc`, `poll_gtc_fill`) — the S0 escalation.
> Half-written artifacts were checked and are **not** a risk: compress, feed
> state and margin saves all use atomic `tmp.replace` / `write_json_atomic`.
> **Staged: the truthful log only.** Executor isolation was implemented and then
> deliberately backed out: swapping `loop._default_executor` for the duration of
> the pipeline also captures money-path `to_thread` calls, and `!pipeline` is an
> operator command that can run during trading — that would queue
> `place_gtc_bid` / `cancel_gtc` / `poll_gtc_fill` behind a 600 s tape read on a
> single worker, which is worse than the bug. The scheduled 23:45 ET run is not
> exposed (trading stops 23:30 ET) and zombies die with the midnight restart, so
> the S0 escalation is reachable only via `!pipeline` during trading. The correct
> fix routes each job body through a dedicated executor explicitly (~9
> `to_thread` sites) and is left for operator review.

> **FINDING CLOB-GAP · S1 · `clob_ws.py:157-182`.** Prints arriving during a
> reconnect are never delivered, and the resubscribe requests `initial_dump:
> True`, which replays the **book only, never trades**. Since
> `MakerBidManager.on_print` is the *entire* paper fill mechanism, a resting
> paper ladder silently under-counts fills at the observed 1–3 reconnects/day,
> and the disconnect logs at DEBUG. This corrupts the deployment authority.
> (The subagent attributed this to `trade_buffer.clear()` at `:155`; the clear
> affects windowed analytics — the fill loss is the undelivered-print gap.)
> **Fixed:** the disconnect now logs at WARNING and records a print-gap
> timestamp that a ladder resting across it stamps onto its fill record, so
> gap-affected samples are identifiable in the validation ledger.

> **FINDING SOURCE-SILENT · S1 · `recording.py:326-330`.** A bare
> `except Exception: return` with **no log at any level**. A slug format change
> such as `…-1786800000-v2` raises, the handler returns, and every window in
> that format goes unchecked forever. The one gate built to catch an unannounced
> upstream change is itself disabled by an unannounced upstream change.
> **Staged:** log at ERROR and count unchecked windows.

> **FINDING F-1 · S2 · `chainlink_feed.py:297-317`.** `_epoch_seconds` rescales
> once (`/1000 if ts > 1e11`). A µs- or ns-scaled `timestamp` — precisely a
> schema change that still parses — survives as ~1.79e12, and
> `cutoff = int(observed_ts) − 7200` then evicts **every** real capture and
> poisons `_last_twap_ts`. `certain_winner` goes None, `boundary_snapshot()`
> returns `{}`, and both the nightly `mechanism_read` and the per-window SOURCE
> gate go dark. No log at any level.

> **FINDING F-3 · S2 · `chainlink_feed.py:497`.** `twap_frozen` re-arms on any
> difference including 1e-9, so last-digit jitter evades the stall veto: proven
> with raw travelling $19.50 over 40 s while the official value held
> 65003.4548 ± 1e-9 and `twap_frozen()` stayed False. Fix: arm on
> `abs(_v − twap_official) < 0.005`.

> **FINDING F-2 · S2 · `chainlink_feed.py:182,453`.** The Binance ring assumes
> `[-1]` is newest and the anchor scan breaks on the first later ts, but nothing
> sorts and the append is unconditional. One out-of-order tick silently reverts
> the ladder to the plain projection; a future-dated tick empties the ring,
> because the prune keys off the incoming ts.

> **FINDING F-5 · S2 · `chainlink_feed.py:122-132`.** `get_strike` returns a
> capture regardless of trust and is consumed un-gated by
> `WindowPathRecorder._sample` (`recording.py:360-361`) into the `window_paths.strike`
> corpus. Labels come from Gamma and are safe; the sampled column is not, and
> nothing marks its trust.

> **FINDING F-8 · S2 · `main.py:2934`.** The 30 s `threading.Timer` shutdown
> watchdog is armed at the *end* of the `finally`, after
> `await db.get_bankroll()` / `db.close()` (`:2917-2918`). A wedged aiosqlite
> worker hangs shutdown *before the watchdog exists* — the 08-17 failure mode at
> a different hang point. Arm it as the first statement of the `finally`.

> **FINDING T3-ABSENT · S2 · `recording.py:588-602`.** The retired-30s A/B tape
> has **zero records on all five days**, confirmed against the raw tapes by
> literal key scan including the 08-18 deploy window. The writer is wired and the
> topic subscribed, but the handler never fires — RTDS is not serving
> `crypto_prices_twap_thirty`, silently. CLAUDE.md §6 documents this tape as the
> A/B evidence against the next silent source swap; **that net does not exist.**

> **FINDING WS1-MAX · S2 · `ws1_freeze_tables.py:33-38`.** The script named
> "freeze" fits MAX at grid points (`mx[k]=xs[-1]`), not the per-tick interval
> maxima that `signal_engine.py:25-28` documents (those live in
> `ws1_interval_max.py`). Re-running it at the ≥14-day re-fit would ship
> **under-bounding** MAX knots. Its input `data/ws1_errors60.csv` is also absent,
> so the freeze is not currently reproducible.

> **FINDING DEPLOY-CAP · S2 · `base.py:400`.** `book_maker_fill` checks free cash
> only, never `max_bankroll_deployed`; a test reached 95% of equity while
> `open_trade` refused the same $15.

> **FINDING LADDER-OPEN · S2 · `main.py:910-913`.** The ladder places without
> checking open positions; a same-window taker position makes `book_maker_fill`
> refuse, leaving unbooked exchange shares. Masked today only by
> `taker_enabled: false`.

**What the SOURCE gate does not cover** (stated explicitly, since it is the
system's primary net): both served *and* captured wrong — if Polymarket moves to
a stream we do not subscribe to, `caps.get(b)` is None and the loop simply
continues; mid-window poisoning that never reaches a label, since the gate runs
once per resolved window; aged-out or post-restart captures, as
`boundary_snapshot()` is in-memory and ~2 h; and it latches on first fire
(`_source_mismatch_fired`), so a false positive permanently disarms it. Most
importantly, **the Binance relay feeding the bridge has no watch of any kind** —
not in the feed, not in `_staleness` (which counts raw only), not in the nightly
ping. It is the only decision input with zero validation of value or order; see
BRIDGE-VALUE.

**4.3 Restart matrix — completed on evident intent (charter truncated).**

| State at death | Verdict |
|---|---|
| (a) rungs resting, LIVE | **VERIFIED covered** — `cancel_all` (`main.py:2311`) precedes trading; `detect_orphan_positions` (`:2394`) enumerates chain vs DB and raises on any unresolved unknown or API/DB failure (fail-closed). Residual: F-9, and the S3 log at `:2311-2314`. |
| (b) rungs resting, PAPER | **FINDING PAPER-RESTART · S1** — no persistence, and `detect_orphan_positions` is `hasattr`-guarded so `PaperTrader` skips it entirely. Every mid-day restart silently deletes a sample from the kill-bar ledger, with no counter. |
| (c) taker FOK in flight | **VERIFIED** — no blind in-process retry; across a crash a settled fill has no DB row and the orphan check fails closed. |
| (d) during post-close hold | **VERIFIED (live) / S1 (paper)** — same as (b). |
| (e) resolution → bankroll sync | **VERIFIED** — `mark_pending_resolution` precedes payout confirmation, boot re-derives from the real wallet, day-close waits on `pending_resolution`, `peak_bankroll` is max-merged. |
| (f) mid-nightly-job | **FINDING (F-7, F-8)** — no restart happens mid-job by design and artifacts are atomic, but the timeout is advisory and the watchdog does not cover `db.close()`. |
| (g) mid-day crash, 60 s systemd restart mid-window | **VERIFIED, no double entry** — OS socket lock plus re-entry block at `main.py:1461-1467` via slug-keyed `has_open_or_pending_market`, the same key `book_maker_fill` books under; a not-yet-built mirror correctly falls back to the async query rather than to `False`. |

---

## S3 findings

- `maker_fill` / `maker_rebate` are **never written anywhere**; all 12 real maker
  fills across both ledgers read `maker_fill = 0`. Fill type is only recoverable
  from `trade_context.signal_leg`.
- Live rows 333-337 booked **2.45–5.60% fewer shares** than paid for (`shares_held`
  short, `fees` carrying the full taker model at ratio 1.0000). Stale residue —
  current `book_maker_fill` sets `fee_in_shares = 0.0` (`base.py:410`) against a
  real taker fee at `:335`. All five lost, so nothing was realized; on a winner
  this would have short-paid the payout.
- `maker_bid.py:312-320` rounds vwap to 4 dp then re-derives notional, so the
  debit ≠ Σ(filled × price).
- `rungs[*]["cancelled"]` is read at `maker_bid.py:199, 282, 295, 307` and
  **written nowhere** — four dead money-path branches.
- `main.py:2311-2314` logs "Boot order sweep — no resting orders carried over"
  unconditionally, and a *failed* `cancel_all` only WARNs while boot continues
  with unknown live orders.
- Gamma `price_to_beat` sets `_strike_trusted = True` unconditionally
  (`main.py:1246-1254`). The behavior is defensible — the served value is the
  resolution source — but CLAUDE.md §1's blanket "no leg deploys capital on it"
  is false as written and needs one clause.
- CLAUDE.md §4 promises `MAKER BID REJECTED` at ERROR for "any rejection", but
  the 5-share starve logs `MAKER RUNG SKIPPED` at INFO and `legal_price` clamps
  silently; that literal exists only at `live_trader.py:617`.
- 60s-rule prose drift in live code: `signal_engine.py:3`,
  `chainlink_feed.py:66, 68-69, 249` still say "30s".
- The queue-depth ops watch is one-sided (warns high, silent on the shrink the
  tape shows).
- `clob_ws.py` names our own receipt clock `timestamp` while the exchange clock is
  `exchange_ts` — a naming hazard for future readers.
- `chainlink_feed.py:528-529` and `clob_ws.py:228` swallow malformed messages with
  no per-topic counter; a schema change could drop 100% of a topic with only the
  60 s watchdog as backstop.
- `chainlink_feed.py:209` — `projected_final_twap` honours `now=` everywhere
  except the spot-age gate, which reads `time.time()`. Live is unaffected
  (`main.py:421` passes the wall clock); any replay gets a bogus staleness verdict.
- `on_cb_tick` is dead code — no `CoinbaseFeed` reference remains in `main.py`.

---

## Corpus caveat for any future re-derivation

The pre-08-18 tape's own `twap_sixty` boundary captures match served values only
**8%** of the time (p50 $0.74, max $38.17): the old subscription delivered a
~12–23 s lagged variant. This does **not** affect the frozen tables, which were
fit against served finals that our raw ZOH reproduces to $0.03. But any
re-derivation that treats the *tape's* captures as ground truth on 08-14..17
will be poisoned. Use served values.
