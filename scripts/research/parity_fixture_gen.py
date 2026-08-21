"""Build the decision-parity replay fixture from real recorded streams.

Extracts, for a handful of real windows, every event the decision path
consumes — Chainlink raw/sixty/Binance reports (micro-tape), CLOB BBO changes
(micro-tape), and CLOB prints (tape) — into one compact committed file that
polybot/tests/test_decision_parity.py replays through the production feed and
both traders. The fixture is REAL data; only trader wiring differs per run.

Usage (data pulled to scripts/research/data/vps-0821/):
    python scripts/research/parity_fixture_gen.py
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

DATA = Path(__file__).parent / "data" / "vps-0821"
OUT = Path(__file__).parents[2] / "polybot" / "tests" / "fixtures" / "parity_windows.json.gz"

# (window_ts, micro/tape day, serve_ptb) — serve_ptb=True exercises the
# Gamma-served-strike branch; False exercises the TWAP-boundary-capture branch.
WINDOWS = [
    (1787236800, "2026-08-20", True),    # armed, filled, won (Down)
    (1787258400, "2026-08-20", False),   # armed, filled, projection flipped, lost (Up)
    (1787304900, "2026-08-21", False),   # armed late (k=8.4), filled, won (Up)
]

PRE_S = 380.0    # events from window_ts - PRE_S (covers the prior boundary + ring warmup)
POST_S = 380.0   # ...to window_ts + POST_S (covers close boundary + post-close hold)


def _open(day: str, prefix: str):
    p = DATA / f"{prefix}_{day}.jsonl"
    if p.exists():
        return p.open("r", encoding="utf-8")
    return gzip.open(DATA / f"{prefix}_{day}.jsonl.gz", "rt", encoding="utf-8")


def _tokens_and_label(window_ts: int) -> tuple[str, str, dict]:
    db = sqlite3.connect(DATA / "polybot_paper_0821.db")
    row = db.execute("SELECT indicator_snapshot FROM positions WHERE market_id LIKE ?",
                     (f"%{window_ts}",)).fetchone()
    tc = json.loads(row[0])["trade_context"]
    lab = db.execute("SELECT resolved_up, final_price, price_to_beat FROM window_labels "
                     "WHERE window_id LIKE ?", (f"%{window_ts}",)).fetchone()
    db.close()
    return tc["token_id_up"], tc["token_id_down"], {
        "resolved_up": lab[0], "final_price": lab[1], "price_to_beat": lab[2]}


def build_window(window_ts: int, day: str, serve_ptb: bool) -> dict:
    token_up, token_down, label = _tokens_and_label(window_ts)
    tokens = {token_up, token_down}
    lo, hi = window_ts - PRE_S, window_ts + POST_S
    events: list[list] = []

    with _open(day, "micro") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            k = r.get("k")
            if k in ("l", "t", "s"):
                rx = r.get("rx")
                if rx is None or not (lo <= rx <= hi):
                    continue
                events.append([round(rx, 3), k, r["ts"], r["p"]])
            elif k == "b":
                rx = r.get("ts")   # b rows stamp receipt in "ts"
                if rx is None or not (lo <= rx <= hi) or r.get("token") not in tokens:
                    continue
                events.append([round(rx, 3), "b", r["token"], r.get("bid"), r.get("ask")])

    with _open(day, "tape") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rx = r.get("ts")
            if rx is None or not (lo <= rx <= hi) or r.get("token") not in tokens:
                continue
            events.append([round(rx, 3), "p", r["token"], r.get("price"), r.get("size")])

    events.sort(key=lambda e: e[0])
    return {
        "window_ts": window_ts, "cid": f"btc-updown-5m-{window_ts}",
        "token_up": token_up, "token_down": token_down,
        "serve_ptb": serve_ptb, "label": label, "events": events,
    }


def main() -> None:
    windows = [build_window(*w) for w in WINDOWS]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"source": "VPS recordings pulled 2026-08-21; real streams, "
                                  "60s-rule era", "span_s": [PRE_S, POST_S]},
               "windows": windows}
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    for w in windows:
        kinds = {}
        for e in w["events"]:
            kinds[e[1]] = kinds.get(e[1], 0) + 1
        print(w["window_ts"], kinds)
    print("wrote", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
