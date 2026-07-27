"""Scar scan — the nightly learning loop over the realized ledger.

Every fill the bot has ever made is sliced along a FIXED, versioned dimension
library; any cell that is consistently negative under the pre-registered flag
rule is auto-registered as a zero-capital SHADOW GATE in memory/state/
scar_gates.json. From its discovery day forward the gate is scored strictly
out-of-sample with the frozen Wald SPRT (polybot/core/sprt.py): accept-H1
graduates it (the nightly ping tells the operator to add its name to
late_window.scar_enforce — enforcement is always a manual config flip, never
automatic); accept-H0 auto-retires it. Discovery is in-sample and cheap by
design — the OOS SPRT is the multiple-comparisons control, so a noise cell
that flags simply dies at its reject boundary and can never re-register.

Fire-path contract (main.py): `derive_dims` + `fire_time_matches` run on the
just-built trade_context; with `late_window.scar_enforce` empty (the default)
the fire path only stamps, never vetoes. Enforced vetoes append to
scar_vetoes.jsonl and resolve nightly against window_labels.

The flag rule and SPRT constants below are design-frozen like the SPRT itself:
tuning them to make a pocket flag (or stop flagging) is relaxing a bar.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from polybot.core.sprt import run_sprt

ET = ZoneInfo("America/New_York")

# ── Pre-registered flag rule (frozen 2026-07-27) ──────────────────────────────
FLAG_MIN_FILLS = 8        # cell size floor
FLAG_MIN_DAYS = 3         # distinct ET days the cell must span
FLAG_MAX_EW = -5.0        # ¢/sh — cell equal-weight mean at or below
FLAG_MAX_T = -1.5         # day-clustered t at or below (never fill-weighted)
FLAG_MAX_COVERAGE = 0.5   # a gate prunes a POCKET; a cell that is most of the
                          # ledger isn't a pocket — population-wide toxicity is
                          # the kill rule's jurisdiction, never a scar veto.
                          # Measured against fills where the dim is STAMPED
                          # (None-dim fills excluded — a sparsely-stamped dim
                          # must not read as low coverage of the future).
MAX_SIBLING_OVERLAP = 0.6 # a candidate whose fills are mostly an existing
                          # active gate's fills is the same pocket wearing a
                          # different label — registering it would give one
                          # noise cluster K correlated SPRT shots

MAX_NEW_PER_NIGHT = 2     # registration cap per scan (ping readability)
MAX_ACTIVE_GATES = 6      # shadow+graduated cap; excess candidates wait

# ── Per-gate SPRT design (same arithmetic + doctrine as the burst test) ───────
SPRT_MU1 = 6.0            # H1: vetoing the cell gains ≥ +6¢/sh on veto days
SPRT_ALPHA = 0.05
SPRT_BETA = 0.23
SPRT_SIGMA_DAYS = 4       # σ frozen on the first 4 qualifying OOS days
                          # (pockets are rare — 6 estimation days could take
                          # weeks; those days never score, same convention)

# Dimensions whose value is knowable BEFORE the order is sent — only these may
# become enforceable gates. Observational dims (booked slip, measured submit
# latency) are report-only: you cannot veto on information the fill created.
FIRE_TIME_DIMS = frozenset({
    "ask_bucket", "tremain", "side", "dow", "refire", "session",
    "atr_regime", "burst", "edge_bucket", "prob_bucket", "cb_move_bucket",
})


def _bucket(v: float | None, cuts: tuple, labels: tuple) -> str | None:
    if v is None:
        return None
    for c, lab in zip(cuts, labels):
        if v < c:
            return lab
    return labels[-1]


def derive_dims(ctx: dict[str, Any], side: str, dow: str,
                entry_price: float | None = None) -> dict[str, str | None]:
    """Dimension labels for one decision. `ctx` is the trade_context (at fire
    time: the dict just built; at scan time: the stored stamp). None = the
    input was cold/absent at fire time — the fill scores in no bucket on that
    dim (None-vs-0.0 is load-bearing, as everywhere)."""
    rb = ctx.get("regime_buckets") or {}
    ask = ctx.get("market_price_up" if side == "Up" else "market_price_down")
    slip = (entry_price - ask) if (entry_price is not None and ask is not None) else None
    return {
        "ask_bucket": _bucket(ask, (0.60, 0.75, 0.85),
                              ("<0.60", "0.60-0.75", "0.75-0.85", "0.85+")),
        "tremain": _bucket(ctx.get("seconds_remaining"), (15, 30),
                           ("<15s", "15-30s", "30-45s")),
        "side": side,
        "dow": dow,
        "refire": ctx.get("scar_refire_class"),
        "session": rb.get("session"),
        "atr_regime": rb.get("atr_regime"),
        "burst": rb.get("burst"),
        "edge_bucket": _bucket(ctx.get("edge"), (0.06, 0.12),
                               ("<0.06", "0.06-0.12", ">0.12")),
        "prob_bucket": _bucket(ctx.get("model_probability"), (0.75, 0.90),
                               ("<0.75", "0.75-0.90", ">0.90")),
        "cb_move_bucket": _bucket(ctx.get("scar_cb_move"), (12.0, 20.0),
                                  ("8-12", "12-20", "20+")),
        # observational (fill-created information — never enforceable)
        "slip": _bucket(slip, (-0.005, 0.0101),
                        ("improved", "clean", "padded")),
        "latency": _bucket(ctx.get("cb_tick_to_submit_ms"), (400, 600),
                           ("<400ms", "400-600ms", ">600ms")),
    }


# ── Registry io ────────────────────────────────────────────────────────────────
def load_registry(path: Path) -> dict:
    try:
        reg = json.loads(Path(path).read_text())
        if isinstance(reg, dict) and isinstance(reg.get("gates"), list):
            return reg
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"version": 1, "gates": []}


def save_registry(path: Path, reg: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(p)


def fire_time_matches(ctx: dict[str, Any], side: str, dow: str, registry: dict,
                      statuses: tuple = ("shadow", "graduated")) -> list[str]:
    """Names of gates (in the given statuses) whose fire-time cell this
    decision falls in. Cheap: a dict build + equality checks. Defensive
    against malformed registry entries — the registry is git-synced and a
    corrupt gate must degrade to "no match", never to a fire-path exception.
    The ENFORCE path passes statuses=("graduated",): only an SPRT-graduated
    gate may veto, whatever settings.yaml says."""
    dims = derive_dims(ctx, side, dow)
    out = []
    for g in registry.get("gates", []):
        if not isinstance(g, dict) or not g.get("name"):
            continue
        if (g.get("status") in statuses and g.get("dim") in FIRE_TIME_DIMS
                and dims.get(g["dim"]) == g.get("bucket")):
            out.append(g["name"])
    return out


# ── Ledger loader (same join + net convention as live_health_read) ────────────
def _load_fills(db_path, since_iso: str | None) -> list[dict]:
    db = Path(db_path)
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        has_pid = any(r[1] == "position_id"
                      for r in con.execute("PRAGMA table_info(trade_history)"))
        join_key = "COALESCE(t.position_id, t.id)" if has_pid else "t.id"
        q = ("SELECT t.pnl AS pnl, t.size AS size, t.exit_timestamp AS ts, "
             "p.shares_held AS shares, p.side AS side, p.entry_price AS entry, "
             "p.indicator_snapshot AS snap FROM trade_history t "
             f"JOIN positions p ON {join_key} = p.id "
             "WHERE t.exit_timestamp IS NOT NULL AND p.shares_held > 0")
        args: tuple = ()
        if since_iso:
            q += " AND t.exit_timestamp >= ?"
            args = (since_iso,)
        rows = con.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
            ctx = json.loads(r["snap"] or "{}").get("trade_context", {}) or {}
        except (ValueError, AttributeError, json.JSONDecodeError):
            continue
        loc = dt.astimezone(ET)
        out.append(dict(day=loc.strftime("%Y-%m-%d"), dow=loc.strftime("%a"),
                        net_cs=(r["pnl"] / r["shares"]) * 100.0,
                        pnl=r["pnl"] or 0.0, side=r["side"] or "",
                        entry=r["entry"], ctx=ctx))
    return out


def _cell_stats(sub: list[dict]) -> dict:
    v = [f["net_cs"] for f in sub]
    by_day: dict[str, list[float]] = {}
    for f in sub:
        by_day.setdefault(f["day"], []).append(f["net_cs"])
    dmeans = [statistics.mean(x) for x in by_day.values()]
    t = None
    if len(dmeans) >= 2 and statistics.stdev(dmeans) > 0:
        t = statistics.mean(dmeans) / (statistics.stdev(dmeans) / len(dmeans) ** 0.5)
    return dict(n=len(v), ew_cs=round(statistics.mean(v), 2), days=len(dmeans),
                t_day=round(t, 2) if t is not None else None,
                win=round(sum(1 for x in v if x > 0) / len(v), 2))


# ── The nightly scan ───────────────────────────────────────────────────────────
def scan(db_path, since_iso: str | None, registry_path: Path,
         enforce: list[str] | None = None, mode: str | None = None) -> dict:
    """Discovery + per-gate OOS SPRT + registry persistence. Returns the report
    dict the nightly ping renders. Alert-only: nothing here touches config.
    `mode` ("live"/"paper") is stamped on registration and gates only score
    fills from their own mode's ledger — a mode flip pauses foreign gates
    instead of splicing two fill populations into one frozen-σ test."""
    fills = _load_fills(db_path, since_iso)
    reg = load_registry(registry_path)
    enforce = enforce or []
    for f in fills:
        f["dims"] = derive_dims(f["ctx"], f["side"], f["dow"], f["entry"])

    def _match_idx(dim: str, bucket) -> set[int]:
        return {i for i, f in enumerate(fills) if f["dims"].get(dim) == bucket}

    # -- discovery: flag-rule sweep over every cell not already a gate --------
    known = {(g["dim"], g["bucket"]) for g in reg["gates"] if isinstance(g, dict)}
    active_gates = [g for g in reg["gates"]
                    if isinstance(g, dict) and g.get("status") != "retired"]
    candidates, watch = [], []
    if fills:
        for dim in fills[0]["dims"]:
            stamped = [f for f in fills if f["dims"][dim] is not None]
            buckets = {f["dims"][dim] for f in stamped}
            for b in sorted(buckets):
                if (dim, b) in known:
                    continue
                sub = [f for f in stamped if f["dims"][dim] == b]
                if len(sub) < 3:
                    continue
                st = _cell_stats(sub)
                flagged = (st["n"] >= FLAG_MIN_FILLS and st["days"] >= FLAG_MIN_DAYS
                           and st["n"] <= FLAG_MAX_COVERAGE * len(stamped)
                           and st["ew_cs"] <= FLAG_MAX_EW
                           and st["t_day"] is not None and st["t_day"] <= FLAG_MAX_T)
                if flagged and dim in FIRE_TIME_DIMS:
                    candidates.append((dim, b, st))
                elif st["ew_cs"] <= FLAG_MAX_EW and st["n"] >= 5:
                    # near-miss / observational — reported, never registered
                    watch.append((dim, b, st, flagged))
    candidates.sort(key=lambda c: c[2]["ew_cs"] * c[2]["n"] ** 0.5)
    watch.sort(key=lambda c: c[2]["ew_cs"] * c[2]["n"] ** 0.5)

    n_active = len(active_gates)
    registered = []
    today = datetime.now(ET).strftime("%Y-%m-%d")
    for dim, b, st in candidates[:MAX_NEW_PER_NIGHT]:
        if n_active >= MAX_ACTIVE_GATES:
            break
        # One active gate per dimension: complementary buckets registered on
        # different nights could jointly veto ~100% of fires — a population
        # kill switch assembled from "pockets".
        if any(g.get("dim") == dim for g in active_gates):
            continue
        # Sibling control: mostly-the-same-fills as an existing active gate =
        # the same noise cluster re-parameterized; one pocket gets ONE SPRT.
        cand_idx = _match_idx(dim, b)
        if any(len(cand_idx & _match_idx(g["dim"], g["bucket"]))
               > MAX_SIBLING_OVERLAP * min(len(cand_idx) or 1,
                                           len(_match_idx(g["dim"], g["bucket"])) or 1)
               for g in active_gates):
            continue
        gate = {
            "name": f"{dim}={b}", "dim": dim, "bucket": b,
            "discovered": today, "status": "shadow", "mode": mode,
            "source": "nightly scan v1", "in_sample": st,
            "sprt": {"mu1": SPRT_MU1, "frozen_sigma": None, "sigma_days": []},
        }
        reg["gates"].append(gate)
        active_gates.append(gate)
        registered.append(f"{dim}={b}")
        n_active += 1

    # -- per-gate OOS SPRT (freeze σ, score days strictly after discovery) ----
    gate_reports = []
    for g in reg["gates"]:
        if not isinstance(g, dict) or g.get("status") == "retired":
            continue
        if mode and g.get("mode") and g["mode"] != mode:
            # foreign-mode gate: pause, never splice populations into its test
            gate_reports.append(dict(
                name=g["name"], status=g["status"],
                enforced=g["name"] in enforce, sprt_state="paused_other_mode",
                lam=None, n_oos=0, n_scored=0, oos_ew=None,
                in_sample=g.get("in_sample", {})))
            continue
        # OOS starts strictly after discovery; a VOIDed test restarts (σ
        # re-estimated on fresh days only — the sprt.py doctrine) from the
        # recorded restart day.
        oos_start = g.get("restarted") or g["discovered"]
        matching = [f for f in fills
                    if f["day"] > oos_start
                    and f["dims"].get(g["dim"]) == g["bucket"]]
        by_day: dict[str, list[float]] = {}
        for f in matching:
            by_day.setdefault(f["day"], []).append(f["net_cs"])
        # x_day = the ¢/sh vetoing would have gained that day
        qualifying = [(d, -statistics.mean(v)) for d, v in sorted(by_day.items())]
        sp = g.setdefault("sprt", {"mu1": SPRT_MU1, "frozen_sigma": None,
                                   "sigma_days": []})
        if sp.get("frozen_sigma") is None:
            need = SPRT_SIGMA_DAYS
            est = qualifying[:need]
            # identical day-gains give σ=0 (unusable) — widen the window
            while (len(est) >= 2 and statistics.stdev([x for _, x in est]) == 0
                   and need < len(qualifying)):
                need += 1
                est = qualifying[:need]
            if len(est) >= SPRT_SIGMA_DAYS and statistics.stdev([x for _, x in est]) > 0:
                sp["frozen_sigma"] = round(statistics.stdev([x for _, x in est]), 4)
                sp["sigma_days"] = [d for d, _ in est]
                sp["frozen_at"] = datetime.now(ET).isoformat()
        sigma_days = set(sp.get("sigma_days", []))
        scored = [x for d, x in qualifying if d not in sigma_days]
        if sp.get("frozen_sigma"):
            r = run_sprt(scored, sp.get("mu1", SPRT_MU1), sp["frozen_sigma"],
                         SPRT_ALPHA, SPRT_BETA)
            state, lam = r.state, r.lam
            if r.state == "accept_h1" and g["status"] == "shadow":
                g["status"] = "graduated"
            elif r.state == "accept_h0":
                g["status"] = "retired"
                g["retired"] = today
            elif r.state == "void":
                # restart with a re-estimated σ on post-void days only — never
                # patch mid-test (sprt.py doctrine; burst restarts by deleting
                # its state file, a gate restarts in place with its history kept)
                g.setdefault("void_history", []).append(
                    {"voided": today, "sigma": sp.get("frozen_sigma")})
                g["restarted"] = today
                g["sprt"] = {"mu1": sp.get("mu1", SPRT_MU1),
                             "frozen_sigma": None, "sigma_days": []}
        else:
            state, lam = "accruing_sigma", None
        gate_reports.append(dict(
            name=g["name"], status=g["status"],
            enforced=g["name"] in enforce, sprt_state=state, lam=lam,
            n_oos=len(matching), n_scored=len(scored),
            oos_ew=round(-statistics.mean([x for _, x in qualifying]), 2)
            if qualifying else None,
            in_sample=g.get("in_sample", {})))

    save_registry(registry_path, reg)
    return dict(n_fills=len(fills), gates=gate_reports, registered=registered,
                watch=[dict(cell=f"{d}={b}", flagged_observational=fl, **st)
                       for d, b, st, fl in watch[:3]])


# ── Enforced-veto journal + nightly resolution ─────────────────────────────────
def record_veto(vetoes_path: Path, gate: str, window_id: str, side: str,
                ask: float, size_usd: float) -> None:
    """Append one enforced veto (fire path, only when scar_enforce hits).
    Best-effort: a journaling failure must never block the trading loop."""
    try:
        p = Path(vetoes_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).timestamp(), "gate": gate,
                "window": window_id, "side": side, "ask": ask,
                "size": round(size_usd, 2)}) + "\n")
    except OSError:
        pass


def resolve_vetoes(vetoes_path: Path, db_path) -> dict:
    """Join enforced vetoes to window_labels: what would each vetoed fire have
    made? UPPER-BOUND estimate in the gate's favor by construction — it assumes
    a clean fill at the decision ask (no FOK-kill reachability, no pre-submit
    veto, gross of fee); read it as "at most this good". Grouped per gate so
    one gate's surplus can never hide another's bleed."""
    empty = dict(n=0, resolved=0, avoided_cs=None, avoided_usd=0.0, per_gate={})
    p = Path(vetoes_path)
    if not p.exists():
        return empty
    vetoes = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            v = json.loads(line)
            if isinstance(v, dict):
                vetoes.append(v)
        except json.JSONDecodeError:
            continue
    if not vetoes:
        return empty
    labels: dict[str, int] = {}
    db = Path(db_path)
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            labels = {r[0]: r[1] for r in con.execute(
                "SELECT window_id, resolved_up FROM window_labels")}
            con.close()
        except sqlite3.OperationalError:
            pass
    per: dict[str, dict] = {}
    for v in vetoes:
        g = per.setdefault(str(v.get("gate")), dict(n=0, would=[], usd=0.0))
        g["n"] += 1
        up = labels.get(v.get("window"))
        ask = v.get("ask")
        if up is None or ask is None or not ask:
            continue
        win = (up == 1) == (v.get("side") == "Up")
        w_cs = (1.0 - ask) * 100.0 if win else -ask * 100.0
        g["would"].append(w_cs)
        g["usd"] += (v.get("size", 0.0) / ask) * (w_cs / 100.0)
    per_gate = {
        name: dict(n=g["n"], resolved=len(g["would"]),
                   avoided_cs=round(-statistics.mean(g["would"]), 2)
                   if g["would"] else None,
                   avoided_usd=round(-g["usd"], 2))
        for name, g in per.items()}
    all_would = [w for g in per.values() for w in g["would"]]
    return dict(n=len(vetoes), resolved=len(all_would),
                avoided_cs=round(-statistics.mean(all_would), 2) if all_would else None,
                avoided_usd=round(-sum(g["usd"] for g in per.values()), 2),
                per_gate=per_gate)
