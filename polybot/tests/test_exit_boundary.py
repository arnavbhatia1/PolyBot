import pytest

from polybot.core.exit_boundary import ExitBoundary, effective_exit_threshold


class TestExitBoundary:
    def test_early_window_more_patient(self):
        """With lots of time left, threshold should be more negative (patient)."""
        eb = ExitBoundary()
        early = eb.compute_exit_threshold(240)
        late = eb.compute_exit_threshold(30)
        assert early < late  # early is more negative = more patient

    def test_at_expiry_tight(self):
        """Near expiry, threshold should be close to -fee_cost."""
        eb = ExitBoundary()
        threshold = eb.compute_exit_threshold(5)
        assert threshold > -0.05  # very tight near expiry

    def test_midway_moderate(self):
        eb = ExitBoundary()
        threshold = eb.compute_exit_threshold(150)
        assert -0.15 < threshold < -0.03

    def test_atm_threshold_always_negative(self):
        """At ATM (default market_price=0.5) the threshold is always negative —
        some adverse edge tolerated. (Deep OTM near expiry can go positive.)"""
        eb = ExitBoundary()
        for secs in [10, 30, 60, 120, 180, 240, 300]:
            t = eb.compute_exit_threshold(secs)
            assert t < 0


class TestEffectiveExitThreshold:
    """The blended fire criterion shared by evaluate_hold and the exit-threshold
    counterfactual replay."""

    def test_atm_is_max_of_floor_and_curve(self):
        # itm_depth = 0 at price 0.5 → pure max(raw threshold, boundary curve).
        curve = ExitBoundary().compute_exit_threshold(120, 0.07, 0.5)
        eff = effective_exit_threshold(-0.10, 120, 0.5, fee_rate=0.07)
        assert eff == pytest.approx(max(-0.10, curve))

    def test_deep_itm_blends_toward_patient_floor(self):
        T, secs, mp = -0.10, 120, 0.90
        d = (mp - 0.5) / 0.5
        floor = T * (1 + 0.5 * d)
        curve = ExitBoundary().compute_exit_threshold(secs, 0.07, mp)
        expected = (1 - d) * max(floor, curve) + d * min(floor, curve)
        assert effective_exit_threshold(T, secs, mp, fee_rate=0.07) == pytest.approx(expected)

    def test_mid_anchors_depth_but_curve_uses_trade_price(self):
        # A wide spread (mid 0.8, executable bid 0.6) must compute itm_depth from
        # the mid while the boundary curve still prices off the executable side.
        with_mid = effective_exit_threshold(-0.10, 120, 0.60, fee_rate=0.07,
                                            market_mid_for_side=0.80)
        without_mid = effective_exit_threshold(-0.10, 120, 0.60, fee_rate=0.07)
        assert with_mid != pytest.approx(without_mid)
