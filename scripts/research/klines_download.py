"""Download Binance BTCUSDT 1s klines for the TWAP era -> data/binance_1s.csv.

Public market-data mirror (data-api.binance.vision) — no auth, not geo-blocked.
Output: ts_sec,close  (kline CLOSE = value known at second-end; the RTDS bz
relay ticks the same 1Hz-on-integer-seconds cadence).
"""
import csv
import sys
import time
import urllib.request
import json
from pathlib import Path

BASE = "https://data-api.binance.vision/api/v3/klines"
START = 1786060800  # 2026-08-07 00:00 UTC (TWAP switch)
OUT = Path(__file__).parent / "data" / "binance_1s.csv"


def fetch(start_ms: int, tries: int = 5):
    url = f"{BASE}?symbol=BTCUSDT&interval=1s&limit=1000&startTime={start_ms}"
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            print(f"retry {i}: {e}", flush=True)
            time.sleep(2 * (i + 1))
    raise RuntimeError("klines fetch failed hard")


def main():
    end = int(time.time())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Resume: continue from last written second.
    start = START
    if OUT.exists():
        with open(OUT, "rb") as f:
            try:
                f.seek(-200, 2)
            except OSError:
                pass
            last = f.read().decode().strip().splitlines()[-1]
            if "," in last and not last.startswith("ts"):
                start = int(float(last.split(",")[0])) + 1
    mode = "a" if start > START else "w"
    n = 0
    with open(OUT, mode, newline="") as f:
        w = csv.writer(f)
        if mode == "w":
            w.writerow(["ts", "close"])
        t = start
        while t < end:
            rows = fetch(t * 1000)
            if not rows:
                t += 1000
                continue
            for k in rows:
                w.writerow([k[0] // 1000, k[4]])
                n += 1
            t = rows[-1][0] // 1000 + 1
            if n % 50000 < 1000:
                print(f"at {t} ({(t - START) / (end - START):.0%})", flush=True)
            time.sleep(0.05)
    print(f"DONE {n} rows -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
