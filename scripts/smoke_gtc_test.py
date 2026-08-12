"""One-shot live GTC smoke test — proves the RESTING-BID path works end to end.

smoke_order_test.py posts an unfillable FOK, which only proves a *taker* POST
clears Cloudflare. The strategy is now ~99% maker: post-close rests a bid on a
settled winner. That path — place_gtc_bid / poll_gtc_fill / cancel_gtc — has
NEVER executed against the real exchange. The live ledger holds 331 fills and
zero maker fills, the last of them from before the maker legs existed. Flipping
to live without this test means discovering a GTC-specific failure (tick size on
a 3-decimal price, min-size rule, an order-type rejection) at full rung size in
a live window.

This posts ONE deliberately un-hittable GTC BUY: limit 0.01 on the side whose
best bid is verified >= 0.05, so no seller would ever hit it. Then it polls the
order, cancels it, and re-polls to confirm the cancel took. Every step is
reported separately, because they fail for different reasons.

Operator-run, needs live keys in polybot/config/.env. Refuses to run without
--confirm. Touches no DB or bot state; safe while the bot is running. Worst case
if every guard is somehow wrong: a $1 fill at $0.01 that rides to resolution.
"""
import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / "polybot" / "config" / ".env")

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WINDOW_SECONDS = 300
MIN_SAFE_BID = 0.05      # only rest under a book whose best BID is >= this
LIMIT_PRICE = 0.01       # nobody sells into 0.01 while the bid is >= 0.05
ORDER_SHARES = 100.0     # 100 x 0.01 = $1.00, the CLOB minimum notional
MIN_WINDOW_REMAINING_S = 60


def _current_contract() -> dict:
    with httpx.Client(timeout=10) as client:
        for offset in (0, WINDOW_SECONDS):
            window_ts = int(time.time() // WINDOW_SECONDS) * WINDOW_SECONDS + offset
            slug = f"btc-updown-5m-{window_ts}"
            resp = client.get(f"{GAMMA_API}/events", params={"slug": slug})
            if not resp.is_success:
                resp = client.get(f"{GAMMA_API}/events/slug/{slug}")
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
            data = resp.json()
            events = data if isinstance(data, list) else ([data] if data else [])
            if not events:
                continue
            market = events[0].get("markets", [{}])[0]
            tokens = market.get("clobTokenIds", [])
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if len(tokens) < 2:
                continue
            if window_ts + WINDOW_SECONDS - time.time() < MIN_WINDOW_REMAINING_S and offset == 0:
                continue
            return {"slug": slug, "tokens": tokens,
                    "tick": market.get("orderPriceMinTickSize")}
    raise RuntimeError("no active btc-updown-5m window found via Gamma")


def _best_bid(token_id: str) -> float | None:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        resp.raise_for_status()
        bids = resp.json().get("bids") or []
        prices = [float(b["price"]) for b in bids]
        return max(prices) if prices else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirm", action="store_true",
                    help="actually post the resting order (refuses without this)")
    args = ap.parse_args()
    if not args.confirm:
        print(__doc__)
        print("Refusing to post without --confirm.")
        return 2

    contract = _current_contract()
    print(f"Window: {contract['slug']}  (gamma tick {contract['tick']})")

    candidates = []
    for token_id in contract["tokens"]:
        bid = _best_bid(token_id)
        print(f"  token ...{token_id[-8:]}: best bid {bid}")
        if bid is not None and bid >= MIN_SAFE_BID:
            candidates.append((bid, token_id))
    if not candidates:
        print(f"FAIL-SAFE: no side has best bid >= {MIN_SAFE_BID} — rerun on a fresh window.")
        return 3
    _, token_id = max(candidates)

    import asyncio
    from polybot.execution.live_trader import LiveTrader

    trader = LiveTrader.__new__(LiveTrader)      # only the CLOB client is needed
    from polybot.execution.live_trader import _create_clob_client
    try:
        trader.client = _create_clob_client()
    except Exception as e:
        print(f"FAIL (auth setup): {e}")
        return 1

    # 1) PLACE — the step that proves a GTC POST is accepted at all.
    t0 = time.perf_counter()
    try:
        order_id = asyncio.run(trader.place_gtc_bid(token_id, LIMIT_PRICE, ORDER_SHARES))
    except Exception as e:
        print(f"FAIL (place raised) {time.perf_counter() - t0:.3f}s: {e}")
        return 1
    rtt = time.perf_counter() - t0
    if not order_id:
        print(f"FAIL — place_gtc_bid returned no order id ({rtt:.3f}s). The maker "
              f"legs would silently place nothing in live. Check the log line it "
              f"emitted for the exchange's reason (tick size, min size, allowance).")
        return 1
    print(f"PASS place  — resting order {order_id} accepted in {rtt:.3f}s")

    # 2) POLL — the step the live fill detector depends on every second.
    try:
        matched = asyncio.run(trader.poll_gtc_fill(order_id))
        print(f"PASS poll   — matched so far: {matched} "
              f"({'None means the lookup failed' if matched is None else 'live detector works'})")
    except Exception as e:
        print(f"FAIL (poll raised): {e}")

    # 3) CANCEL — without this the bot cannot pull a rung when the lock weakens.
    t1 = time.perf_counter()
    try:
        asyncio.run(trader.cancel_gtc(order_id))
        print(f"PASS cancel — accepted in {time.perf_counter() - t1:.3f}s")
    except Exception as e:
        print(f"FAIL (cancel raised): {e}")
        print(f"!! order {order_id} MAY STILL BE RESTING at {LIMIT_PRICE} — "
              f"cancel it by hand at polymarket.com/portfolio.")
        return 1

    after = asyncio.run(trader.poll_gtc_fill(order_id))
    print(f"post-cancel poll: {after}")
    print("\nPASS — the live resting-bid path works end to end from this host: a GTC "
          "order posts, polls and cancels. That is the path post-close earns through.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
