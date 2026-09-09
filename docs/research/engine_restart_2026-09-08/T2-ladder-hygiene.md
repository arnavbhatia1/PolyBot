# T2 — Ladder hygiene and per-rung schedules (engine-true), 2026-09-09

Slice of the 09-08 engine-restart program, pre-registration `docs/research/engine_restart_2026-09-08.md` §T2 (bars frozen). Everything below is computed locally on the pulled corpus (`win_streams.jsonl.gz`, 7,018 era windows, 26 ET days 08-13..09-07; tape/micro recordings; book-dynamics `micro_b_dedup.parquet`). Nothing touched the VPS, the bot, or any websocket; no tracked repo file was edited (repo `ws2_ladder_replay.py` md5 `fbce52f8899c8595dc02fd576792518c` before and after). Harness: `ladder_replay_v2.py` (this directory), a copy of the repo harness with the change list in its module docstring. All dollar totals are counted once per WINDOW (one booked ladder per window); per-rung tables count windows per rung and therefore sum to more than the distinct fill count by construction. **Measured** = replay/tape/log fact; **computed** = derived statistic; **inferred** = interpretation.

## 0. Verdicts first (pre-declared cells, literal bars)

| bar | pre-declared cell | measured | verdict |
|---|---|---|---|
| H1 faithfulness | re-arm ON, need 0.6, k_max 25, R8, $200: eps 1787271900 / 1787358600 / 1787620800 flip-fill losses; ep 1788743700 at k_max 58 | 3/3 reproduce as losses (-100.00 arm 3/3, -100.00 arm 2/2, -75.00 arm 3/3); 1788743700 @k58 -75.00 (arm 1, k 49.7, 119.7 sh) | **PASS** |
| H1 adoption: one arm per window iff loser $ removed > winner $ forgone in BOTH halves | k25, protocol exclusion | H1-half removed 100.00 vs forgone 6.25 (2 re-arm fills); H2-half removed 0.00 vs forgone 0.00 (0 re-arm fills) | **FAIL** (second half 0 vs 0: the strict inequality is not met; first half passes 16:1) |
| H2 adoption: reprice cancel iff loser $ removed >= 2 x winner $ forgone in both halves, ANTI unchanged | k25, thr 0.05, re-arm ON (production), protocol exclusion | H1-half removed 100.00 vs forgone 1070.31; H2-half removed 0.00 vs forgone 12.50; ANTI -808,417 -> -805,367 (changed) | **FAIL** |
| H3 S1 over S0 iff $ >= S0 both halves, losses <= S0, no flip-fill loss on a relaxed rung, ANTI <= 0 | k25, re-arm ON, protocol exclusion | $ +797.03 vs S0 +995.31 (H1-half +769.35 vs +982.81, H2-half +27.68 vs +12.50); losses 5 vs 1; relaxed-rung flip-fill loss windows 5; ANTI -755,342 | **FAIL** |
| H3 S2 over S0 (same rule) | k25, re-arm ON, protocol exclusion | $ +836.09 vs S0 +995.31 (H1-half +761.98 vs +982.81, H2-half +74.11 vs +12.50); losses 7 vs 1; relaxed-rung flip-fill loss windows 7; ANTI -724,828 | **FAIL** |
| H4 descriptive frontier | need {0.6,0.8,1.0} x k_max {15,25}, re-arm modeled | table in §6; no adoption bar (owned by the 09-11 re-decision) | UNDECIDABLE (descriptive, non-adoptive) |

Every verdict above holds under all three exclusion variants (§2.3): the pre-registered `dead_book_windows.csv` (253), confirmed-dead-only (225), and none.

## 1. Regression check and harness

- **Unmodified repo harness** (read-only import, `reg_check.py`): need 0.6 / k_max 25 / R8 / $200 / `r1_tables()['P995']` -> armed 6358, fills 17, wins 17, +1,882.72 — **identical to RESEARCH.md 09-08 (r27)**: 17 / 17 / +$1,882.72. k_max 58: 28 fills / 26 wins / +1,956.28, ep 1788743700 armed Up k 49.7, 119.71 sh, -75.00 (RESEARCH.md: −$75.00). load_corpus 34.6 s, run() 2.0 / 3.9 s.
- **Table identity** (pre-mortem 12): `r1_tables()['P995']` == `polybot.core.signal_engine.TWAP_MARGIN_P995` value-for-value: True (asserted in `t2_tests.py`). No locally re-fit table exists anywhere in T2; no model is fitted (pre-mortem 4 is moot — nothing is trained).
- **run2(rearm=False, pc_mode='label') == run()** row-for-row (side, why, fills, rungs, place_k, pnl): 6358/6358 rows at k_max 25 and 6394/6394 at k_max 58 (asserted).
- **Unit tests** (pre-mortem 14, synthetic window, deployed table): zero-fill floor cancel -> re-arms (2 arms) [PASS]; partial fill on arm 1 (0.80 rung, $25 notional) -> booked, no re-arm [PASS]; flip -> re-arm on the SAME tick on the other side [PASS]; sub-$1 unbooked fill (at-price credit 5 sh at 0.05) -> dropped as `_book` does, re-arm allowed [PASS]; rearm=False -> 1 arm [PASS].
- Harness changes (full list in the `ladder_replay_v2.py` docstring): (1) absolute paths; (2) `run2()` re-arm state machine; (3) per-rung `needs` with production semantics; (4) `pc_mode='capture'` = `certain_winner()` replica; (5) H2 reprice cancel from micro BBO; (6) `exclude`; (7) helpers. The original `run()` is untouched.

### 1.1 What production does after a cancel (code, read this session)

