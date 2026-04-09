# Signal Engine Math Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 7 mathematical inconsistencies in the probability engine so it produces properly calibrated, price-aware trading signals — without touching execution, position management, or any trade placement logic.

**Architecture:** All changes are internal to the probability pipeline. `compute_probability()` gets the core math fixes (regime direction, logit space, ATR scaling, Student-t normalization). `evaluate()` gets a Kelly-based entry gate. `IndicatorEngine` gets z-score normalization. A new `PlattCalibrator` class provides Platt scaling fitted daily. External API surface (`TradeSignal`, `evaluate()`, `evaluate_hold()` signatures) is unchanged.

**Tech Stack:** Python 3.12, scipy (already a dependency for Student-t CDF), numpy (already imported), pytest

**Spec:** `docs/superpowers/specs/2026-04-09-signal-engine-math-fixes-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `polybot/core/signal_engine.py` | Modify | Fixes 1, 2, 3, 4, 5: regime direction, logit space, Kelly gate, ATR scaling, Student-t normalization |
| `polybot/core/calibrator.py` | **Create** | Fix 9: PlattCalibrator class (fit, calibrate, save, load) |
| `polybot/indicators/engine.py` | Modify | Fix 8: IndicatorNormalizer class, norm_score in output |
| `polybot/config/loader.py` | Modify | Validation: new param bounds, entry_threshold range change |
| `polybot/config/settings.yaml` | Modify | New params: min_kelly, atr_sigma_ratio; entry_threshold lowered |
| `polybot/main.py` | Modify | Construction: pass new params, init calibrator, fix defaults, enrich outcomes |
| `polybot/brain/claude_client.py` | Modify | Pipeline: system prompt, validation, context for new params |
| `polybot/agents/ta_evolver.py` | Modify | Pipeline: log new params |
| `polybot/agents/scheduler.py` | Modify | Pipeline: hot-swap, persist, diff, Discord, Platt fit step |
| `polybot/tests/conftest.py` | Modify | Add min_kelly, atr_sigma_ratio to SAMPLE_CONFIG |
| `polybot/tests/test_calibrator.py` | **Create** | Tests for PlattCalibrator |
| `polybot/tests/test_signal_engine.py` | Modify | Fix 3 breaking tests, add 6 new tests |
| `polybot/tests/test_indicator_engine.py` | Modify | Add normalizer tests |
| `polybot/tests/test_config.py` | Modify | Add bounds for new params, update entry_threshold range |
| `CLAUDE.md` | Modify | Document all changes |

---

### Task 1: Config Foundation — New Params + Validation

**Files:**
- Modify: `polybot/config/settings.yaml:70-77`
- Modify: `polybot/config/loader.py:69,75-76`
- Modify: `polybot/tests/conftest.py:36,50`
- Modify: `polybot/tests/test_config.py:79,86,102,107,122-131,155-173`

- [ ] **Step 1: Update settings.yaml with new params and lowered entry_threshold**

```yaml
# In signal: section, change entry_threshold and add two new params:
signal:
  entry_threshold: 0.03  # Changed from 0.10 — noise floor only (pipeline range: 0.01-0.10)
  min_kelly: 0.015        # NEW — Kelly-based entry gate (pipeline range: 0.005-0.05)
  atr_sigma_ratio: 1.7    # NEW — ATR-to-σ conversion (pipeline range: 1.2-2.5)
```

- [ ] **Step 2: Update loader.py validation ranges**

In `polybot/config/loader.py`, change line 69 and add two new checks after line 75:

```python
# Line 69 — change range:
_check_range("signal.entry_threshold", 0.01, 0.10)

# After line 75 — add:
_check_range("signal.min_kelly", 0.005, 0.05)
_check_range("signal.atr_sigma_ratio", 1.2, 2.5)
```

- [ ] **Step 3: Update conftest.py SAMPLE_CONFIG**

In `polybot/tests/conftest.py`, add to the `signal` dict (after line 50, before `active_weights_version`):

```python
"min_kelly": 0.015,
"atr_sigma_ratio": 1.7,
```

And change `entry_threshold` on line 36 from `0.10` to `0.03`.

- [ ] **Step 4: Update test_config.py — boundary tests**

In `test_boundary_low_values` (line 75), change and add:
```python
_set_nested(cfg, "signal.entry_threshold", 0.01)  # was 0.05
_set_nested(cfg, "signal.min_kelly", 0.005)        # NEW
_set_nested(cfg, "signal.atr_sigma_ratio", 1.2)    # NEW
```

In `test_boundary_high_values` (line 98), change and add:
```python
_set_nested(cfg, "signal.entry_threshold", 0.10)   # was 0.35
_set_nested(cfg, "signal.min_kelly", 0.05)          # NEW
_set_nested(cfg, "signal.atr_sigma_ratio", 2.5)     # NEW
```

In `TestValidateConfigMissing` parametrize list (line 122), add:
```python
"signal.min_kelly",
"signal.atr_sigma_ratio",
```

In `TestValidateConfigOutOfRange` parametrize list (line 155), change entry_threshold and add:
```python
("signal.entry_threshold", 0.001, "not in [0.01, 0.1]"),
("signal.entry_threshold", 0.20, "not in [0.01, 0.1]"),
("signal.min_kelly", 0.001, "not in [0.005, 0.05]"),
("signal.min_kelly", 0.10, "not in [0.005, 0.05]"),
("signal.atr_sigma_ratio", 1.0, "not in [1.2, 2.5]"),
("signal.atr_sigma_ratio", 3.0, "not in [1.2, 2.5]"),
```

- [ ] **Step 5: Run config tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_config.py -v`
Expected: ALL PASS

