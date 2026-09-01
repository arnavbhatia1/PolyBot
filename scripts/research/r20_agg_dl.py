"""Download Binance spot BTCUSDT aggTrades (tick-level) for selected era days."""
import httpx, time
from pathlib import Path
OUT = Path(__file__).parent / "data" / "vps-0831" / "binance_agg"
DAYS = ["2026-08-20", "2026-08-24", "2026-08-25", "2026-08-27", "2026-08-29"]
c = httpx.Client(timeout=600, follow_redirects=True)
for d in DAYS:
    dst = OUT / f"agg_{d}.zip"
    if dst.exists() and dst.stat().st_size > 1000:
        print(d, "exists"); continue
    url = f"https://data.binance.vision/data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{d}.zip"
    for a in range(3):
        try:
            r = c.get(url)
            if r.status_code == 200:
                dst.write_bytes(r.content); print(d, f"{len(r.content)/1e6:.0f} MB", flush=True); break
            print(d, r.status_code); break
        except Exception as e:
            print(d, "retry", a, e); time.sleep(5)
print("DONE")
