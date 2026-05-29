from polybot.core.signal_engine import SignalEngine


class TestSizingChainRegression:
    """Verify the sizing chain produces sane Kelly values across edge cases."""

    def _make_engine(self, **overrides):
        defaults = dict(
            min_edge=0.04, kelly_fraction=0.15, momentum_weight=-0.02,
            min_model_probability=0.58, student_t_df=5, regime_weight=0.03,
            flow_weight=0.04, min_kelly=0.015, atr_sigma_ratio=1.4, min_atr=8.0,
        )
        defaults.update(overrides)
        return SignalEngine(**defaults)

    def test_kelly_positive_for_valid_edge(self):
        """When model has genuine edge, Kelly should be positive."""
        engine = self._make_engine()
        k = engine._kelly(0.70, 0.60)  # 70% prob, 60% market
        assert k > 0

    def test_kelly_zero_for_no_edge(self):
        """When model prob equals market price, Kelly should be zero."""
        engine = self._make_engine()
        k = engine._kelly(0.60, 0.60)
        assert k == 0

    def test_kelly_zero_for_negative_edge(self):
        """When market is better than model, Kelly should be zero."""
        engine = self._make_engine()
        k = engine._kelly(0.55, 0.60)
        assert k == 0

    def test_kelly_scales_with_edge(self):
        """More edge = bigger Kelly."""
        engine = self._make_engine()
        k_small = engine._kelly(0.65, 0.60)
        k_large = engine._kelly(0.80, 0.60)
        assert k_large > k_small

    def test_kelly_pure_no_conviction(self):
        """Kelly no longer has conviction scaling — pure edge×prob sizing."""
        engine = self._make_engine()
        # Higher prob = higher Kelly (monotonic, no conviction jumps)
        k_low = engine._kelly(0.65, 0.58)
        k_mid = engine._kelly(0.78, 0.58)
        k_high = engine._kelly(0.92, 0.58)
        assert k_low < k_mid < k_high

    def test_extreme_market_prices_safe(self):
        """Kelly doesn't blow up at extreme market prices."""
        engine = self._make_engine()
        assert engine._kelly(0.70, 0.01) >= 0
        assert engine._kelly(0.70, 0.99) == 0
        assert engine._kelly(0.70, 0.001) >= 0