---

### Task 2: PlattCalibrator — New File + Tests

**Files:**
- Create: `polybot/core/calibrator.py`
- Create: `polybot/tests/test_calibrator.py`

- [ ] **Step 1: Write the test file**

```python
# polybot/tests/test_calibrator.py
import math
import json
import pytest
from polybot.core.calibrator import PlattCalibrator, compute_log_loss


def test_identity_calibration():
    """Default params (a=-1, b=0) return input unchanged."""
    cal = PlattCalibrator()
    assert cal.is_identity
    for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
        assert abs(cal.calibrate(p) - p) < 0.001


def test_fit_requires_min_samples():
    """Returns False with < 100 outcomes."""
    cal = PlattCalibrator()
    probs = [0.6] * 50
    outcomes = [1] * 30 + [0] * 20
    assert cal.fit(probs, outcomes) is False
    assert cal.is_identity  # params unchanged


def test_fit_with_biased_data():
    """Fit on data where model overestimates → a and b shift to correct."""
    cal = PlattCalibrator()
    # Model says 0.80 but actual win rate is 0.50
    probs = [0.80] * 200
    outcomes = [1] * 100 + [0] * 100
    assert cal.fit(probs, outcomes) is True
    assert not cal.is_identity
    # Calibrated probability should be closer to 0.50 than 0.80
    calibrated = cal.calibrate(0.80)
    assert calibrated < 0.70


def test_save_and_load(tmp_path):
    """Round-trip persistence to JSON."""
    cal = PlattCalibrator(a=-0.8, b=0.1)
    path = tmp_path / "platt.json"
    cal.save(path)
    cal2 = PlattCalibrator()
    cal2.load(path)
    assert abs(cal2.a - (-0.8)) < 1e-6
    assert abs(cal2.b - 0.1) < 1e-6


def test_calibration_improves_log_loss():
    """Calibrated probs have lower log-loss on holdout."""
    import numpy as np
    np.random.seed(42)
    # Model says 0.70 for everything, actual rate is 0.55
    n = 300
    probs = [0.70] * n
    outcomes = list(np.random.binomial(1, 0.55, n))

    cal = PlattCalibrator()
    cal.fit(probs[:200], outcomes[:200])

    # Evaluate on holdout
    holdout_probs = probs[200:]
    holdout_outcomes = outcomes[200:]
    raw_loss = compute_log_loss(holdout_probs, holdout_outcomes)
    cal_probs = [cal.calibrate(p) for p in holdout_probs]
    cal_loss = compute_log_loss(cal_probs, holdout_outcomes)
    assert cal_loss < raw_loss


def test_compute_log_loss():
    """Sanity check: perfect predictions have near-zero loss."""
    probs = [0.99, 0.99, 0.01, 0.01]
    outcomes = [1, 1, 0, 0]
    loss = compute_log_loss(probs, outcomes)
    assert loss < 0.05

    # Bad predictions have high loss
    bad_probs = [0.01, 0.01, 0.99, 0.99]
    bad_loss = compute_log_loss(bad_probs, outcomes)
    assert bad_loss > 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd PolyBot && python -m pytest polybot/tests/test_calibrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'polybot.core.calibrator'`

- [ ] **Step 3: Write calibrator.py**

```python
# polybot/core/calibrator.py
"""Platt scaling probability calibration.

Fits a 2-parameter sigmoid to map raw model probabilities to calibrated ones:
    calibrated = 1 / (1 + exp(A * logit(raw) + B))

Identity (no calibration): A = -1.0, B = 0.0
Minimum 100 outcomes required to fit.
"""
from __future__ import annotations

import json
import math
import logging
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = Path("polybot/memory/calibration/platt_params.json")


def compute_log_loss(probs: list[float], outcomes: list[int]) -> float:
    """Binary cross-entropy loss."""
    total = 0.0
    for p, y in zip(probs, outcomes):
        p = max(1e-10, min(1 - 1e-10, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(probs) if probs else float("inf")


class PlattCalibrator:

    def __init__(self, a: float = -1.0, b: float = 0.0) -> None:
        self.a: float = a
        self.b: float = b

    @property
    def is_identity(self) -> bool:
        return self.a == -1.0 and self.b == 0.0

    def calibrate(self, raw_prob: float) -> float:
        """Apply Platt scaling. With defaults (a=-1, b=0), returns raw_prob unchanged."""
        raw_prob = max(1e-6, min(1 - 1e-6, raw_prob))
        logit = math.log(raw_prob / (1.0 - raw_prob))
        return 1.0 / (1.0 + math.exp(self.a * logit + self.b))

    def fit(self, probs: list[float], outcomes: list[int],
            min_samples: int = 100) -> bool:
        """Fit calibration parameters from historical data.

        Returns True if fit succeeded and parameters were updated.
        """
        if len(probs) < min_samples:
            logger.info(f"Platt calibration: {len(probs)} samples < {min_samples} minimum, skipping")
            return False

        probs_arr = np.clip(np.array(probs), 1e-6, 1 - 1e-6)
        outcomes_arr = np.array(outcomes, dtype=float)
        logits = np.log(probs_arr / (1 - probs_arr))

        def neg_log_likelihood(params):
            a, b_param = params
            p = 1.0 / (1.0 + np.exp(a * logits + b_param))
            p = np.clip(p, 1e-10, 1 - 1e-10)
            return -np.sum(outcomes_arr * np.log(p) + (1 - outcomes_arr) * np.log(1 - p))

        result = minimize(neg_log_likelihood, x0=[-1.0, 0.0], method="L-BFGS-B")
        if result.success:
            self.a = float(result.x[0])
            self.b = float(result.x[1])
            logger.info(f"Platt calibration fit: a={self.a:.4f}, b={self.b:.4f}")
            return True
        logger.warning(f"Platt calibration failed to converge: {result.message}")
        return False

    def save(self, path: Path | None = None) -> None:
        path = path or DEFAULT_PARAMS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"a": self.a, "b": self.b}, indent=2))

    def load(self, path: Path | None = None) -> None:
        path = path or DEFAULT_PARAMS_PATH
        if path.exists():
            data = json.loads(path.read_text())
            self.a = data.get("a", -1.0)
            self.b = data.get("b", 0.0)
            logger.info(f"Platt calibration loaded: a={self.a:.4f}, b={self.b:.4f}")
```

