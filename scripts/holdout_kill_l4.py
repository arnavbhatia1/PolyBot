"""Hold-out test: would killing L4 (momentum_weight = 0) help or hurt?

For each trade in the last HOLDOUT_DAYS, reconstructs L4's logit contribution from
the stored indicator snapshot and subtracts it from model_probability_raw. Compares
log-loss and trading-impact (gate-conditional gain_pct) on the same trades.

Fair head-to-head: both sides are pre-calibration raw probabilities, so the calibrator
cannot favor one variant. The calibrator is monotone-preserving and would be re-fit
under either world.
"""
from __future__ import annotations

import glob
import json
import math
from datetime import datetime, timedelta, timezone

import yaml

HOLDOUT_DAYS = 3
OUTCOMES_GLOB = "polybot/memory/outcomes/*.json"
CONFIG_PATH = "polybot/config/settings.yaml"

# Match signal_engine.py constants
_REGIME_MOMENTUM_AMPLIFY = 1.5
_REGIME_MOMENTUM_DAMPEN = 0.5
_MOMENTUM_WEIGHT_CLAMP = 0.10


def load_cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f).get("signal", {})


def effective_momentum_weight(autocorr: float, threshold: float, momentum_weight: float) -> float:
    base = abs(momentum_weight)
    if threshold <= 0:
        t_abs = 0.0
    else:
        t_abs = abs(math.tanh(autocorr / threshold))
    mag = base * (_REGIME_MOMENTUM_DAMPEN + (_REGIME_MOMENTUM_AMPLIFY - _REGIME_MOMENTUM_DAMPEN) * t_abs)
    return min(_MOMENTUM_WEIGHT_CLAMP, mag)


def compute_momentum_score(snap: dict, weights: dict, autocorr: float, threshold: float) -> float:
    """Reproduce signal_engine.compute_momentum from stored indicator scores."""
    def _s(name: str) -> float:
        ind = snap.get(name, {})
        if not isinstance(ind, dict):
            return 0.0
        return ind.get("norm_score", ind.get("score", 0.0)) or 0.0

    mean_revert = (
        _s("rsi") * weights.get("rsi", 0.20)
        + _s("stochastic") * weights.get("stochastic", 0.20)
        + _s("vwap") * weights.get("vwap", 0.20)
    )
    trend_confirm = (
        _s("macd") * weights.get("macd", 0.25)
        + _s("obv") * weights.get("obv", 0.15)
    )
    t = math.tanh(autocorr / threshold) if threshold > 0 else 0.0
    mr_mult = -t + (1.0 - abs(t)) * _REGIME_MOMENTUM_DAMPEN
    tc_mult = _REGIME_MOMENTUM_DAMPEN + (1.0 - _REGIME_MOMENTUM_DAMPEN) * max(0.0, t)
    score = mr_mult * mean_revert + tc_mult * trend_confirm
    return max(-1.0, min(1.0, score))


def logit(p: float) -> float:
    p = max(1e-9, min(1.0 - 1e-9, p))
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_outcomes(holdout_days: int) -> tuple[list[dict], datetime]:
    files = sorted(glob.glob(OUTCOMES_GLOB))
    rows: list[dict] = []
    latest_ts: datetime | None = None
    for fp in files:
        try:
            raw = json.loads(open(fp).read())
        except Exception:
            continue
        recs = raw if isinstance(raw, list) else [raw]
        for r in recs:
            ts = parse_ts(r.get("exit_timestamp") or r.get("timestamp"))
            if ts is None:
                continue
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
            rows.append((ts, r))
    if not rows or latest_ts is None:
        return [], datetime.now(timezone.utc)
    cutoff = latest_ts - timedelta(days=holdout_days)
    holdout = [r for ts, r in rows if ts >= cutoff]
    return holdout, cutoff


