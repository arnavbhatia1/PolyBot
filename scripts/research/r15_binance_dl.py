"""R15 (info program, Phase A): Binance public daily dumps for the 60s era.

Downloads to data/vps-0831/binance/:
  spot 1s klines   BTCUSDT-1s-<day>.zip        (full columns incl. takerBuyVol)
  perp 1m klines   BTCUSDT-1m-<day>.zip        (futures um)
  perp metrics     BTCUSDT-metrics-<day>.zip   (5m open interest etc.)
Days 2026-08-13 .. 2026-08-31 (daily files publish next day; today's absent).
"""
import time
from pathlib import Path

import httpx

SP = Path(__file__).parent
OUT = SP / "data" / "vps-0831" / "binance"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://data.binance.vision/data"
DAYS = [f"2026-08-{d:02d}" for d in range(13, 32)]
URLS = []
for day in DAYS:
    URLS.append((f"{BASE}/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-{day}.zip",
                 f"spot1s_{day}.zip"))
    URLS.append((f"{BASE}/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{day}.zip",
                 f"perp1m_{day}.zip"))
    URLS.append((f"{BASE}/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{day}.zip",
                 f"metrics_{day}.zip"))

def main():
    c = httpx.Client(timeout=120, follow_redirects=True)
    ok = err = skip = 0
    for url, name in URLS:
        dst = OUT / name
        if dst.exists() and dst.stat().st_size > 1000:
            skip += 1
            continue
        for attempt in range(3):
            try:
                r = c.get(url)
                if r.status_code == 200:
                    dst.write_bytes(r.content)
                    ok += 1
                    print(f"{name}: {len(r.content)/1e6:.1f} MB", flush=True)
                    break
                elif r.status_code == 404:
                    print(f"{name}: 404", flush=True)
                    err += 1
                    break
            except Exception as e:
                print(f"{name}: retry {attempt} ({e})", flush=True)
                time.sleep(3)
        else:
            err += 1
    print(f"DONE ok={ok} skip={skip} err={err}")

if __name__ == "__main__":
    main()