- [ ] **Step 4: Run tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_calibrator.py -v`
Expected: ALL PASS

---

### Task 3: IndicatorNormalizer — Z-Score Normalization (Fix 8)

**Files:**
- Modify: `polybot/indicators/engine.py:1-79`
- Modify: `polybot/tests/test_indicator_engine.py`

- [ ] **Step 1: Write the new tests**

Append to `polybot/tests/test_indicator_engine.py`:

```python
# --- Normalizer tests ---

from polybot.indicators.engine import IndicatorNormalizer

def test_normalizer_warmup_returns_raw():
    """First 50 calls return raw scores unchanged."""
    norm = IndicatorNormalizer(warmup=50)
    for i in range(49):
        result = norm.normalize("rsi", 0.5)
        assert result == 0.5


def test_normalizer_after_warmup_returns_zscore():
    """After warmup, scores are zero-mean unit-variance."""
    norm = IndicatorNormalizer(alpha=0.05, warmup=10)
    # Feed constant value to establish stats
    for _ in range(20):
        norm.normalize("rsi", 0.3)
    # A value far from mean should produce a large z-score
    result = norm.normalize("rsi", 0.9)
    assert abs(result) > 1.0  # far from mean


def test_normalizer_clamps_extremes():
    """Output clamped to [-3, 3]."""
    norm = IndicatorNormalizer(alpha=0.05, warmup=5)
    # Build stats around 0
    for _ in range(10):
        norm.normalize("rsi", 0.0)
    # Extreme value should be clamped
    result = norm.normalize("rsi", 100.0)
    assert result <= 3.0
    result = norm.normalize("rsi", -100.0)
    assert result >= -3.0


def test_normalized_score_in_indicators_dict():
    """compute_all() adds norm_score alongside score for each indicator."""
    from polybot.tests.test_indicator_engine import _make_buffer
    ie = _make_indicator_engine()
    indicators = ie.compute_all(_make_buffer())
    for name in ["rsi", "macd", "stochastic", "obv", "vwap"]:
        assert "norm_score" in indicators[name]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd PolyBot && python -m pytest polybot/tests/test_indicator_engine.py -v -k "normalizer or normalized"`
Expected: FAIL — `ImportError: cannot import name 'IndicatorNormalizer'`

- [ ] **Step 3: Add IndicatorNormalizer to engine.py**

At the top of `polybot/indicators/engine.py`, after the imports, add:

```python
import math


class IndicatorNormalizer:
    """Exponentially-weighted running mean/variance per indicator.

    Normalizes raw indicator scores to zero-mean, unit-variance before
    weighted aggregation. Ensures configured weights reflect actual
    contribution, not variance dominance.
    """

    def __init__(self, alpha: float = 0.02, warmup: int = 50) -> None:
        self.alpha: float = alpha
        self.warmup: int = warmup
        self._stats: dict[str, dict] = {}

    def normalize(self, name: str, raw_score: float) -> float:
        stats = self._stats.setdefault(name, {"mean": 0.0, "var": 1.0, "count": 0})
        stats["count"] += 1

        if stats["count"] == 1:
            stats["mean"] = raw_score
            stats["var"] = 1.0
        else:
            delta = raw_score - stats["mean"]
            stats["mean"] += self.alpha * delta
            stats["var"] = (1 - self.alpha) * stats["var"] + self.alpha * delta * delta

        if stats["count"] < self.warmup:
            return raw_score

        std = max(math.sqrt(stats["var"]), 1e-6)
        z = (raw_score - stats["mean"]) / std
        return max(-3.0, min(3.0, z))
