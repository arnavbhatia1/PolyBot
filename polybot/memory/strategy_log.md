
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

## 2026-04-12T04:06:21.493243+00:00

**Source:** Claude (confidence: medium)

**Key Findings:**
- Win rate of 75.6% across 82 trades is strong, but edge calibration is inverted: 10-20% edge bucket wins at 84.8% vs only 63.9% for 20-35% edge bucket — high-edge trades are underperforming, suggesting model overconfidence at extreme probabilities
- Down trades significantly underperform Up trades: 68.8% win rate vs 85.3%, and Down avg_ret=-0.0427 vs Up avg_ret=+0.1607 — the negative avg_ret on Down trades indicates losses are large when wrong
- Scalp exit system is severely miscalibrated: only 43.1% of scalps were actually optimal exits. For positions with 90s+ remaining, accuracy drops to 28% — meaning 72% of the time holding would have been better
- Scalp analysis by time bucket is the clearest signal in the dataset: 0-30s scalps are correct 72.2% of the time (good), 30-90s only 33.3% (bad), 90s+ only 28% (very bad) — exit threshold is cutting winners far too early
- High-ATR regime shows meaningfully lower win rate (66.7%) vs low/mid ATR (78.6%/81.5%), suggesting the model struggles in volatile conditions where the student-t distribution may still underestimate tail risk
- Flow signal on Down trade losses shows a clear pattern: trades #4, #11, #23, #24 all had positive flow (buying pressure) yet model bet Down — positive flow opposing direction should be a disqualifying signal for Down entries
- Edge inversion (20-35% edge losing more than 10-20% edge) persists from prior cycle, pointing to systematic overestimation of probability at extreme z-scores — atr_sigma_ratio increase from 1.4 to 1.5 in prior cycle was appropriate but may not be enough
- RSI remains the strongest individual indicator at 77.9% accuracy with notable asymmetry: bearish RSI signals win 86.1% vs bullish at 70.7% — RSI appears most reliable as a mean-reversion signal against overbought conditions
- MACD shows opposite asymmetry to RSI: bullish MACD wins 82.1% vs bearish 67.5% — MACD is more reliable in momentum (Up) direction, less so for Down calls
- Stochastic mirrors RSI pattern: bearish stoch wins 84.4% vs bullish 68.2% — both RSI and Stochastic are better at identifying overbought/reversal than oversold/bounce conditions
- OBV data is missing from the per-indicator analysis section despite being in the trade log — insufficient signal data to evaluate OBV independently
- The 107-second average time remaining on suboptimal scalps (vs 57s for optimal) confirms that exits made with substantial time remaining are almost always premature and costly