- `maker_bid.maintain()` cancels every rung and calls `_retire(reason)` when the signed displacement drops under `min_need() * p99.5(k)` ("projection flipped" if signed <= 0, else "sign inside noise"), when the projection is None ("projection cold"), when every rung is full ("filled"), or post-close ("lock missed the winner" / "outcome unverified" after 5 s / "post-close hold over") [maker_bid.py:244-295].
- `_retire()` cancels the GTCs, re-polls live fills, then ALWAYS `_book(a, reason)` and ALWAYS sets `self.active = None` [maker_bid.py:298-331]. `_book` books ONE blended position iff `filled > 0 and notional >= MIN_NOTIONAL_USD ($1)`; otherwise it logs MAKER OFF and books nothing [maker_bid.py:333-370].
- The placement hook runs on every decision tick where the ladder is not resting (`resting_on` false, main.py:998) and refuses ONLY when `db.has_open_or_pending_market(cid)` / `has_position_for_market(cid)` is true — i.e. a position with status open/pending_resolution exists for the window [main.py:1046-1055; db/models.py:151-155, 381-387]. Otherwise `consider_placement` arms again the moment `|disp| >= min_need() * margin(k)` with k in [k_min, k_max] [main.py:1055-1079].
- **Zero-fill floor cancel -> the ladder re-arms** (any number of times while k stays in [6, 25]). **Partial fill -> booked at retire -> position exists -> no re-arm** in that window. `maintain()` runs BEFORE the entry evaluation in each loop iteration [main.py:2246 vs 2255+], so a flip cancel and the opposite-side re-arm can happen in the same iteration.
- Logged evidence (pre-mortem 13): `docs/audit/data/polybot.log.1` + `polybot.log` (07-13..08-27) hold 2805 era windows with a MAKER LADDER line, 134 of them with >= 2 ladder lines (real re-arms); the OFF reason before a re-arm was 'sign inside noise' 113x, 'projection cold' 56x, 'projection flipped' 6x, post-close 6x; 5 of 181 re-arms changed side; **0 fills were booked on an arm >= 2 in that log period** (configs then: need 2.0 -> 1.0 on the pre-08-27 tables). The re-arm state machine is therefore validated by production logs, not only by code reading.
- Cross-check of the harness against those logs at the then-deployed config (need 1.0, old P995, k_max 25, 5 rungs; 08-18 17:02Z..08-27 19:03Z; `log_crosscheck.py`): 2094 windows armed in both (log-only 13, harness-only 346 — production also has trading hours, book gates and strike-trust vetoes the harness lacks); first-arm k log−harness median +0.45 s, p10/p90 -0.73/+1.01 s, 90% within 2 s (N 2094); floor/flip re-arm windows: log 66, harness 42, both 34. The harness UNDER-counts re-arms (production evaluates a continuously drifting projection at 4 Hz; the harness at ~1 Hz raw ticks), so every re-arm exposure number below is a lower bound on production.

## 2. Data hygiene findings that bear on every bar

### 2.1 Clocks (pre-mortem 1)
All k, placement, cancel and fill-window computations use the receipt clock (`rx` for raw reports, tape `ts`, micro `b.ts`). The only payload-clock uses are the ones production itself makes: the Binance bridge anchor (`spot_bridge_delta` anchors on the raw report's payload ts) and the boundary-trust rule (`ts - B <= 0.5`). Grep of the harness and drivers: no `ets`, no payload ts in any k or decision computation.

### 2.2 Forbidden calls (pre-mortem 10)
Grep of every T2 script for subscribe / websocket / ws:// / wss:// / place_order / cancel_order / place_gtc / polybot.main / run_polybot / HTTP clients: zero hits.

### 2.3 The dead-book exclusion list over-includes live books (pre-mortem 2)
`book-dynamics/dead_book_windows.csv` (253) was built as "contested book while |s| >= 1 at any final-minute grid point" (`contested_final.log`), not from book activity. Independent detectors: (A) micro tape, <= 1 distinct BBO price-state change on a token in the final 90 s -> 642 windows (over-flags decided 0.99/1.00 books); (B) window_paths max book age > 60 s in the final 60 s -> 224 of 5260 windows through 09-01. CSV ∩ A = 223; CSV \ A = 30; CSV ∩ B = 148 of the 174 CSV windows inside B's date range.
Classifying the CSV itself (`dead_book_classified.csv`): **225 windows are confirmed dead** (<= 2 BBO records per token, placeholder 0.50/0.51 or 0.10/0.90 last state) and **28 have live books** (226..2,122 BBO changes; decided 0.99/0.01 last states). Those 28 include **2 of the 3 known loser windows (1787271900, 1787620800) and 7 of the 17 baseline fill windows (+$793.66 of the +$1,882.72, incl. the +$557.81 window 1787581200 with 2,122 BBO changes)**. A window whose CLOB subscription is dead delivers no prints and cannot fill; these did. The pre-registered exclusion therefore removes the adverse-selection events T2 was written to measure. Per the threshold-lowering ban the pre-declared exclusion is reported FIRST and every verdict is evaluated under it; the confirmed-dead-only (225, `dead_confirmed.csv`) and no-exclusion variants are reported alongside. **No verdict changes across the three variants.** Recommend the program fix the Phase-1 artifact before T1/T3/T4 rely on it.

### 2.4 Post-close `certain_winner` gate (pre-mortem 20)
`pc_mode='capture'` replays `certain_winner()` from the sixty-topic boundary captures (boundaries.json: first report in [B, B+300), trusted iff payload ts − B <= 0.5 s and a prior report exists). Over the 6107 armed windows (k25, protocol exclusion): 702 would fail closed at close+5 s ("outcome unverified": open capture untrusted in 408, close capture in 330; 1 missing), 16 would cancel as "lock missed the winner". **Dollar effect: none** — post-close fills are 0 in every run (label and capture), hold-expiry fills carry the same pnl, totals identical (+995.31 both). The 16 windows where the captures name a different winner than the label are all 08-14..08-17 — before the 08-18 sixty-topic fix the recorded `t` stream is the retired 30 s topic — so capture mode is only meaningful from 08-18; none of those 16 changed a fill's pnl (1786666200's +$1,032.81 fill is pre-close).

## 3. H1 — re-arms

### 3.1 Faithfulness gate (re-arm ON, need 0.6, R8, $200; no exclusion, as the named windows must appear as they did)

| ep | arms (side, place_k, why, cancel_k, filled sh, r at placement) | booked arm | pnl | in dead CSV |
|---|---|---|---|---|
| 1787271900 @k25 | [['Up', 24.7, 'floor', 19.4, 0.0, 0.911], ['Up', 18.1, 'floor', 16.3, 0.0, 0.807], ['Up', 15.0, 'floor', 11.2, 191.1, 0.752]] | 3 of 3 | -100.00 (loss) | True |
| 1787358600 @k25 | [['Up', 24.8, 'floor', 23.2, 0.0, 0.811], ['Up', 22.2, 'flip', 17.5, 191.1, 0.719]] | 2 of 2 | -100.00 (loss) | False |
| 1787620800 @k25 | [['Down', 25.0, 'floor', 20.9, 0.0, 0.796], ['Down', 20.2, 'floor', 19.4, 0.0, 0.62], ['Down', 13.8, 'flip', 12.3, 119.7, 0.633]] | 3 of 3 | -75.00 (loss) | True |
| 1788267900 @k25 | [['Down', 17.2, 'hold-expiry', -60.0, 0.0, 0.875]] | None of 1 | +0.00 (no fill) | False |
| 1788743700 @k58 (live loss) | [['Up', 49.7, 'floor', 48.6, 119.7]] | 1 of 1 | -75.00 (RESEARCH.md −$75.00 / realized −$77.22) | False |