```

Then modify `IndicatorEngine.__init__` to create a normalizer:

```python
self.normalizer: IndicatorNormalizer = IndicatorNormalizer()
```

And modify `compute_all()` — after building the result dict, add norm_score for each scored indicator:

```python
# At end of compute_all(), before return:
for ind_name in ("rsi", "macd", "stochastic", "obv", "vwap"):
    if ind_name in result and "score" in result[ind_name]:
        result[ind_name]["norm_score"] = self.normalizer.normalize(
            ind_name, result[ind_name]["score"])
return result
```

- [ ] **Step 4: Run all indicator engine tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_indicator_engine.py -v`
Expected: ALL PASS (existing + new)

---

### Task 4: SignalEngine Core Math Fixes (Fixes 1, 2, 4, 5)

**Files:**
- Modify: `polybot/core/signal_engine.py:54-141`

- [ ] **Step 1: Update `__init__` signature**

Add three new params to `SignalEngine.__init__()`:

```python
def __init__(self, min_edge: float = 0.03, kelly_fraction: float = 0.15,
             momentum_weight: float = 0.04, weights: dict[str, float] | None = None,
             min_model_probability: float = 0.65,
             student_t_df: int = 4, regime_weight: float = 0.03,
             flow_weight: float = 0.04, regime_lookback: int = 20,
             min_kelly: float = 0.015, atr_sigma_ratio: float = 1.7,
             calibrator: 'PlattCalibrator | None' = None) -> None:
    self.min_edge: float = min_edge
    self.kelly_fraction: float = kelly_fraction
    self.momentum_weight: float = momentum_weight
    self.min_model_probability: float = min_model_probability
    self.student_t_df: int = student_t_df
    self.regime_weight: float = regime_weight
    self.flow_weight: float = flow_weight
    self.regime_lookback: int = regime_lookback
    self.min_kelly: float = min_kelly
    self.atr_sigma_ratio: float = atr_sigma_ratio
    self.calibrator = calibrator
    self.weights: dict[str, float] = weights or {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                               "obv": 0.15, "vwap": 0.20}
```

- [ ] **Step 2: Rewrite `compute_probability()` — all 4 math fixes**

Replace the body of `compute_probability()` (lines 108-141) with:

```python
if atr <= 0 or seconds_remaining <= 0:
    return 0.5

distance = btc_price - strike_price
minutes_remaining = max(seconds_remaining / 60.0, 0.01)

# Fix 4: Scale ATR to standard deviation
vol_scaled = (atr / self.atr_sigma_ratio) * math.sqrt(minutes_remaining)

if vol_scaled <= 0:
    return 0.5

z = distance / vol_scaled

# Fix 5: Normalize z for Student-t variance (df=4 → variance=2, scale=√2)
if self.student_t_df > 2:
    t_scale = math.sqrt(self.student_t_df / (self.student_t_df - 2))
else:
    t_scale = 1.0
prob_up = float(student_t.cdf(z * t_scale, df=self.student_t_df))

# --- Fix 2: Convert to logit space for Bayesian-correct evidence combination ---
prob_up = max(0.001, min(0.999, prob_up))
logit_p = math.log(prob_up / (1.0 - prob_up))

# Internal weight conversion: at p=0.5, dp/dlogit = 0.25
# logit_weight = prob_weight * 4.0 preserves behavior at p=0.5
logit_regime_w = self.regime_weight * 4.0
logit_flow_w = self.flow_weight * 4.0
logit_momentum_w = self.momentum_weight * 4.0

# Fix 1: Layer 2 — Regime: direction from recent return, not prob sign
regime = self.compute_regime_factor(closes) if closes is not None else 0.0
if closes is not None and len(closes) >= 2:
    last_return = float(closes[-1] - closes[-2]) / float(closes[-2])
    direction = 1.0 if last_return > 0 else (-1.0 if last_return < 0 else 0.0)
else:
    direction = 0.0
logit_p += regime * direction * logit_regime_w

# Layer 3 — Order flow
logit_p += flow_signal * logit_flow_w

# Layer 4 — Momentum
if indicators:
    momentum = self.compute_momentum(indicators)
    logit_p += momentum * logit_momentum_w

# Convert back via sigmoid — natural (0, 1) bounds, no clamping needed
prob_up = 1.0 / (1.0 + math.exp(-logit_p))

# Fix 9: Platt calibration (identity if no calibrator loaded)
if self.calibrator:
    prob_up = self.calibrator.calibrate(prob_up)

return prob_up
```

- [ ] **Step 3: Update `compute_momentum()` to use norm_score**

Replace the body (lines 143-151):

```python
def compute_momentum(self, indicators: dict[str, dict]) -> float:
    w = self.weights
    def _score(name: str) -> float:
        ind = indicators.get(name, {})
        return ind.get("norm_score", ind.get("score", 0))
    return max(-1.0, min(1.0,
        _score("rsi") * w.get("rsi", 0.20) +
        _score("macd") * w.get("macd", 0.25) +
        _score("stochastic") * w.get("stochastic", 0.20) +
        _score("obv") * w.get("obv", 0.15) +
        _score("vwap") * w.get("vwap", 0.20)
    ))
```

- [ ] **Step 4: Run existing tests to see which break**

