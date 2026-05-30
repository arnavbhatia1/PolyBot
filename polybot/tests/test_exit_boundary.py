from polybot.core.exit_boundary import ExitBoundary


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

    def test_threshold_always_negative(self):
        eb = ExitBoundary()
        for secs in [10, 30, 60, 120, 180, 240, 300]:
            t = eb.compute_exit_threshold(secs)
            assert t < 0  # always negative (some adverse edge tolerated)
