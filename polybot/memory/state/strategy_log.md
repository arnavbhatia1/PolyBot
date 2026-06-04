# Strategy Evolution Log

## 2026-05-29T03:45:48.913232+00:00

**Source:** Claude (low)
**Proposed Changes (5):**
  - atr_sigma_ratio=1.55 (High-ATR regime WR is 34.8% vs 45.8% mid-ATR (n=23); current ratio 1.3 is near-minimum and produces overconfident probabilities in volatile periods — raising to 1.55 widens the t-distribution spread, reducing overconfidence and edge overstatement in high-volatility windows.)
  - exit_edge_threshold=-0.08 (structural probe — counterfactual hold-better at edge ≈ -0.08)
  - derived_log_atr_ratio_weight=0.005 (structural probe — L6 feature never raised off zero)
  - derived_autocorr_signed_mag_weight=0.005 (structural probe — L6 feature never raised off zero)
  - derived_flow_disagreement_weight=0.005 (structural probe — L6 feature never raised off zero)

**Manual Suggestions (0) [operator-only]:**
  - none

**Findings:**
- None

**Warnings:**
- N=72 is at minimum threshold — all proposed changes carry high variance; one bad streak can mask signal.
- All trend metrics IMPROVING: any parameter change risks disrupting natural recovery momentum.
- loss_cut_whipsaw_blocked at 56905 suggests aggressive stop-loss cycling; if bankroll is declining, operator should review loss_cut_fraction manually.

**Reasoning:** All core performance metrics are actively improving without intervention, so the behavioral rules require restraint. The one structural issue not explained by the improving trend is high-ATR regime underperformance — the current atr_sigma_ratio of 1.3 is near the hard floor and likely generates overconfident probabilities when volatility spikes, producing bad entries that the loss-cut then fires on. A single conservative raise to 1.55 targets this without touching the flow or logit stack that is driving the recent improvement.

## 2026-05-30T03:45:38.090567+00:00

**Source:** Claude (low)
**Proposed Changes (0):**
  - none

**Manual Suggestions (0) [operator-only]:**
  - none

**Findings:**
- None

**Warnings:**
- Only 0 trades — insufficient data (need >=50)

**Reasoning:** insufficient data (N<50)

## 2026-05-30T04:55:23.895040+00:00

**Source:** Local
**Proposed Changes (5):**
  - logit_scale=4.5 (exploratory up step)
  - student_t_df=6 (exploratory up step)
  - exit_edge_threshold=-0.05 (structural probe — counterfactual hold-better at edge ≈ -0.05)
  - momentum_weight=0.08 (exploratory up step)
  - regime_weight=0.04 (exploratory up step)

**Manual Suggestions (2) [operator-only]:**
  - exit_edge_threshold: -0.1 -> -0.08 [?]
    scalps beat holds — relax scalp threshold
  - flip_edge_premium: 0.015 -> 0.035 [?]
    flip Sharpe -0.018 trails base by 0.131 and negative — raise premium aggressively

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-05-30T06:38:29.199785+00:00

**Source:** Local
**Proposed Changes (5):**
  - flow_weight=0.06 (exploratory up step)
  - spot_flow_weight=0.08 (exploratory down step)
  - exit_edge_threshold=-0.03 (structural probe — counterfactual hold-better at edge ≈ -0.03)
  - prev_margin_weight=0.03 (exploratory up step)
  - min_atr=9.0 (exploratory down step)

**Manual Suggestions (2) [operator-only]:**
  - exit_edge_threshold: -0.1 -> -0.08 [?]
    scalps beat holds — relax scalp threshold
  - flip_edge_premium: 0.015 -> 0.035 [?]
    flip Sharpe -0.029 trails base by 0.150 and negative — raise premium aggressively

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-05-30T06:43:47.295293+00:00

**Source:** Local
**Proposed Changes (5):**
  - kelly_fraction=0.1 (exploratory up step)
  - min_model_probability=0.58 (exploratory up step)
  - min_edge=0.03 (exploratory down step)
  - min_kelly=0.006 (exploratory down step)
  - regime_momentum_threshold=0.11 (exploratory down step)

**Manual Suggestions (1) [operator-only]:**
  - exit_edge_threshold: -0.1 -> -0.08 [?]
    scalps beat holds — relax scalp threshold

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-05-30T07:10:36.375143+00:00