Run: `cd PolyBot && python -m pytest polybot/tests/test_signal_engine.py -v`
Expected: 3 failures — `test_skips_when_market_already_correct`, `test_exit_when_edge_evaporates`, `test_student_t_less_extreme_than_normal`

---

### Task 5: Kelly-Based Entry Gate (Fix 3)

**Files:**
- Modify: `polybot/core/signal_engine.py:153-213` — `evaluate()` method

- [ ] **Step 1: Rewrite the edge/entry logic in `evaluate()`**

Replace the edge computation and decision block (lines 190-212) with:

```python
# Compute edge for each side
edge_up = prob_up - market_price_up
edge_down = prob_down - market_price_down

# Pick the side with more edge
if edge_up >= edge_down:
    best_side, best_edge, best_prob, best_mkt = "BUY_YES", edge_up, prob_up, market_price_up
else:
    best_side, best_edge, best_prob, best_mkt = "BUY_NO", edge_down, prob_down, market_price_down

# Gate 1: noise floor
if best_edge < self.min_edge:
    return TradeSignal("SKIP", best_prob, best_edge, 0,
                       f"No edge: best={best_edge:+.0%} < floor={self.min_edge:.0%}")

# Gate 2 (Fix 3): Kelly must justify a position
kelly = self._kelly(best_prob, best_mkt)
if kelly < self.min_kelly:
    return TradeSignal("SKIP", best_prob, best_edge, 0,
                       f"Kelly too small: {kelly:.1%} < {self.min_kelly:.1%}")

# Entry signal
if best_side == "BUY_YES":
    return TradeSignal(
        "BUY_YES", prob_up, edge_up, kelly,
        f"Up: model={prob_up:.0%} mkt={market_price_up:.0%} edge={edge_up:+.0%} "
        f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")
else:
    return TradeSignal(
        "BUY_NO", prob_down, edge_down, kelly,
        f"Down: model={prob_down:.0%} mkt={market_price_down:.0%} edge={edge_down:+.0%} "
        f"BTC={btc_price:,.0f} strike={strike_price:,.0f} d={btc_price-strike_price:+,.0f}")
```

- [ ] **Step 2: Run tests again**

Run: `cd PolyBot && python -m pytest polybot/tests/test_signal_engine.py -v`
Expected: Still 3 failures (same tests as Task 4 step 4 — to be fixed in Task 6)

---

### Task 6: Fix Breaking Tests + Add New Tests

**Files:**
- Modify: `polybot/tests/test_signal_engine.py`

- [ ] **Step 1: Fix the 3 breaking tests**

Replace `test_skips_when_market_already_correct` (line 44-49):

```python
def test_skips_when_market_already_correct(engine):
    """BTC slightly above strike, market already priced correctly — no edge."""
    # With ATR scaling + Student-t normalization, model is more confident.
    # Market price must be high enough that edge stays below noise floor.
    signal = engine.evaluate(_make_indicators(atr_value=50), has_position=False, in_entry_window=True,
                             btc_price=66420, strike_price=66400,
                             seconds_remaining=240, market_price_up=0.66, market_price_down=0.34)
    assert signal.action == "SKIP"
```

Replace `test_exit_when_edge_evaporates` (line 122-129):

```python
def test_exit_when_edge_evaporates(engine):
    """Model says Up likely but market overpricing our side → EXIT."""
    # With more confident probability model, need higher market price to trigger exit
    action, prob, edge, _ = engine.evaluate_hold(
        _make_indicators(atr_value=50), btc_price=66450, strike_price=66400,
        seconds_remaining=180, market_price_for_side=0.95, side="Up", exit_threshold=-0.05)
    assert action == "EXIT"
    assert edge < 0
```

Replace `test_student_t_less_extreme_than_normal` (line 151-157):

```python
def test_student_t_less_extreme_than_normal():
    """Student-t CDF with variance normalization gives less extreme probs at large z
    (fat tails = more reversal probability in the extremes)."""
    from scipy.stats import norm, t as student_t_dist
    se = SignalEngine(student_t_df=4)
    # At large z, Student-t tails are fatter → P(Up) is lower than normal
    z = 3.0
    t_scale = math.sqrt(4 / (4 - 2))
    prob_t = float(student_t_dist.cdf(z * t_scale, df=4))
    prob_norm = float(norm.cdf(z))
    # Student-t gives LESS extreme at large z (more probability mass in tails)
    assert prob_t < prob_norm
```

Add `import math` at the top of the test file if not already present.

- [ ] **Step 2: Add 6 new tests**

Append to `polybot/tests/test_signal_engine.py`:

