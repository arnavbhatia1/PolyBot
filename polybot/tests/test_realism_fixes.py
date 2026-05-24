"""Realism tests: convex slippage and the 60/40 hold-out split."""

import pytest
import pytest_asyncio
from unittest.mock import MagicMock

from polybot.execution.base import slippage_pct as _slippage_pct
from polybot.agents.scheduler import AgentScheduler


# ---------------------------------------------------------------------------
# Convex market impact model
# ---------------------------------------------------------------------------

class TestConvexSlippage:
    """_slippage_pct should use fill_pct * impact * (1 + fill_pct)."""

    def test_zero_depth_returns_zero(self):
        assert _slippage_pct(100, 0, 0.03) == 0.0

    def test_zero_order_returns_zero(self):
        assert _slippage_pct(0, 1000, 0.03) == 0.0

    def test_full_depth_gives_double_impact(self):
        """At 100% depth consumption, convex model gives 2x the base impact."""
        result = _slippage_pct(1000, 1000, 0.03)
        assert result == pytest.approx(0.06)  # 0.03 * 1.0 * (1 + 1.0) = 0.06

    def test_half_depth_gives_more_than_linear(self):
        """At 50% depth, convex > linear.  Linear would be 0.015."""
        result = _slippage_pct(500, 1000, 0.03)
        assert result == pytest.approx(0.0225)  # 0.5 * 0.03 * 1.5
        assert result > 0.015  # strictly more than linear

    def test_quarter_depth(self):
        result = _slippage_pct(250, 1000, 0.03)
        assert result == pytest.approx(0.25 * 0.03 * 1.25)

    def test_convexity_increases_with_fill(self):
        """Marginal cost should increase — verify convexity."""
        s10 = _slippage_pct(100, 1000, 0.03)
        s50 = _slippage_pct(500, 1000, 0.03)
        s90 = _slippage_pct(900, 1000, 0.03)
        # Cost per unit of fill should increase
        cost_per_unit_low = s10 / 0.10
        cost_per_unit_mid = s50 / 0.50
        cost_per_unit_high = s90 / 0.90
        assert cost_per_unit_mid > cost_per_unit_low
        assert cost_per_unit_high > cost_per_unit_mid

    def test_capped_at_full_depth(self):
        """Order larger than book should be capped at fill_pct=1.0."""
        result = _slippage_pct(2000, 1000, 0.03)
        assert result == pytest.approx(0.06)  # same as full depth

    def test_custom_impact_factor(self):
        result = _slippage_pct(500, 1000, 0.05)
        assert result == pytest.approx(0.5 * 0.05 * 1.5)


# ---------------------------------------------------------------------------
# Hold-out split (60/40 chronological)
# ---------------------------------------------------------------------------