def main() -> None:
    cfg = load_cfg()
    momentum_weight = float(cfg.get("momentum_weight", 0.04))
    regime_thr = float(cfg.get("regime_momentum_threshold", 0.15))
    logit_scale = float(cfg.get("logit_scale", 4.0))
    weights = cfg.get("weights", {})
    min_edge = float(cfg.get("min_edge", 0.04))
    min_prob = float(cfg.get("min_model_probability", 0.56))

    holdout, cutoff = load_outcomes(HOLDOUT_DAYS)
    print(f"Holdout cutoff: {cutoff.isoformat()}  |  trades in holdout: {len(holdout)}")
    print(f"Config: momentum_weight={momentum_weight}  threshold={regime_thr}  logit_scale={logit_scale}")
    if len(holdout) < 30:
        print(f"WARNING: < 30 trades in holdout — result will be noisy.")
    if not holdout:
        return

    sum_ll_current = 0.0
    sum_ll_killed = 0.0
    n = 0
    counter_results: list[dict] = []
    big_l4_helped = 0
    big_l4_hurt = 0
    big_l4_threshold = 0.10  # |L4 logit contribution| > 0.10

    for r in holdout:
        snap = r.get("indicator_snapshot", {})
        if not isinstance(snap, dict):
            continue
        tc = snap.get("trade_context", {})
        if not isinstance(tc, dict):
            continue
        side = r.get("side", "")
        raw = tc.get("model_probability_raw")
        if raw is None:
            continue
        autocorr = float(tc.get("regime_autocorr", 0.0))
        # Reconstruct L4 in logit space, signed for Up
        score_up_signed = compute_momentum_score(snap, weights, autocorr, regime_thr)
        eff_w = effective_momentum_weight(autocorr, regime_thr, momentum_weight)
        l4_logit_up = score_up_signed * eff_w * logit_scale
        # P_raw is for our chosen side; convert to side-signed L4 contribution
        l4_logit_for_side = l4_logit_up if side == "Up" else -l4_logit_up

        p_current = float(raw)
        z_current = logit(p_current)
        z_killed = z_current - l4_logit_for_side
        p_killed = sigmoid(z_killed)

        correct = 1 if r.get("correct") else 0
        ll_current = -(correct * math.log(max(1e-9, p_current)) + (1 - correct) * math.log(max(1e-9, 1 - p_current)))
        ll_killed = -(correct * math.log(max(1e-9, p_killed)) + (1 - correct) * math.log(max(1e-9, 1 - p_killed)))
        sum_ll_current += ll_current
        sum_ll_killed += ll_killed
        n += 1

        # Counterfactual gate: would killing L4 have changed the trade decision?
        market_for_side = (
            tc.get("market_price_up") if side == "Up" else tc.get("market_price_down")
        ) or r.get("entry_price")
        edge_current = p_current - market_for_side if market_for_side else 0.0
        edge_killed = p_killed - market_for_side if market_for_side else 0.0

        # Decision under each model
        passed_current = (p_current >= min_prob) and (edge_current >= min_edge)
        passed_killed = (p_killed >= min_prob) and (edge_killed >= min_edge)

        counter_results.append({
            "gain_pct": r.get("gain_pct", 0.0),
            "correct": correct,
            "p_current": p_current,
            "p_killed": p_killed,
            "l4_logit_for_side": l4_logit_for_side,
            "edge_current": edge_current,
            "edge_killed": edge_killed,
            "passed_current": passed_current,
            "passed_killed": passed_killed,
            "exit_reason": r.get("exit_reason", ""),
        })

        # Did L4 push us toward the right answer on big-L4 trades?
        if abs(l4_logit_for_side) > big_l4_threshold:
            current_correct = (p_current >= 0.5) == bool(correct)
            killed_correct = (p_killed >= 0.5) == bool(correct)
            if current_correct and not killed_correct:
                big_l4_helped += 1
            elif killed_correct and not current_correct:
                big_l4_hurt += 1

    if n == 0:
        print("No usable rows.")
        return

    ll_c = sum_ll_current / n
    ll_k = sum_ll_killed / n
    print()
    print(f"=== Probabilistic accuracy (head-to-head on same trades) ===")
    print(f"n = {n}")
    print(f"  log-loss current (with L4):    {ll_c:.5f}")
    print(f"  log-loss killed  (no L4):      {ll_k:.5f}")
    print(f"  delta (killed - current):      {ll_k - ll_c:+.5f}    {'(killing HURTS)' if ll_k > ll_c else '(killing HELPS or ties)'}")

    # Trading impact: gate-conditional gain_pct
    actual_taken = [c for c in counter_results if c["passed_current"]]
    cf_would_take = [c for c in counter_results if c["passed_killed"]]
    print()
    print(f"=== Trading impact (gate-conditional) ===")
    print(f"  trades passing current gates: {len(actual_taken)}")
    print(f"  trades passing killed-L4 gates: {len(cf_would_take)}")
    if actual_taken:
        wr_actual = sum(c["correct"] for c in actual_taken) / len(actual_taken)
        mean_gain_actual = sum(c["gain_pct"] for c in actual_taken) / len(actual_taken)
        print(f"  actual-taken:  WR={wr_actual:.3f}  mean_gain_pct={mean_gain_actual:+.4f}")
    if cf_would_take:
        wr_cf = sum(c["correct"] for c in cf_would_take) / len(cf_would_take)
        mean_gain_cf = sum(c["gain_pct"] for c in cf_would_take) / len(cf_would_take)
        print(f"  killed-L4 cf:  WR={wr_cf:.3f}  mean_gain_pct={mean_gain_cf:+.4f}")

    # Selection delta
    only_actual = [c for c in counter_results if c["passed_current"] and not c["passed_killed"]]
    only_killed = [c for c in counter_results if c["passed_killed"] and not c["passed_current"]]
    both = [c for c in counter_results if c["passed_current"] and c["passed_killed"]]
    print()
    print(f"  trades L4 PULLED IN (taken because of L4, dropped without):  n={len(only_actual)}")
    if only_actual:
        wr = sum(c["correct"] for c in only_actual) / len(only_actual)
        g = sum(c["gain_pct"] for c in only_actual) / len(only_actual)
        print(f"     WR={wr:.3f}  mean_gain_pct={g:+.4f}  {'(L4 pulled in WINNERS)' if g > 0 else '(L4 pulled in LOSERS)'}")
    print(f"  trades L4 PUSHED OUT (skipped because of L4, would take without): n={len(only_killed)}")
    if only_killed:
        wr = sum(c["correct"] for c in only_killed) / len(only_killed)
        g = sum(c["gain_pct"] for c in only_killed) / len(only_killed)
        print(f"     WR={wr:.3f}  mean_gain_pct={g:+.4f}  {'(L4 pushed out WINNERS)' if g > 0 else '(L4 pushed out LOSERS)'}")
    print(f"  trades both take:  n={len(both)}")

    # Big-L4 sub-analysis
    print()
    print(f"=== Trades where |L4 logit contribution| > {big_l4_threshold} ===")
    n_big = sum(1 for c in counter_results if abs(c["l4_logit_for_side"]) > big_l4_threshold)
    print(f"  n = {n_big}  ({100*n_big/len(counter_results):.1f}% of holdout)")
    print(f"  L4 changed direction of prediction in favor: {big_l4_helped}")
    print(f"  L4 changed direction of prediction against:  {big_l4_hurt}")

    # Verdict
    print()
    print("=== VERDICT ===")
    if ll_k <= ll_c:
        print(f"  Log-loss: killing L4 does NOT hurt probabilistic accuracy (delta {ll_k-ll_c:+.5f}).")
    else:
        print(f"  Log-loss: killing L4 HURTS probabilistic accuracy by {ll_k-ll_c:+.5f}.")
    if only_actual and only_killed:
        d_actual = sum(c["gain_pct"] for c in only_actual)
        d_killed = sum(c["gain_pct"] for c in only_killed)
        net = d_killed - d_actual  # positive means killed-world gains more pnl
        print(f"  PnL delta: killed-world would gain {d_killed:+.3f} from pulled-in trades, lose {d_actual:+.3f} from current pulled-in.")
        print(f"  Net selection impact of killing L4: {net:+.3f} aggregate gain_pct.")


if __name__ == "__main__":
    main()