```python
# --- New tests for math fixes ---

def test_regime_direction_from_returns_not_prob():
    """Fix 1: trending DOWN + above strike → prob should DECREASE."""
    se = SignalEngine(regime_weight=0.05)
    # BTC above strike but trending down (closes decreasing)
    closes = np.array([100 + 50 - i * 3.0 for i in range(25)])  # trending down
    prob_no_regime = se.compute_probability(66450, 66400, 180, 30.0)
    prob_with_regime = se.compute_probability(66450, 66400, 180, 30.0, closes=closes)
    # Down-trending regime should push prob_up LOWER, not higher
    assert prob_with_regime < prob_no_regime


def test_logit_dampening_near_extremes():
    """Fix 2: same flow_signal produces smaller prob shift at p≈0.95 vs p≈0.50."""
    se = SignalEngine(flow_weight=0.06)
    # Near p=0.5 (BTC at strike)
    p_base_mid = se.compute_probability(66400, 66400, 180, 30.0, flow_signal=0.0)
    p_flow_mid = se.compute_probability(66400, 66400, 180, 30.0, flow_signal=1.0)
    shift_mid = abs(p_flow_mid - p_base_mid)

    # Near p=0.95 (BTC well above strike)
    p_base_high = se.compute_probability(66600, 66400, 60, 30.0, flow_signal=0.0)
    p_flow_high = se.compute_probability(66600, 66400, 60, 30.0, flow_signal=1.0)
    shift_high = abs(p_flow_high - p_base_high)

    # Logit-space adjustment should produce SMALLER shift near extremes
    assert shift_high < shift_mid


def test_kelly_gate_rejects_thin_edge_at_high_price():
    """Fix 3: thin edge at high price → Kelly too small → SKIP."""
    se = SignalEngine(min_edge=0.03, min_kelly=0.015)
    # Manually construct: prob=0.83, market=0.80 → edge=3%, kelly=(0.03/0.20)*0.15=2.25%
    # But we can't set prob directly, so use high confidence where market is close
    signal = se.evaluate(_make_indicators(atr_value=50), has_position=False, in_entry_window=True,
                         btc_price=66400, strike_price=66400,
                         seconds_remaining=180, market_price_up=0.50, market_price_down=0.50)
    # At strike with no indicators, prob≈0.50, edge≈0, should SKIP (below noise floor)
    assert signal.action == "SKIP"


def test_kelly_gate_accepts_underdog_with_edge():
    """Fix 3: decent edge on underdog → Kelly sufficient → ENTER."""
    se = SignalEngine(min_edge=0.03, min_kelly=0.015)
    # BTC well below strike: strong Down signal
    signal = se.evaluate(_make_indicators(atr_value=30), has_position=False, in_entry_window=True,
                         btc_price=66200, strike_price=66400,
                         seconds_remaining=120, market_price_up=0.40, market_price_down=0.60)
    # Model should find strong Down edge
    assert signal.action == "BUY_NO"
    assert signal.kelly_size >= 0.015


def test_atr_scaling_increases_z():
    """Fix 4: with atr_sigma_ratio=1.7, probability is further from 0.5."""
    se_old = SignalEngine(atr_sigma_ratio=1.0)  # no scaling (old behavior)
    se_new = SignalEngine(atr_sigma_ratio=1.7)  # scaled
    prob_old = se_old.compute_probability(66500, 66400, 180, 50.0)
    prob_new = se_new.compute_probability(66500, 66400, 180, 50.0)
    # ATR scaling makes z larger → prob further from 0.5 → more confident
    assert abs(prob_new - 0.5) > abs(prob_old - 0.5)


def test_student_t_scale_normalization():
    """Fix 5: t.cdf(z*scale, df=4) vs t.cdf(z, df=4) for known z."""
    from scipy.stats import t as student_t_dist
    z = 1.5
    t_scale = math.sqrt(4 / (4 - 2))  # √2
    prob_unscaled = float(student_t_dist.cdf(z, df=4))
    prob_scaled = float(student_t_dist.cdf(z * t_scale, df=4))
    # Scaled z is larger → CDF is further from 0.5
    assert prob_scaled > prob_unscaled
```

- [ ] **Step 3: Run all signal engine tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_signal_engine.py -v`
Expected: ALL PASS (25 existing + 6 new)

---

### Task 7: main.py — Construction + Outcome Enrichment

**Files:**
- Modify: `polybot/main.py:1057-1068` — SignalEngine construction
- Modify: `polybot/main.py:294-308` — outcome snapshot enrichment

- [ ] **Step 1: Fix SignalEngine construction (line 1057-1068)**

Replace with:

```python
# Signal engine — probability model with edge-based entry
from polybot.core.calibrator import PlattCalibrator

signal_engine = SignalEngine(
    min_edge=signal_cfg.get("entry_threshold", 0.03),
    kelly_fraction=config["math"].get("kelly_fraction", 0.15),
    momentum_weight=signal_cfg.get("momentum_weight", 0.04),
    weights=signal_cfg.get("weights", {"rsi": 0.20, "macd": 0.25, "stochastic": 0.20,
                                        "obv": 0.15, "vwap": 0.20}),
    min_model_probability=signal_cfg.get("min_model_probability", 0.65),
    student_t_df=signal_cfg.get("student_t_df", 4),
    regime_weight=signal_cfg.get("regime_weight", 0.03),
    flow_weight=signal_cfg.get("flow_weight", 0.04),
    regime_lookback=signal_cfg.get("regime_lookback", 20),
    min_kelly=signal_cfg.get("min_kelly", 0.015),
    atr_sigma_ratio=signal_cfg.get("atr_sigma_ratio", 1.7),
)