**3 of 3 reproduce** (gate passes; all three are re-arm fills — arm 3, 2, 3 — and vanish under one-arm; a fourth re-arm loss, ep 1787678700 on 08-25 (−$200, arm 2), is found in the same run — see §3.3). The live loss at k_max 58 reproduces on arm 1 exactly as today. **Hold-out 1788267900 (pre-mortem 22) does NOT reproduce as a loss**: the bridged (deployed) r toward Up peaked at 0.58 (k 23.6) and 0.56 (k 21.7) and never reached 0.6 in [6, 25]; the ladder armed Down at k 17.2 (r 0.875), the winning side, no fill. book-dynamics classified it with the PLAIN projection (s 0.613 at k 22.7) — a plain-vs-bridged artifact, not a bot-zone loss. Under S1/S2 (H3) this window does fill on the 0.35 rung and WINS (+$46.43).

### 3.2 Re-arm ON vs OFF (pre-declared cell k_max 25 first; then k_max 58; ANTI; three exclusion variants)

| run | armed | fills | wins | losses | $ | loss $ | multi-arm windows | fills on arm>=2 | $ on arm>=2 | H1-half fills / $ | H2-half fills / $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| k25 re-arm ON (production) — protocol excl. | 6107 | 12 | 11 | 1 | +995.31 | -100.00 | 66 | 2 | -93.75 | 10 / +982.81 | 2 / +12.50 |
| k25 one arm (OFF) — protocol excl. | 6107 | 10 | 10 | 0 | +1,089.06 | +0.00 | 0 | 0 | +0.00 | 8 / +1,076.56 | 2 / +12.50 |
| ANTI k25 ON | 6107 | 4050 | 1 | 4049 | -808,417.19 | -809,450.00 | 17 | 13 | -2,600.00 | 2008 / -400,117.19 | 2042 / -408,300.00 |
| ANTI k25 OFF | 6107 | 4037 | 1 | 4036 | -805,817.19 | -806,850.00 | 0 | 0 | +0.00 | 2001 / -398,717.19 | 2036 / -407,100.00 |
| k25 ON, certain_winner replica | 6107 | 12 | 11 | 1 | +995.31 | -100.00 | 66 | 2 | -93.75 | 10 / +982.81 | 2 / +12.50 |
| k25 OFF, certain_winner replica | 6107 | 10 | 10 | 0 | +1,089.06 | +0.00 | 0 | 0 | +0.00 | 8 / +1,076.56 | 2 / +12.50 |
| k58 re-arm ON — protocol excl. | 6141 | 31 | 28 | 3 | +1,105.41 | -200.00 | 449 | 11 | -37.50 | 23 / +1,154.44 | 8 / -49.04 |
| k58 OFF — protocol excl. | 6141 | 20 | 18 | 2 | +1,142.90 | -100.00 | 0 | 0 | +0.00 | 13 / +1,198.19 | 7 / -55.29 |
| ANTI k58 ON | 6141 | 5152 | 4 | 5148 | -1,025,510.40 | -1,028,653.54 | 173 | 157 | -31,400.00 | 2457 / -489,395.24 | 2695 / -536,115.17 |
| ANTI k58 OFF | 6141 | 4995 | 4 | 4991 | -994,110.40 | -997,253.54 | 0 | 0 | +0.00 | 2397 / -477,395.24 | 2598 / -516,715.17 |
| k25 re-arm ON — confirmed_dead_only | 6135 | 22 | 18 | 4 | +1,413.97 | -475.00 | 69 | 5 | -468.75 | 19 / +1,381.76 | 3 / +32.21 |
| k25 OFF — confirmed_dead_only | 6135 | 17 | 17 | 0 | +1,882.72 | +0.00 | 0 | 0 | +0.00 | 14 / +1,850.51 | 3 / +32.21 |
| ANTI k25 ON — confirmed_dead_only | 6135 | 4078 | 3 | 4075 | -811,551.57 | -814,650.00 | 17 | 13 | -2,600.00 | 2031 / -402,251.57 | 2047 / -409,300.00 |
| ANTI k25 OFF — confirmed_dead_only | 6135 | 4065 | 3 | 4062 | -808,951.57 | -812,050.00 | 0 | 0 | +0.00 | 2024 / -400,851.57 | 2041 / -408,100.00 |
| k58 re-arm ON — confirmed_dead_only | 6169 | 43 | 37 | 6 | +1,550.03 | -575.00 | 456 | 15 | -406.25 | 34 / +1,579.36 | 9 / -29.33 |
| k58 OFF — confirmed_dead_only | 6169 | 28 | 26 | 2 | +1,956.28 | -100.00 | 0 | 0 | +0.00 | 20 / +1,991.85 | 8 / -35.58 |
| ANTI k58 ON — confirmed_dead_only | 6169 | 5180 | 6 | 5174 | -1,029,044.78 | -1,033,778.54 | 173 | 157 | -31,400.00 | 2480 / -491,929.62 | 2700 / -537,115.17 |
| ANTI k58 OFF — confirmed_dead_only | 6169 | 5023 | 6 | 5017 | -997,644.78 | -1,002,378.54 | 0 | 0 | +0.00 | 2420 / -479,929.62 | 2603 / -517,715.17 |
| k25 re-arm ON — none | 6358 | 22 | 18 | 4 | +1,413.97 | -475.00 | 72 | 5 | -468.75 | 19 / +1,381.76 | 3 / +32.21 |
| k25 OFF — none | 6358 | 17 | 17 | 0 | +1,882.72 | +0.00 | 0 | 0 | +0.00 | 14 / +1,850.51 | 3 / +32.21 |
| ANTI k25 ON — none | 6358 | 4078 | 3 | 4075 | -811,551.57 | -814,650.00 | 20 | 13 | -2,600.00 | 2031 / -402,251.57 | 2047 / -409,300.00 |
| ANTI k25 OFF — none | 6358 | 4065 | 3 | 4062 | -808,951.57 | -812,050.00 | 0 | 0 | +0.00 | 2024 / -400,851.57 | 2041 / -408,100.00 |
| k58 re-arm ON — none | 6394 | 43 | 37 | 6 | +1,550.03 | -575.00 | 471 | 15 | -406.25 | 34 / +1,579.36 | 9 / -29.33 |
| k58 OFF — none | 6394 | 28 | 26 | 2 | +1,956.28 | -100.00 | 0 | 0 | +0.00 | 20 / +1,991.85 | 8 / -35.58 |
| ANTI k58 ON — none | 6394 | 5180 | 6 | 5174 | -1,029,044.78 | -1,033,778.54 | 188 | 157 | -31,400.00 | 2480 / -491,929.62 | 2700 / -537,115.17 |
| ANTI k58 OFF — none | 6394 | 5023 | 6 | 5017 | -997,644.78 | -1,002,378.54 | 0 | 0 | +0.00 | 2420 / -479,929.62 | 2603 / -517,715.17 |

ANTI (same rule, other side) is the edge<0 control for a binary-sign ladder (there is no continuous model edge to bucket; pre-mortem 6): it loses ~$200 per armed window because the losing token prints through every rung — as expected, and it stays hugely negative in every variant.

