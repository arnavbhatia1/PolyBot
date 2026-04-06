"""Polymarket US API client with Ed25519 authentication.

Auth flow per docs.polymarket.us:
  message = str(timestamp_ms) + method + path
  signature = Ed25519.sign(message)
  headers = {X-PM-Access-Key, X-PM-Timestamp, X-PM-Signature(base64)}
"""

import base64
import logging
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

BASE_URL = "https://api.polymarket.us"


class PolymarketUSClient:
    """Authenticated client for the Polymarket US API."""

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        # Decode Ed25519 private key: base64-decode the secret, first 32 bytes
        raw = base64.b64decode(secret_key)
        self._signing_key = Ed25519PrivateKey.from_private_bytes(raw[:32])

    def _auth_headers(self, method: str, path: str) -> dict:
        """Generate the three required auth headers for a request."""
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path
        signature = self._signing_key.sign(message.encode())
        return {
            "X-PM-Access-Key": self.api_key,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": base64.b64encode(signature).decode(),
        }

    async def _request(self, method: str, path: str, json_body: dict | None = None,
                       params: dict | None = None) -> dict:
        """Make an authenticated request to the US API."""
        headers = self._auth_headers(method, path)
        headers["Content-Type"] = "application/json"
        url = BASE_URL + path

        async with httpx.AsyncClient(timeout=10) as client:
            logger.debug(f"US API {method} {path}")
            resp = await client.request(method, url, headers=headers,
                                        json=json_body, params=params)
            logger.debug(f"US API response: {resp.status_code}")
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                logger.error(f"US API returned non-JSON: {resp.text[:200]}")
                return {}

    # --- Orders ---

    async def place_order(self, market_slug: str, intent: str, price: float,
                          quantity: int, order_type: str = "ORDER_TYPE_MARKET",
                          tif: str = "TIME_IN_FORCE_FILL_OR_KILL") -> dict:
        """Place an order on Polymarket US.

        Args:
            market_slug: e.g. "btc-updown-5m-1234567890"
            intent: ORDER_INTENT_BUY_LONG, ORDER_INTENT_BUY_SHORT,
                    ORDER_INTENT_SELL_LONG, ORDER_INTENT_SELL_SHORT
            price: 0.01-0.99
            quantity: number of shares
            order_type: ORDER_TYPE_MARKET or ORDER_TYPE_LIMIT
            tif: time in force

        Returns:
            API response dict with order state, orderId, fills, etc.
        """
        body = {
            "marketSlug": market_slug,
            "type": order_type,
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "quantity": quantity,
            "intent": intent,
            "tif": tif,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        return await self._request("POST", "/v1/orders", json_body=body)

    async def close_position(self, market_slug: str, intent: str,
                             price: float, quantity: int) -> dict:
        """Close an existing position."""
        body = {
            "marketSlug": market_slug,
            "type": "ORDER_TYPE_MARKET",
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "quantity": quantity,
            "intent": intent,
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        return await self._request("POST", "/v1/order/close-position", json_body=body)

    # --- Portfolio ---

    async def get_balance(self) -> float:
        """Get available USD balance (buying power)."""
        try:
            resp = await self._request("GET", "/v1/account/balances")
            # Response includes balance, buyingPower, etc.
            return float(resp.get("buyingPower", resp.get("balance", 0)))
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return 0.0

    async def get_positions(self) -> list[dict]:
        """Get all open positions."""
        try:
            resp = await self._request("GET", "/v1/portfolio/positions")
            if isinstance(resp, list):
                return resp
            # Response might be a dict with positions nested
            return resp.get("positions", [])
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    async def get_order(self, order_id: str) -> dict:
        """Get order status by ID."""
        return await self._request("GET", f"/v1/order/{order_id}")
