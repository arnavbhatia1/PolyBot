"""Verify Polymarket CLOB credentials and connection."""
import os
from dotenv import load_dotenv

load_dotenv("polybot/config/.env")

# Check required keys exist
private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
funder = os.getenv("POLYMARKET_FUNDER")

missing = []
if not private_key:
    missing.append("POLYMARKET_PRIVATE_KEY")
if not funder:
    missing.append("POLYMARKET_FUNDER")
if missing:
    print(f"MISSING from polybot/config/.env: {missing}")
    exit(1)

print(f"Keys present: POLYMARKET_PRIVATE_KEY=***{private_key[-6:]}, POLYMARKET_FUNDER={funder[:8]}...")

# Test CLOB authentication
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    client = ClobClient(
        "https://clob.polymarket.com",
        key=private_key,
        chain_id=137,
        signature_type=2,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("Authentication: OK")
except Exception as e:
    print(f"Authentication: FAILED — {e}")
    print("Check your POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER values.")
    exit(1)

# Test balance fetch
try:
    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    balance = int(result.get("balance", "0")) / 1e6
    print(f"USDC Balance: ${balance:,.2f}")
    if balance < 1.0:
        print("WARNING: Low balance — deposit USDC on Polymarket before trading.")
except Exception as e:
    print(f"Balance fetch: FAILED — {e}")
    exit(1)

print()
print("Ready for live trading. Run:")
print("  python -m polybot.main --mode live")