**Risk Warnings:**
- CRITICAL: Exit threshold -0.05 is causing massive value destruction — 57% of scalp exits are wrong, and suboptimal scalps have avg missed gain of +1.55 gain_pct. This is the single largest improvement opportunity in the system
- Down trade negative avg_ret (-0.0427) means the system is net losing on Down trades when accounting for loss magnitude — consider whether the min_model_probability filter is high enough for Down-direction trades specifically
- Edge calibration inversion (high edge = lower win rate) is a red flag for model overconfidence at extreme probabilities — probability estimates above ~85% may be poorly calibrated and should be treated with skepticism
- High-ATR trades (atr>~40) show 66.7% win rate — in extremely volatile regimes, the model's z-score calculation may be systematically biased. Consider whether atr_sigma_ratio should be dynamic or if high-ATR trades should have a higher min_model_probability threshold
- Positive flow on Down trade losses (trades #4, #11, #23, #24, #42) is a recurring pattern — flow conflicting with trade direction should increase skepticism, not just reduce edge. The flow layer may need directional conflict logic
- Sharpe ratio of 0.077 is extremely low despite a 75.6% win rate — this confirms the scalp exits are destroying risk-adjusted returns by converting winning positions into small gains or losses that don't compensate for resolution losses
- Sample size of 82 trades is sufficient for top-level analysis but sub-bucket statistics (e.g., n=5 for 0-60s entries, n=18 for 0-30s scalps) still carry wide confidence intervals — changes should remain conservative

**Reasoning:** This cycle has 82 trades — above the 50-trade threshold — and the data tells a coherent story across multiple dimensions. The most actionable finding by far is the scalp exit miscalibration, and the recommended changes center primarily on fixing that.

The exit_edge_threshold is the highest-priority change. With scalp accuracy at only 43.1% overall and just 28% for exits made with 90+ seconds remaining, the current -0.05 threshold is systematically cutting winners. The holding_edge at suboptimal scalps averages -0.0427, meaning these positions are being exited just above the threshold — they briefly dip below -0.05 and get cut even though resolution would have been profitable 72% of the time. Moving the threshold to -0.10 means positions need to deteriorate significantly before being exited, which should allow the strong underlying win rate to express itself through resolution rather than being truncated by premature scalps. The 0-30s window (72.2% scalp accuracy) remains the only time zone where exits are genuinely value-additive, and those are handled correctly at any threshold since the system already applies a time urgency bonus near expiry. This is a large change for this parameter but justified given the overwhelming evidence — the current -0.05 is clearly wrong.

The edge calibration inversion (84.8% win rate at 10-20% edge vs 63.9% at 20-35% edge) is a persistent and serious concern. High-edge trades should win more often — when they don't, it means the model is overconfident at extreme probabilities. The prior cycle increased atr_sigma_ratio from 1.4 to 1.5, which was the right direction. Maintaining 1.5 is appropriate this cycle while monitoring whether the inversion persists. If it does in the next cycle, a further increase to 1.6 or a bump in student_t_df to 6 (slightly fatter tails, more cautious probabilities at extremes) should be considered. For now, student_t_df stays at 5.

The Up/Down asymmetry deserves serious attention. Up trades win at 85.3% with positive avg_ret (+0.1607), while Down trades win at only 68.8% with negative avg_ret (-0.0427). The negative avg_ret on Down trades is alarming — it means when Down trades lose, they lose big (full resolution losses), and the wins may be coming via scalps at thin margins. Looking at the trade log, many Down losses (trades #4, #11, #23, #24) had positive flow signals (buying pressure) contrary to the Down bet. Flow conflicting with direction should be a much stronger disqualifying signal. The flow_weight increase from 0.04 to 0.06 (implemented last cycle) is maintained — we don't want to increase it further without seeing whether the directional conflict issue can be handled at the application layer rather than by weight alone.

Indicator weight recommendations stay consistent with last cycle: RSI up to 0.25 and VWAP up to 0.25 (both show solid accuracy and complementary signal types), MACD down to 0.20 (momentum signal less reliable on Down calls), OBV down to 0.10 (weakest measurable signal, missing from per-indicator breakdown). Stochastic holds at 0.20. The sum is 1.00. These weights have now been consistent for two cycles, which is appropriate — changing them again without new evidence would be noise-chasing.

The high-ATR finding (66.7% win rate at high volatility vs 78-81% elsewhere) doesn't yet warrant a parameter change, but it is a risk pattern to monitor. The model may be underestimating uncertainty in volatile regimes because ATR spikes are inherently unstable estimators of instantaneous volatility. A future enhancement could be ATR-conditional probability dampening, but that is beyond current parameter tuning scope.

min_time_remaining stays at 30 seconds (implemented last cycle). The 0-60s bucket showing 100% win rate at n=5 is too small to draw conclusions from, and the 30-second floor prevents the most time-pressured entries without being overly restrictive.

Trading hours remain unchanged — no time-of-day pattern is visible with current data structure. All other parameters (kelly_fraction=0.15, min_kelly=0.015, min_edge=0.04, min_model_probability=0.65, regime_weight=0.04, momentum_weight=0.04) are held stable. The primary lever this cycle is the exit_edge_threshold correction from -0.05 to -0.10, which addresses the most clearly identified and quantified source of value destruction in the system.

**Recommended Weights:** rsi=0.25, macd=0.20, stochastic=0.20, obv=0.10, vwap=0.25
**Recommended Parameters:** momentum_weight=0.02, min_edge=0.04, kelly_fraction=0.15, min_kelly=0.015, atr_sigma_ratio=1.5
