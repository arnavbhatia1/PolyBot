"""Pipeline analytics utilities: time weighting, distribution shift detection, SPRT aggregation."""
from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def compute_sample_weights(outcomes: list[dict[str, Any]], half_life_days: float = 14.0) -> list[float]:
    """Exponential time-decay weights. Yesterday = 2x weight of 14 days ago.

    Uses outcome['timestamp'] (UTC ISO string). Returns normalized weights summing to 1.0.
    """
    now = datetime.now(timezone.utc).timestamp()
    decay = math.log(2) / max(half_life_days, 0.1)
    raw = []
    for o in outcomes:
        ts = o.get("timestamp", "")
        if not ts:
            raw.append(1.0)
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_days = (now - dt.timestamp()) / 86400
            raw.append(math.exp(-age_days * decay))
        except (ValueError, OSError):
            raw.append(1.0)
    total = sum(raw) or 1.0
    return [w / total for w in raw]


def weighted_win_rate(outcomes: list[dict[str, Any]], weights: list[float]) -> float:
    """Win rate weighted by sample weights."""
    num = sum(w for o, w in zip(outcomes, weights) if o.get("correct", False))
    den = sum(weights)
    return num / den if den > 0 else 0.0


def weighted_sharpe(outcomes: list[dict[str, Any]], weights: list[float]) -> float:
    """Per-trade Sharpe weighted by sample weights."""
    gains = []
    ws = []
    for o, w in zip(outcomes, weights):
        gp = o.get("gain_pct", 0)
        gains.append(gp)
        ws.append(w)
    if len(gains) < 2:
        return 0.0
    w_sum = sum(ws)
    if w_sum <= 0:
        return 0.0
    mean = sum(g * w for g, w in zip(gains, ws)) / w_sum
    var = sum(w * (g - mean) ** 2 for g, w in zip(gains, ws)) / w_sum
    std = math.sqrt(var) if var > 0 else 0.0
    return mean / std if std > 0 else 0.0


def detect_distribution_shift(recent: list[dict[str, Any]], historical: list[dict[str, Any]],
                               features: list[str] | None = None,
                               significance: float = 0.05) -> dict[str, Any]:
    """Two-sample Kolmogorov-Smirnov test on key features.

    Lightweight implementation (no scipy dependency) using the KS statistic
    and the asymptotic p-value approximation.

    Returns dict mapping feature -> {statistic, p_value, shifted} for features
    that show significant shift.
    """
    if features is None:
        features = ["edge", "atr", "model_probability", "seconds_remaining"]

    results = {}
    for feat in features:
        recent_vals = _extract_feature(recent, feat)
        hist_vals = _extract_feature(historical, feat)
        if len(recent_vals) < 10 or len(hist_vals) < 10:
            continue

        ks_stat = _ks_statistic(recent_vals, hist_vals)
        n1, n2 = len(recent_vals), len(hist_vals)
        # Asymptotic p-value: P(D > observed) ≈ 2*exp(-2*(n_eff)*D^2)
        n_eff = (n1 * n2) / (n1 + n2)
        lambda_val = (math.sqrt(n_eff) + 0.12 + 0.11 / math.sqrt(n_eff)) * ks_stat
        p_value = max(0.0, min(1.0, 2.0 * math.exp(-2.0 * lambda_val ** 2)))

        shifted = p_value < significance
        if shifted:
            results[feat] = {
                "statistic": round(ks_stat, 4),
                "p_value": round(p_value, 4),
                "shifted": True,
                "recent_mean": round(sum(recent_vals) / len(recent_vals), 4),
                "hist_mean": round(sum(hist_vals) / len(hist_vals), 4),
            }
            logger.warning(f"Distribution shift in {feat}: KS={ks_stat:.3f} p={p_value:.3f}")

    return results


def _extract_feature(outcomes: list[dict[str, Any]], feature: str) -> list[float]:
    """Extract a numeric feature from trade_context."""
    vals = []
    for o in outcomes:
        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
        v = ctx.get(feature, 0)
        if isinstance(v, (int, float)) and v is not None:
            vals.append(float(v))
    return vals


def _ks_statistic(sample1: list[float], sample2: list[float]) -> float:
    """Two-sample KS statistic (max absolute difference between ECDFs)."""
    s1 = sorted(sample1)
    s2 = sorted(sample2)
    n1, n2 = len(s1), len(s2)

    # Merge and walk both sorted arrays
    all_vals = sorted(set(s1 + s2))
    max_diff = 0.0
    i1 = i2 = 0
    for v in all_vals:
        while i1 < n1 and s1[i1] <= v:
            i1 += 1
        while i2 < n2 and s2[i2] <= v:
            i2 += 1
        ecdf1 = i1 / n1
        ecdf2 = i2 / n2
        max_diff = max(max_diff, abs(ecdf1 - ecdf2))
    return max_diff


def aggregate_sprt_evidence(outcomes: list[dict[str, Any]], recent_n: int = 50) -> dict[str, Any]:
    """Aggregate SPRT state from recent trade_context.

    Returns dict with:
      state: 'positive' | 'negative' | 'inconclusive'
      avg_confidence: float (0-1)
      enter_pct: fraction of trades where SPRT said ENTER
    """
    recent = outcomes[-recent_n:] if len(outcomes) > recent_n else outcomes

    enter_count = 0
    skip_count = 0
    confidences = []

    for o in recent:
        ctx = o.get("indicator_snapshot", {}).get("trade_context", {})
        sprt_status = ctx.get("sprt_status", "")
        sprt_confidence = ctx.get("sprt_confidence", 0)

        if sprt_status == "ENTER":
            enter_count += 1
        elif sprt_status == "SKIP":
            skip_count += 1
        if sprt_confidence > 0:
            confidences.append(sprt_confidence)

    total = enter_count + skip_count
    enter_pct = enter_count / total if total > 0 else 0.5
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    if enter_pct >= 0.6 and avg_conf >= 0.5:
        state = "positive"
    elif enter_pct <= 0.3 or avg_conf < 0.2:
        state = "negative"
    else:
        state = "inconclusive"

    return {"state": state, "avg_confidence": round(avg_conf, 3), "enter_pct": round(enter_pct, 3)}
