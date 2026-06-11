"""Phase 3 exit-value model: two heads trained nightly on the window-path stream.

Head 1: P(window resolves Up | state) — calibrated probability for exit valuation.
Head 2: E[own-side best bid 60s ahead − bid now] — one-leg drift for exit timing.

Small by design: pure-numpy logistic / ridge regression with recency weighting —
the effective sample is windows (~288/day), not ticks, and the model must stay
interpretable. Artifacts are JSON on disk; the nightly job logs Brier/MAE and
refuses to overwrite the served artifact when either degrades >15% vs the
trailing average (alert + keep previous). DEPLOYMENT IS KILL-BAR-GATED: the
bot does not consume the artifact until the 5-day shadow comparison against
ExitBoundary passes through the counterfactual replay (tasks/todo.md Phase 3).
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from polybot.paths import MEMORY_DIR

logger = logging.getLogger(__name__)

MODEL_DIR: Path = MEMORY_DIR / "exit_model"
ARTIFACT_PATH: Path = MODEL_DIR / "exit_model.json"
METRICS_PATH: Path = MODEL_DIR / "metrics_history.json"

# Operator amendment 2026-06-11 (hard 06-22 deadline): first fit at ~4 days of
# labels instead of 7 — ~800 windows is sufficient for a 13-feature logistic,
# and the 5-day kill-bar shadow still gates deployment.
MIN_DAYS_OF_LABELS = 4
MIN_LABELED_WINDOWS = MIN_DAYS_OF_LABELS * 200
RECENCY_HALF_LIFE_DAYS = 11.0
DEGRADE_TOLERANCE = 0.15            # >15% worse than 7-run trailing avg → keep previous
DRIFT_HORIZON_S = 60.0

FEATURES = [
    "bid_up", "ask_up", "bid_down", "ask_down",
    "spread_up", "spread_down",
    "depth3_bid_up", "depth3_ask_up", "depth3_bid_down", "depth3_ask_down",
    "coinbase_minus_strike", "elapsed_s",
]


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit_logistic(X: np.ndarray, y: np.ndarray, w: np.ndarray,
                  l2: float = 1e-3, iters: int = 500, lr: float = 0.5) -> np.ndarray:
    beta = np.zeros(X.shape[1])
    wn = w / w.sum()
    for _ in range(iters):
        p = _sigmoid(X @ beta)
        grad = X.T @ (wn * (p - y)) + l2 * beta
        beta -= lr * grad
    return beta

def _fit_ridge(X: np.ndarray, y: np.ndarray, w: np.ndarray, l2: float = 1e-2) -> np.ndarray:
    W = np.diag(w / w.sum())
    A = X.T @ W @ X + l2 * np.eye(X.shape[1])
    return np.linalg.solve(A, X.T @ W @ y)


async def load_training_frame(db: Any, drift_horizon_s: float = DRIFT_HORIZON_S
                              ) -> dict[str, np.ndarray] | None:
    """Rows = 1 Hz samples of labeled windows; columns = FEATURES + labels.

    window_paths lives in its own gitignored DB (recording.PATHS_DB) — ATTACHed
    here; window_labels lives in the per-mode DB. The drift label pairs each
    sample with the same window's sample ~drift_horizon_s later.
    """
    from polybot.recording import PATHS_DB
    await db.conn.execute("ATTACH DATABASE ? AS paths", (str(PATHS_DB),))
    try:
        cur = await db.conn.execute("""
            SELECT p.window_id, p.ts, p.elapsed_s, p.bid_up, p.ask_up, p.bid_down,
                   p.ask_down, p.depth3_bid_up, p.depth3_ask_up, p.depth3_bid_down,
                   p.depth3_ask_down, p.coinbase_price, p.strike, l.resolved_up
            FROM paths.window_paths p JOIN window_labels l ON l.window_id = p.window_id
            WHERE p.bid_up IS NOT NULL AND p.ask_up IS NOT NULL
              AND p.bid_down IS NOT NULL AND p.ask_down IS NOT NULL
              AND p.coinbase_price IS NOT NULL AND p.strike IS NOT NULL
            ORDER BY p.window_id, p.ts
        """)
        rows = await cur.fetchall()
    finally:
        await db.conn.execute("DETACH DATABASE paths")
    if not rows:
        return None

    feats, y_up, drift_up, ts_arr = [], [], [], []
    by_window: dict[str, list] = {}
    for r in rows:
        by_window.setdefault(r["window_id"], []).append(r)
    for samples in by_window.values():
        n = len(samples)
        for i, r in enumerate(samples):
            # forward bid for the drift head: first sample >= horizon ahead
            fwd = None
            for j in range(i + 1, n):
                if samples[j]["ts"] - r["ts"] >= drift_horizon_s:
                    fwd = samples[j]
                    break
            feats.append([
                r["bid_up"], r["ask_up"], r["bid_down"], r["ask_down"],
                r["ask_up"] - r["bid_up"], r["ask_down"] - r["bid_down"],
                r["depth3_bid_up"] or 0.0, r["depth3_ask_up"] or 0.0,
                r["depth3_bid_down"] or 0.0, r["depth3_ask_down"] or 0.0,
                r["coinbase_price"] - r["strike"], r["elapsed_s"],
            ])
            y_up.append(float(r["resolved_up"]))
            drift_up.append((fwd["bid_up"] - r["bid_up"]) if fwd is not None else np.nan)
            ts_arr.append(r["ts"])
    return {
        "X": np.asarray(feats, dtype=float),
        "y_up": np.asarray(y_up, dtype=float),
        "drift_up": np.asarray(drift_up, dtype=float),
        "ts": np.asarray(ts_arr, dtype=float),
        "n_windows": len(by_window),
    }


def fit(frame: dict[str, np.ndarray]) -> dict[str, Any]:
    X, y, drift, ts = frame["X"], frame["y_up"], frame["drift_up"], frame["ts"]
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Xn = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])

    age_days = (ts.max() - ts) / 86400.0
    w = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)

    # Chronological 80/20 split for honest metrics.
    cut = int(len(Xn) * 0.8)
    beta_p = _fit_logistic(Xn[:cut], y[:cut], w[:cut])
    p_hat = _sigmoid(Xn[cut:] @ beta_p)
    brier = float(np.mean((p_hat - y[cut:]) ** 2))
    # Market-implied baseline: mid of the Up book.
    mid_up = (X[cut:, 0] + X[cut:, 1]) / 2.0
    brier_market = float(np.mean((mid_up - y[cut:]) ** 2))

    has_drift = ~np.isnan(drift)
    beta_d = _fit_ridge(Xn[:cut][has_drift[:cut]], drift[:cut][has_drift[:cut]],
                        w[:cut][has_drift[:cut]])
    d_hat = Xn[cut:][has_drift[cut:]] @ beta_d
    mae = float(np.mean(np.abs(d_hat - drift[cut:][has_drift[cut:]]))) if has_drift[cut:].any() else float("nan")
    mae_martingale = float(np.mean(np.abs(drift[cut:][has_drift[cut:]]))) if has_drift[cut:].any() else float("nan")

    return {
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "features": FEATURES,
        "norm_mu": mu.tolist(), "norm_sd": sd.tolist(),
        "beta_prob_up": beta_p.tolist(),
        "beta_drift_up": beta_d.tolist(),
        "drift_horizon_s": DRIFT_HORIZON_S,
        "n_samples": int(len(Xn)), "n_windows": int(frame["n_windows"]),
        "brier_oos": round(brier, 5),
        "brier_market_baseline": round(brier_market, 5),
        "drift_mae_oos": round(mae, 5),
        "drift_mae_martingale": round(mae_martingale, 5),
        "deployed": False,  # flips only when the Phase 3 kill bar passes
    }


def _metrics_history() -> list[dict[str, Any]]:
    try:
        return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def nightly_refit_job(db: Any):
    """Returns the coroutine the NightlyScheduler runs. Trains when enough
    labeled windows exist; logs Brier/MAE; keeps the previous artifact when
    metrics degrade >15% vs the trailing-7 average."""
    async def _job() -> dict[str, Any]:
        frame = await load_training_frame(db)
        if frame is None or frame["n_windows"] < MIN_LABELED_WINDOWS:
            n = 0 if frame is None else frame["n_windows"]
            return {"status": "waiting_for_data", "labeled_windows": n,
                    "needed": MIN_LABELED_WINDOWS}
        if len(frame["X"]) < MIN_LABELED_WINDOWS:
            return {"status": "waiting_for_data", "samples": int(len(frame["X"])),
                    "needed": MIN_LABELED_WINDOWS}
        artifact = fit(frame)

        hist = _metrics_history()
        recent = hist[-7:]
        keep_previous = False
        if recent:
            avg_brier = sum(h["brier_oos"] for h in recent) / len(recent)
            avg_mae = sum(h["drift_mae_oos"] for h in recent if not math.isnan(h["drift_mae_oos"])) / max(
                1, sum(1 for h in recent if not math.isnan(h["drift_mae_oos"])))
            if (artifact["brier_oos"] > avg_brier * (1 + DEGRADE_TOLERANCE)
                    or (not math.isnan(artifact["drift_mae_oos"])
                        and artifact["drift_mae_oos"] > avg_mae * (1 + DEGRADE_TOLERANCE))):
                keep_previous = True

        hist.append({"at": artifact["fitted_at"], "brier_oos": artifact["brier_oos"],
                     "drift_mae_oos": artifact["drift_mae_oos"],
                     "kept_previous": keep_previous})
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        METRICS_PATH.write_text(json.dumps(hist[-90:], indent=1), encoding="utf-8")
        if keep_previous and ARTIFACT_PATH.exists():
            logger.warning("exit model degraded >15%% vs trailing avg — keeping previous artifact")
            return {"status": "degraded_kept_previous", **{k: artifact[k] for k in
                    ("brier_oos", "brier_market_baseline", "drift_mae_oos")}}
        # Preserve the deployed flag across refits (kill bar flips it, not the trainer).
        try:
            prev = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
            artifact["deployed"] = bool(prev.get("deployed", False))
        except Exception:
            pass
        tmp = ARTIFACT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(artifact, indent=1), encoding="utf-8")
        tmp.replace(ARTIFACT_PATH)
        return {"status": "refit",
                "windows": artifact["n_windows"], "samples": artifact["n_samples"],
                "brier_oos": artifact["brier_oos"],
                "brier_market_baseline": artifact["brier_market_baseline"],
                "drift_mae_oos": artifact["drift_mae_oos"],
                "drift_mae_martingale": artifact["drift_mae_martingale"]}
    return _job


def cleanup_job(db: Any, retention_days: int = 90):
    """Nightly retention sweep on window_paths (the plan's rolling 90 days)."""
    async def _job() -> dict[str, Any]:
        import aiosqlite
        from polybot.recording import PATHS_DB
        cutoff = time.time() - retention_days * 86400
        async with aiosqlite.connect(str(PATHS_DB)) as conn:
            await conn.execute("PRAGMA busy_timeout=15000")
            try:
                cur = await conn.execute("DELETE FROM window_paths WHERE ts < ?", (cutoff,))
                await conn.commit()
                return {"rows_deleted": cur.rowcount}
            except aiosqlite.OperationalError:
                return {"rows_deleted": 0}
    return _job