**Source:** Local
**Proposed Changes (5):**
  - atr_sigma_ratio=1.2 (exploratory down step (×1.5))
  - final_logit_clamp=4.5 (exploratory up step)
  - min_model_probability=0.59 (exploratory up step (×1.5))
  - l5_regime_damp_cap=0.8 (exploratory up step)
  - atr_regime_shift_threshold=0.5 (exploratory down step)

**Manual Suggestions (2) [operator-only]:**
  - exit_edge_threshold: -0.1 -> -0.08 [?]
    scalps beat holds — relax scalp threshold
  - flip_edge_premium: 0.015 -> 0.035 [?]
    flip Sharpe -0.016 trails base by 0.138 and negative — raise premium aggressively

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-05-30T07:28:49.450927+00:00

**Source:** Local
**Proposed Changes (5):**
  - logit_scale=4.75 (exploratory up step (×1.5))
  - student_t_df=4 (exploratory down step (×1.5))
  - min_model_probability=0.59 (exploratory up step (×1.5))
  - momentum_weight=0.0 (exploratory down step (×1.5))
  - regime_weight=0.045 (exploratory up step (×1.5))

**Manual Suggestions (2) [operator-only]:**
  - exit_edge_threshold: -0.1 -> -0.08 [?]
    scalps beat holds — relax scalp threshold
  - flip_edge_premium: 0.015 -> 0.035 [?]
    flip Sharpe -0.016 trails base by 0.137 and negative — raise premium aggressively

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-05-31T03:45:39.712690+00:00

**Source:** Local
**Proposed Changes (5):**
  - flow_weight=0.07 (exploratory up step (×1.5))
  - spot_flow_weight=0.07 (exploratory down step (×1.5))
  - min_model_probability=0.58 (exploratory up step)
  - prev_margin_weight=0.035 (exploratory up step (×1.5))
  - min_atr=16.5 (exploratory up step (×1.5))

**Manual Suggestions (1) [operator-only]:**
  - flip_edge_premium: 0.015 -> 0.035 [?]
    flip Sharpe -0.021 trails base by 0.055 and negative — raise premium aggressively

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-06-01T03:45:15.660923+00:00

**Source:** Local
**Proposed Changes (5):**
  - kelly_fraction=0.05 (exploratory down step (×1.5))
  - min_edge=0.025 (exploratory down step (×1.5))
  - min_kelly=0.016 (exploratory up step (×1.5))
  - regime_momentum_threshold=0.09 (exploratory down step (×1.5))
  - exit_edge_threshold=-0.07 (exploratory up step (×1.5))

**Manual Suggestions (0) [operator-only]:**
  - none

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-06-02T03:45:20.599110+00:00

**Source:** Local
**Proposed Changes (5):**
  - atr_sigma_ratio=1.2 (exploratory down step (×2.0))
  - final_logit_clamp=4.75 (exploratory up step (×1.5))
  - l5_regime_damp_cap=0.85 (exploratory up step (×1.5))
  - atr_regime_shift_threshold=0.45 (exploratory down step (×1.5))
  - derived_log_atr_ratio_weight=0.015 (exploratory up step (×1.5))

**Manual Suggestions (0) [operator-only]:**
  - none

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-06-03T03:45:38.170138+00:00

**Source:** Local
**Proposed Changes (5):**
  - logit_scale=4.75 (exploratory up step (×1.5))
  - student_t_df=3 (exploratory down step (×2.0))
  - momentum_weight=0.0 (exploratory down step (×2.0))
  - regime_weight=0.045 (exploratory up step (×1.5))
  - derived_autocorr_signed_mag_weight=0.015 (exploratory up step (×1.5))

**Manual Suggestions (0) [operator-only]:**
  - none

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender

## 2026-06-04T03:45:55.716000+00:00

**Source:** Local
**Proposed Changes (5):**
  - flow_weight=0.02 (exploratory down step (×1.5))
  - spot_flow_weight=0.07 (exploratory down step (×1.5))
  - prev_margin_weight=0.035 (exploratory up step (×1.5))
  - min_atr=18.0 (exploratory up step (×2.0))
  - min_model_probability=0.59 (exploratory up step (×1.5))

**Manual Suggestions (0) [operator-only]:**
  - none

**Findings:**
- None

**Warnings:**
- None

**Reasoning:** Local recommender
