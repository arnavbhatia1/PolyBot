## 2026-04-15T04:06:35.467990+00:00

**Source:** Local fallback (Claude unavailable)

**Analysis:** {'win_rate': 0.5252525252525253, 'avg_gain_pct': 0.09578460606060607, 'total_trades': 297}

**Recommended Weights:** {'rsi': 0.1967, 'macd': 0.2514, 'stochastic': 0.2002, 'obv': 0.1505, 'vwap': 0.2011}

## 2026-04-15T05:26:17.403148+00:00

**Source:** Claude (medium) | **Weights:** rsi=0.15, macd=0.30, stochastic=0.20, obv=0.10, vwap=0.25
**Params:** momentum_weight=0.03, regime_weight=0.04, flow_weight=0.05, student_t_df=4, min_edge=0.04, min_kelly=0.015, atr_sigma_ratio=1.6, kelly_fraction=0.13, min_model_probability=0.58, exit_edge_threshold=-0.08, min_time_remaining=60, trading_start_hour_et=0, trading_end_hour_et=23, trading_end_minute=59

**Findings:**
- ATR distribution shift is severe: mean dropped from 38.7 to 17.8 (KS=0.516, p=0.000) — system is now operating in a stru
- Low ATR regime shows 39.0% win rate (n=100) vs mid/high ATR at 58-61% — the new low-ATR environment is the primary perfo
- Down trades significantly outperform Up trades: 55.8% WR vs 51.0%, avg_ret 0.1322 vs 0.0786 — VWAP bearish signal (61.0%
- Mean-reverting regime: 64% WR (n=22) — highest performing regime; volatile 56% WR (n=43); neutral weakest at 51% WR (n=2
- Edge calibration is badly non-monotonic: Q1 ratio=1.47, Q2=0.47, Q3=1.08, Q4=0.30 — highest predicted edges are worst re

**Warnings:**
- CRITICAL: ATR has halved — the atr_sigma_ratio of 1.6 calibrated for higher volatility may now produce probability estim
- Low ATR win rate of 39.0% is below break-even — if current environment persists as low-ATR, the system will lose money s
- Edge overestimation at high confidence (Q4 ratio=0.30) means the model is badly miscalibrated at the top quartile — high

**Reasoning:** The dominant theme in this analysis cycle is the severe ATR distribution shift — the mean ATR has dropped from 38.7 to 17.8, a 54% reduction, confirmed by KS statistic of 0.516 at p=0.000. This is not a gradual drift but a structural regime change in BTC volatility. This shift likely explains the 39.0% win rate in low-ATR conditions, which is the most concerning single data point in the report. The system was calibrated for a higher-volatility environment, and many of its parameters (particularl...
