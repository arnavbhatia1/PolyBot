"""Orchestration: snapshot / label / ivcheck / analyze passes.

snapshot_pass  — discover open long-horizon crypto markets, pull CLOB books + the live
                 Deribit surface, compute the option-implied prob per rung, store a row.
label_pass     — for snapshotted markets that have settled, read Gamma resolution.
ivcheck_pass   — the instant 'already arbed?' test: one snapshot, no DB, no waiting.
analyze        — join snapshots to resolutions, run the calibration + kill bar.

Measurement-only: reads books and option quotes, writes one local sqlite DB. No orders.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from polybot.calibration import analysis, clob, deribit, discovery
from polybot.calibration.discovery import MarketRef
from polybot.calibration.store import CalibrationStore

_BOOK_CONCURRENCY = 8
# Polymarket coin slug -> Deribit currency. Only these have a liquid option surface to
# cross-check against; other coins (SOL, XRP) are still forward-recorded but get iv_implied=None.
_DERIBIT_CURRENCY = {"bitcoin": "BTC", "ethereum": "ETH"}


async def fetch_surfaces(client: httpx.AsyncClient) -> dict[str, deribit.OptionSurface]:
    """One Deribit option surface per coin that has one. A coin's failure is non-fatal."""
    surfaces: dict[str, deribit.OptionSurface] = {}
    for coin, cur in _DERIBIT_CURRENCY.items():
        try:
            surfaces[coin] = await deribit.fetch_surface(client, cur)
        except Exception:  # noqa: BLE001 — missing one surface must not abort the pass
            continue
    return surfaces


async def _collect_rows(client: httpx.AsyncClient,
                        surfaces: dict[str, deribit.OptionSurface],
                        refs: list[MarketRef]) -> list[dict]:
    """Fetch each ref's CLOB quote + compute the option-implied prob (against ITS OWN
    coin's surface) -> snapshot dicts. Coins without a surface get iv_implied=None.
    Skips rungs with no strike, no future end, or no executable ask."""
    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    sem = asyncio.Semaphore(_BOOK_CONCURRENCY)
    tradeable = [r for r in refs
                 if r.strike and r.end_dt and r.end_dt > now and not r.closed]

    async def one(ref: MarketRef) -> dict | None:
        async with sem:
            book = await clob.fetch_book(client, ref.token0_id)
        q = clob.quote_from_book(book)
        if q["pm_ask"] is None:
            return None
        surface = surfaces.get(ref.coin)
        iv_used = iv_implied = spot = fwd = side = None
        T = None
        if surface is not None:
            T = surface.years_to(ref.end_dt)
            iv_used = surface.iv_at(ref.strike, T) if T > 0 else None
            _, fwd = surface.atm_forward(T)
            iv_implied = deribit.implied_prob(surface, ref.pricing_kind, ref.strike, ref.end_dt)
            spot = surface.spot
            side = ("ABOVE" if ref.pricing_kind == "digital"
                    else ("UP" if ref.strike >= surface.spot else "DN"))
        end_ts = ref.end_dt.timestamp()
        return {
            "taken_ts": now.isoformat(), "taken_epoch": now_epoch,
            "condition_id": ref.condition_id, "token0_id": ref.token0_id,
            "slug": ref.slug, "coin": ref.coin, "family": ref.family,
            "pricing_kind": ref.pricing_kind, "strike": ref.strike, "side": side,
            "end_ts": end_ts, "horizon_days": (end_ts - now_epoch) / 86400.0,
            "lead_s": end_ts - now_epoch,
            "pm_bid": q["pm_bid"], "pm_ask": q["pm_ask"], "pm_mid": q["pm_mid"],
            "ask_depth_usd": q["ask_depth_usd"], "bid_depth_usd": q["bid_depth_usd"],
            "iv_implied": iv_implied, "iv_used": iv_used,
            "spot": spot, "fwd": fwd, "t_years": T,
        }

    results = await asyncio.gather(*(one(r) for r in tradeable))
    return [r for r in results if r is not None]


