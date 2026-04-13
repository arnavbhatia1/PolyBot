Calibration System — Critical Assessment
Honest verdict: 2 structural bugs, 1 design gap. The rest is fine.

🔴 Critical Bug #1 — Pipeline Sequencing (Platt Fitted Before Weights Change)
The pipeline runs in this order:

BiasDetector
PlattCalibrator fits A, B on old model probabilities ← here
TAEvolver
WeightOptimizer adopts new weights ← here

When WeightOptimizer changes signal weights, the pre-Platt probabilities the model now produces are different from what Platt was trained on. The bot runs all next day with Platt params calibrated to the old model's output distribution.
Fix: After WeightOptimizer adopts new weights, either:

Re-run PlattCalibrator using the new weights to re-score training candles (requires raw features stored, not just model_probability)
Or simpler: if weights changed, reset A=1.0, B=0.0 (identity) and let it recalibrate organically tomorrow. Stale wrong Platt is worse than no Platt.

Add a weights_changed flag from WeightOptimizer → PlattCalibrator.

🔴 Critical Bug #2 — No Damping on A, B Updates
With 120 training samples (200 minimum × 0.60 split), A and B have wide confidence intervals. A single unusual day (fat-tail move, low trade count, regime shift) can produce A, B values that are statistically valid but practically wrong.
No smoothing exists between days — params jump.
Fix: Exponential blend on adoption:
CopyA_final = 0.65 * A_fitted + 0.35 * A_previous
B_final = 0.65 * B_fitted + 0.35 * B_previous
Only bypass blending if the holdout log-loss improvement is large (>5%). Store A_previous, B_previous in platt_params.json alongside current.

🟡 Design Gap — Platt Can't Capture Favorite-Longshot Bias
Platt scaling is a global linear transform in logit space — it corrects uniform over/under-confidence across all probability ranges. Favorite-longshot bias is non-linear: low-probability contracts (~20-30%) are systematically overpriced on Polymarket, high-probability ones (~70-80%) are underpriced.
Platt will partially reduce this but can't capture the shape. You'd need isotonic regression or piecewise calibration — but isotonic needs ~500+ samples per bin to be stable. At current data volumes, Platt is the right call.
Pragmatic fix: Log reliability diagram data (bucket predicted prob into 10 bins, track actual win rate per bin) in trade_context. After 500+ trades, evaluate whether the residual non-linearity justifies isotonic regression. Don't add it now.

✅ What's Fine (Don't Touch)

200 trade minimum — thin but adequate for 2 parameters. The stationarity assumption is the real risk, not sample size.
60/40 chronological split — correct, no leakage.
Log-loss validation before adoption — correct gate.
platt_params.json persistence — git handles versioning.
No intraday refit — acceptable. Intraday drift is second-order compared to the two bugs above.


Summary for Claude Code
CopyFILE: agents/scheduler.py
- Move PlattCalibrator call to AFTER WeightOptimizer
- Pass weights_changed=True/False from WeightOptimizer to PlattCalibrator

FILE: core/calibrator.py  
- If weights_changed=True: write A=1.0, B=0.0, skip fitting
- On normal fit: blend A/B with previous values (0.65/0.35)
- Store A_previous, B_previous in platt_params.json

FILE: memory/calibration/platt_params.json schema:
  { "A": float, "B": float, "A_previous": float, "B_previous": float, 
    "fitted_on_trades": int, "holdout_log_loss": float, "timestamp": str }Add to Conversation