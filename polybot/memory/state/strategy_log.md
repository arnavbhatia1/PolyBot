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