async def snapshot_pass(store: CalibrationStore, max_pages: int = 8) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        surfaces = await fetch_surfaces(client)
        refs = await discovery.discover(client, max_pages=max_pages, open_only=True)
        rows = await _collect_rows(client, surfaces, refs)
    n = await store.insert_snapshots(rows)
    fams: dict[str, int] = {}
    for r in rows:
        fams[r["family"]] = fams.get(r["family"], 0) + 1
    btc = surfaces.get("bitcoin")
    return {"discovered": len(refs), "snapshotted": n, "by_family": fams,
            "spot": btc.spot if btc else None,
            "surfaces": {c: len(s.term) for c, s in surfaces.items()}}


async def label_pass(store: CalibrationStore) -> dict:
    now_epoch = datetime.now(timezone.utc).timestamp()
    needing = await store.conditions_needing_label(now_epoch)
    if not needing:
        return {"checked": 0, "labeled": 0}
    by_slug: dict[str, list[dict]] = {}
    for row in needing:
        by_slug.setdefault(row["slug"], []).append(row)
    labeled = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for slug, rows in by_slug.items():
            event = await discovery.fetch_event_by_slug(client, slug)
            if not event:
                continue
            outcomes = {r.condition_id: r.outcome for r in discovery.parse_event(event)}
            resolved_ts = event.get("endDate")
            wanted = {r["condition_id"] for r in rows}
            for cid in wanted:
                out = outcomes.get(cid)
                if out is None:
                    continue
                tok = next((r["token0_id"] for r in rows if r["condition_id"] == cid), "")
                await store.upsert_resolution(cid, slug, tok, out, resolved_ts)
                labeled += 1
    return {"checked": len(needing), "labeled": labeled}


async def ivcheck_pass(max_pages: int = 8) -> dict:
    """Instant options cross-check (no DB): per coin/family PM-ask vs option-implied, plus
    a per-rung table so you can eyeball whether the ladder is already arbed."""
    async with httpx.AsyncClient(timeout=10) as client:
        surfaces = await fetch_surfaces(client)
        refs = await discovery.discover(client, max_pages=max_pages, open_only=True)
        rows = await _collect_rows(client, surfaces, refs)
    btc = surfaces.get("bitcoin")
    return {"spot": btc.spot if btc else None, "n_rows": len(rows),
            "cross_check": analysis.iv_cross_check(rows), "rows": rows}


async def analyze_pass(store: CalibrationStore) -> dict:
    join_rows = await store.join_rows()
    latest = await store.latest_snapshots()
    report = analysis.evaluate(join_rows, snapshot_rows=latest)
    report["status"] = await store.status()
    return report


async def monitor_loop(store: CalibrationStore, snapshot_interval_s: float = 3600.0,
                       label_interval_s: float = 43200.0, max_pages: int = 8,
                       iterations: int | None = None, sleep_fn=asyncio.sleep,
                       clock=time.monotonic, log=print) -> None:
    """Continuous accumulation loop for supervised (run_polybot.ps1) operation: snapshot
    every snapshot_interval_s, label every label_interval_s. A transient failure in one
    pass is logged and never kills the loop. iterations=None runs forever (the bounded
    form is for tests). Holds one store connection for the loop's lifetime."""
    log(f"calibration monitor: snapshot/{snapshot_interval_s:.0f}s label/{label_interval_s:.0f}s")
    last_label: float | None = None
    i = 0
    while iterations is None or i < iterations:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            rep = await snapshot_pass(store, max_pages=max_pages)
            log(f"[{stamp}] snapshot stored={rep['snapshotted']} "
                f"discovered={rep['discovered']} by_family={rep['by_family']}")
        except Exception as e:  # noqa: BLE001 — a bad pass must not kill the supervised loop
            log(f"[{stamp}] snapshot error: {e}")
        now = clock()
        if last_label is None or (now - last_label) >= label_interval_s:
            try:
                lrep = await label_pass(store)
                log(f"[{stamp}] label checked={lrep['checked']} labeled={lrep['labeled']}")
            except Exception as e:  # noqa: BLE001
                log(f"[{stamp}] label error: {e}")
            last_label = now
        i += 1
        if iterations is not None and i >= iterations:
            break
        await sleep_fn(snapshot_interval_s)