### 3.3 Adoption rule (one arm per window): loser $ removed vs winner $ forgone, per half

| exclusion | k_max | H1-half removed / forgone (n re-arm fills) | H2-half removed / forgone (n) | ALL removed / forgone | adopt (both halves strict) | day-block bootstrap of ON−OFF (26 ET days, B=5000) |
|---|---|---|---|---|---|---|
| protocol_csv253 (pre-declared) | 25 | 100.00 / 6.25 (2) | 0.00 / 0.00 (0) | 100.00 / 6.25 | NO | total -93.75, 95% CI [-300.0, 18.75], p(sum<=0) 0.7648 |
| protocol_csv253, certain_winner pc | 25 | 100.00 / 6.25 (2) | 0.00 / 0.00 (0) | 100.00 / 6.25 | NO | total -93.75, 95% CI [-300.0, 18.75], p(sum<=0) 0.7648 |
| protocol_csv253 | 58 | 100.00 / 56.25 (10) | 0.00 / 6.25 (1) | 100.00 / 62.50 | NO | total -37.5, 95% CI [-218.75, 68.75], p(sum<=0) 0.6314 |
| confirmed_dead_only | 25 | 475.00 / 6.25 (5) | 0.00 / 0.00 (0) | 475.00 / 6.25 | NO | — |
| confirmed_dead_only | 58 | 475.00 / 62.50 (14) | 0.00 / 6.25 (1) | 475.00 / 68.75 | NO | — |
| none | 25 | 475.00 / 6.25 (5) | 0.00 / 0.00 (0) | 475.00 / 6.25 | NO | — |
| none | 58 | 475.00 / 62.50 (14) | 0.00 / 6.25 (1) | 475.00 / 68.75 | NO | — |

Re-arm fill windows at the pre-declared cell (protocol exclusion): 1787358600 (2026-08-21, H1, arm 2/2, -100.00, Up vs winner Down); 1787376000 (2026-08-22, H1, arm 2/2, +6.25, Up vs winner Up).
With no exclusion: 1787271900 (2026-08-20, H1, arm 3/3, -100.00); 1787358600 (2026-08-21, H1, arm 2/2, -100.00); 1787376000 (2026-08-22, H1, arm 2/2, +6.25); 1787620800 (2026-08-24, H1, arm 3/3, -75.00); 1787678700 (2026-08-25, H1, arm 2/2, -200.00).

**Reading.** Every loser-side re-arm dollar in 26 days sits in the first half (08-20, 08-21, 08-24, 08-25); every winner-side re-arm fill is a single 0.80-rung fill worth +$6.25. Without the CSV exclusion a FOURTH re-arm flip-fill loss appears that the pre-registration did not name: **ep 1787678700 (08-25 ET, arm 2 of 2, Down vs winner Up, full-ladder sweep, −$200.00)** — a live-book window the CSV hides. Over the whole corpus one-arm-per-window removes $475.00 of losses and forgoes $6.25 at k_max 25 ($475.00 vs $68.75 at k_max 58) — but the pre-registered rule demands the strict inequality in BOTH halves and the second half has no loser re-arm event at k_max 25 under any exclusion variant (0 vs 0; at k_max 58: 0 vs +6.25). **Verdict: FAIL by the letter — 'one arm per window' is not adopted by this rule.** The direction of all available evidence favors it (5 re-arm fills in the first half: 4 losses totalling −$475, 1 win of +$6.25); the second half simply carries no re-arm fills to test.

Pre-mortem 15 (zero-delta windows): 6105 windows have an exactly-0.0 ON−OFF delta; 6041 never re-armed (single arm), 64 re-armed without any fill on a later arm, 0 re-armed AND filled on a later arm with a zero delta (must be 0 — it is). No blind-replay pattern: every non-zero delta is a re-arm fill and every zero delta has no re-arm fill.
Arm-index statistics (k25 ON, protocol excl.): arm 1 N 6107, side==winner 6106, fills 10 (+1,089.06); arm 2 N 66, side==winner 65, fills 2 (-93.75, 1 loss); arm 3 N 3, fills 0. Cancel reasons of arm 1: {'hold-expiry': 6024, 'floor': 82, 'cold': 1}.

## 4. H2 — reprice cancel

Rule as pre-registered: while resting, cancel when in the trailing 1 s the complement token's best ask rose >= 0.05 or our side's best ask fell >= 0.05 (micro BBO, trailing information only; cancel at the event + CANCEL_LAT 54 ms; decision latency 0 in the pre-declared cell, 0.25 s as sensitivity). Re-arm regime: production ON with a per-window latch after a reprice cancel (a design choice production would need — without it the ladder re-arms on the next tick, reported as 'no latch'); OFF also reported.

### 4.1 The four known loss windows + the live loss (pre-mortems 16, 17)

| ep | side / winner | arm | place_k / r | first fill k | trailing trigger @0.03 (k, kind, move, lead before first fill) | @0.05 | @0.08 | ±1 s forward-peeking @0.05 (comp ask rise / own ask fall) | pnl base -> if cancelled at trigger |
|---|---|---|---|---|---|---|---|---|---|
| 1787271900 @k25 | Up / Down | 3/3 | 15.04 / 0.752 | 13.933 | 15.032, comp_ask_up, 0.16, 1.099 s | 15.032, comp_ask_up, 0.16, 1.099 s | 15.032, comp_ask_up, 0.16, 1.099 s | 0.43 / 0.43 -> True | -100.00 -> -0.00 |
| 1787358600 @k25 | Up / Down | 2/2 | 22.16 / 0.719 | 17.987 | 18.161, comp_ask_up, 0.33, 0.174 s | 18.161, comp_ask_up, 0.33, 0.174 s | 18.161, comp_ask_up, 0.33, 0.174 s | 0.57 / 0.57 -> True | -100.00 -> -0.00 |
| 1787620800 @k25 | Down / Up | 3/3 | 13.78 / 0.633 | 12.95 | 13.522, own_ask_down, 0.04, 0.572 s | 13.406, own_ask_down, 0.05, 0.456 s | 13.105, comp_ask_up, 0.2, 0.155 s | 0.57 / 0.57 -> True | -75.00 -> -0.00 |
| 1788267900 | — | — | — | — | not a booked fill at k25 (armed Down k 17.2, winning side, no fill) | — | — | — | — |
| 1788743700 @k58 | Up / Down | 1/1 | 49.68 / 0.636 | 49.587 | 49.678, comp_ask_up, 0.54, 0.091 s | 49.678, comp_ask_up, 0.54, 0.091 s | 49.678, comp_ask_up, 0.54, 0.091 s | 0.64 / 0.62 -> True | -75.00 -> -0.00 |

