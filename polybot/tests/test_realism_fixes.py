"""Tests for the 5 realism fixes: convex slippage, price-sum gate, net-edge gate,
min-trade-count, and hold-out split."""

import pytest
import pytest_asyncio
from unittest.mock import MagicMock

# Import the slippage function from main
from polybot.main import _slippage_pct
from polybot.agents.scheduler import AgentScheduler


# ---------------------------------------------------------------------------
# Fix 5: Convex market impact model
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
# Fix 4: Hold-out split (60/40 chronological)
# ---------------------------------------------------------------------------

class TestHoldoutSplit:
    """run_daily_pipeline should split outcomes 60/40 chronologically."""

    @pytest.mark.asyncio
    async def test_split_passes_train_to_bias_and_evolver(self):
        """BiasDetector and TAEvolver should receive the first 60% of outcomes."""
        received = {}

        async def mock_bias(outcomes=None):
            received["bias_count"] = len(outcomes) if outcomes else 0
            return {}

        async def mock_ta(analysis, outcomes=None):
            received["ta_count"] = len(outcomes) if outcomes else 0
            return {}

        async def mock_wo(recs, outcomes=None):
            received["wo_count"] = len(outcomes) if outcomes else 0

        outcomes = [
            {"timestamp": f"2026-04-{i:02d}T12:00:00Z", "correct": True,
             "gain_pct": 0.1, "log_return": 0.1, "weight_version": "v1",
             "indicator_snapshot": {}}
            for i in range(1, 11)  # 10 outcomes
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

        assert received["bias_count"] == 6   # 60% of 10
        assert received["ta_count"] == 6
        assert received["wo_count"] == 4     # 40% of 10

    @pytest.mark.asyncio
    async def test_split_is_chronological(self):
        """Train set should contain the oldest outcomes, validation the newest."""
        received = {}

        async def mock_bias(outcomes=None):
            received["bias_timestamps"] = [o["timestamp"] for o in (outcomes or [])]
            return {}

        async def mock_ta(analysis, outcomes=None):
            return {}

        async def mock_wo(recs, outcomes=None):
            received["wo_timestamps"] = [o["timestamp"] for o in (outcomes or [])]

        outcomes = [
            {"timestamp": f"2026-04-{i:02d}T12:00:00Z", "correct": True,
             "gain_pct": 0.1, "log_return": 0.1, "weight_version": "v1",
             "indicator_snapshot": {}}
            for i in range(1, 11)
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

        # Train = first 6 (oldest), validation = last 4 (newest)
        assert received["bias_timestamps"][-1] < received["wo_timestamps"][0]

    @pytest.mark.asyncio
    async def test_small_dataset_still_works(self):
        """With very few outcomes, split should not crash."""
        received = {}

        async def mock_bias(outcomes=None):
            received["bias_count"] = len(outcomes) if outcomes else 0
            return {}

        async def mock_ta(analysis, outcomes=None):
            return {}

        async def mock_wo(recs, outcomes=None):
            received["wo_count"] = len(outcomes) if outcomes else 0

        outcomes = [
            {"timestamp": "2026-04-01T12:00:00Z", "correct": True,
             "gain_pct": 0.1, "log_return": 0.1, "weight_version": "v1",
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

        # With 1 outcome: split_idx = max(1, int(0.6)) = 1, so train=1, val=0
        # validation falls back to all_outcomes when empty
        assert received["bias_count"] == 1
