# Enhanced Probability Model & Trading Edge — Design Spec

## Problem

The paper trader has never entered a trade. Root causes:
1. Market prices show `Up=0.99 Dn=0.99` — bot only reads ask side, blind to effective pricing via complementary bids
2. Vanilla Brownian motion model (normal CDF) matches what market makers use as baseline — no edge
3. Indicators tuned for daily timeframes, not 1-min candles
4. Strike derivation misses first ~60s of each window (off-by-one)
5. ATR gate computed but never enforced
6. Scalp exits don't account for spread + fee cost

## Approach: Enhanced Brownian + Order Flow (Approach A)

Keep the solid Brownian motion foundation. Layer independent signal sources that each capture information the market partially but not fully prices.

## Changes

### 1. Enhanced Probability Model (signal_engine.py)

Four-layer probability stack:

**Layer 1 — Fat-tailed base (Student-t CDF, df=4):**
```
z = distance / (smart_vol * sqrt(time))
P_base = student_t_cdf(z, df=4)
```
BTC 1-min returns have kurtosis ~6-8 (normal=3). Normal CDF underestimates reversal probability. Student-t with df=4 captures the fat tails. When BTC is $50 above strike with 2 min left, normal says 85% Up, Student-t says ~78%. The 7% difference is edge on the underdog side.

**Layer 2 — Regime detection (autocorrelation):**
```
autocorr = correlation(returns[-5:], returns[-6:-1])
regime_adj = autocorr * regime_weight (max ±5%)
```
Positive autocorr = trending = amplify base probability away from 0.5.
Negative autocorr = mean reverting = dampen toward 0.5.

**Layer 3 — Order flow signal (from CLOB WebSocket):**
```
book_imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
trade_flow = net_buy_volume / total_volume (from buffered last_trade_price events)
flow_signal = 0.6 * book_imbalance + 0.4 * trade_flow
flow_adj = flow_signal * flow_weight (max ±6%)
```
Order flow LEADS price. Informed traders accumulate before CLOB reprices.

**Layer 4 — Momentum nudge (indicators, reduced to ±4%):**
Same indicators but at half weight. Demoted because indicators are the weakest signal.

**Final:** `P(Up) = clamp(P_base + regime_adj + flow_adj + momentum, 0.03, 0.97)`

### 2. Complementary Pricing (main.py)

Fix the `Up=0.99 Dn=0.99` problem. In negRisk binary markets:
```python
effective_buy_down = min(ask_down, 1.0 - bid_up)
effective_buy_up = min(ask_up, 1.0 - bid_down)
```
If bid_up=0.97, effective Down cost = $0.03, not the $0.99 showing on the Down ask book.

### 3. Order Flow Infrastructure

**ClobWebSocket (clob_ws.py):**
- Add `trade_buffer: dict[str, deque(maxlen=50)]` — accumulates `last_trade_price` events per token
- Each entry: `{price, size, side, timestamp}`

**New module (core/order_flow.py):**
```python
def compute_flow_signal(trade_buffer, book_up, book_down, side="Up") -> dict:
    # Returns: {book_imbalance, trade_flow, flow_score, trade_count}
```

**Market Scanner (market_scanner.py) — new endpoints:**
- `GET /prices-history?market=ASSET_ID&interval=1h&fidelity=1` — CLOB price momentum
  - No auth required. Returns `{history: [{t: timestamp, p: price}]}`
- `GET /oi?market=CONDITION_ID` — open interest
  - No auth required. Returns `[{market, value}]`

**NOT using GET /trades** — requires API key auth. WebSocket `last_trade_price` events provide trade flow without auth.

### 4. Indicator Timeframes (settings.yaml)

| Indicator | Current | New | Rationale |
|-----------|---------|-----|-----------|
| RSI | period=14 | period=5 | 5 minutes = current window |
| MACD | 12/26/9 | 5/13/4 | ~40% of standard periods |
| Stochastic | k=14, d=3 | k=5, d=2 | Same scaling |
| EMA | 9/21 | 3/8 | Fast enough for window |
| OBV | slope=5 | slope=3 | 3-minute trend |
| ATR | period=14 | period=7 | 7-minute vol window |

Momentum weight: 0.08 -> 0.04 (indicators demoted to Layer 4).

### 5. Scalp Bar Enhancement (signal_engine.py evaluate_hold)

Fee-aware threshold:
```python
scalp_cost = (spread_cost + exit_fee_per_share) / entry_price
effective_threshold = base_exit_threshold - scalp_cost
time_urgency = max(0, 1.0 - seconds_remaining / 120)
effective_threshold += time_urgency * 0.05
```
Early in window: higher bar (only scalp if very negative).
Near expiry: lower bar (better to take a small loss than risk total loss).

### 6. Strike Derivation Fix (main.py ~line 506)

```python
# Bug: < 60 (misses candle at exactly 60s boundary)
# Fix: <= 60
if abs(c.timestamp / 1000 - window_ts) <= 60:
```

### 7. ATR Gate Enforcement (signal_engine.py evaluate)

```python
if not indicators.get("atr", {}).get("passes", True):
    return TradeSignal("SKIP", 0.5, 0, 0, f"ATR gate: {reason}")
```

## New Parameters (settings.yaml additions)

```yaml
signal:
  momentum_weight: 0.04      # was 0.08 — demoted (Layer 4)
  regime_weight: 0.05         # max ±5% regime adjustment (Layer 2)
  flow_weight: 0.06           # max ±6% order flow adjustment (Layer 3)
  student_t_df: 4             # degrees of freedom for fat-tailed CDF
  scalp_time_urgency_window: 120  # seconds — urgency ramps in last 2 min
```

## File Changes

| File | Changes |
|------|---------|
| `core/signal_engine.py` | Complete rewrite — 4-layer model, ATR gate, fee-aware scalp |
| `core/order_flow.py` | NEW — compute_flow_signal() |
| `core/clob_ws.py` | Add trade_buffer deque per token |
| `core/market_scanner.py` | Add fetch_prices_history(), fetch_open_interest() |
| `main.py` | Strike fix, complementary pricing, wire new signals |
| `config/settings.yaml` | Indicator timeframes, new signal params |
| `indicators/engine.py` | Update DEFAULT_PARAMS |
| Tests | Update signal_engine, add order_flow, add complementary pricing tests |
| `CLAUDE.md` | Reflect new architecture |

## Implementation Order

1. **Parallel (no deps):** signal_engine.py, order_flow infra, indicator timeframes
2. **After 1:** main.py integration (wires everything together)
3. **After 2:** tests, CLAUDE.md update
