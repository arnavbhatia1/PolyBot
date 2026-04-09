# Signal Engine Math Fixes — Design Spec

**Date:** 2026-04-09
**Scope:** 7 mathematical/structural fixes to `signal_engine.py`, `indicators/engine.py`, and a new `core/calibrator.py`
**Constraint:** Engine must continue producing net-positive trades. All fixes are internal to the probability pipeline — external API (`evaluate()`, `evaluate_hold()`, `TradeSignal`) is unchanged. Learning pipeline continues tuning the same config parameters.

## Scope Boundary — What This DOES NOT Touch

This spec optimizes the **math/algorithm engine only** — how probabilities are computed, how edge is assessed, and how the daily pipeline tunes parameters. The following are explicitly **out of scope and unchanged**:

- **Trade placement / execution:** `base.py`, `paper_trader.py`, `live_trader.py` — fee math, slippage model, FOK orders, fill simulation, CLOB interaction
- **Position management:** `main.py` trading loop, entry/exit flow, resolution handling, orphan detection. **Note:** `evaluate_hold()` is structurally unchanged but benefits from all math fixes because it calls the same `compute_probability()` — held positions get better-calibrated exit signals on every tick
- **Market discovery:** `market_scanner.py`, `clob_ws.py`, `binance_feed.py` — API calls, WebSocket, candle buffer
- **Order flow computation:** `order_flow.py` — book imbalance, trade flow (unchanged, still feeds into Layer 3)
- **Individual indicators:** `rsi.py`, `macd.py`, `stochastic.py`, `ema.py`, `obv.py`, `vwap.py`, `atr.py` — raw score computation is unchanged; only the aggregation/normalization layer in `engine.py` changes
- **Database schema:** `db/models.py` — no schema changes
- **Discord bot:** `discord_bot/` — no changes (receives same alert data)
- **Circuit breaker:** `circuit_breaker.py` — drawdown-based Kelly scaling untouched
- **Outcome recording:** `outcome_reviewer.py`, `counterfactual_tracker.py` — recording logic unchanged
- **Bias detector:** `bias_detector.py` — analysis logic unchanged (it reads outcomes, not the engine)

**This is a terminal optimization of the engine math. After this, the baseline is re-locked and only the daily learning pipeline tunes parameters.**

## Files Touched

| File | Change Type | Fixes |
|------|-------------|-------|
| `core/signal_engine.py` | Modify | 1, 2, 3, 4, 5 |
| `indicators/engine.py` | Modify | 8 |
| `core/calibrator.py` | **New** | 9 |
| `config/settings.yaml` | Modify | 3, 4 (new params) |
| `brain/claude_client.py` | Modify | Pipeline: system prompt, validation, context formatting |
| `agents/ta_evolver.py` | Modify | Pipeline: log new param recommendations |
| `agents/scheduler.py` | Modify | 9 (calibration step) + pipeline wiring for min_kelly, atr_sigma_ratio |
| `config/loader.py` | Modify | Validation bounds: entry_threshold range, new min_kelly/atr_sigma_ratio checks |
| `main.py` | Modify | SignalEngine construction: add min_kelly, atr_sigma_ratio, calibrator init; fix defaults |
| `tests/test_signal_engine.py` | Modify | 3 tests break (Hole 2), 6 new tests added |
| `tests/test_indicator_engine.py` | Modify | 8 (normalization tests) |
| `tests/test_calibrator.py` | **New** | 9 |
| `tests/test_integration.py` | Modify | Updated expected values |
| `tests/test_config.py` | Modify | New bounds for min_kelly, atr_sigma_ratio, updated entry_threshold range |
| `tests/conftest.py` | Modify | Add min_kelly, atr_sigma_ratio to SAMPLE_CONFIG |
| `CLAUDE.md` | Modify | Document all changes |
| `README.md` | Modify | If probability model section exists |

## Fix 1: Regime Direction Bug

**File:** `core/signal_engine.py` — `compute_probability()` (line ~128)

**Problem:** Direction of regime adjustment is derived from `sign(prob_up - 0.5)` — the model's current lean toward Up or Down. This is wrong. The autocorrelation tells you returns are persistent/reverting, but the DIRECTION of persistence comes from recent returns, not from the price-vs-strike relationship.

**Failure case:** BTC at $85,100, strike $85,000 (prob_up slightly > 0.5). Last 10 candles trending DOWN (BTC fell from $85,500). Positive autocorrelation (returns consistently negative). Code pushes prob_up HIGHER (wrong). Correct: push prob_up LOWER (BTC trending down).

**Change:**

```python
# Before (buggy):
direction = 1.0 if prob_up > 0.5 else -1.0

# After:
if closes is not None and len(closes) >= 2:
    last_return = (closes[-1] - closes[-2]) / closes[-2]
    direction = 1.0 if last_return > 0 else (-1.0 if last_return < 0 else 0.0)
else:
    direction = 0.0
```

**Semantics after fix:**
- Trending (autocorr > 0) + recent up → push prob_up higher
- Trending (autocorr > 0) + recent down → push prob_up lower
- Mean-reverting (autocorr < 0) + recent up → push prob_up lower (expect reversal)
- Mean-reverting (autocorr < 0) + recent down → push prob_up higher (expect reversal)

## Fix 2: Log-Odds (Logit) Space Adjustments

