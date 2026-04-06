"""Verify Polymarket CLOB API credentials are valid."""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
import os
from dotenv import load_dotenv

load_dotenv("polybot/config/.env")

pk = os.getenv("PRIVATE_KEY")
api_key = os.getenv("POLYMARKET_API_KEY")
secret = os.getenv("POLYMARKET_SECRET")
passphrase = os.getenv("POLYMARKET_PASSPHRASE")

# Check all keys exist
missing = []
if not pk: missing.append("PRIVATE_KEY")
if not api_key: missing.append("POLYMARKET_API_KEY")
if not secret: missing.append("POLYMARKET_SECRET")
if not passphrase: missing.append("POLYMARKET_PASSPHRASE")
if missing:
    print(f"MISSING: {missing}")
    exit(1)

print("All keys present.")

# Test CLOB connection
clob = ClobClient(
    "https://clob.polymarket.com",
    key=pk,
    chain_id=137,
    creds=ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase),
)

resp = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
balance = float(resp.get("balance", 0)) / 1e6
print("CLOB connection: OK")
print(f"USDC Balance: ${balance:.2f}")
print()
print("Ready for live trading. Fund the wallet and run:")
print("  python -m polybot.main --mode live")
