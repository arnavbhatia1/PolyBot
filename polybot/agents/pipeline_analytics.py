"""Pipeline analytics utilities: time weighting, distribution shift detection, SPRT aggregation."""
from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# For the recency decay used across backtest, calibratorfit, and rollback Sharpe.
RECENCY_DECAY_PER_DAY: float = 0.94


def sharpe(returns: list[float]) -> float:
    """Per-trade unannualized Sharpe from a list of gain_pct values.

    Population variance (divide by n, not n-1) for consistency with the
    weighted variant below. Returns 0.0 for n<2 or zero variance.
    """
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    var = sum((r - avg) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.0
    return avg / std if std > 0 else 0.0


def weighted_sharpe_from_returns(returns: list[float], weights: list[float]) -> float:
    """Per-trade Sharpe on a precomputed returns list with parallel sample weights.

    Equivalent to ``weighted_sharpe(outcomes, weights)`` but operates on a list of
    returns (e.g., Kelly-sized backtest outputs) rather than outcome dicts. Mean
    and variance both use the weights, so this is a proper weighted Sharpe — NOT
    Sharpe of (return × weight), which would inflate the variance with the
    weights' own dispersion.
    """
    if len(returns) < 2 or len(returns) != len(weights):
        return 0.0
    w_sum = sum(weights)
    if w_sum <= 0:
        return 0.0
    mean = sum(r * w for r, w in zip(returns, weights)) / w_sum
    var = sum(w * (r - mean) ** 2 for r, w in zip(returns, weights)) / w_sum
    std = math.sqrt(var) if var > 0 else 0.0
    return mean / std if std > 0 else 0.0


def utc_ts_to_et_date(ts: str) -> str:
    """Convert a UTC ISO timestamp string to an ET date string YYYY-MM-DD.

    Falls back to the leading 10 chars on parse failure so a malformed
    timestamp never crashes a daily rollup.
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if ts else ""


def compute_sample_weights(outcomes: list[dict[str, Any]]) -> list[float]:
    """Recency weights using the canonical RECENCY_DECAY_PER_DAY (0.94/day,
    ~11-day half-life). Single source of truth shared with the backtest and
    calibrator-fit weighting. Returns normalized weights summing to 1.0.
    """
    now = datetime.now(timezone.utc).timestamp()
    raw = []
    for o in outcomes:
        ts = o.get("timestamp", "")
        if not ts:
            raw.append(1.0)
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_days = max(0.0, (now - dt.timestamp()) / 86400)
            raw.append(RECENCY_DECAY_PER_DAY ** age_days)
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


def detect_distribution_shifts(train: list[dict[str, Any]],
                                test: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Two-sample Kolmogorov-Smirnov test on key feature distributions between
    train and test slices of the optimizer pool. Surfaced into the evolver's
    analysis dict so Claude sees whether the optimizer's test window looks like
    a different regime — purely informational, no veto. Noisy on small samples
    is acceptable; the signal stabilizes as the windows fill in.

    Returns ``{feature_name: {statistic, p_value, n_train, n_test}}``. Features
    with fewer than 30 non-null values in either side are skipped.
    """
    try:
        from scipy.stats import ks_2samp
    except ImportError:
        return {}

    def _ctx(o: dict[str, Any]) -> dict[str, Any]:
        return o.get("indicator_snapshot", {}).get("trade_context", {}) or {}

    features: list[tuple[str, Any]] = [
        ("model_probability_raw", lambda o: _ctx(o).get("model_probability_raw")),
        ("atr",                   lambda o: _ctx(o).get("atr")),
        ("regime_autocorr",       lambda o: _ctx(o).get("regime_autocorr")),
        ("market_price_up",       lambda o: _ctx(o).get("market_price_up")),
    ]

    out: dict[str, dict[str, Any]] = {}
    for name, getter in features:
        a = [v for v in (getter(o) for o in train) if v is not None]
        b = [v for v in (getter(o) for o in test) if v is not None]
        if len(a) < 30 or len(b) < 30:
            continue
        try:
            stat, p = ks_2samp(a, b)
        except Exception:
            continue
        out[name] = {
            "statistic": round(float(stat), 4),
            "p_value": round(float(p), 4),
            "n_train": len(a),
            "n_test": len(b),
        }
    return out


def aggregate_sprt_evidence(outcomes: list[dict[str, Any]], recent_n: int = 50) -> dict[str, Any]:
    """Aggregate SPRT signal-aggressiveness from recent trade_context.

    Measures how often the per-trade SPRT accumulator said ENTER — i.e., how
    aggressively the model has been firing. Does NOT measure win rate or
    edge realization. Renamed labels (was 'positive'/'negative'/'inconclusive')
    to stop downstream consumers reading 'negative' as 'performance is bad'
    when it actually means 'few ENTER signals recently'.

    Returns dict with:
      state: 'aggressive' | 'passive' | 'inconclusive'
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
        state = "aggressive"
    elif enter_pct <= 0.3 or avg_conf < 0.2:
        state = "passive"
    else:
        state = "inconclusive"

    return {"state": state, "avg_confidence": round(avg_conf, 3), "enter_pct": round(enter_pct, 3)}


def format_trends(outcomes: list[dict[str, Any]], n_buckets: int = 5,
                  min_per_bucket: int = 50) -> str:
    """Compute trend trajectory of key metrics over the last N buckets of trades.

    Splits the most recent outcomes into n_buckets chronological slices of equal
    size, computes per-bucket WR / Sharpe / Q4 edge realization, and returns a
    markdown summary with trend labels (IMPROVING / STABLE / DEGRADING) so Claude
    can see whether a metric is already self-resolving and avoid proposing fixes
    for it. Returns empty string if insufficient data.
    """
    if not outcomes:
        return ""

    # Sort by exit timestamp so buckets are chronologically clean
    sorted_o = sorted(outcomes, key=lambda o: o.get("exit_timestamp", o.get("timestamp", "")))
    bucket_size = max(min_per_bucket, len(sorted_o) // n_buckets)
    if bucket_size * n_buckets > len(sorted_o):
        # Not enough data for the requested bucketization
        n_buckets = max(2, len(sorted_o) // bucket_size)
        if n_buckets < 2:
            return ""

    # Use the LAST n_buckets * bucket_size trades, split into n_buckets equal slices
    needed = bucket_size * n_buckets
    recent = sorted_o[-needed:]

    buckets: list[dict[str, Any]] = []
    for i in range(n_buckets):
        start = i * bucket_size
        end = (i + 1) * bucket_size
        bucket = recent[start:end]
        if not bucket:
            continue

        wins = sum(1 for o in bucket if o.get("correct"))
        wr = wins / len(bucket)
        gains = [float(o.get("gain_pct", 0) or 0) for o in bucket]
        mean_g = sum(gains) / len(gains)
        var_g = sum((g - mean_g) ** 2 for g in gains) / len(gains) if len(gains) > 1 else 0
        std_g = math.sqrt(var_g) if var_g > 0 else 1.0
        sharpe = mean_g / std_g if std_g > 0 else 0.0

        # Q4 edge realization — top quartile of trades by signal_prob - market_price
        edge_gain_pairs: list[tuple[float, float]] = []
        for o in bucket:
            ctx = o.get("indicator_snapshot", {}).get("trade_context", {}) or {}
            p = ctx.get("model_probability_raw", ctx.get("model_probability", 0)) or 0
            side = (o.get("side") or "").lower()
            mp = ctx.get(f"market_price_{side}", 0) or 0
            if p > 0 and mp > 0:
                edge_gain_pairs.append((float(p) - float(mp), float(o.get("gain_pct", 0) or 0)))
        q4_realization: float | None = None
        if len(edge_gain_pairs) >= 8:
            edge_gain_pairs.sort(key=lambda x: x[0])
            q4 = edge_gain_pairs[-max(1, len(edge_gain_pairs) // 4):]
            q4_pred = sum(e for e, _ in q4) / len(q4)
            q4_actual = sum(g for _, g in q4) / len(q4)
            if q4_pred > 0:
                q4_realization = q4_actual / q4_pred

        buckets.append({
            "n": len(bucket),
            "wr": wr,
            "mean_gain": mean_g,
            "sharpe": sharpe,
            "q4_realization": q4_realization,
        })

    if len(buckets) < 2:
        return ""

    def _trend(values: list[float], noise: float) -> str:
        """Compare last-half average to first-half average; label as IMPROVING/STABLE/DEGRADING."""
        if len(values) < 2:
            return "—"
        half = len(values) // 2
        first = sum(values[:half]) / half if half else 0
        last = sum(values[-half:]) / half if half else 0
        delta = last - first
        if abs(delta) < noise:
            return "STABLE"
        return "IMPROVING" if delta > 0 else "DEGRADING"

    def _row(label: str, values: list[float], fmt: str, noise: float) -> str:
        progression = " -> ".join(fmt.format(v) for v in values)
        delta = values[-1] - values[0] if len(values) >= 2 else 0
        return f"- **{label}**: {progression}  [d={delta:+.4f}, {_trend(values, noise)}]"

    lines = [
        f"## Recent Trends (last {sum(b['n'] for b in buckets)} trades in "
        f"{len(buckets)} chronological buckets of {bucket_size})",
        "If a metric is IMPROVING over these buckets, it is self-resolving — "
        "do NOT propose parameter changes that target it. Doing so risks adopting "
        "noise that reverses the natural improvement.",
        "",
    ]
    lines.append(_row("Win rate", [b["wr"] for b in buckets], "{:.1%}", noise=0.02))
    lines.append(_row("Mean gain_pct", [b["mean_gain"] for b in buckets], "{:+.4f}", noise=0.005))
    lines.append(_row("Sharpe", [b["sharpe"] for b in buckets], "{:+.3f}", noise=0.05))

    q4_vals = [b["q4_realization"] for b in buckets if b["q4_realization"] is not None]
    if len(q4_vals) >= 2:
        lines.append(_row("Q4 edge realization", q4_vals, "{:.2f}", noise=0.05))

    return "\n".join(lines)