All three known losses (and the live k58 loss) are **reprices**: the complement's ask jumps 0.16–0.54 in the trailing second 0.09–1.10 s BEFORE our first fill, on trailing-only information, at every threshold (0.03/0.05/0.08 — no window's classification flips; 1787620800 fires on 'own ask down' at 0.03/0.05 and on 'complement ask up' at 0.08, still before the fill). The trailing-only and the ±1 s forward-peeking classifications agree on all four. With a 0-latency cancel none of them fills. book-dynamics' "3 of 4 reprices" becomes 3 of 3 (the 4th was not a bot-zone loss, §3.1).

### 4.2 Dollars: base vs H2, both regimes, both halves, ANTI (protocol exclusion; confirmed-dead variant in `h2_results_confirmed_dead_only.json`)

| regime / cell | base fills / $ | H2 fills / $ | loser $ removed H1 / H2 half | winner $ forgone H1 / H2 half | reprice-cancel windows | ANTI base -> H2 | adopt |
|---|---|---|---|---|---|---|---|
| k25, re-arm ON + latch (pre-declared) | 12 / +995.31 | 2 / +12.50 | 100.00 / 0.00 | 1070.31 / 12.50 | 39 | -808,417 -> -805,367 | NO |
| k25, one arm | 10 / +1,089.06 | 2 / +12.50 | 0.00 / 0.00 | 1064.06 / 12.50 | 38 | -805,817 -> -802,767 | NO |
| k58, re-arm ON + latch | 31 / +1,105.41 | 3 / +32.21 | 100.00 / 100.00 | 1228.48 / 44.71 | 133 | -1,025,510 -> -1,019,388 | NO |
| k58, one arm | 20 / +1,142.90 | 3 / +32.21 | 0.00 / 100.00 | 1172.23 / 38.46 | 112 | -994,110 -> -988,588 | NO |
| k25 ON, decision latency 0.25 s | 12 / +995.31 | 6 / -68.75 | 0.00 / 0.00 | 1057.81 / 6.25 | 38 | — | NO |
| k25 ON, thr 0.03 | 12 / +995.31 | 2 / +12.50 | 100.00 / 0.00 | 1070.31 / 12.50 | 69 | — | NO |
| k25 ON, thr 0.08 | 12 / +995.31 | 2 / +12.50 | 100.00 / 0.00 | 1070.31 / 12.50 | 29 | — | NO |
| k25 ON, no latch | 12 / +995.31 | 4 / +25.00 | 100.00 / 0.00 | 1057.81 / 12.50 | 39 | — | NO |

Confirmed-dead-only exclusion, k25 ON: base 22 / +1,413.97 -> H2 2 / +12.50; removed 475.00 / 0.00, forgone 1844.26 / 32.21 -> adopt False.

**Reading.** The reprice signal is real (it precedes all three losses), but it is the same event that produces every winner fill: a sweep through our side's book is, by definition, our ask falling and the complement's ask rising. At k25 the rule removes $100 of losses and forgoes $1,070 of winner fills (12 -> 2 fills); with a 0.25 s decision latency it no longer even catches the loss (the sweep completes in < 0.25 s) while still forgoing $1,058. ANTI changes (fewer ANTI fills), violating 'ANTI unchanged'. **Verdict: FAIL — do not adopt.** Day-block bootstrap of H2−base (26 days, B=5000): total −982.81, 95% CI [−3,142, +169].

## 5. H3 — per-rung need schedules

Production semantics (maker_bid.ladder / consider_placement / maintain; engine-map confirmed): the ladder places at the first tick with r >= min(needs); at that tick only rungs whose need <= r rest — a rung whose need is not yet met NEVER joins later (`consider_placement` returns while `active` is set); the cancel floor is min(needs) for every rung. So S1/S2 arm earlier (at r >= 0.3 / 0.15) with only the deep rungs resting, and the 0.80/0.65/0.50 rungs are absent from every window whose first r >= 0.3 tick came before r reached 0.6 — e.g. 1786666200: S0 fills the whole ladder for +$1,032.81; S1 places at r 0.311 (k 10.5) with rungs <= 0.35 only, +$988.10; S2 at r 0.154 (k 18.2) with rungs <= 0.15 only, +$841.67.

### 5.1 Pre-declared cells: k_max 25, re-arm ON (production), protocol exclusion; then k_max 58 and re-arm OFF

| schedule / cell | armed | fills | wins | losses | $ | loss $ | H1-half fills / $ (S0) | H2-half fills / $ (S0) | relaxed-rung flip-fill loss windows | relaxed-only arms: n / $ / wins | ANTI $ (fills) | adopt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 k25 re-arm ON | 6107 | 12 | 11 | 1 | +995.31 | -100.00 | 10 / +982.81 | 2 / +12.50 | — | — | -808,417 (4050) | (baseline) |
| S1 k25 re-arm ON | 6301 | 13 | 8 | 5 | +797.03 | -275.00 | 10 / +769.35 (+982.81) | 3 / +27.68 (+12.50) | 5 | 6 / +859.53 / 2 | -755,342 (4374) | NO |
| S2 k25 re-arm ON | 6421 | 16 | 9 | 7 | +836.09 | -325.00 | 12 / +761.98 (+982.81) | 4 / +74.11 (+12.50) | 7 | 11 / +897.62 / 5 | -724,828 (4533) | NO |
| S0 k58 re-arm ON | 6141 | 31 | 28 | 3 | +1,105.41 | -200.00 | 23 / +1,154.44 | 8 / -49.04 | — | — | -1,025,510 (5152) | (baseline) |
| S1 k58 re-arm ON | 6360 | 33 | 24 | 9 | +904.31 | -400.00 | 24 / +847.99 (+1,154.44) | 9 / +56.32 (-49.04) | 9 | 12 / +827.38 / 4 | -802,814 (5818) | NO |
| S2 k58 re-arm ON | 6492 | 44 | 28 | 16 | +1,582.43 | -625.01 | 31 / +1,054.67 (+1,154.44) | 13 / +527.76 (-49.04) | 15 | 27 / +1,503.59 / 12 | -654,357 (6113) | NO |
| S0 k25 re-arm OFF | 6107 | 10 | 10 | 0 | +1,089.06 | +0.00 | 8 / +1,076.56 | 2 / +12.50 | — | — | -805,817 (4037) | (baseline) |
| S1 k25 re-arm OFF | 6301 | 7 | 4 | 3 | +781.85 | -225.00 | 7 / +781.85 (+1,076.56) | 0 / +0.00 (+12.50) | 3 | 3 / +863.10 / 1 | -753,842 (4362) | NO |
| S2 k25 re-arm OFF | 6421 | 8 | 5 | 3 | +840.55 | -175.00 | 8 / +840.55 (+1,076.56) | 0 / +0.00 (+12.50) | 3 | 4 / +908.34 / 2 | -722,223 (4500) | NO |
| S0 k58 re-arm OFF | 6141 | 20 | 18 | 2 | +1,142.90 | -100.00 | 13 / +1,198.19 | 7 / -55.29 | — | — | -994,110 (4995) | (baseline) |
| S1 k58 re-arm OFF | 6360 | 22 | 16 | 6 | +810.49 | -325.00 | 18 / +803.28 (+1,198.19) | 4 / +7.21 (-55.29) | 6 | 7 / +809.53 / 2 | -790,198 (5710) | NO |
| S2 k58 re-arm OFF | 6492 | 24 | 17 | 7 | +1,157.65 | -225.00 | 19 / +1,175.44 (+1,198.19) | 5 / -17.79 (-55.29) | 7 | 10 / +1,029.76 / 3 | -645,726 (5921) | NO |

