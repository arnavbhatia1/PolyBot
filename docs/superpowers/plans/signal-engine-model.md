Signal Engine & Probability Model — Implementation Spec
Critical Issues (Fix Before Implementing)
ISSUE 1: iv_ratio Contradiction
CLAUDE.md states Deribit IV is "NOT applied to CDF vol scaling" but the Layer 1 formula explicitly uses it:
Copyvol = (ATR_effective / atr_sigma_ratio) × sqrt(minutes) × iv_ratio
Resolution: iv_ratio must be hardcoded to 1.0 in signal_engine.py. Remove the parameter from the z-score path. Deribit IV is logged in trade_context only.

ISSUE 2: min_model_probability Gate Mismatch
Config sets signal.min_model_probability: 0.58. Entry gates description says confidence >= 0.65. These are two different values and the code will use one of them.
Resolution: The 10-entry-gates list in Phase 2 is stale. 0.58 is authoritative (config overrides comments). The gate reads final_prob >= settings.signal.min_model_probability.

ISSUE 3: Flow Layer Multicollinearity
L3 (CLOB book imbalance), L3b (CVD), L3c (wall pressure) all measure order flow state. In a directional move, all three fire simultaneously. Combined maximum logit shift:
Copy(flow_weight + spot_flow_weight + wall_weight) × logit_scale
= (0.04 + 0.04 + 0.05) × 4.0
= 0.52 logit units
At the entry threshold (P=0.58, logit=0.323), three correlated flow layers can nearly double the base signal. This is not diversification — it's triple-counting the same evidence.
Resolution: Implement a flow layer cap. After summing L3 + L3b + L3c contributions, clamp the combined adjustment:
pythonCopyMAX_FLOW_LOGIT = 0.35  # ~P(0.50) -> P(0.587), one layer's worth
flow_combined = clamp(l3_adj + l3b_adj + l3c_adj, -MAX_FLOW_LOGIT, MAX_FLOW_LOGIT)
Expose max_flow_logit: 0.35 in settings.yaml under signal.

ISSUE 4: Platt Scaling Input Must Be logit(raw_prob), Not raw_prob
The Platt formula is 1 / (1 + exp(A × logit(raw_prob) + B)). If the pipeline accidentally passes raw_prob (not logit(raw_prob)), the calibration is wrong for all values. The raw_prob is already a probability — it must be converted to log-odds before Platt.
Resolution: The final line before returning must be:
pythonCopylogit_raw = math.log(raw_prob / (1 - raw_prob))  # explicit, not assumed
calibrated = 1.0 / (1.0 + math.exp(A * logit_raw + B))
Never pass raw_prob directly to Platt.

ISSUE 5: L4 Negative Weight Sign Convention
momentum_weight: -0.02 fades indicators (mean-reversion). This means RSI overbought (bullish score) reduces P(Up). The sign must be consistent: every indicator must output positive=bullish_for_BTC, negative=bearish_for_BTC, before the weight is applied.
Critical: If any indicator flips its sign convention (e.g., OBV returns raw value instead of normalized direction), the negative weight will accidentally amplify instead of fade.
Resolution: IndicatorNormalizer.normalize() must guarantee output in [-1, +1] with positive=bullish for all 5 indicators before the weight multiplication.

ISSUE 6: IndicatorNormalizer Cold Start Produces Garbage
The running EMA for z-scoring has no statistics until ~30 samples warm up. In the first 30 windows of a session, cvd_z and indicator z-scores are undefined or based on single-observation EMAs. tanh(garbage) returns a valid-looking float in [-1, +1] — the model silently misfires.
Resolution:
pythonCopyif self.sample_count < MIN_WARMUP_SAMPLES:  # MIN_WARMUP_SAMPLES = 30
    return 0.0  # neutral, no adjustment
Expose normalizer_warmup_samples: 30 in settings.yaml. During warmup, L3b and L4 contribute zero logit adjustment. L1 CDF still operates normally.

What This Spec Does NOT Change

Layer weights (pipeline-tuned only)
Student-t df=5
Kelly sizing chain (separate spec)
SPRT logic (separate spec)
Platt fitting (done in pipeline, not here)