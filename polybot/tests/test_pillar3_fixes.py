"""Pillar 3 learning-loop fixes — one focused test per closure."""
from __future__ import annotations

import json
import math
import time
import types

import numpy as np
import pytest

from polybot.agents.recommender_base import (
    EXPLORE_STEPS,
    STRUCTURAL_PROBES,
    BaseRecommender,
    empirical_noise_floor,
    _RAMP_NOISE_FLOOR_FALLBACK,
)
from polybot.agents.weight_optimizer import WeightOptimizer
from polybot.core.calibrator import IsotonicCalibrator


# ---- 3.1 — recovery from negative baseline ----

def test_weight_optimizer_allows_recovery_from_negative_baseline():
    opt = WeightOptimizer()
    adopt, reason, z = opt.should_adopt(-0.05, 0.08, n_trades=200)
    assert adopt is True
    assert z > 0.3


def test_weight_optimizer_blocks_outright_collapse():
    opt = WeightOptimizer()
    adopt, reason, _z = opt.should_adopt(0.10, -0.20, n_trades=200)
    assert adopt is False
    assert "abs floor" in reason


# ---- 3.4 — empirical noise floor tracks JK_SE ----

def test_empirical_noise_floor_scales_with_baseline_se():
    assert empirical_noise_floor(None) == _RAMP_NOISE_FLOOR_FALLBACK
    assert empirical_noise_floor(0.020) == pytest.approx(0.3 * 0.020)
    # Floor at the fallback when JK_SE is tiny
    assert empirical_noise_floor(0.001) == _RAMP_NOISE_FLOOR_FALLBACK


# ---- 3.5 — `exit_edge_threshold` in EXPLORE_STEPS ----

def test_exit_edge_threshold_is_an_explore_step():
    assert "exit_edge_threshold" in EXPLORE_STEPS
    assert EXPLORE_STEPS["exit_edge_threshold"] > 0


# ---- 3.6 — calibrator surfaces fit diagnostics ----

def test_calibrator_publishes_fit_diagnostics_on_reject():
    cal = IsotonicCalibrator()
    rng = np.random.default_rng(0)
    probs = rng.uniform(0.4, 0.6, 200).tolist()
    outcomes = rng.integers(0, 2, 200).tolist()
    cal.fit(probs, outcomes, min_samples=150)
    d = cal.last_fit_diagnostics
    assert d.get("decision") in ("rejected_ci", "adopted")
    assert "oob_ci_lower_nats" in d
    assert "oob_ci_median_nats" in d
    assert d["bootstrap_n_completed"] > 0


# ---- 3.7 — crisis trigger has trailing-3-day branch ----

def test_crisis_trailing_3d_branch_present():
    """Scheduler crisis math uses `_trailing_3d_sharpe < 0.0` as an OR condition."""
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert "_trailing_3d_sharpe" in src
    assert "or (len(_trailing_gains) >= 20 and _trailing_3d_sharpe < 0.0)" in src


# ---- 3.8 — holdout state is logged ----

def test_holdout_inactive_logged():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert 'pipeline_info["holdout_active"] = False' in src
    assert 'pipeline_info["holdout_active"] = True' in src


# ---- 3.13 — holdout adoption margin scales with holdout JK_SE ----

def test_holdout_adoption_margin_scales_with_jk_se():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert "HOLDOUT_ADOPTION_MARGIN = max(0.02, _ZF * _holdout_se)" in src


# ---- 3.14 — MIN_REGIME_N lowered ----

def test_min_regime_n_lowered():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert "MIN_REGIME_N = 8" in src


# ---- 3.16 — baseline cache invalidated after revert ----

def test_revert_invalidates_baseline_cache():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    # The line follows the reverted_any save.
    idx = src.find("self.pipeline_tracker._save(records)")
    assert idx > 0
    after = src[idx: idx + 200]
    assert "_invalidate_baseline_cache" in after


# ---- 3.17 — bias_detector ghost gain_pct uses market_price ----

def test_bias_detector_uses_market_price_for_ghost_gain():
    from polybot.agents.bias_detector import BiasDetector
    import inspect
    src = inspect.getsource(BiasDetector.analyze_ghosts)
    assert "_market_price_gain" in src
    assert "market_price_up" in src and "market_price_down" in src


# ---- 3.18 — pipeline_tracker prefers exit_timestamp ----

def test_pipeline_tracker_prefers_exit_timestamp():
    from pathlib import Path
    src = Path("polybot/agents/pipeline_tracker.py").read_text(encoding="utf-8")
    assert 'o.get("exit_timestamp") or o.get("timestamp", "")' in src


# ---- 3.2 — L6 old_value lookup branch ----