**File:** `core/signal_engine.py` — `compute_probability()` (lines ~125-141)

**Problem:** Layers 2-4 are added to probability directly (`prob_up += adjustment`). This treats a +4% shift identically whether prob_up is 0.50 or 0.93. In Bayesian terms, the same evidence should produce smaller probability shifts near extremes (where you're already very confident).

**Change:** Convert base probability to logit, apply all adjustments there, convert back via sigmoid.

```python
# Clamp base prob to avoid log(0)
prob_up = max(0.001, min(0.999, prob_up))
logit_p = math.log(prob_up / (1.0 - prob_up))

# Internal conversion: at p=0.5, dp/dlogit = 0.25
# So logit_weight = prob_weight * 4.0 preserves behavior at p=0.5
logit_regime_w = self.regime_weight * 4.0
logit_flow_w = self.flow_weight * 4.0
logit_momentum_w = self.momentum_weight * 4.0

# Layer 2: Regime
logit_p += regime * direction * logit_regime_w

# Layer 3: Order flow
logit_p += flow_signal * logit_flow_w

# Layer 4: Momentum
if indicators:
    momentum = self.compute_momentum(indicators)
    logit_p += momentum * logit_momentum_w

# Convert back — sigmoid naturally bounds to (0, 1)
prob_up = 1.0 / (1.0 + math.exp(-logit_p))
```

**Config compatibility:** The pipeline continues to tune `regime_weight`, `flow_weight`, `momentum_weight` in the same units. The 4x conversion happens internally. A 0.01 change in config still has approximately the same effect at p=0.5, but correctly reduced effect near extremes.

**No manual clamping needed.** The sigmoid naturally returns values in (0, 1). The old `max(0.03, min(0.97, ...))` clamp is removed.

## Fix 3: Kelly-Based Entry Gate

**File:** `core/signal_engine.py` — `evaluate()` (lines ~195-212)

**Problem:** Flat `edge >= 0.10` threshold ignores price level. A 10% edge at p=0.30 is a 33% mispricing; at p=0.80 it's 12.5%. The Kelly formula already accounts for this, but the entry gate doesn't use it.

**Change:** Dual gate — lowered edge floor (noise rejection) + Kelly minimum (primary gate).

```python
# Noise floor (lowered from 0.10)
if best_edge < self.min_edge:
    return TradeSignal("SKIP", ..., f"No edge: {best_edge:+.0%} < floor {self.min_edge:.0%}")

# Primary gate: Kelly must justify a position
kelly = self._kelly(best_prob, best_market_price)
if kelly < self.min_kelly:
    return TradeSignal("SKIP", ..., f"Kelly too small: {kelly:.1%} < {self.min_kelly:.1%}")
```

**New config parameter:**
```yaml
signal:
  entry_threshold: 0.03    # Lowered from 0.10 — noise floor only
  min_kelly: 0.015          # NEW — 1.5% of bankroll minimum (pipeline tunable, range: 0.005-0.05)
```

**New `__init__` parameter:** `min_kelly: float = 0.015`

**Trade selection effect:**

| Scenario | Old (flat 10%) | New (Kelly >= 1.5%) |
|----------|----------------|---------------------|
| p=0.40, c=0.30, edge=8% | SKIP | ENTER (kelly=1.7%) |
| p=0.70, c=0.62, edge=8% | SKIP | ENTER (kelly=3.2%) |
| p=0.55, c=0.52, edge=3% | SKIP | SKIP (kelly=0.9%) |
| p=0.90, c=0.80, edge=10% | ENTER | ENTER (kelly=7.5%) |

Opens up underdog trades where the math supports it. Rejects thin-edge trades at all prices.

**evaluate_hold() unchanged.** The exit threshold uses holding_edge, not Kelly. Exit logic stays as-is.

## Fix 4: ATR-to-Sigma Scaling Factor

**File:** `core/signal_engine.py` — `compute_probability()` (line ~115)

**Problem:** ATR (Average True Range) measures expected candle range, not standard deviation. For BTC 1-min candles, ATR ≈ 1.7σ. Using ATR directly inflates the z-score denominator by ~1.7x, compressing probabilities toward 0.5.

**Change:**

```python
# Before:
vol_scaled = atr * math.sqrt(minutes_remaining)

# After:
vol_scaled = (atr / self.atr_sigma_ratio) * math.sqrt(minutes_remaining)
```

**New config parameter:**
```yaml
signal:
  atr_sigma_ratio: 1.7    # NEW — empirical ATR/σ for BTC 1-min candles (pipeline tunable, range: 1.2-2.5)
```

**New `__init__` parameter:** `atr_sigma_ratio: float = 1.7`

**Calibration note:** 1.7 is the literature-standard ratio for intraday crypto bars with hundreds of ticks per candle. The pipeline can tune this. For empirical validation, compare `atr / close_to_close_std` over 200+ candles. The exact ratio varies with market regime (1.5 in quiet markets, 2.0 in volatile), but 1.7 is a robust central estimate.

## Fix 5: Student-t Variance Normalization

**File:** `core/signal_engine.py` — `compute_probability()` (line ~123)

**Problem:** Student-t with df=4 has variance = df/(df-2) = 2, std = √2 ≈ 1.414. The z-score is computed assuming unit variance, but the t-distribution has variance 2. This double-dips on conservatism: the z is already "standardized" but the t-CDF treats it as if it came from a wider distribution.

**Change:**

```python
# Before:
prob_up = float(student_t.cdf(z, df=self.student_t_df))

# After:
# Normalize z to match the Student-t scale parameter
t_scale = math.sqrt(self.student_t_df / (self.student_t_df - 2))
prob_up = float(student_t.cdf(z * t_scale, df=self.student_t_df))
```

For df=4: `t_scale = √2 ≈ 1.414`. The z-score is amplified before feeding into the CDF, compensating for the CDF's wider spread.

**Combined effect with Fix 4:** Old model compressed z by ~2.4x total (1.7 from ATR, 1.41 from t-variance). Both fixes together remove this compression. Probabilities move further from 0.5 → more confident estimates → larger edges on favorites, fewer phantom underdog edges.

## Fix 8: Indicator Z-Score Normalization

**File:** `indicators/engine.py`

**Problem:** Indicator scores are all [-1, 1] but have very different empirical distributions. RSI clusters near 0, MACD spikes, OBV uses an S-curve. Fixed weights don't reflect actual variance contribution — an indicator with 3x the variance of another effectively has 3x the weight.

**Change:** Add `IndicatorNormalizer` class with EMA-based running statistics.

```python
class IndicatorNormalizer:
    """Exponentially-weighted running mean/variance per indicator.

    Normalizes raw indicator scores to zero-mean, unit-variance before
    weighted aggregation. This ensures configured weights reflect actual
    contribution, not variance dominance.
    """

    def __init__(self, alpha: float = 0.02, warmup: int = 50):
        self.alpha = alpha          # EMA decay (~50-sample half-life)
        self.warmup = warmup        # Return raw scores until stats stabilize
        self._stats: dict[str, dict] = {}
        # Each entry: {"mean": float, "var": float, "count": int}

    def normalize(self, name: str, raw_score: float) -> float:
        """Update running stats and return normalized score.

        During warmup (first `warmup` samples): returns raw score unchanged.
        After warmup: returns (score - mean) / std, clamped to [-3, 3].
        """
        stats = self._stats.setdefault(name, {"mean": 0.0, "var": 1.0, "count": 0})
        stats["count"] += 1

        # Update EMA mean and variance
        if stats["count"] == 1:
            stats["mean"] = raw_score
            stats["var"] = 1.0
        else:
            delta = raw_score - stats["mean"]
            stats["mean"] += self.alpha * delta
            stats["var"] = (1 - self.alpha) * stats["var"] + self.alpha * delta * delta

        # During warmup: return raw
        if stats["count"] < self.warmup:
            return raw_score

        # Normalize
        std = max(math.sqrt(stats["var"]), 1e-6)
        z = (raw_score - stats["mean"]) / std
        return max(-3.0, min(3.0, z))
```

**Integration with IndicatorEngine:**

```python
class IndicatorEngine:
    def __init__(self, ...):
        ...
        self.normalizer = IndicatorNormalizer()

    def compute_score(self, indicators: dict) -> float:
        w = self._weights
        # Normalize each indicator score before weighting
        rsi_z = self.normalizer.normalize("rsi", indicators["rsi"]["score"])
        macd_z = self.normalizer.normalize("macd", indicators["macd"]["score"])
        stoch_z = self.normalizer.normalize("stochastic", indicators["stochastic"]["score"])
        obv_z = self.normalizer.normalize("obv", indicators["obv"]["score"])
        vwap_z = self.normalizer.normalize("vwap", indicators["vwap"]["score"])

        score = (rsi_z * w.get("rsi", 0.20) +
                 macd_z * w.get("macd", 0.25) +
                 stoch_z * w.get("stochastic", 0.20) +
                 obv_z * w.get("obv", 0.15) +
                 vwap_z * w.get("vwap", 0.20))
        return max(-1.0, min(1.0, score))
```

**SignalEngine.compute_momentum()** must also use normalized scores. Two options:
- (A) `IndicatorEngine.compute_score()` returns the normalized composite — `SignalEngine` just calls it
- (B) `SignalEngine.compute_momentum()` accesses the normalizer directly

**Decision: Option A.** `SignalEngine.compute_momentum()` stays as-is for backward compat, but `IndicatorEngine` passes normalized scores through to the indicators dict. The normalized scores are stored alongside raw scores:

```python
# In IndicatorEngine.compute_all():
result["rsi"]["norm_score"] = self.normalizer.normalize("rsi", result["rsi"]["score"])
# ... same for macd, stochastic, obv, vwap
```

Then `SignalEngine.compute_momentum()` prefers `norm_score` if present, falls back to `score`:

```python
def compute_momentum(self, indicators: dict) -> float:
    w = self.weights
    def _score(name):
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

**Warmup behavior:** First 50 evaluations use raw scores (identical to current behavior). After warmup, normalized scores kick in. This means the bot can trade immediately on startup without waiting for normalization to converge.

## Fix 9: Platt Scaling Calibration

**New file:** `core/calibrator.py`

**Problem:** The model produces probabilities, but there's no evidence they're calibrated. When the model says "70% Up," does 70% of such trades actually resolve Up? Without calibration, "10% edge" may be phantom.

**Implementation:**

```python
"""Platt scaling probability calibration.

Fits a 2-parameter sigmoid to map raw model probabilities to calibrated ones:
    calibrated = 1 / (1 + exp(A * logit(raw) + B))

Identity (no calibration): A = -1.0, B = 0.0
Minimum 100 outcomes required to fit.
"""

import json
import math
import logging
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

DEFAULT_PARAMS_PATH = Path("polybot/memory/calibration/platt_params.json")


class PlattCalibrator:

    def __init__(self, a: float = -1.0, b: float = 0.0):
        self.a = a  # Identity default
        self.b = b

    @property
    def is_identity(self) -> bool:
        return self.a == -1.0 and self.b == 0.0

    def calibrate(self, raw_prob: float) -> float:
        """Apply Platt scaling to a raw model probability.

        With default parameters (a=-1, b=0), returns raw_prob unchanged.
        """
        raw_prob = max(1e-6, min(1 - 1e-6, raw_prob))
        logit = math.log(raw_prob / (1.0 - raw_prob))
        return 1.0 / (1.0 + math.exp(self.a * logit + self.b))

    def fit(self, probs: list[float], outcomes: list[int],
            min_samples: int = 100) -> bool:
        """Fit calibration parameters from historical data.

        Args:
            probs: Raw model probabilities at time of prediction
            outcomes: 1 if the model's predicted side won, 0 if it lost
            min_samples: Minimum outcomes required to fit

        Returns:
            True if fit succeeded and parameters were updated
        """
        if len(probs) < min_samples:
            logger.info(f"Platt calibration: {len(probs)} samples < {min_samples} minimum, skipping")
            return False

        probs_arr = np.array(probs)
        outcomes_arr = np.array(outcomes, dtype=float)

        # Clamp to avoid log(0)
        probs_arr = np.clip(probs_arr, 1e-6, 1 - 1e-6)
        logits = np.log(probs_arr / (1 - probs_arr))

        def neg_log_likelihood(params):
            a, b = params
            p = 1.0 / (1.0 + np.exp(a * logits + b))
            p = np.clip(p, 1e-10, 1 - 1e-10)
            return -np.sum(outcomes_arr * np.log(p) + (1 - outcomes_arr) * np.log(1 - p))

        result = minimize(neg_log_likelihood, x0=[-1.0, 0.0], method="L-BFGS-B")

        if result.success:
            self.a = float(result.x[0])
            self.b = float(result.x[1])
            logger.info(f"Platt calibration fit: a={self.a:.4f}, b={self.b:.4f}")
            return True
        else:
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

**Integration with SignalEngine:**

`SignalEngine.__init__()` accepts an optional `PlattCalibrator` instance. Applied as the last step in `compute_probability()`:

```python
# After all 4 layers and sigmoid conversion
if self.calibrator:
    prob_up = self.calibrator.calibrate(prob_up)
```

**Integration with daily pipeline (`agents/scheduler.py`):**

After BiasDetector runs, before TAEvolver:

```python
# Load outcomes
outcomes = load_outcomes()
training, validation = split_60_40(outcomes)

# Fit calibrator on training set
calibrator = PlattCalibrator()
probs = [o["trade_context"]["model_probability"] for o in training]
results = [1 if o["profitable"] else 0 for o in training]
if calibrator.fit(probs, results):
    # Validate on holdout — only adopt if log-loss improves
    val_probs = [o["trade_context"]["model_probability"] for o in validation]
    val_results = [1 if o["profitable"] else 0 for o in validation]

    old_loss = compute_log_loss(val_probs, val_results)  # raw probs
    cal_probs = [calibrator.calibrate(p) for p in val_probs]
    new_loss = compute_log_loss(cal_probs, val_results)

    if new_loss < old_loss:
        calibrator.save()
        signal_engine.calibrator = calibrator
```

**Note on outcome data:** The outcome JSON must include `model_probability` (the raw probability the model computed at entry time). This is already stored in `indicator_snapshot` → `trade_context` → `model_probability`. Verified in CLAUDE.md: "Outcome data enriched with `trade_context` in indicator_snapshot."

## Pipeline Integration — New Params Fed Into Daily Learning

The daily pipeline must know about `min_kelly`, `atr_sigma_ratio`, and Platt calibration. This requires changes across 3 pipeline files.

### brain/claude_client.py — System Prompt + Validation + Context

**1. STRATEGY_SYSTEM_PROMPT** — Add new params to constraint docs and response format:

```
## Parameter Constraints (add these lines)
- min_kelly: 0.005 to 0.05 range (Kelly-based entry gate — minimum fraction of bankroll)
- atr_sigma_ratio: 1.2 to 2.5 range (ATR-to-σ conversion — lower = more aggressive probabilities)

## Response Format (add these keys to the JSON template)
  "recommended_min_kelly": 0.XX,
  "recommended_atr_sigma_ratio": X.X,
```

Also update the model description in the prompt to reflect the math fixes:
- Layer 1 formula: `z = (BTC_price - strike) / ((ATR / atr_sigma_ratio) * sqrt(minutes))`, then `z_scaled = z * sqrt(df/(df-2))`, `P(Up) = t.cdf(z_scaled, df)`
- Layers 2-4: "applied in log-odds (logit) space, automatically converted from config weights"
- Entry gate: "Kelly-based entry gate (min_kelly) replaces flat edge threshold as primary gate"
- Add: "Platt calibration applied after all 4 layers — calibration params fitted daily"

**2. _validate_strategy_response()** — Add clamping:

```python
data["recommended_min_kelly"] = max(0.005, min(0.05,
    data.get("recommended_min_kelly", 0.015)))
data["recommended_atr_sigma_ratio"] = max(1.2, min(2.5,
    float(data.get("recommended_atr_sigma_ratio", 1.7))))
```

Also update `min_edge` clamping range from `(0.05, 0.35)` to `(0.01, 0.10)` since it's now a noise floor, not the primary gate.

**3. _format_strategy_context()** — Include in current config section:

```python
f"min_kelly (entry gate): {cfg.get('min_kelly', 0.015)}\n"
f"atr_sigma_ratio: {cfg.get('atr_sigma_ratio', 1.7)}\n"
```

### agents/scheduler.py — Config Dict + Hot-Swap + Persist + Diff

**1. _run_ta_evolver()** (line ~62) — Add to `current_config` dict:

```python
current_config = {
    ...existing params...
    "min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
    "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
}
```

**2. _run_weight_optimizer()** — Hot-swap block (line ~185):

```python
if "recommended_min_kelly" in recommendations:
    self.signal_engine.min_kelly = _clamp(recommendations["recommended_min_kelly"], 0.005, 0.05)
if "recommended_atr_sigma_ratio" in recommendations:
    self.signal_engine.atr_sigma_ratio = _clamp(float(recommendations["recommended_atr_sigma_ratio"]), 1.2, 2.5)
```

Update `min_edge` clamping range to match new noise-floor role:
```python
# Before:
val = _clamp(recommendations["recommended_min_edge"], 0.05, 0.35)
# After:
val = _clamp(recommendations["recommended_min_edge"], 0.01, 0.10)
```

**3. _run_weight_optimizer()** — Persist to settings.yaml block (line ~218):

```python
if "recommended_min_kelly" in recommendations:
    sig["min_kelly"] = recommendations["recommended_min_kelly"]
if "recommended_atr_sigma_ratio" in recommendations:
    sig["atr_sigma_ratio"] = recommendations["recommended_atr_sigma_ratio"]
```

**4. _run_weight_optimizer()** — Discord alert message:

```python
if "recommended_min_kelly" in recommendations:
    msg += f"\nmin_kelly: `{recommendations['recommended_min_kelly']}`"
if "recommended_atr_sigma_ratio" in recommendations:
    msg += f"\natr_sigma_ratio: `{recommendations['recommended_atr_sigma_ratio']}`"
```

**5. run_daily_pipeline()** — Config diff snapshot (line ~334):

```python
old_config = {
    ...existing params...
    "min_kelly": getattr(self.signal_engine, 'min_kelly', 0.015),
    "atr_sigma_ratio": getattr(self.signal_engine, 'atr_sigma_ratio', 1.7),
}
# ... and same keys in new_vals dict
```

**6. run_daily_pipeline()** — Platt calibration step (after BiasDetector, before TAEvolver):

```python
# Platt calibration fitting
from polybot.core.calibrator import PlattCalibrator
calibrator = PlattCalibrator()
calibrator.load()  # Load existing params if any

if len(train_outcomes) >= 100:
    probs = []
    outcomes_binary = []
    for o in train_outcomes:
        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
        mp = ctx.get("model_probability", 0)
        if mp > 0:
            probs.append(mp)
            outcomes_binary.append(1 if o.get("correct", False) else 0)

    if calibrator.fit(probs, outcomes_binary):
        # Validate on holdout
        val_probs, val_outcomes = [], []
        for o in validation_outcomes:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
            mp = ctx.get("model_probability", 0)
            if mp > 0:
                val_probs.append(mp)
                val_outcomes.append(1 if o.get("correct", False) else 0)

        if val_probs:
            from polybot.core.calibrator import compute_log_loss
            old_loss = compute_log_loss(val_probs, val_outcomes)
            cal_probs = [calibrator.calibrate(p) for p in val_probs]
            new_loss = compute_log_loss(cal_probs, val_outcomes)

            if new_loss < old_loss:
                calibrator.save()
                if self.signal_engine:
                    self.signal_engine.calibrator = calibrator
                logger.info(f"Platt calibration adopted: log-loss {old_loss:.4f} -> {new_loss:.4f}")
            else:
                logger.info(f"Platt calibration rejected: no improvement ({old_loss:.4f} -> {new_loss:.4f})")
```

### agents/ta_evolver.py — Logging

**_save_claude_log()** — Add to logged params:

```python
mk = recommendations.get("recommended_min_kelly", "?")
ar = recommendations.get("recommended_atr_sigma_ratio", "?")

# In the entry string:
f"**Recommended Parameters:** momentum_weight={mw}, min_edge={me}, kelly_fraction={kf}, "
f"min_kelly={mk}, atr_sigma_ratio={ar}\n"
```

### calibrator.py — Add compute_log_loss Helper

The `compute_log_loss` function used by the scheduler:

```python
def compute_log_loss(probs: list[float], outcomes: list[int]) -> float:
    """Binary cross-entropy loss."""
    total = 0.0
    for p, y in zip(probs, outcomes):
        p = max(1e-10, min(1 - 1e-10, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(probs) if probs else float("inf")
```

### Pipeline Parameter Summary

Complete list of pipeline-tunable params after all changes:

| Parameter | Config Key | Claude Recommendation Key | Clamp Range | Hot-Swap Target |
|-----------|-----------|--------------------------|-------------|-----------------|
| Indicator weights | `signal.weights` | `recommended_weights` | each ≥ 0.05, sum = 1.0 | `indicator_engine` + `signal_engine.weights` |
| Momentum weight | `signal.momentum_weight` | `recommended_momentum_weight` | 0.02-0.10 | `signal_engine.momentum_weight` |
| Regime weight | `signal.regime_weight` | `recommended_regime_weight` | 0.02-0.10 | `signal_engine.regime_weight` |
| Flow weight | `signal.flow_weight` | `recommended_flow_weight` | 0.02-0.12 | `signal_engine.flow_weight` |
| Student-t df | `signal.student_t_df` | `recommended_student_t_df` | 3-8 | `signal_engine.student_t_df` |
| Entry threshold | `signal.entry_threshold` | `recommended_min_edge` | **0.01-0.10** (was 0.05-0.35) | `signal_engine.min_edge` |
| **Min Kelly** | `signal.min_kelly` | `recommended_min_kelly` | **0.005-0.05** | `signal_engine.min_kelly` |
| **ATR sigma ratio** | `signal.atr_sigma_ratio` | `recommended_atr_sigma_ratio` | **1.2-2.5** | `signal_engine.atr_sigma_ratio` |
| Kelly fraction | `math.kelly_fraction` | `recommended_kelly_fraction` | 0.05-0.25 | `signal_engine.kelly_fraction` |
| Min model prob | `signal.min_model_probability` | `recommended_min_model_probability` | 0.55-0.85 | `signal_engine.min_model_probability` |
| Exit threshold | `signal.exit_edge_threshold` | `recommended_exit_edge_threshold` | -0.25-0.0 | `scheduler._exit_edge_threshold` |
| Min time remaining | `market.min_time_remaining_seconds` | `recommended_min_time_remaining` | 0-120 | `scheduler._min_time_remaining` |
| Trading start | `schedule.trading_start_hour_et` | `recommended_trading_start_hour_et` | 0-23 | `scheduler._trading_start` |
| Trading end | `schedule.trading_end_hour_et` | `recommended_trading_end_hour_et` | 0-23 | `scheduler._trading_end` |
| **Platt params** | `memory/calibration/platt_params.json` | N/A (fitted directly) | N/A | `signal_engine.calibrator` |

**Bold** = new in this spec.

## Config Changes Summary

```yaml
signal:
  entry_threshold: 0.03       # Changed from 0.10 — noise floor only (pipeline range: 0.01-0.10)
  min_kelly: 0.015             # NEW — primary entry gate (pipeline range: 0.005-0.05)
  atr_sigma_ratio: 1.7         # NEW — ATR/σ conversion (pipeline range: 1.2-2.5)
  # Unchanged: momentum_weight, regime_weight, flow_weight, student_t_df,
  #            min_model_probability, exit_edge_threshold, regime_lookback, weights
```

## Data Flow (Complete After All Fixes)

```
BTC price, strike, ATR, seconds_remaining
        │
        ▼
LAYER 1 — Fat-tailed base (Student-t CDF):
  vol_scaled = (ATR / 1.7) × √minutes          [Fix 4: proper σ scaling]
  z = distance / vol_scaled
  z_scaled = z × √(df / (df-2))                 [Fix 5: t-variance normalization]
  base_prob = t.cdf(z_scaled, df=4)
        │
        ▼ Convert to logit space                 [Fix 2]
  logit_p = log(base_prob / (1 - base_prob))
        │
LAYER 2 — Regime:
  autocorr = 1-lag autocorrelation (last N returns)
  direction = sign(last_return)                  [Fix 1: correct direction source]
  logit_p += autocorr × direction × (regime_weight × 4.0)
        │
LAYER 3 — Order flow:
  logit_p += flow_signal × (flow_weight × 4.0)
        │
LAYER 4 — Momentum:
  normalized_scores = z-score per indicator      [Fix 8: variance normalization]
  momentum = weighted_sum(normalized_scores)
  logit_p += momentum × (momentum_weight × 4.0)
        │
        ▼ Convert back via sigmoid               [Fix 2: natural bounds]
  prob_up = 1 / (1 + exp(-logit_p))
        │
        ▼ Platt calibration                      [Fix 9]
  prob_calibrated = platt(prob_up)
        │
        ▼
  prob_down = 1 - prob_calibrated
  edge_up = prob_calibrated - market_price_up
  edge_down = prob_down - market_price_down
  kelly = (prob × b - q) / b × kelly_fraction
        │
        ▼
  ENTRY GATES:
    ├── prob >= 0.65?            (confidence gate — unchanged)
    ├── edge >= 0.03?            (noise floor — lowered)        [Fix 3]
    ├── kelly >= 0.015?          (primary gate — NEW)           [Fix 3]
    ├── spread, depth, price sanity, timing gates (unchanged)
```

## Test Plan

### Updated Tests (signal_engine — 24 existing)

All existing tests keep the same structure (same gates, same logic). Expected probability values will shift due to fixes 4+5 (z-score recalibration). Each test's `assert` values updated to match new math:

- `test_student_t_less_extreme_than_normal` — probabilities move further from 0.5 now (fixes 4+5 remove over-compression), so the Student-t vs normal comparison delta changes
- `test_regime_factor_trending` / `_reverting` — regime direction tests updated to use return-based direction
- `test_momentum_nudges_probability` — logit-space nudge produces slightly different values near extremes
- `test_buys_up_when_btc_above_strike` — entry gate now checks Kelly, test inputs must produce kelly >= 0.015
- All edge comparison tests — lower noise floor (0.03 vs 0.10)

### New Tests

**signal_engine (6 new):**
- `test_regime_direction_from_returns_not_prob` — trending down + above strike → prob decreases (the bug case)
- `test_logit_dampening_near_extremes` — same flow_signal produces smaller prob shift at p=0.93 vs p=0.50
- `test_kelly_gate_rejects_thin_edge_at_high_price` — edge=3% at p=0.80 rejected (kelly too small)
- `test_kelly_gate_accepts_underdog_with_small_edge` — edge=8% at p=0.30 accepted (kelly sufficient)
- `test_atr_scaling_increases_z` — with atr_sigma_ratio=1.7, z is larger than without scaling
- `test_student_t_scale_normalization` — t.cdf(z*scale, df=4) vs t.cdf(z, df=4) for known z values

**indicator_engine (4 new):**
- `test_normalizer_warmup_returns_raw` — first 50 calls return raw scores
- `test_normalizer_after_warmup_returns_zscore` — after warmup, scores are zero-mean unit-variance
- `test_normalizer_clamps_extremes` — output clamped to [-3, 3]
- `test_normalized_score_in_indicators_dict` — `norm_score` key present after compute_all

**calibrator (5 new):**
- `test_identity_calibration` — default params (a=-1, b=0) return input unchanged
- `test_fit_with_biased_data` — fit on data where model overestimates → a and b shift to correct
- `test_fit_requires_min_samples` — returns False with < 100 outcomes
- `test_save_and_load` — round-trip persistence to JSON
- `test_calibration_improves_log_loss` — calibrated probs have lower log-loss on holdout

**integration (1 updated, 1 new):**
- `test_full_trade_flow` — updated expected values
- `test_calibrated_probability_in_trade_flow` — end-to-end with loaded calibrator

### Config Tests
- `test_config.py` — add validation bounds for `min_kelly` (0.005-0.05) and `atr_sigma_ratio` (1.2-2.5)

## Holes Found During Review — MUST Address

### HOLE 1: Config Validation Will CRASH On Startup

`loader.py:69` validates `signal.entry_threshold` in `[0.05, 0.35]`. The new default `0.03` is below `0.05`. **`validate_config()` will raise `ValueError` and the bot won't start.**

**Fix:** Change validation range in `loader.py`:
```python
# Before:
_check_range("signal.entry_threshold", 0.05, 0.35)
# After:
_check_range("signal.entry_threshold", 0.01, 0.10)
```

Also add missing validation for new params:
```python
_check_range("signal.min_kelly", 0.005, 0.05)
_check_range("signal.atr_sigma_ratio", 1.2, 2.5)
```

### HOLE 2: Three Tests BREAK (Mathematically Verified)

The z-score amplification from Fixes 4+5 changes probability values enough to flip assertions:

**`test_skips_when_market_already_correct` (line 44):**
- OLD: z=0.200, prob=0.574, edge=0.044 → SKIP
- NEW: z_scaled=0.481, prob=0.672, edge=0.142 → **BUY_YES** (expected SKIP)
- Fix: Increase market_price_up to 0.64 so edge stays below 0.10 with new math

**`test_exit_when_edge_evaporates` (line 122):**
- OLD: z=0.577, prob=0.703, holding_edge=-0.147 → EXIT
- NEW: z_scaled=1.388, prob=0.881, holding_edge=+0.031 → **HOLD** (expected EXIT)
- Fix: Increase market_price_for_side to 0.92 so holding_edge stays negative

**`test_student_t_less_extreme_than_normal` (line 151):**
- OLD: prob=0.9999 clamped to 0.97, passes `prob < 0.99`
- NEW: prob=0.9999 with sigmoid (no clamp), **fails** `prob < 0.99`
- Fix: Test at moderate z where Student-t fat tails show (smaller distance). Or change assertion to test the tail property: at large z, Student-t gives lower P than CDF of same z would on normal

### HOLE 3: main.py Default Values Are Wrong (Pre-Existing)

`main.py:1058-1066` uses fallback defaults that don't match `settings.yaml`:

```python
min_edge=signal_cfg.get("entry_threshold", 0.20)    # settings.yaml: 0.10, new: 0.03
regime_weight=signal_cfg.get("regime_weight", 0.05)  # settings.yaml: 0.03
flow_weight=signal_cfg.get("flow_weight", 0.06)      # settings.yaml: 0.04
```

**Fix:** Align all defaults with settings.yaml values:
```python
min_edge=signal_cfg.get("entry_threshold", 0.03)      # Match new default
regime_weight=signal_cfg.get("regime_weight", 0.03)    # Match settings.yaml
flow_weight=signal_cfg.get("flow_weight", 0.04)        # Match settings.yaml
```

### HOLE 4: main.py Missing Calibrator + New Params at Construction

The spec describes PlattCalibrator integration but doesn't cover the exact wiring in `main.py:1057-1068`:

**Fix:** After `SignalEngine` construction, add:
```python
from polybot.core.calibrator import PlattCalibrator

signal_engine = SignalEngine(
    ...existing params...
    min_kelly=signal_cfg.get("min_kelly", 0.015),          # NEW
    atr_sigma_ratio=signal_cfg.get("atr_sigma_ratio", 1.7), # NEW
)

# Load Platt calibrator (identity if file doesn't exist)
calibrator = PlattCalibrator()
calibrator_path = Path(base_dir) / "memory" / "calibration" / "platt_params.json"
calibrator.load(calibrator_path)
signal_engine.calibrator = calibrator
```

### HOLE 5: conftest.py Sample Config Missing New Params

`conftest.py` `SAMPLE_CONFIG` fixture doesn't include `min_kelly` or `atr_sigma_ratio`. When `validate_config()` checks for them, tests using `loaded_config` will crash.

**Fix:** Add to conftest.py sample config:
```yaml
signal:
  ...existing params...
  min_kelly: 0.015
  atr_sigma_ratio: 1.7
```

### HOLE 6: Weight Optimizer Backtest Is Invalid With New Math

`scheduler.py:113-156` backtests by adjusting stored momentum scores and re-estimating edge. After these changes, the backtest is inaccurate because:

1. **Indicator normalization (Fix 8):** Stored outcomes have raw scores, not z-normalized. Backtest uses raw scores to recompute momentum — the result won't match what the live engine produces.
2. **Logit-space (Fix 2):** Backtest adds momentum_delta to edge linearly, but the live engine applies it in logit space (effect depends on probability level).
3. **ATR scaling + Student-t (Fixes 4+5):** Stored `model_probability` was computed with old z-score math. Can't re-derive with new math (needs raw ATR/candle data).
4. **Kelly gate (Fix 3):** Backtest doesn't check min_kelly.
5. **Platt calibration (Fix 9):** Backtest doesn't apply calibrator.

**Fix — pragmatic approach:**
- **For indicator weight recommendations:** Continue current backtest logic (best available approximation). Use `norm_score` from outcomes when available (newly stored outcomes will have it; old ones fall back to raw `score`).
- **For layer weight / structural params** (`momentum_weight`, `regime_weight`, `flow_weight`, `student_t_df`, `atr_sigma_ratio`, `min_kelly`): Skip the hypothetical-edge backtest entirely — just hot-swap directly with clamping. These params change the probability model fundamentally and can't be backtested from stored outcomes. The Sharpe comparison still applies to the OVERALL outcome set.
- **Apply calibrator** to stored model_probability when calibrator is active.
- **Check Kelly gate** in the backtest loop.

Add a comment in the code explaining this limitation: stored outcomes from before the math fixes use the old probability model, so backtesting against them with new-math assumptions is inherently approximate.

### HOLE 7: Outcome Storage Needs Enrichment

When recording trades, `indicator_snapshot` must store `norm_score` alongside raw `score` for each indicator, and the calibrated probability alongside the raw one. This enables:
- Future backtests to use normalized scores
- Calibration fitting to use calibrated probabilities
- Historical analysis to compare raw vs calibrated model accuracy

**Fix:** In `main.py` where indicator_snapshot is built (~line 299):
```python
"trade_context": {
    ...existing fields...
    "model_probability_raw": signal.prob,           # Pre-calibration
    "model_probability": calibrated_prob,            # Post-calibration (or same as raw if no calibrator)
}
```

And for each indicator in the snapshot:
```python
"rsi": {"score": raw_score, "norm_score": indicators["rsi"].get("norm_score", raw_score)},
```

### HOLE 8: Scheduler Clamp Ranges Need Updating

`scheduler.py:197-199` clamps `min_edge` to `[0.05, 0.35]`:
```python
val = _clamp(recommendations["recommended_min_edge"], 0.05, 0.35)
```

This must change to `[0.01, 0.10]` to match the new noise-floor role. Otherwise the pipeline can't tune entry_threshold below 0.05.

### HOLE 9: IndicatorEngine.compute_score() Is Dead Code

`compute_score()` in `indicators/engine.py:65-72` is defined but **never called in production**. Only `SignalEngine.compute_momentum()` does the weighted sum. The normalizer integration via `norm_score` in the indicators dict is the correct path (as spec already describes). Just noting this for awareness — `compute_score()` should also get the normalizer update for consistency, but it's not on the critical path.

## Safety Invariants

These MUST hold after all fixes:

1. **Identity on startup.** With no calibration file and during indicator normalization warmup, the engine produces the same SHAPE of output as before (directionally same trades), just with different magnitudes from fixes 4+5.
2. **Sigmoid bounds.** Log-odds conversion naturally bounds probability to (0, 1). No manual clamping.
3. **Kelly >= 0.** The `_kelly()` formula is unchanged. `max(0, ...)` guard remains.
4. **Exit logic unchanged.** `evaluate_hold()` uses `holding_edge = model_prob - market_price` with the same threshold logic. The model_prob is now better calibrated, but the comparison structure is identical.
5. **Config backward compat.** Old config files still work — `min_kelly` and `atr_sigma_ratio` have defaults in `__init__`. Missing keys use defaults.
6. **Pipeline compatibility.** TAEvolver and WeightOptimizer continue tuning the same parameter names. The engine's internal 4x logit conversion is invisible to the pipeline.
7. **No new external dependencies.** `scipy.optimize.minimize` is already imported (scipy is a dependency for Student-t CDF). `numpy` is already imported.