Confirmed-dead-only exclusion (same cells):

| schedule / cell | fills | wins | losses | $ | H1-half $ (S0) | H2-half $ (S0) | relaxed-rung loss windows | adopt |
|---|---|---|---|---|---|---|---|---|
| S0 k25 re-arm ON | 22 | 18 | 4 | +1,413.97 | +1,381.76 | +32.21 | — | (baseline) |
| S1 k25 re-arm ON | 20 | 12 | 8 | +1,138.77 | +1,111.09 (+1,381.76) | +27.68 (+32.21) | 7 | NO |
| S2 k25 re-arm ON | 22 | 12 | 10 | +981.40 | +907.29 (+1,381.76) | +74.11 (+32.21) | 9 | NO |
| S0 k58 re-arm ON | 43 | 37 | 6 | +1,550.03 | +1,579.36 | -29.33 | — | (baseline) |
| S1 k58 re-arm ON | 39 | 28 | 11 | +884.66 | +828.35 (+1,579.36) | +56.32 (-29.33) | 10 | NO |
| S2 k58 re-arm ON | 49 | 31 | 18 | +1,391.36 | +863.61 (+1,579.36) | +527.76 (-29.33) | 16 | NO |
| S0 k25 re-arm OFF | 17 | 17 | 0 | +1,882.72 | +1,850.51 | +32.21 | — | (baseline) |
| S1 k25 re-arm OFF | 13 | 8 | 5 | +1,323.59 | +1,323.59 (+1,850.51) | +0.00 (+32.21) | 4 | NO |
| S2 k25 re-arm OFF | 13 | 8 | 5 | +1,185.86 | +1,185.86 (+1,850.51) | +0.00 (+32.21) | 4 | NO |
| S0 k58 re-arm OFF | 28 | 26 | 2 | +1,956.28 | +1,991.85 | -35.58 | — | (baseline) |
| S1 k58 re-arm OFF | 27 | 20 | 7 | +990.85 | +983.64 (+1,991.85) | +7.21 (-35.58) | 6 | NO |
| S2 k58 re-arm OFF | 28 | 20 | 8 | +1,166.58 | +1,184.37 (+1,991.85) | -17.79 (-35.58) | 7 | NO |

### 5.2 Per-rung tallies at the pre-declared cell (k25, re-arm ON, protocol exclusion) — windows per rung (pre-mortem 18: a sweep fills several rungs of ONE window; loss counts above are per window)

| rung | S0 placed / fill / win / loss windows, $ | S1 placed / fill / win / loss, $ | S2 placed / fill / win / loss, $ |
|---|---|---|---|
| 0.8 | 6107 / 12 / 11 / 1, +43.75 | 4649 / 7 / 6 / 1, +12.50 | 4640 / 5 / 4 / 1, -0.00 |
| 0.65 | 6107 / 2 / 1 / 1, -11.54 | 4649 / 1 / 0 / 1, -25.00 | 4640 / 2 / 1 / 1, -11.54 |
| 0.5 | 6107 / 2 / 1 / 1, +0.00 | 4649 / 1 / 0 / 1, -25.00 | 4640 / 1 / 0 / 1, -25.00 |
| 0.35 | 6107 / 2 / 1 / 1, +21.43 | 6301 / 7 / 2 / 5, -32.14 | 5470 / 7 / 3 / 4, +39.29 |
| 0.2 | 6107 / 1 / 1 / 0, +100.00 | 6301 / 2 / 1 / 1, +75.00 | 5470 / 0 / 0 / 0, +0.00 |
| 0.15 | 6107 / 1 / 1 / 0, +141.67 | 6301 / 2 / 1 / 1, +116.67 | 6421 / 5 / 2 / 3, +208.34 |
| 0.1 | 6107 / 1 / 1 / 0, +225.00 | 6301 / 2 / 1 / 1, +200.00 | 6421 / 3 / 1 / 2, +175.00 |
| 0.05 | 6107 / 1 / 1 / 0, +475.00 | 6301 / 1 / 1 / 0, +475.00 | 6421 / 2 / 1 / 1, +450.00 |

Relaxed-rung flip-fill loss windows (S1 k25 ON): 1787060700 (H1, r 0.419, k 20.5, rungs ['0.35'], -25.00, floor); 1787258400 (H1, r 0.335, k 24.6, rungs ['0.35'], -25.00, flip); 1787358600 (H1, r 0.811, k 24.8, rungs ['0.8', '0.65', '0.5', '0.35'], -100.00, flip); 1787676900 (H1, r 0.316, k 18.2, rungs ['0.35', '0.2', '0.15', '0.1'], -100.00, flip); 1787871900 (H2, r 0.382, k 18.9, rungs ['0.35'], -25.00, flip).
Relaxed-rung flip-fill loss windows (S2 k25 ON): 1786757400 (H1, r 0.172, k 18.3, rungs ['0.15', '0.1', '0.05'], -75.00, flip); 1787060700 (H1, r 0.419, k 20.5, rungs ['0.35'], -25.00, floor); 1787258400 (H1, r 0.335, k 24.6, rungs ['0.35'], -25.00, flip); 1787350200 (H1, r 0.161, k 13.7, rungs ['0.15'], -25.00, flip); 1787358600 (H1, r 0.811, k 24.8, rungs ['0.8', '0.65', '0.5', '0.35'], -100.00, flip); 1787676900 (H1, r 0.165, k 24.4, rungs ['0.15', '0.1'], -50.00, flip); 1787871900 (H2, r 0.382, k 18.9, rungs ['0.35'], -25.00, flip).