def test_l6_old_value_branch_present():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert 'param.startswith("derived_") and param.endswith("_weight")' in src
    assert "self.signal_engine.derived_weights.get(_fname)" in src


# ---- 3.3 — candidate_sharpe always recorded ----

def test_candidate_sharpe_always_recorded():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    idx = src.find("Always record diagnostic fields up front")
    assert idx > 0, "early-record block missing"
    # Below the block, the `<10 trades` branch must not reset candidate_sharpe.
    after = src[idx: idx + 1000]
    assert '"candidate_sharpe": round(candidate_sharpe, 4)' in after


# ---- 3.22 — baseline-cache double-set removed ----

def test_no_duplicate_baseline_kelly_sharpe_set():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    # Should appear in __init__ (None), precompute (line 376-ish), and _run_weight_optimizer's
    # initial cache (line 1251). The trailing duplicate at the end of _run_weight_optimizer is gone.
    occurrences = src.count("self._baseline_kelly_sharpe = round(current_sharpe, 4)")
    assert occurrences == 2  # precompute + per-cycle cache; no duplicate at function tail


# ---- 3.23 — per-bootstrap weight renormalization ----

def test_calibrator_renormalizes_weights_per_bootstrap():
    from pathlib import Path
    src = Path("polybot/core/calibrator.py").read_text(encoding="utf-8")
    assert "w_b_norm = w_b / w_b.sum() * len(w_b)" in src
    assert "w_oob_norm = w_oob / w_oob.sum() * len(w_oob)" in src


# ---- 3.15 — dropped ghost logging ----

def test_dropped_ghost_logged():
    from pathlib import Path
    src = Path("polybot/agents/scheduler.py").read_text(encoding="utf-8")
    assert "ghost dropped:" in src


# ---- 3.4(3) — L1-trio larger steps ----

def test_l1_trio_explore_steps_widened():
    assert EXPLORE_STEPS["atr_sigma_ratio"] == 0.15
    assert EXPLORE_STEPS["min_atr"] == 3.0


# ---- 3.5 — structural probes for exit_edge_threshold ----

def test_structural_probes_include_exit_edge_threshold_sweep():
    sweep = [v for (p, v, _r) in STRUCTURAL_PROBES if p == "exit_edge_threshold"]
    assert sorted(sweep) == [-0.08, -0.05, -0.03]


# ---- 3.4(2) — structural probes turn on never-adopted L6 features ----

def test_structural_probes_force_l6_turn_on():
    l6 = {p for (p, _v, _r) in STRUCTURAL_PROBES if p.startswith("derived_") and p.endswith("_weight")}
    # flow_disagreement is the only L6 weight already at non-zero default.
    # The other three should appear in structural probes.
    assert "derived_log_atr_ratio_weight" in l6
    assert "derived_autocorr_signed_mag_weight" in l6
    assert "derived_liq_signed_sqrt_weight" in l6


# ---- Structural-probe firing logic ----

class _Probe(BaseRecommender):
    SOURCE_NAME = "probe"

    def recommend(self):
        self._rule_structural_probes()
        return self._finalize()


def test_structural_probe_fires_when_no_evidence():
    cfg = {"exit_edge_threshold": -0.10, "derived_log_atr_ratio_weight": 0.0,
           "derived_autocorr_signed_mag_weight": 0.0, "derived_liq_signed_sqrt_weight": 0.0}
    rec = _Probe({}, cfg)
    rec._rule_structural_probes()
    proposed = {p["param"] for p in rec.proposals}
    assert "exit_edge_threshold" in proposed
    assert "derived_log_atr_ratio_weight" in proposed


def test_structural_probe_skips_when_value_matches_live():
    cfg = {"exit_edge_threshold": -0.08, "derived_log_atr_ratio_weight": 0.005,
           "derived_autocorr_signed_mag_weight": 0.0, "derived_liq_signed_sqrt_weight": 0.0}
    rec = _Probe({}, cfg)
    rec._rule_structural_probes()
    proposed = [(p["param"], p["value"]) for p in rec.proposals]
    # The -0.08 exit_edge probe should be skipped (matches live config).
    assert ("exit_edge_threshold", -0.08) not in proposed
    # But -0.05 and -0.03 still propose.
    assert ("exit_edge_threshold", -0.05) in proposed


def test_structural_probe_skips_failed_values():
    cfg = {"exit_edge_threshold": -0.10}
    analysis = {"cumulative_failures": {"exit_edge_threshold": ["-0.08 (Δ=-0.015)"]}}
    rec = _Probe(analysis, cfg)
    rec._rule_structural_probes()
    proposed = [(p["param"], p["value"]) for p in rec.proposals]
    assert ("exit_edge_threshold", -0.08) not in proposed
    # -0.05 still proposes (not in failures).
    assert ("exit_edge_threshold", -0.05) in proposed
