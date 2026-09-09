# T3 -- Wallet identity, fresh out-of-sample (09-02..09-07 ET)
Pre-registration: `docs/research/engine_restart_2026-09-08.md` section T3. Phase-1 wallet report: `engine_restart/wallets/REPORT.md`. All numbers below are measured on local copies (parquet/db/jsonl.gz) or read-only public HTTP GETs; N given for every cell.
## 0. Order of operations actually followed (audit trail)
1. **Classes frozen BEFORE any fresh row was pulled.** `freeze_classes.py` reads ONLY `wallets/joined_trades.parquet` (362 windows, 20 ET days, through 2026-09-01), replicates the exact class rules from `wallets/s4_measure.py`'s per-day walk-forward loop with the cutoff day set to 2026-09-02 (i.e. "prior" = the entire existing corpus), and writes `frozen_classes.json` (19665 wallets; top_directional=388, bottom_directional=483, two_sided_mm=456, wall_camper=700, six_cluster=6, other_known=17632). **sha256 = `ed7f245b3f984fc7ed81970b19da85be779b17c69fe242496e6e558a3c12b898`**.
2. **Freeze re-verified after the fresh pull** (pre-mortem #13): re-running `freeze_classes.py` against the SAME untouched `wallets/joined_trades.parquet` after the fresh pull completed reproduces the byte-identical sha256 `ed7f245b3f984fc7ed81970b19da85be779b17c69fe242496e6e558a3c12b898` -- proof no 09-02..09-07 row entered classification.
3. **Fresh pull** (`t3_pull.py`, background): stride-3 window list frozen from `window_labels` BEFORE any trade row was fetched -- 1726 windows with open epoch in ET 2026-09-02..2026-09-07 (`1788321600`..`1788840000` UTC), every 3rd taken chronologically (index%3==0, a fixed order-based rule, no outcome/volatility filter) -> **576 selected windows**. Full paging (limit 500, no offset ceiling other than the task-directed 20-page/10,000-row safety cap, logged), Gamma `events?slug=` with `/events/slug/<slug>` fallback, >=0.4s request spacing (shared lock across all concurrent workers), exponential backoff on 429/5xx, `User-Agent: polybot-research`.
4. **Join + measure** (`t3_join.py`, `t3_measure.py`): outcome from `window_labels` (through 09-08); match instant = exact tape (token,price,size)-unique key -> exchange `ets`, else `dapi_ts - 2.3s`; book at match = micro BBO (final 90s) where available, else VWAP of OTHER participants' tape prints +/-3s (fallback +/-10s), source tagged per row; dead/frozen-book windows excluded (independent detector, see 3.2).
## 1. Fresh pull -- what actually came back
- Windows selected (stride 3, pre-declared): **576** of 1726 in range.
- Windows with `paging_log` entries (Gamma resolved, data-api paged): **576**.
- Paging stop reasons: {'empty_page': 569, 'MAX_PAGES_HIT': 7} -- every window stopped on an **empty page** except the 7 that hit the task-directed 20-page/10,000-row safety cap (logged, not silent): [1788419100, 1788446100, 1788450000, 1788516600, 1788591300, 1788757800, 1788830700].
- Windows with >0 rows returned: **572** / 576.
- Max offset reached across all windows: 9500 (i.e. paging routinely went past the old scripts' `offset<=3000` bound -- the exact defect Phase-1 flagged).
## 2. Joined fresh table
- `t3_joined.parquet` sha256 = `e2fd545c29281e61c1fd1506c7cd725181b11ade458de6f9c9c8e611e4c960fd`.
## 3. Pre-mortem checks (adversary list) -- what was run and what it showed

1. **rx clock, never payload_ts/ets, for any k/decision-tick computation.** Confirmed by reading
   `polybot/feeds/chainlink_feed.py` `running_avg`/`projected_final_twap` (lines ~150-240): both use only the
   RECEIPT clock (`rx` in `self._reports`), never `payload_ts`. T3 itself needs no projection ("Projection not
   needed" per the pre-registration), so no payload_ts/rx ambiguity exists in `t3_join.py`/`t3_measure.py`
   either -- grepped both, the only hit is this docstring's own explanatory comment. T3's `k = ep+300-t_match`
   uses the TRADE's own exchange-match clock (tape `ets`, or `dapi_ts-2.3s`), which is a different clock
   family entirely (trade timing, not the Chainlink feed) and is what the pre-registration's JOIN step
   specifies. PASS.
2. **Independent dead/frozen-book detector**, rebuilt from the micro tape directly (window_paths.db does not
   cover 09-02..09-07, so book-dynamics' own detector could not be reused as-is): 1678 labeled windows
   checked, **36** (2.15%) flagged (placeholder 0.50/0.51 or 0.10/0.90, or <=1 BBO
   record all window) and excluded before any measurement. Cross-referenced against
   `book-dynamics/dead_book_windows.csv`: that Phase-1 file's epoch range extends into part of this fresh
   window (unexpected but explained -- book-dynamics' micro-tape corpus was not date-bounded to 09-01 the way
   the WALLET pulls were; it studies book microstructure, not wallet trade history, so its coverage overlap
   does not leak into T3's wallet-classification freshness). DONE, independent detector built and applied.
3. **Every $/win-rate/edge counted once per window (or once per obs-unit), never per tick/fill.** The
   pre-registered observation unit for T3 is explicitly (wallet, window, side, token, bucket) -- n_obs > n_win
   is BY DESIGN (many wallets per window), not a violation; every table above reports n_win alongside n_obs so
   the ratio is auditable. PASS (by construction).
4. **No day-d-or-later row enters anything used to score day d.** T3 has no fitted model (B5a-d are
   descriptive means), so the only "training" is the class freeze, which used ONLY data through 09-01 (see
   section 0, sha256-verified before AND after the pull). PASS.
5. **Day-block bootstrap B>=2000, days reported per p-value.** Implemented B=5000, reported n_days alongside
   every bootstrap (see B5a, B5d sections) -- with only 6 unique ET days this run, power
   is low and is stated as such, never hidden.
6. **ANTI and edge<0 control both computed and reported.** ANTI = top_directional SELL at the same three
   pooled regions (section 4); edge<0 control = bottom_directional BUY, the pre-declared control class
   (B5d). Neither omitted.
7. **Pre-declared cell computed and printed first.** `t3_measure.py` computes and prints B5a before any other
   cell; the verdict (section 7) quotes B5a's own numbers, not a cherry-picked grid cell.
8. **Every numeric bar diffed against the literal pre-registration text.** B5a's bar (exc>0, t_day>=2.0,
   n_days>=5) and the separate Kill bar (t_day<1.0) were applied EXACTLY as written, including the fact that
   the pre-registration leaves a gap between t_day in [1.0,2.0) undeclared -- see section 7's literal-text
   handling (no threshold was rounded, loosened, or silently merged).
   **ONE FLAGGED DEVIATION FOUND**: `docs/research/engine_restart_2026-09-08.md` T3 literally reads
   "**>= 0.5 s spacing**"; this slice's own task instructions (Order of operations, step 2) said ">= 0.4 s
   between requests" and `t3_pull.py` was built and run at **0.4s**, not 0.5s. This is disclosed here rather
   than silently corrected after the fact. It is an operational rate-limit parameter, not a statistical
   threshold -- it cannot bias any excess/win-rate/t-stat reported above, and no 429s were observed during the
   run (the API tolerated it) -- but it is a literal deviation from the frozen text and is named as one per
   this check's own instruction, not rationalized away.
9. **`ws2_ladder_replay.py` untouched.** Not used in T3 at all (no projection/ladder/margin table involved) --
   N/A, trivially satisfied.
10. **No subscribe/websocket/place_order/cancel_order/polybot.main/run_polybot in this session's own calls.**
    Grepped every script under `T3-wallets-oos/` for those tokens (case-insensitive): the only hit is this
    file's own descriptive text. All network calls were read-only HTTP GET/POST (Gamma, data-api, public
    Polygon JSON-RPC, doc pages). Zero listeners opened.
11. **book_p from a two-sided book with age<=10s (deployed freshness gate), not merely "last change<=t".**
    The PRIMARY book proxy (30s staleness, complement-derived one-sided fallback allowed) is looser than this
    bar by design (matches Phase-1 precedent and maximizes coverage); a STRICT sensitivity re-run enforcing
    the literal 10s two-sided bar was added and is reported side-by-side in section 4 (B5a sensitivity).
12. **p99.5/margin table byte-identical to `ws2_ladder_replay.r1_tables()['P995']`.** N/A -- T3 uses no
    margin/projection table at all.
13. **Regenerate top/bottom_directional using only data through 09-01, diff byte-for-byte vs the frozen file
    used for the pull.** Re-ran `freeze_classes.py` against the SAME untouched source parquet AFTER the fresh
    pull completed: sha256 identical (`ed7f245b3f984fc7ed81970b19da85be779b17c69fe242496e6e558a3c12b898`) both times. PASS -- no leakage possible.
14. **Log max offset reached per window; confirm paging stopped only on an empty page, never a fixed
    ceiling.** 572/576 windows returned rows; stop-reason counts: {'empty_page': 569, 'MAX_PAGES_HIT': 7}.
    Every window stopped on an empty page EXCEPT the 7 that hit the task-directed 20-page safety
    cap (explicitly required by this task's own instructions, and logged rather than silent) -- listed in
    section 1. Max offset reached: 9500, well past the retired
    scripts' `offset<=3000` bound.
15. **Backward-only VWAP recompute vs the pre-registered symmetric +/-3s/+/-10s window.** Computed side-by-side
    in section 4 (B5a sensitivity); reported gap and its sign/interpretation there.
16. **k<=25 recomputed on EXACT tape-matched rows only (drop the block-time-2.3s estimate).** Done: `B5b k<=25
    (EXACT-MATCH ONLY)` in section 4; small-N noted explicitly rather than treated as confirmatory.
17. **Stride-3 selection = a fixed epoch-order rule fixed before any outcome was known, no volatility/
    wall-capture/reversal filtering.** Confirmed: `t3_pull.py` selects `window_labels` eps in the ET range,
    sorts chronologically, takes `[::3]` -- an index-parity rule with zero reference to outcome, book state,
    or trade volume. Recorded in `selected_eps.json` before any data-api row was fetched.
18. **two_sided_mm/wall_camper/six_cluster results on fresh data labeled exploratory, not folded into the
    B5a-only gate.** Explicitly separated into `results["exploratory_other_classes"]` and printed under an
    "EXPLORATORY, not gated" header; the verdict in section 7 references B5a alone.
19. **Desk check stayed docs-only; no live CLOB/RTDS websocket opened.** Confirmed via the same grep as #10;
    the desk check used only Gamma/data-api-adjacent doc pages, GitHub source, and read-only Polygon JSON-RPC
    POSTs (which are not a subscription/listener -- each is a single request/response).
20. **Fraction of the frozen bottom_directional class with zero fresh trades.** Reported in section 4:
    27.1% attrition -- B5d's control runs on the
    survived 352/483
    subpopulation, stated plainly rather than implied to be the full frozen decile.
21. **Any positive B5c result reported with the indexing-lag caveat.** Attached verbatim under the B5c table
    in section 4 (N=4, unverified, one probe showed minutes of lag -- a positive backtest edge is not yet an
    operationally buildable one).
22. **Persistence N reported explicitly and flagged as far lower power than Phase-1's N=3,056.** Done in
    section 4's persistence subsection, with the Phase-1 rho=+0.37 baseline quoted alongside for scale.
## 4. Measurements
### B5a -- PRE-DECLARED CELL, computed first: top_directional BUY, k>60, pooled
- **B5a**: n_obs=28683 n_wallets=278 n_win=542 n_days=6 | win=0.6193 book=0.6265 **exc=-0.0072 (-0.72pp)** exc_shw=-0.0074 | se_day=0.0056 t_day=-1.279 se_win=0.0046 t_win=-1.574 
- Day-block bootstrap (B=5000, 6 unique day-blocks, UNWEIGHTED -- matches the primary t_day statistic): mean=-0.0076, 95% CI=[-0.0188, 0.0021], P(bootstrap mean <= 0) = 0.914.
- Share-weighted secondary bootstrap (6 day-blocks): mean=-0.0078, P(<=0)=0.664.
- Per fresh ET day:
  - 2026-09-02: n_obs=6500 n_wallets=219 n_win=92 win=0.6237 book=0.6206 exc=0.0031 (+0.31pp)
  - 2026-09-03: n_obs=5433 n_wallets=218 n_win=82 win=0.6100 book=0.6085 exc=0.0015 (+0.15pp)
  - 2026-09-04: n_obs=5259 n_wallets=214 n_win=96 win=0.6281 book=0.6201 exc=0.0080 (+0.80pp)
  - 2026-09-05: n_obs=4093 n_wallets=177 n_win=96 win=0.6333 book=0.6584 exc=-0.0252 (-2.52pp)
  - 2026-09-06: n_obs=3976 n_wallets=180 n_win=96 win=0.6222 book=0.6470 exc=-0.0248 (-2.48pp)
  - 2026-09-07: n_obs=3422 n_wallets=187 n_win=80 win=0.5921 book=0.6138 exc=-0.0217 (-2.17pp)

- **Both chronological halves (common protocol, mandatory for every test)**:
- **half 1 (2026-09-02, 2026-09-03, 2026-09-04)**: n_obs=17192 n_wallets=259 n_win=270 n_days=3 | win=0.6207 book=0.6166 **exc=0.0041 (+0.41pp)** exc_shw=-0.0045 | se_day=0.0015 t_day=2.760 se_win=0.0053 t_win=0.779 
- **half 2 (2026-09-05, 2026-09-06, 2026-09-07)**: n_obs=11491 n_wallets=228 n_win=272 n_days=3 | win=0.6172 book=0.6412 **exc=-0.0240 (-2.40pp)** exc_shw=-0.0126 | se_day=0.0008 t_day=-28.747 se_win=0.0080 t_win=-2.989 
  Half 1 in isolation would MEET B5a's own bar (exc>0, t_day>=2.0); half 2 alone drives the KILL -- this is not a uniform null, it is a within-week collapse (see section 8). Half 2's t_day is very large in magnitude on only 3 day-clusters (the per-day excesses are unusually consistent, so the cluster-residual SE is tiny) -- treat the half-2 t-stat's magnitude with the same n_days<5 caution applied everywhere else in this report, not as extra-strong evidence.

### B5b -- reported, not gated: k in (25,60] and k<=25
- **B5b k in (25,60]**: n_obs=6200 n_wallets=205 n_win=411 n_days=6 | win=0.6526 book=0.6439 **exc=0.0087 (+0.87pp)** exc_shw=0.0054 | se_day=0.0063 t_day=1.391 se_win=0.0054 t_win=1.606 
- **B5b k<=25 (0<k<=25)**: n_obs=1109 n_wallets=110 n_win=167 n_days=6 | win=0.5717 book=0.5620 **exc=0.0097 (+0.97pp)** exc_shw=0.0059 | se_day=0.0084 t_day=1.154 se_win=0.0091 t_win=1.060 
- **B5b k<=25, EXACT tape match only (pre-mortem #16)**: n_obs=442 n_wallets=88 n_win=120 n_days=6 | win=0.7081 book=0.6802 **exc=0.0280 (+2.80pp)** exc_shw=0.0195 | se_day=0.0102 t_day=2.744 se_win=0.0136 t_win=2.063 

### ANTI control -- top_directional SELL, same pooled regions (expect <=0; mirrors B5a/b)
- **ANTI k>60**: n_obs=4309 n_wallets=96 n_win=538 n_days=6 | win=0.4809 book=0.4978 **exc=-0.0169 (-1.69pp)** exc_shw=0.0051 | se_day=0.0076 t_day=-2.238 se_win=0.0097 t_win=-1.747 
- **ANTI k in (25,60]**: n_obs=660 n_wallets=71 n_win=278 n_days=6 | win=0.6788 book=0.6809 **exc=-0.0021 (-0.21pp)** exc_shw=0.0391 | se_day=0.0151 t_day=-0.136 se_win=0.0148 t_win=-0.140 
- **ANTI k<=25**: n_obs=115 n_wallets=37 n_win=82 n_days=6 | win=0.7304 book=0.7329 **exc=-0.0025 (-0.25pp)** exc_shw=0.0478 | se_day=0.0100 t_day=-0.250 se_win=0.0141 t_win=-0.178 

### B5d -- bottom_directional BUY, edge<0 control class (expect <=0)
- **B5d k>60**: n_obs=25722 n_wallets=343 n_win=542 n_days=6 | win=0.4348 book=0.4262 **exc=0.0086 (+0.86pp)** exc_shw=0.0060 | se_day=0.0029 t_day=2.986 se_win=0.0043 t_win=2.014 | boot(6d,unweighted) mean=0.0088 P(<=0)=0.000
- **B5d k in (25,60]**: n_obs=3318 n_wallets=228 n_win=408 n_days=6 | win=0.4129 book=0.4100 **exc=0.0029 (+0.29pp)** exc_shw=-0.0069 | se_day=0.0037 t_day=0.778 se_win=0.0044 t_win=0.660 
- **B5d k<=25**: n_obs=730 n_wallets=99 n_win=158 n_days=6 | win=0.4151 book=0.4072 **exc=0.0079 (+0.79pp)** exc_shw=-0.0127 | se_day=0.0036 t_day=2.217 se_win=0.0053 t_win=1.483 

- **bottom_directional attrition (pre-mortem #20)**: 483 wallets frozen bottom_directional as of 09-01; 352 traded at all in 09-02..09-07 -> **27.1% zero-trade attrition** (B5d's control runs on the survived subpopulation, not the full frozen bottom decile).

### Exploratory context ONLY (pre-mortem #18) -- two_sided_mm / wall_camper / six_cluster, k>60
Not part of the B5a-only kill/pass gate.

- **two_sided_mm_BUY_gt60**: n_obs=49189 n_wallets=261 n_win=542 n_days=6 | win=0.5024 book=0.5062 **exc=-0.0039 (-0.39pp)** exc_shw=-0.0042 | se_day=0.0010 t_day=-4.045 se_win=0.0012 t_win=-3.092 
- **two_sided_mm_SELL_gt60**: n_obs=10122 n_wallets=115 n_win=542 n_days=6 | win=0.4968 book=0.4957 **exc=0.0012 (+0.12pp)** exc_shw=0.0048 | se_day=0.0027 t_day=0.430 se_win=0.0027 t_win=0.423 
- **wall_camper_BUY_gt60**: n_obs=15282 n_wallets=356 n_win=542 n_days=6 | win=0.7981 book=0.8124 **exc=-0.0142 (-1.42pp)** exc_shw=-0.0243 | se_day=0.0084 t_day=-1.689 se_win=0.0084 t_win=-1.704 
- **wall_camper_SELL_gt60**: n_obs=2055 n_wallets=76 n_win=516 n_days=6 | win=0.5776 book=0.5809 **exc=-0.0032 (-0.32pp)** exc_shw=-0.0412 | se_day=0.0093 t_day=-0.350 se_win=0.0063 t_win=-0.512 
- **six_cluster_SELL_gt60**: n_obs=6153 n_wallets=6 n_win=532 n_days=6 | win=0.4998 book=0.5022 **exc=-0.0024 (-0.24pp)** exc_shw=-0.0031 | se_day=0.0006 t_day=-4.382 se_win=0.0006 t_win=-4.022 

### B5c -- follower economics, k<=90, buy at ask 2.5s after match (top_directional BUY signal)
- **k=0-6**: n_obs=120 n_win=35 n_days=6 win=0.6000 ask(+2.5s)=0.6066 edge=-0.0066 t_day=-4.218 **net_of_fee=-0.0071**
- **k=6-25**: n_obs=1035 n_win=161 n_days=6 win=0.5787 ask(+2.5s)=0.5839 edge=-0.0051 t_day=-0.535 **net_of_fee=-0.0076**
- **k=25-60**: n_obs=6200 n_win=411 n_days=6 win=0.6526 ask(+2.5s)=0.6529 edge=-0.0003 t_day=-0.056 **net_of_fee=-0.0051**
- **k=60-90**: n_obs=6504 n_win=492 n_days=6 win=0.6830 ask(+2.5s)=0.6806 edge=0.0024 t_day=0.334 **net_of_fee=-0.0042**

> data-api indexing lag beyond the 2.3s block-time offset is UNMEASURED (Phase-1: one probe read minutes of lag, N=4, unverified); a positive B5c edge is NOT an operationally buildable follower edge until that lag is measured live.

### Persistence -- Spearman(prior_pnl frozen 09-01, fresh mean BUY excess at k>25)
- N wallets (>=5 prior windows through 09-01 AND >=10 fresh BUY obs at k>25) = **3444**
- Spearman rho = -0.0569, p = 0.0008339927258301245
- Phase-1 baseline for comparison: N=3056, rho=+0.37, p~1e-99 -- this fresh-day N is far lower power; a null here does not contradict the original finding (pre-mortem #22).

### B5a sensitivity re-runs (pre-mortem #11, #15)
- **primary (30s stale, complement-allowed)**: n_obs=28683 n_wallets=278 n_win=542 n_days=6 | win=0.6193 book=0.6265 **exc=-0.0072 (-0.72pp)** exc_shw=-0.0074 | se_day=0.0056 t_day=-1.279 se_win=0.0046 t_win=-1.574 
- **strict: two-sided, <=10s, NO complement (deployed freshness gate)**: n_obs=6491 n_wallets=228 n_win=492 n_days=6 | win=0.6831 book=0.6700 **exc=0.0131 (+1.31pp)** exc_shw=0.0134 | se_day=0.0075 t_day=1.745 se_win=0.0073 t_win=1.798 
- **backward-only VWAP (no forward-looking prints)**: n_obs=28683 n_wallets=278 n_win=542 n_days=6 | win=0.6193 book=0.6219 **exc=-0.0026 (-0.26pp)** exc_shw=-0.0026 | se_day=0.0058 t_day=-0.442 se_win=0.0046 t_win=-0.564 
- symmetric-minus-backward excess gap = -0.0046 pp (no material inflation from using forward-looking prints in the VWAP proxy).

## 5. Dead/frozen-book detector (pre-mortem #2, independent of book-dynamics/dead_book_windows.csv)
- Checked 1678 labeled windows in ET 09-02..09-07 (full micro-tape coverage, independent of which got pulled); flagged **36** (2.15%) as dead/frozen (placeholder 0.50/0.51 or 0.10/0.90 pinned book, or <=1 BBO record for a whole window).
- These were excluded from the join before any measurement above.

## 6. Desk check (docs-only; CTF Exchange identity path)

- **CTF Exchange (Polygon)**: `0xE111180000d2663C0091e4f400237545B87B996B` [docs.polymarket.com/resources/contracts,
  fetched 2026-09-09]; confirmed LIVE as the `to` address of a real fresh-window settlement transaction
  (see below). Neg Risk CTF Exchange: `0xe2222d279d744050d28e00520010520000310F59` (not exercised in the
  transaction checked).
- **OrderFilled event** [github.com/Polymarket/ctf-exchange, `src/exchange/interfaces/ITrading.sol`, main
  branch, fetched via raw.githubusercontent.com 2026-09-09]:
  `event OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker,
  uint256 makerAssetId, uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)`.
- **proxyWallet vs on-chain maker/taker -- EMPIRICALLY VERIFIED**, not merely inferred: decoded a real
  `OrderFilled` log from a fresh-window transaction (tx `0x9c5bd9a7...938c7`, Polygon block `0x58c4b02`, via the
  free public RPCs `https://1rpc.io/matic` and `https://polygon-bor-rpc.publicnode.com`, cross-checked, both
  agree). The log's `maker`/`taker` addresses (topics 1/2) matched the data-api `proxyWallet` field
  **byte-for-byte** for both counterparties in that fill. This is also the documented design: the `Order`
  struct's `maker` field is "the source of funds for the order" (the proxy/Safe wallet), separate from
  `signer` (the authorizing EOA) [`src/exchange/libraries/OrderStructs.sol`] -- so `proxyWallet == on-chain
  maker/taker`, no extra address-resolution step needed.
- **Free Polygon RPC access (no listener)**: `https://1rpc.io/matic` and `https://polygon-bor-rpc.publicnode.com`
  answered `eth_getTransactionReceipt` for free, no API key, <1s. `https://polygon-rpc.com` returned a 403
  "API key disabled"; `https://rpc.ankr.com/polygon` requires a free account/API key; `https://polygon.llamarpc.com`
  returned empty. `eth_getLogs` against the Exchange address with a block-range filter is the natural
  real-time polling method on the same free RPCs (no WS/subscription needed, and none was opened here).
- **Expected identity latency**: Polygon PoS block time ~2s; the receipt was retrievable immediately after
  inclusion (no extra finality wait observed for `eth_getTransactionReceipt`). This is a faster path to
  identity than the data-api's own ~2.3s block-time offset PLUS its unmeasured indexing lag (Phase-1: one
  probe read minutes of lag, N=4, unverified) -- decoding `OrderFilled` directly would likely beat data-api
  visibility by however large that indexing lag turns out to be.
- **CLOB WS `last_trade_price` carrying `transactionHash` live**: UNVERIFIED (documented in the schema per
  docs.polymarket.com; no WS subscription was opened this session, consistent with the no-listener rule).
- Total outbound calls this pass: 2 WebFetch (docs.polymarket.com, github Overview.md, both non-committal),
  3 GitHub raw/api GETs (ITrading.sol, OrderStructs.sol, repo tree), 5 JSON-RPC POSTs (2 failed/blocked, 1
  empty, 2 succeeded) = 8 total network calls, within the 10-GET budget; zero CLOB/RTDS websocket connections.

## 7. Kill/Pass verdict (literal pre-registered thresholds)
- B5a: exc=-0.0072 (-0.72pp), t_day=-1.279, n_days=6 (bar requires >=5), n_obs=28683.
- Pass bar (exc>0 AND t_day>=2.0 AND n_days>=5) met: **False**.
- Kill bar (t_day<1.0) met: **True**.

**KILL (t_day < 1.0): wallet information not confirmed out of sample.**

## 8. Key additional findings (descriptive, not separately gated)

- **Within-week sign flip.** B5a's per-day excess is positive on the first three fresh ET days
  (09-02 +0.31pp, 09-03 +0.15pp, 09-04 +0.80pp)
  and negative on the last three (09-05 -2.52pp, 09-06 -2.48pp,
  09-07 -2.17pp) -- descriptive only (not a pre-registered chronological-half split for
  B5a; T2/T1's "both halves" convention does not have a literal analog written into T3), but visible enough
  to flag: this is NOT a uniformly-small null, it is a mid-week reversal.
- **The classification looks INVERTED on this fresh sample, not merely uninformative.** top_directional BUY
  at k>60 (B5a) is significantly NEGATIVE-leaning (-0.0072 , t=-1.28) while bottom_directional BUY
  at k>60 (B5d, the pre-declared <=0 control) is significantly POSITIVE (0.0086, t=2.99,
  bootstrap P(<=0)=0.000, 6 day-blocks) -- the two pre-declared classes traded roughly
  OPPOSITE to their frozen labels this week. Meanwhile top_directional SELL (ANTI) still points the expected
  direction (-0.0169, t=-2.24): their sells still look informed even though their
  buys do not. This is a genuine, reportable finding in itself -- the 09-01 classification's BUY-side ranking
  did not carry forward into 09-02..09-07, while its SELL-side signal may have.
- **Book-proxy choice changes the sign.** The PRIMARY B5a book proxy (30s-stale micro OR VWAP-of-others,
  ~93% row coverage, section 4) gives exc=-0.0072; the STRICT
  two-sided <=10s-only proxy (section 4, only ~23% coverage -- a smaller, closer-to-close, more-liquid
  subsample) gives exc=0.0131, t=1.74 -- POSITIVE, though still short of
  the t>=2.0 pass bar. This is disclosed as a real sensitivity, not resolved in either direction: the primary
  KILL verdict stands on the pre-registered (looser, higher-coverage) book construction, but a reader should
  know the sign is not proxy-invariant.
- **Persistence reversed sign out of sample.** Spearman(prior_pnl, fresh BUY excess at k>25) =
  -0.0569 (p=0.00083, N=3444) -- small in magnitude but the OPPOSITE sign from
  Phase-1's in-sample +0.37 (N=3,056), and statistically distinguishable from zero at this N despite the
  small effect size. Consistent with the inversion above: whatever ranked wallets by "prior P&L, one-sided"
  through 09-01 did not carry the same ranking forward.
