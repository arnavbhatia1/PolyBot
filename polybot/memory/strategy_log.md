
## 2026-04-11T22:10:53.943888+00:00

**Source:** Claude (confidence: medium)

**Key Findings:**
- CRITICAL: Edge calibration is inverted — 10-20% edge bucket wins 92% vs 61.5% for 20-35% edge bucket. Higher modeled edge does NOT predict better outcomes, suggesting model overconfidence at extreme probability readings.
- Strong directional asymmetry: Up trades win 90% (n=20) vs Down trades 67.7% (n=31). Down trades represent 60.8% of all trades yet significantly underperform, suggesting a bearish bias or miscalibration in downside signals.
- RSI is the strongest individual indicator at 78.0% accuracy with notable bearish edge (90.5% bearish accuracy vs 69.0% bullish). VWAP also shows strong bearish signal (81.2% bearish). Both warrant increased weight.
- OBV shows near-zero signal values across nearly all trades (consistently ±0.00 to ±0.02) indicating it provides minimal discriminatory power and should be reduced.
- Scalp exit logic is severely miscalibrated: overall scalp accuracy only 47.4% (worse than random), and critically only 12.5% accuracy at 90s+ remaining. The system is exiting too early on positions that would have resolved profitably.
- Flow signal shows a concerning pattern: 7 of 12 losses (trades #11, #18, #20, #30, #31, #45, #49) had positive flow values (+0.08 to +0.47) while trading DOWN — the flow signal may be directionally misleading on down trades.
- Time pattern: 0-60s entries win 100% (n=4) and 180-300s entries win 84.2% (n=19), while 60-180s is weakest at 67.9% (n=28). The mid-range entry timing is a weak spot.
- Sharpe ratio of 0.063 is extremely low given 76.5% win rate, indicating returns are not consistent — large variance from full losses (-100%) dragging down risk-adjusted performance.

**Risk Warnings:**
- OVERCONFIDENCE AT HIGH EDGE: The inverted edge-calibration (92% win at 10-20% edge vs 61.5% at 20-35%) is a serious model miscalibration. Trades where the model is most confident are losing more often. Consider that extreme probability readings (near 100%) may indicate model saturation rather than genuine edge.
- DOWN TRADE BIAS: 12 losses analyzed — 10 were on Down positions. The model appears systematically overconfident on downside moves. At 67.7% win rate on down trades vs 90% on up trades, consider whether bearish indicators are being double-counted (RSI overbought + stochastic overbought both firing simultaneously on same underlying condition).
- SCALP LOGIC CAUSING SIGNIFICANT LOSSES: 10 suboptimal scalps with avg missed gain of +41.5% is extremely costly. With exit_edge_threshold at -0.10, the system is exiting positions 130 seconds before expiry that would have won. Tightening to -0.05 should reduce premature exits significantly.
- FLOW SIGNAL RELIABILITY ON DOWN TRADES: Positive flow on down trade entries correlated with most losses. If flow shows buying pressure (+) but model says DOWN, this conflict may indicate the down signal is wrong. Consider using flow as a veto rather than just an additive signal.
- SAMPLE SIZE WARNING: With only 51 trades, all sub-bucket analyses (especially time patterns with n=4 for 0-60s) carry high variance. Changes should be conservative and directional rather than aggressive.
- ATR OUTLIERS: Trades #39 (ATR=26.3), #44 (ATR=35.3), #45 (ATR=41.7), #46 (ATR=40.9) show extreme volatility. Trade #45 was a loss at ATR=41.7. High-ATR environments may warrant more conservative probability estimates — slight increase in atr_sigma_ratio helps widen the distribution.

**Reasoning:** This dataset of 51 trades reveals several important structural patterns that warrant targeted adjustments. The overall 76.5% win rate is strong, but the Sharpe of 0.063 signals high return variance, and the edge calibration inversion is the most concerning finding.

The most critical issue is the inverted edge-calibration: trades in the 10-20% edge bucket win 92% of the time while trades in the 20-35% bucket win only 61.5%. This is backwards from what a well-calibrated model should produce. It suggests that when the model generates very large edge readings (high probability estimates), it is often because multiple indicators are simultaneously at extreme readings (RSI=+1.00, stochastic=+1.00, etc.), which likely reflects indicator saturation rather than genuine compounding edge. In these cases the model may be double-counting correlated signals. Increasing student_t_df from 5 to 4 slightly fattens tails, which counteracts overconfidence at extremes by keeping probabilities slightly more conservative at extreme z-scores. The atr_sigma_ratio increase from 1.4 to 1.5 also widens the volatility denominator slightly, producing more moderate (less extreme) probability readings.

The directional asymmetry is stark: Up trades win 90% vs Down 67.7%. Looking at the loss trades in detail, 10 of 12 losses are on Down positions. Many of these losses (trades #11, #18, #20, #30, #31, #45, #49) show RSI=+1.00 AND stochastic=+1.00 simultaneously — both overbought signals firing at maximum. This suggests RSI and stochastic are highly correlated in this dataset and are providing redundant rather than independent information on down signals. Increasing RSI weight to 0.25 while reducing MACD to 0.20 reflects RSI's superior accuracy (78% vs 75%), and VWAP increases to 0.25 given its strong bearish accuracy (81.2%). OBV is reduced to 0.10 given its near-zero signal values across virtually all trades.

The scalp exit miscalibration is severe and actionable. With only 47.4% scalp accuracy overall and a catastrophic 12.5% accuracy at 90s+ remaining, the current exit_edge_threshold of -0.10 is far too aggressive (exiting too readily). For positions with more than 90 seconds remaining, holding is the correct decision 87.5% of the time. Tightening the threshold to -0.05 means the system requires a more negative holding_edge before exiting, effectively requiring stronger evidence of adverse movement before cutting. The suboptimal scalps had avg holding_edge of -0.057 at exit, meaning positions were being cut just below the -0.05 level — these would be held under the new threshold. The 4 optimal scalps (0-30s remaining at 100% accuracy) are correctly handled at any reasonable threshold since those are genuinely late-stage exits.

Flow weight increases from 0.04 to 0.06 reflects the observation that flow provides useful signal, particularly on up trades. However, the pattern of positive flow on down trade losses suggests flow is being used in a potentially contradictory way — when flow is positive (buying pressure) on a down trade, that's a conflict signal. The increase in flow_weight is modest to capture genuine flow edge without amplifying the conflict issue.

Regime weight increases slightly from 0.03 to 0.04 given that trending vs mean-reverting regimes appear relevant — the high-ATR period (trades 44-51) showed mixed results, and having slightly more regime sensitivity may help.

Trading hours remain unchanged — there is no clear time-of-day pattern in the data (the ET hour distribution isn't provided in enough detail to justify changes, and the time-remaining patterns reflect entry timing within contracts, not time of day).

Minimum time remaining increases from 0 to 30 seconds to enforce the observation that the 0-30s scalp accuracy is 100% (good exits) but entering with only 22s remaining (trade #1) is extremely late and lucky. A 30-second floor prevents the most time-pressured entries while still allowing the 0-60s bucket opportunities.

All changes are within the conservative ≤0.05 per cycle guidance. The confidence is medium given the sample size of 51 trades — these patterns are directionally clear but statistically the sub-buckets have wide confidence intervals.

**Recommended Weights:** rsi=0.25, macd=0.20, stochastic=0.20, obv=0.10, vwap=0.25
**Recommended Parameters:** momentum_weight=0.02, min_edge=0.04, kelly_fraction=0.15, min_kelly=0.015, atr_sigma_ratio=1.5
