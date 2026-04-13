"""GARCH(1,1) volatility predictor for near-term BTC vol.

Fits a simple GARCH(1,1) model on 1-minute returns to forecast the next
5-minute realized volatility. Compares to Deribit IV to detect vol mispricing.

When GARCH forecast > Deribit IV: realized vol will exceed implied
  -> L1 CDF is overconfident, widen edge threshold, reduce size
When GARCH forecast < Deribit IV: market overestimates vol
  -> L1 CDF is underconfident, tighten threshold, increase size
"""
from __future__ import annotations

import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Default GARCH(1,1) params (pre-estimated from BTC 1-min data)
# These can be re-estimated by the pipeline
DEFAULT_OMEGA = 1e-6    # long-run variance intercept
DEFAULT_ALPHA = 0.10    # reaction to recent shock
DEFAULT_BETA = 0.85     # persistence of volatility


class GarchPredictor:
    """Simple GARCH(1,1) volatility forecaster.

    sigma²_t = omega + alpha * epsilon²_(t-1) + beta * sigma²_(t-1)

    Computes conditional variance from a series of returns,
    then forecasts the next 5-minute cumulative variance.
    """

    def __init__(self, omega: float = DEFAULT_OMEGA,
                 alpha: float = DEFAULT_ALPHA,
                 beta: float = DEFAULT_BETA) -> None:
        self.omega = omega
        self.alpha = alpha
        self.beta = beta

    def forecast_5min_vol(self, returns: np.ndarray) -> float:
        """Forecast annualized volatility for the next 5 minutes.

        Args:
            returns: Array of 1-minute log returns (at least 20 values).

        Returns:
            Annualized volatility forecast (decimal, e.g., 0.80 = 80%).
            Returns 0.0 if insufficient data.
        """
        if len(returns) < 20:
            return 0.0

        # Compute conditional variance series
        sigma2 = np.var(returns)  # initialize with sample variance
        for r in returns:
            sigma2 = self.omega + self.alpha * r * r + self.beta * sigma2

        # Forecast next 5 one-minute variances (GARCH multi-step)
        # h-step forecast: sigma²(t+h) = omega/(1-alpha-beta) + (alpha+beta)^h * (sigma²(t) - omega/(1-alpha-beta))
        persistence = self.alpha + self.beta
        long_run_var = self.omega / max(1 - persistence, 1e-10)

        cum_var = 0.0
        forecast_var = sigma2
        for _ in range(5):
            cum_var += forecast_var
            forecast_var = long_run_var + persistence * (forecast_var - long_run_var)

        # Convert 5-min cumulative variance to annualized vol
        # sqrt(cum_var) is 5-min std dev, annualize by * sqrt(525600/5)
        vol_5min = math.sqrt(max(cum_var, 0))
        annualized = vol_5min * math.sqrt(525600 / 5)

        return annualized

    def compute_vol_ratio(self, returns: np.ndarray, deribit_iv: float) -> float:
        """Ratio of GARCH forecast to Deribit IV.

        > 1.0: GARCH expects MORE vol than market (CDF overconfident)
        < 1.0: GARCH expects LESS vol than market (CDF underconfident)

        Returns 1.0 (neutral) if insufficient data or deribit_iv is zero.
        """
        if deribit_iv <= 0:
            return 1.0
        forecast = self.forecast_5min_vol(returns)
        if forecast <= 0:
            return 1.0
        ratio = forecast / deribit_iv
        # Clamp to reasonable range
        return max(0.5, min(2.0, ratio))

    def compute_sizing_adjustment(self, returns: np.ndarray, deribit_iv: float) -> float:
        """Kelly sizing multiplier based on realized vol vs implied vol.

        Uses simple realized vol ratio (no GARCH parameter estimation):
        recent_vol (last 25 returns) vs baseline_vol (last 100 returns).

        If recent >> baseline: vol expanding -> reduce size 0.7x
        If recent << baseline: vol contracting -> increase size 1.3x
        """
        if len(returns) < 30:
            return 1.0
        recent = np.std(returns[-25:]) if len(returns) >= 25 else np.std(returns)
        baseline = np.std(returns[-100:]) if len(returns) >= 100 else np.std(returns)
        if baseline <= 0:
            return 1.0
        ratio = recent / baseline
        if ratio > 1.5:
            return 0.7   # vol expanding -> reduce
        elif ratio < 0.6:
            return 1.3   # vol contracting -> boost
        return 1.0