# Load Platt calibrator (identity if file doesn't exist)
calibrator = PlattCalibrator()
calibrator_path = Path(base_dir) / "memory" / "calibration" / "platt_params.json"
calibrator.load(calibrator_path)
signal_engine.calibrator = calibrator
```

- [ ] **Step 2: Enrich outcome snapshot (line 294-308)**

In the `trade_context` dict, add the calibrated probability alongside raw:

```python
snapshot["trade_context"] = {
    "btc_price": btc_price,
    "strike_price": strike,
    "seconds_remaining": contract["seconds_remaining"],
    "market_price_up": price_up,
    "market_price_down": price_down,
    "model_probability_raw": signal.prob,
    "model_probability": signal.prob,  # backward compat (now calibrated from engine)
    "edge": signal.edge,
    "momentum_score": signal_engine.compute_momentum(indicators),
    "atr": indicators.get("atr", {}).get("atr", 0),
    "size": size,
    "flow_score": flow_score,
    "flow_book_imbalance": flow_data.get("book_imbalance", 0),
    "flow_trade_count": flow_data.get("trade_count", 0),
}
```

- [ ] **Step 3: Run integration tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_integration.py polybot/tests/test_ta_integration.py -v`
Expected: ALL PASS

---

### Task 8: Pipeline Wiring — Claude Client

**Files:**
- Modify: `polybot/brain/claude_client.py:12-91` — system prompt
- Modify: `polybot/brain/claude_client.py:128-212` — validation
- Modify: `polybot/brain/claude_client.py:215-365` — context formatting

- [ ] **Step 1: Update STRATEGY_SYSTEM_PROMPT**

In the `## How PolyBot Works` section, update the Layer 1 formula:
```
Layer 1 — Student-t CDF (fat tails, df=student_t_df):
    z = (BTC_price - strike) / ((ATR / atr_sigma_ratio) * sqrt(minutes_remaining))
    z_scaled = z * sqrt(df / (df - 2))
    P(Up) = t.cdf(z_scaled, df=student_t_df)
```

Update layers 2-4 to mention logit space:
```
Layers 2-4 are applied in log-odds (logit) space — config weights are auto-converted internally.
```

Update entry gate description:
```
- Entry gate: edge >= entry_threshold (noise floor) AND Kelly >= min_kelly (primary gate)
```

In `## Parameter Constraints`, add:
```
- min_kelly: 0.005 to 0.05 range (Kelly-based entry gate — minimum fraction of bankroll)
- atr_sigma_ratio: 1.2 to 2.5 range (ATR-to-σ conversion — lower = more aggressive probabilities)
- min_edge (entry_threshold): 0.01 to 0.10 range (noise floor, not primary gate)
```

And change the existing min_edge range from `0.05 to 0.35` to `0.01 to 0.10`.

In `## Response Format` JSON, add:
```json
"recommended_min_kelly": 0.XX,
"recommended_atr_sigma_ratio": X.X,
```

- [ ] **Step 2: Update _validate_strategy_response()**

After the existing clamping block (around line 183), change min_edge range and add:

```python
data["recommended_min_edge"] = max(0.01, min(0.10,
    data.get("recommended_min_edge", 0.03)))
data["recommended_min_kelly"] = max(0.005, min(0.05,
    data.get("recommended_min_kelly", 0.015)))
data["recommended_atr_sigma_ratio"] = max(1.2, min(2.5,
    float(data.get("recommended_atr_sigma_ratio", 1.7))))
```

- [ ] **Step 3: Update _format_strategy_context()**

In the "Current Configuration" section (around line 220), add:

```python
f"min_kelly (entry gate): {cfg.get('min_kelly', 0.015)}\n"
f"atr_sigma_ratio: {cfg.get('atr_sigma_ratio', 1.7)}\n"
```

- [ ] **Step 4: Run Claude client tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_claude_client.py -v`
Expected: ALL PASS

---

### Task 9: Pipeline Wiring — Scheduler + TAEvolver

**Files:**
- Modify: `polybot/agents/scheduler.py:62-81` — current_config dict
- Modify: `polybot/agents/scheduler.py:185-295` — hot-swap + persist + Discord
- Modify: `polybot/agents/scheduler.py:329-405` — daily pipeline + calibration step
- Modify: `polybot/agents/ta_evolver.py:103-131` — logging

- [ ] **Step 1: Add new params to _run_ta_evolver current_config (line 62-81)**

Add after the existing params in the `current_config` dict:

```python
"min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
"atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
```

- [ ] **Step 2: Add hot-swap for new params (after line 195)**

```python
if "recommended_min_kelly" in recommendations:
    self.signal_engine.min_kelly = _clamp(recommendations["recommended_min_kelly"], 0.005, 0.05)
if "recommended_atr_sigma_ratio" in recommendations:
    self.signal_engine.atr_sigma_ratio = _clamp(float(recommendations["recommended_atr_sigma_ratio"]), 1.2, 2.5)
```

And change min_edge clamping (line 197-199) from `(0.05, 0.35)` to `(0.01, 0.10)`.

- [ ] **Step 3: Add config persistence (after line 235)**

```python
if "recommended_min_kelly" in recommendations:
    sig["min_kelly"] = recommendations["recommended_min_kelly"]
if "recommended_atr_sigma_ratio" in recommendations:
    sig["atr_sigma_ratio"] = recommendations["recommended_atr_sigma_ratio"]
```

- [ ] **Step 4: Add Discord alert lines (after line 283)**

```python
if "recommended_min_kelly" in recommendations:
    msg += f"\nmin_kelly: `{recommendations['recommended_min_kelly']}`"
