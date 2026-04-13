Entry Logic & SPRT Gate — Critical Review
Overall verdict: 2 real issues worth fixing, 1 moderate concern, rest is solid.

🔴 Critical #1 — SPRT i.i.d. Violation Has a Simple Fix Being Ignored
The flaw: SPRT assumes each observation is independent. CLOB ticks and 1-min return signals arrive serially correlated — autocorrelation ρ≈0.2-0.4 at 1s lag in trending regimes. This inflates the effective evidence count, causing SPRT to hit the ENTER threshold faster than the math justifies. The actual false-positive rate is higher than the configured alpha=0.05.
Why it matters practically: In trending regimes (exactly when the model is most active), autocorrelation is highest, so SPRT fires fastest — the opposite of when you want to be cautious about correlated "new" evidence.
The fix is simple and doesn't touch architecture: Instead of feeding SPRT every tick, downsample observations to every 10-15 seconds. This reduces effective autocorrelation without changing the SPRT math, thresholds, or any downstream logic. No frozen code touched.

🔴 Critical #2 — Momentum Disagreement Gate Is Logically Inverted
The flaw: momentum_weight = -0.02 (negative — the model fades indicators for mean reversion). This means bullish RSI/MACD/etc. pushes the model toward predicting Down. But the "momentum disagreement halves edge" gate appears to check raw indicator direction vs bet direction — so if indicators are bullish and you're betting Down, it calls that disagreement and halves edge.
The contradiction: From the model's perspective, bullish indicators creating a Down signal is agreement — the negative weight is doing exactly what it should. The disagreement gate then penalizes a signal it already correctly processed, effectively double-penalizing.
What needs checking in the code: signal_engine.py — wherever the disagreement multiplier is computed, verify whether it checks sign(raw_indicator_score) vs sign(bet_direction) or sign(weighted_indicator_contribution) vs sign(bet_direction). If it's the former, the sign needs to flip when momentum_weight < 0.

🟡 Moderate — Alpha Decay Early Entry Has Insufficient Sample Size
The issue: Alpha decay rate is estimated during the 60s observe phase — meaning you have maybe 10-20 signal evaluations to fit a decay curve. That regression has enormous uncertainty. A spurious fast-decay reading can trigger should_enter_now() at second 25-35, bypassing the observe phase designed to filter weak signals.
Mitigation already in place: The double gate (should_enter_now() AND SPRT = ENTER) means you still need SPRT confirmation. This limits the damage. The risk is SPRT hitting ENTER due to the i.i.d. issue above at the same time as a noisy alpha decay fires — compounding both errors.
Not urgent on its own, but becomes critical if Critical #1 is unaddressed.

✅ Everything Else Is Sound

60s observe phase design — correct for 5-min contracts
10 gate ordering — computationally reasonable, logically clean
Edge noise floor 0.04 — ~2x fee buffer, defensible
Max edge cap 0.20 — post-Platt, this is correctly catching data/feed anomalies
Entry timing phases (0/60/180/240s) — the 90% + half-Kelly in final 60s is correct given binary payoff dynamics
price_sum gate [0.98, 1.02] — catches negRisk settlement anomalies


For Claude Code
Fix priority:

sprt.py — add observation downsampling (10-15s minimum interval between fed observations), configurable via sprt.observation_interval_s in settings.yaml
signal_engine.py — find momentum disagreement multiplier, verify it uses sign(weighted_contribution) not sign(raw_score) when momentum_weight < 0
No changes to frozen baseline files beyond the disagreement sign check