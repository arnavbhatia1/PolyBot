## 2026-04-15T04:06:35.467990+00:00

**Source:** Local fallback (Claude unavailable)

**Analysis:** {'win_rate': 0.5252525252525253, 'avg_gain_pct': 0.09578460606060607, 'total_trades': 297}

**Recommended Weights:** {'rsi': 0.1967, 'macd': 0.2514, 'stochastic': 0.2002, 'obv': 0.1505, 'vwap': 0.2011}

## 2026-04-15T05:36:20.580217+00:00

**Source:** Claude (medium) | **Weights:** rsi=0.15, macd=0.30, stochastic=0.20, obv=0.10, vwap=0.25
**Params:** momentum_weight=0.03, regime_weight=0.05, flow_weight=0.06, student_t_df=4, min_edge=0.04, min_kelly=0.015, atr_sigma_ratio=1.7, kelly_fraction=0.13, min_model_probability=0.6, exit_edge_threshold=-0.06, min_time_remaining=30, trading_start_hour_et=0, trading_end_hour_et=23, trading_end_minute=59

**Findings:**
- RSI accuracy is 50.2% — essentially coin flip, cutting its weight in half
- VWAP bearish signal wins 61% — boosting VWAP weight to capture directional edge
- Down trades win 55.8% vs Up at 51% — model should lean harder on bearish signals
- Scalp accuracy 57.2% means we exit too early — tightening exit threshold to hold longer
- Late entries (0-60s) win 73% — lowering min_time_remaining to catch more late-stage setups

**Warnings:**
- Low ATR regime still showing 39% WR — system struggles in low-volatility conditions
- Q4 edge calibration ratio=0.30 means highest-confidence trades badly underperform — overconfidence risk
- ATR mean halved (38.7→17.8) — if low-vol persists, overall edge may remain structurally compressed

**Reasoning:** RSI is near-random at 50.2% accuracy and is being cut to free up weight for VWAP, which shows clear directional edge especially on bearish signals (61%). Scalp accuracy of 57.2% is below the 60% threshold indicating we exit too early, so the exit threshold is tightened from -0.08 to -0.06 to hold positions longer. The persistent low-ATR environment continues to suppress win rates, so atr_sigma_ratio is nudged higher to 1.7 to generate more conservative probabilities in low-volatility conditions.