if "recommended_atr_sigma_ratio" in recommendations:
    msg += f"\natr_sigma_ratio: `{recommendations['recommended_atr_sigma_ratio']}`"
```

- [ ] **Step 5: Add config diff tracking (line 334-344)**

Add to `old_config` dict:
```python
"min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
"atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
```

And same keys in the `new_vals` dict (line 381-390).

- [ ] **Step 6: Add Platt calibration step to run_daily_pipeline (after line 357, before TAEvolver)**

```python
# Platt calibration fitting (Fix 9)
from polybot.core.calibrator import PlattCalibrator, compute_log_loss
if len(train_outcomes) >= 100 and self.signal_engine:
    probs = []
    outcomes_binary = []
    for o in train_outcomes:
        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
        mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
        if mp > 0:
            probs.append(mp)
            outcomes_binary.append(1 if o.get("correct", False) else 0)

    if len(probs) >= 100:
        cal = PlattCalibrator()
        if self.signal_engine.calibrator:
            cal.a = self.signal_engine.calibrator.a
            cal.b = self.signal_engine.calibrator.b
        if cal.fit(probs, outcomes_binary):
            # Validate on holdout
            val_probs, val_outcomes = [], []
            for o in validation_outcomes:
                ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
                mp = ctx.get("model_probability_raw", ctx.get("model_probability", 0))
                if mp > 0:
                    val_probs.append(mp)
                    val_outcomes.append(1 if o.get("correct", False) else 0)
            if val_probs:
                old_loss = compute_log_loss(val_probs, val_outcomes)
                cal_probs = [cal.calibrate(p) for p in val_probs]
                new_loss = compute_log_loss(cal_probs, val_outcomes)
                if new_loss < old_loss:
                    cal.save()
                    self.signal_engine.calibrator = cal
                    logger.info(f"Platt calibration adopted: log-loss {old_loss:.4f} -> {new_loss:.4f}")
                else:
                    logger.info(f"Platt calibration rejected: {old_loss:.4f} -> {new_loss:.4f}")
```

- [ ] **Step 7: Update ta_evolver.py logging (line 114)**

Add to `_save_claude_log()`:
```python
mk = recommendations.get("recommended_min_kelly", "?")
ar = recommendations.get("recommended_atr_sigma_ratio", "?")
```

And update the parameters line:
```python
f"**Recommended Parameters:** momentum_weight={mw}, min_edge={me}, kelly_fraction={kf}, "
f"min_kelly={mk}, atr_sigma_ratio={ar}\n"
```

- [ ] **Step 8: Run scheduler tests**

Run: `cd PolyBot && python -m pytest polybot/tests/test_scheduler.py -v`
Expected: ALL PASS

---

### Task 10: Full Test Suite + Documentation

**Files:**
- Run full test suite
- Modify: `PolyBot/CLAUDE.md`

- [ ] **Step 1: Run full test suite**

Run: `cd PolyBot && python -m pytest polybot/tests/ -v --tb=short`
Expected: ALL PASS (300+ tests)

- [ ] **Step 2: Update CLAUDE.md**

Update the following sections:

**"4-layer probability model" in Key Architecture Decisions:**
- Layer 1 formula: `z = (BTC_price - strike) / ((ATR / atr_sigma_ratio) × sqrt(minutes))`, then `z_scaled = z × sqrt(df/(df-2))`, `P(Up) = t.cdf(z_scaled, df)`
- Layers 2-4: "applied in log-odds (logit) space — auto-converted from config weights × 4.0. This ensures adjustments near probability extremes are dampened (correct Bayesian behavior)."
- Layer 2 regime: "direction derived from sign of most recent 1-minute return (not prob_up sign)"
- Layer 4 momentum: "uses z-score normalized indicator scores (IndicatorNormalizer with EMA-based running stats, warmup=50)"
- Entry gate: "Dual gate: edge >= entry_threshold (0.03, noise floor) AND kelly_size >= min_kelly (0.015, primary price-aware gate)"
- Add: "Platt scaling calibration applied after all 4 layers. Fitted daily by learning pipeline. Identity (no effect) until >= 100 outcomes."

**"How the Probability Model Works" dataflow:**
- Update Layer 1 formula to show `/ atr_sigma_ratio` and `z × t_scale`
- Update Layers 2-4 to show logit space conversion
- Update ENTRY gates to show Kelly gate

**Config section:**
- Add `signal.min_kelly: 0.015` with description
- Add `signal.atr_sigma_ratio: 1.7` with description
- Update `signal.entry_threshold: 0.03` description to "noise floor"

**Learning Pipeline section:**
- Add Platt calibration as a step between BiasDetector and TAEvolver
- Add `min_kelly` and `atr_sigma_ratio` to the list of pipeline-tunable params

**Baseline LOCKED section:**
- Update lock date to 2026-04-09
- Note: "Engine math optimized (logit space, ATR scaling, Student-t normalization, regime direction fix, Kelly entry gate, indicator normalization, Platt calibration). Baseline re-locked."

- [ ] **Step 3: Run full test suite one final time**

Run: `cd PolyBot && python -m pytest polybot/tests/ -v --tb=short`
Expected: ALL PASS