**Reading.** Both relaxed schedules fail every clause at the pre-declared cell: dollars below S0 in the first half (S1 +769.35 vs +982.81; S2 +761.98 vs +982.81), more losses (5 and 7 vs 1), and 5 / 7 flip-fill loss windows on relaxed rungs. The 0.35 rung at need 0.3 fills 7 windows and wins 2 (S1) / 3 (S2) against a 35% break-even. S2 at k_max 58 has more total dollars than S0 (+$1,582 vs +$1,105, driven by the 0.15 rung: 12 fill windows, 6 wins, +$700) but 16 losses vs 3 and 15 relaxed-rung loss windows; the day-block bootstrap of S2−S0 at k58 is total +477, 95% CI [−497, +1,590]. ANTI <= 0 everywhere. **Verdict: S1 FAIL, S2 FAIL.** Also a code fact for the operator: with production's placement-time-only rung filter, any relaxed schedule silently drops the 0.80/0.65/0.50 rungs from the windows that arm early (S1 rests them in 4,649 of 6,301 armed windows vs 6,107 of 6,107 for S0).
Pre-mortem 21: the 135-share at-price queue constant was measured 08-17 on the 0.80..0.20 levels; its use on the 0.15/0.10/0.05 rungs (added 09-01/09-04) is an unverified extrapolation — the same assumption the deployed S0 already carries, so it does not favor S1/S2 over S0.

## 6. H4 — descriptive frontier (NON-ADOPTIVE; owned by the 09-11 re-decision; not folded into any H1/H2/H3 total)

| need | k_max | re-arm | armed | fills | wins | losses | $ | loss $ | H1-half fills / $ | H2-half fills / $ | fills on arm>=2 ($) | ANTI $ (fills) | day-block 95% CI of $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.6 | 15 | ON | 6088 | 5 | 5 | 0 | +1,057.81 | +0.00 | 4 / +1,051.56 | 1 / +6.25 | 0 (+0.00) | -619,051 (3096) | [6.25, 3135.93] |
| 0.6 | 15 | OFF | 6088 | 5 | 5 | 0 | +1,057.81 | +0.00 | 4 / +1,051.56 | 1 / +6.25 | 0 (+0.00) | -618,251 (3092) | [6.25, 3135.93] |
| 0.6 | 25 | ON | 6107 | 12 | 11 | 1 | +995.31 | -100.00 | 10 / +982.81 | 2 / +12.50 | 2 (-93.75) | -808,417 (4050) | [-187.66, 3148.43] |
| 0.6 | 25 | OFF | 6107 | 10 | 10 | 0 | +1,089.06 | +0.00 | 8 / +1,076.56 | 2 / +12.50 | 0 (+0.00) | -805,817 (4037) | [31.25, 3167.18] |
| 0.8 | 15 | ON | 5978 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -595,375 (2977) | [0.0, 18.75] |
| 0.8 | 15 | OFF | 5978 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -595,175 (2976) | [0.0, 18.75] |
| 0.8 | 25 | ON | 5995 | 2 | 2 | 0 | +12.50 | +0.00 | 2 / +12.50 | 0 / +0.00 | 1 (+6.25) | -771,742 (3865) | [0.0, 31.25] |
| 0.8 | 25 | OFF | 5995 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -767,742 (3845) | [0.0, 18.75] |
| 1.0 | 15 | ON | 5856 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -573,600 (2868) | [0.0, 18.75] |
| 1.0 | 15 | OFF | 5856 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -572,200 (2861) | [0.0, 18.75] |
| 1.0 | 25 | ON | 5871 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -739,400 (3697) | [0.0, 18.75] |
| 1.0 | 25 | OFF | 5871 | 1 | 1 | 0 | +6.25 | +0.00 | 1 / +6.25 | 0 / +0.00 | 0 (+0.00) | -735,200 (3676) | [0.0, 18.75] |

(protocol exclusion; the confirmed-dead-only table is in `h4_results_confirmed_dead_only.json`)

| need | k_max | re-arm | fills | wins | losses | $ | H1-half $ | H2-half $ |  (confirmed-dead-only) |
|---|---|---|---|---|---|---|---|---|---|
| 0.6 | 15 | ON | 12 | 10 | 2 | +1,438.97 | +1,413.01 | +25.96 | |
| 0.6 | 15 | OFF | 12 | 10 | 2 | +1,438.97 | +1,413.01 | +25.96 | |
| 0.6 | 25 | ON | 22 | 18 | 4 | +1,413.97 | +1,381.76 | +32.21 | |
| 0.6 | 25 | OFF | 17 | 17 | 0 | +1,882.72 | +1,850.51 | +32.21 | |
| 0.8 | 15 | ON | 6 | 6 | 0 | +562.41 | +542.70 | +19.71 | |
| 0.8 | 15 | OFF | 6 | 6 | 0 | +562.41 | +542.70 | +19.71 | |
| 0.8 | 25 | ON | 10 | 10 | 0 | +1,138.97 | +1,119.26 | +19.71 | |
| 0.8 | 25 | OFF | 7 | 7 | 0 | +793.66 | +773.95 | +19.71 | |
| 1.0 | 15 | ON | 4 | 4 | 0 | +365.02 | +345.31 | +19.71 | |
| 1.0 | 15 | OFF | 4 | 4 | 0 | +365.02 | +345.31 | +19.71 | |
| 1.0 | 25 | ON | 6 | 6 | 0 | +929.08 | +909.37 | +19.71 | |
| 1.0 | 25 | OFF | 5 | 5 | 0 | +596.27 | +576.56 | +19.71 | |

## 7. Pre-mortem checks (all 22)

