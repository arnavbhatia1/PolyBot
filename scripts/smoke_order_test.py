"""One-shot live ORDER-POST smoke test — proves the signed-order path clears Cloudflare.

`verify_keys.py` exercises only GET-authenticated endpoints (key derivation,
balance/allowance). The go-live runbook (tasks/todo.md) additionally requires
proof that an EIP-712-signed order POST from this host reaches the exchange —
Cloudflare treats POSTs differently and a 403 there would brick the first live
window. This posts ONE deliberately unfillable FOK BUY on the current 5-min BTC
window: limit 0.01 against a side whose best ask is verified >= 0.05, so the FOK
cannot cross and is killed by the matching engine. Any well-formed exchange
response (success=false, "couldn't be fully filled", etc.) PROVES the path;
a Cloudflare 403 / HTML response FAILS it.

Operator-run, needs live keys in polybot/config/.env. Refuses to run without
--confirm. Touches no DB or bot state; safe while the bot is running.
Worst case if every guard is somehow wrong: a $1 fill at $0.01.
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
MIN_SAFE_ASK = 0.05      # only post against a book whose best ask is >= this
LIMIT_PRICE = 0.01       # FOK buy limit — cannot cross a book with asks >= MIN_SAFE_ASK
ORDER_USDC = 1.0         # CLOB minimum order
MIN_WINDOW_REMAINING_S = 60  # skip end-of-window books (one side's asks collapse)


def _current_contract() -> dict:
    """Fetch the active btc-updown-5m window from Gamma; retry the next window if
    this one is too close to expiry."""
    with httpx.Client(timeout=10) as client:
        for offset in (0, WINDOW_SECONDS):
            window_ts = int(time.time() // WINDOW_SECONDS) * WINDOW_SECONDS + offset
            slug = f"btc-updown-5m-{window_ts}"
            resp = client.get(f"{GAMMA_API}/events", params={"slug": slug})
            if not resp.is_success:  # deprecated endpoint enforced — undeprecated fallback
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
            remaining = window_ts + WINDOW_SECONDS - time.time()
            if remaining < MIN_WINDOW_REMAINING_S and offset == 0:
                continue
            return {"slug": slug, "tokens": tokens}
    raise RuntimeError("no active btc-updown-5m window found via Gamma")


def _best_ask(token_id: str) -> float | None:
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        resp.raise_for_status()
        asks = resp.json().get("asks") or []
        prices = [float(a["price"]) for a in asks]
        return min(prices) if prices else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="actually post the order (refuses without this)")
    args = parser.parse_args()
    if not args.confirm:
        print(__doc__)
        print("Refusing to post without --confirm.")
        return 2

    contract = _current_contract()
    print(f"Window: {contract['slug']}")

    # Post against the side with the HIGHER best ask — furthest from crossable.
    candidates = []
    for token_id in contract["tokens"]:
        ask = _best_ask(token_id)
        print(f"  token ...{token_id[-8:]}: best ask {ask}")
        if ask is not None and ask >= MIN_SAFE_ASK:
            candidates.append((ask, token_id))
    if not candidates:
        print(f"FAIL-SAFE: no side has best ask >= {MIN_SAFE_ASK} — rerun on a fresh window.")
        return 3
    _, token_id = max(candidates)

    from polybot.execution.live_trader import _create_clob_client
    from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
    from py_clob_client_v2.order_builder.constants import BUY

    try:
        client = _create_clob_client()
    except Exception as e:
        print(f"FAIL (auth setup): {e}")
        return 1

    mo = MarketOrderArgs(token_id=token_id, amount=ORDER_USDC, side=BUY, price=LIMIT_PRICE)
    t0 = time.perf_counter()
    try:
        resp = client.post_order(client.create_market_order(mo), OrderType.FOK)
    except Exception as e:
        rtt = time.perf_counter() - t0
        msg = str(e)
        low = msg.lower()
        if "403" in low or "cloudflare" in low or "forbidden" in low:
            print(f"FAIL — Cloudflare/geo BLOCKED the order POST ({rtt:.3f}s): {msg}")
            print("Order POSTs do NOT clear from this host. Do not go live here.")
        elif "401" in low or "signature" in low or "unauthorized" in low:
            print(f"FAIL — auth/signing rejected ({rtt:.3f}s): {msg}")
        else:
            print(f"INCONCLUSIVE — network-level error, POST may not have reached "
                  f"the exchange ({rtt:.3f}s): {msg}")
        return 1

    rtt = time.perf_counter() - t0
    print(f"Exchange responded in {rtt:.3f}s: {resp}")
    if resp.get("success") and resp.get("status") == "matched":
        print(f"WARNING: the guard FOK actually FILLED (~{ORDER_USDC / LIMIT_PRICE:.0f} shares "
              f"@ {LIMIT_PRICE}) — position rides to resolution; max loss ${ORDER_USDC:.2f}.")
    print("PASS — a signed order POST reached the matching engine from this host. "
          f"Warm-POST RTT sample: {rtt:.3f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