class TestHoldoutSplit:
    """run_daily_pipeline must exclude the last HOLDOUT_DAYS from the evolver
    context (BiasDetector + TAEvolver) and the walk-forward optimizer, leaving
    a true OOS window for the holdout confirmation gate.

    Uses 400 recent outcomes so the holdout (~3 days near today) is large
    enough to engage AND the remaining opt pool clears MIN_TRADES_FOR_LEARNING.
    """

    @staticmethod
    def _make_outcomes(n, days_span=30):
        """n outcomes spread across the last `days_span` days ending today."""
        from datetime import datetime, timezone, timedelta
        end = datetime.now(timezone.utc)
        return [
            {"timestamp": (end - timedelta(days=days_span * (n - 1 - i) / max(n - 1, 1))).isoformat().replace("+00:00", "Z"),
             "correct": True, "gain_pct": 0.1, "log_return": 0.1,
             "indicator_snapshot": {}}
            for i in range(n)
        ]

    @pytest.mark.asyncio
    async def test_split_passes_train_to_bias_and_evolver(self):
        """BiasDetector and TAEvolver receive opt_outcomes (holdout excluded);
        the optimizer also gets opt_outcomes and holdout_outcomes separately."""
        received = {}

        async def mock_bias(outcomes=None):
            received["bias_count"] = len(outcomes) if outcomes else 0
            return {}

        async def mock_ta(analysis, outcomes=None):
            received["ta_count"] = len(outcomes) if outcomes else 0
            return {}

        async def mock_wo(recs, outcomes=None, **kwargs):
            received["wo_count"] = len(outcomes) if outcomes else 0
            received["wo_holdout"] = len(kwargs.get("holdout_outcomes") or [])
            return {"decision": "skipped"}

        # 400 outcomes spanning the last 30 days → ~40 in the trailing 3-day holdout.
        outcomes = self._make_outcomes(400, days_span=30)

        reviewer = MagicMock()
        reviewer.load_all_outcomes.return_value = outcomes

        scheduler = AgentScheduler(
            outcome_reviewer=reviewer, bias_detector=MagicMock(),
            ta_evolver=MagicMock(), weight_optimizer=MagicMock())
        scheduler._run_bias_detector = mock_bias
        scheduler._run_ta_evolver = mock_ta
        scheduler._run_weight_optimizer = mock_wo

        await scheduler.run_daily_pipeline()

        # Holdout must engage: bias / TA receive opt_outcomes (< 400), and the
        # optimizer sees opt + a non-empty holdout pool.
        assert received["bias_count"] < 400
        assert received["bias_count"] == received["ta_count"]
        assert received["wo_count"] == received["bias_count"]
        assert received["wo_holdout"] > 0
        assert received["wo_holdout"] + received["wo_count"] == 400

    @pytest.mark.asyncio
    async def test_split_is_chronological(self):
        """opt_outcomes (passed to BiasDetector + TAEvolver) must be the
        chronologically-older portion; holdout is the newest."""
        received = {}

        async def mock_bias(outcomes=None):
            received["bias_timestamps"] = [o["timestamp"] for o in (outcomes or [])]
            return {}

        async def mock_ta(analysis, outcomes=None):
            received["ta_timestamps"] = [o["timestamp"] for o in (outcomes or [])]
            return {}

        async def mock_wo(recs, outcomes=None, **kwargs):
            received["wo_timestamps"] = [o["timestamp"] for o in (outcomes or [])]
            received["holdout_timestamps"] = [o["timestamp"] for o in (kwargs.get("holdout_outcomes") or [])]
            return {"decision": "skipped"}

        outcomes = self._make_outcomes(400, days_span=30)

        reviewer = MagicMock()
        reviewer.load_all_outcomes.return_value = outcomes

        scheduler = AgentScheduler(
            outcome_reviewer=reviewer, bias_detector=MagicMock(),
            ta_evolver=MagicMock(), weight_optimizer=MagicMock())
        scheduler._run_bias_detector = mock_bias
        scheduler._run_ta_evolver = mock_ta
        scheduler._run_weight_optimizer = mock_wo

        await scheduler.run_daily_pipeline()

        # Each layer receives chronologically-sorted opt_outcomes.
        assert received["bias_timestamps"] == sorted(received["bias_timestamps"])
        assert received["ta_timestamps"] == received["bias_timestamps"]
        # Holdout entries are strictly newer than every opt entry.
        assert received["holdout_timestamps"]
        assert min(received["holdout_timestamps"]) >= max(received["bias_timestamps"])

    @pytest.mark.asyncio
    async def test_small_dataset_still_works(self):
        """With very few outcomes, pipeline skips learning but BiasDetector still runs."""
        received = {}

        async def mock_bias(outcomes=None):
            received["bias_count"] = len(outcomes) if outcomes else 0
            return {}

        async def mock_ta(analysis, outcomes=None):
            received["ta_called"] = True
            return {}

        async def mock_wo(recs, outcomes=None):
            received["wo_called"] = True

        outcomes = [
            {"timestamp": "2026-04-01T12:00:00Z", "correct": True,
             "gain_pct": 0.1, "log_return": 0.1,
             "indicator_snapshot": {}}
        ]

        reviewer = MagicMock()
        reviewer.load_all_outcomes.return_value = outcomes

        scheduler = AgentScheduler(
            outcome_reviewer=reviewer, bias_detector=MagicMock(),
            ta_evolver=MagicMock(), weight_optimizer=MagicMock())
        scheduler._run_bias_detector = mock_bias
        scheduler._run_ta_evolver = mock_ta
        scheduler._run_weight_optimizer = mock_wo

        await scheduler.run_daily_pipeline()

        # BiasDetector still runs (useful for monitoring)
        assert received["bias_count"] == 1
        # TAEvolver and WeightOptimizer skipped (< 50 trades)
        assert "ta_called" not in received
        assert "wo_called" not in received