| check | result |
|---|---|
| 1 rx clock everywhere | Confirmed by grep (§2.1): k, placement, cancel, fill windows on receipt clocks; payload ts only where production uses it (bridge anchor, boundary trust). |
| 2 independent dead-book detector | Built two (§2.3): micro-tape <=1 change (642), window_paths book age > 60 s (224). Diff vs CSV: CSV∩A 223, CSV\A 30; 28 CSV windows have live books incl. 2 loser and 7 fill windows. The CSV misses nothing that matters to T2 in the other direction (fills come from prints; a window with no prints cannot fill). |
| 3 counted once per window | summarize2() tallies one row per armed window; per-rung tables count windows per rung and are labeled so (§5.2). No tick-level or per-fill dollar totals anywhere. |
| 4 walk-forward assertion | Not applicable: T2 fits no model. The only table is the deployed TWAP_MARGIN_P995 (asserted identical, pre-mortem 12). |
| 5 day-block bootstrap | Every p-value/CI: whole-ET-day resamples, B=5000, 26 unique day blocks (reported next to each: §3.3, §4.2, §5.1 verdict JSON, §6). The adoption rules themselves are deterministic. |
| 6 ANTI + edge<0 control | ANTI run for every claimed cell (H1, H2, H3, H4). The edge<0 bucket is not definable for a binary-sign ladder; ANTI is that control and is stated as such. No omission. |
| 7 pre-declared cell first | Drivers run k25/need 0.6/R8/$200 (H1), thr 0.05 (H2), S0->S1->S2 at k25 ON (H3) before any other cell; §0 quotes those cells. |
| 8 literal bar diff | See §8 table. One deviation to flag: H3 'no flip-fill loss on any relaxed rung' evaluated per WINDOW (a loss window whose booking arm filled a relaxed rung) — the pre-registration's unit is ambiguous; per-rung-fill counting would only add losses, never remove one, so the FAIL verdicts stand under either reading. |
| 9 copied before edit | Repo ws2_ladder_replay.py md5 fbce52f8899c8595dc02fd576792518c unchanged (git status clean for the file); edits only in scratch ladder_replay_v2.py; sys.dont_write_bytecode set on the read-only import. |
| 10 forbidden-call grep | Zero hits (§2.2). |
| 11 book_p freshness | No book_p feature or baseline is used in T2. H2's triggers are BBO CHANGE events (fresh by construction, < 1 s trailing window). |
| 12 table identity | Asserted: True. |
| 13 real multi-arm window | 134 logged era windows with >= 2 MAKER LADDER lines (119 floor/flip re-arms); 0 fills on arm >= 2 in the 07-13..08-27 logs; harness cross-check §1.1. |
| 14 refusal-logic diff + unit tests | Production refuses only on has_open_or_pending_market/has_position_for_market (status open/pending_resolution) — harness: booked arm (fills>0 and notional>=$1) blocks; partial-fill and zero-fill tests pass (§1). |
| 15 zero-delta inspection | 6105 zero-delta windows: 6041 single-arm, 64 multi-arm without a later fill, 0 with one (§3.3). |
| 16 trailing-only H2 classifier | Re-derived (§4.1): trailing-only and ±1 s peeking agree on all 4 windows (3 losses + live loss); the 4th 'known loss' is not a bot-zone loss under the bridged sign. |
| 17 thresholds 0.03/0.05/0.08 | No window's classification flips (§4.1). |
| 18 H3 per-window tallies | Loss counts and relaxed-rung flip-fill losses are per window (§5.1/5.2). |
| 19 H4 labeled non-adoptive | §6 header and every H4 JSON carry the label; H4 numbers appear nowhere in §0–5. |
| 20 certain_winner gate | Applied (§2.4): 702 of 6,107 armed windows would fail closed at close+5 s; 0 post-close fills in any run; winner-side dollars unchanged. |
| 21 AT_PRICE_QUEUE_SH extrapolation | Flagged (§5.2): the 135-share constant on 0.15/0.10/0.05 is unverified; shared by S0. |
| 22 hold-out 1788267900 | Does not reproduce as a loss (bridged r toward Up peaks 0.58 in [6,25]; armed Down, the winner); book-dynamics used the plain projection (§3.1). |

## 8. Bars applied vs the pre-registration's literal wording

| bar (literal) | applied | deviation |
|---|---|---|
| H1: re-arm ON at need 0.6 / k_max 25 / R8 / $200 must show eps 1787271900, 1787358600, 1787620800 as flip-fills; at k_max 58 ep 1788743700; < 2 of 3 -> no claim | exactly that, no exclusion applied to the gate (the named windows must appear) | none |
| H1: adopt one arm per window iff loser $ removed > winner $ forgone in BOTH halves | strict > per half; halves = ET days 08-13..25 / 08-26..09-07 | none (halves convention from information-structure; not defined in the pre-registration) |
| H2: cancel when within the trailing 1 s complement best ask rose >= 0.05 or own best ask fell >= 0.05; adopt iff loser $ removed >= 2 x winner $ forgone in both halves, ANTI unchanged | thr 0.05, 1 s trailing, event-true, latency 0; latch after cancel; ANTI compared exactly | latch is a design choice (no-latch variant reported: same verdict) |
| H3: S0/S1/S2 exactly as listed, k_max 25 and 58, re-arm as adopted from H1; adopt iff $ >= S0 both halves, losses <= S0, no flip-fill loss on any relaxed rung; ANTI <= 0 | schedules exact; production placement semantics; both re-arm regimes (H1 not adopted -> production ON is the operative regime) | 'relaxed rung' loss unit = window (see pre-mortem 8) |
| H4: need {0.6,0.8,1.0} x k_max {15,25} with re-arm modeled | exact; OFF added as context | none |
| common: dead-book windows (CSV, 253) excluded; 60 s era; bridged projection; deployed P995; B >= 2000; halves; ANTI | applied as written FIRST; two extra exclusion variants reported alongside | reporting-only additions; no threshold changed |

## 9. Gaps and limitations

- Second-half re-arm evidence is empty at k_max 25 (no loser re-arm fill 08-26..09-07); the H1 rule cannot pass without it. Any re-decision needs more days, not more replay.
- The harness evaluates cancels only at raw ticks (~1 Hz); production's maintain() runs at 4 Hz on a continuously drifting projection and re-arms ~2x as often (34 of 66 logged floor re-arms reproduced). Re-arm exposure here is a lower bound.
- Placement also happens only at raw ticks in the harness; production's hot-path evaluates on any wake while |disp| >= 0.9 x margin. First-arm timing matches the logs within ±1 s for 80% of windows.
- H2's 'no latch' variant re-arms at the next raw tick; production's hot path could re-arm within the same iteration on a book wake, making the rule even weaker than modeled.
- capture-mode post-close is only meaningful from 08-18 (the recorded `t` stream before that is the retired 30 s topic); it changed no dollars.
- The paper fill rule (strictly-below full fill; 135-share at-price credit) remains unvalidated against live ladder fills (0 live fills post-era); every dollar here is paper-rule dollars.
- Tape captures 8–40% of post-close volume (information-structure 3f); post-close fills are 0 in every run here, so no post-close dollars are claimed.
- The dead-book CSV defect (§2.3) is a Phase-1 artifact used by every Phase-2 test; T2's verdicts are invariant to it, other slices may not be.

## 10. Artifacts (this directory)

- ladder_replay_v2.py (harness, change list in docstring)
- ws2_ladder_replay_ORIGINAL_COPY.py (verbatim copy of the repo harness)
- build_v2.py, t2_ext_src.py (generator)
- reg_check.py / reg_check.json (regression with the unmodified repo harness)
- t2_tests.py / t2_tests.json (identity + unit tests)
- h1_rearm.py / h1_results.json / h1_rows.json / h1_rearm.log
- h1_exclusion.py / h1_rearm_lib.py / h1_exclusion.json / h1_exclusion.log
- h2_reprice.py / h2_results_{protocol_csv253,confirmed_dead_only}.json / h2_rows_*.json / h2_*.log
- h3_schedules.py / h3_results_*.json / h3_rows_*.json / h3_*.log
- h4_frontier.py / h4_results_*.json / h4_*.log
- deadbook_check.py / deadbook_check.json / dead_book_union.csv / dead_book_classified.csv / dead_confirmed.csv
- log_rearm_evidence.json (parsed MAKER LADDER/OFF/FILLED events per window from the audit logs)
- log_crosscheck.py / log_crosscheck.json
- report_build.py (this report's generator)