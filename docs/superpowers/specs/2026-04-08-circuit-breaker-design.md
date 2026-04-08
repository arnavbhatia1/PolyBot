# Circuit Breaker Design

## Problem

PolyBot has no performance-based guard on entries. If the model enters a bad streak (regime shift, stale signals, unusual market structure), it keeps betting at full Kelly. Three losses at full Kelly on binary outcomes can draw down 15-30% of bankroll before the daily learning pipeline has a chance to adapt.

## Solution

A streak-based circuit breaker that halves Kelly sizing after consecutive losses and restores it after consecutive wins. Resets daily at session open.

## State Machine

```
NORMAL (kelly_multiplier = 1.0)
  ── loss ──> increment consecutive_losses
                if consecutive_losses >= 3 ──> REDUCED
  ── win ───> reset consecutive_losses to 0, stay NORMAL

REDUCED (kelly_multiplier = 0.5)
  ── win ───> increment wins_since_reduction
                if wins_since_reduction >= 2 ──> NORMAL (reset all)
  ── loss ──> reset wins_since_reduction to 0, stay REDUCED

DAY OPEN ──> reset all counters ──> NORMAL
```

## New File: `execution/circuit_breaker.py`

### Class: `CircuitBreaker`

**Constructor args:**
- `losses_to_reduce: int = 3` — consecutive losses before halving Kelly
- `wins_to_restore: int = 2` — wins at half Kelly before restoring full

**State (in-memory only, not persisted):**
- `consecutive_losses: int = 0`
- `wins_since_reduction: int = 0`
- `reduced: bool = False`

**Methods:**

`record_win() -> str | None`
- Sets `consecutive_losses = 0`
- If `reduced`: increments `wins_since_reduction`
  - If `wins_since_reduction >= wins_to_restore`: sets `reduced = False`, resets `wins_since_reduction = 0`, returns `"restored"` (for logging/alerts)
- Returns `None` if no state change

`record_loss() -> str | None`
- Increments `consecutive_losses`, sets `wins_since_reduction = 0`
- If `consecutive_losses >= losses_to_reduce` and not already `reduced`: sets `reduced = True`, returns `"reduced"` (for logging/alerts)
- Returns `None` if no state change

`kelly_multiplier` (property) -> `float`
- Returns `0.5` if `reduced`, else `1.0`

`reset()`
- Sets `consecutive_losses = 0`, `wins_since_reduction = 0`, `reduced = False`

## Config: `settings.yaml`

```yaml
circuit_breaker:
  losses_to_reduce: 3    # consecutive losses before halving Kelly
  wins_to_restore: 2     # wins at half Kelly before restoring full
```

## Integration in `main.py`

### 1. Initialization (~line 815, after trader creation)

```python
from polybot.execution.circuit_breaker import CircuitBreaker

cb_cfg = config.get("circuit_breaker", {})
breaker = CircuitBreaker(
    losses_to_reduce=cb_cfg.get("losses_to_reduce", 3),
    wins_to_restore=cb_cfg.get("wins_to_restore", 2),
)
```

### 2. Day open reset (~line 271, alongside existing day counter resets)

```python
breaker.reset()
```

### 3. After every trade closes (3 locations)

Resolution (~line 348), scalp exit (~line 428), orphan resolution (~line 315) — all follow the same pattern:

```python
cb_event = breaker.record_win() if pnl > 0 else breaker.record_loss()
if cb_event and alert_manager:
    await alert_manager.send_circuit_breaker(cb_event, breaker)
```

### 4. Entry sizing (~line 619)

```python
size = round(bankroll * signal.kelly_size * breaker.kelly_multiplier, 2)
```

### 5. Logging

When `cb_event == "reduced"`:
```
CIRCUIT BREAKER: 3 consecutive losses — Kelly halved to 0.075
```

When `cb_event == "restored"`:
```
CIRCUIT BREAKER: 2 wins at half Kelly — restored to full 0.15
```

## Discord Alert

Add `send_circuit_breaker(event, breaker)` to `AlertManager`. Sends to the trade channel:

- **Reduced:** Warning embed — "Circuit breaker tripped: 3 consecutive losses. Kelly reduced to 50%."
- **Restored:** Info embed — "Circuit breaker restored: 2 wins at half Kelly. Back to full sizing."

## What This Does NOT Change

- Position management (hold/scalp/resolve) — unaffected, always runs
- Entry gates (edge, depth, spread, probability) — unaffected
- Learning pipeline — unaffected (could tune `losses_to_reduce`/`wins_to_restore` in the future)
- Signal engine — unaffected, only sizing changes

## Edge Cases

- **Bot restart mid-day:** Streak resets (in-memory). Acceptable — daily reset would clear it soon anyway.
- **First trade of the day is a loss:** `consecutive_losses = 1`, no action. Needs 3 in a row.
- **Win while not reduced:** Just resets `consecutive_losses`. No effect on Kelly.
- **Loss while already reduced:** Resets `wins_since_reduction` counter (progress toward restoration lost). Stays at half Kelly.
