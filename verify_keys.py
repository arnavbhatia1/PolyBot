"""Verify Polymarket US API credentials are valid."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv("polybot/config/.env")

api_key = os.getenv("POLYMARKET_API_KEY")
secret = os.getenv("POLYMARKET_SECRET")

# Check keys exist
missing = []
if not api_key: missing.append("POLYMARKET_API_KEY")
if not secret: missing.append("POLYMARKET_SECRET")
if missing:
    print(f"MISSING: {missing}")
    exit(1)

print("All keys present.")

# Test US API connection
async def verify():
    from polybot.execution.polymarket_us import PolymarketUSClient
    client = PolymarketUSClient(api_key=api_key, secret_key=secret)
    balance = await client.get_balance()
    print(f"US API connection: OK")
    print(f"Balance: ${balance:,.2f}")
    print()
    print("Ready for live trading. Run:")
    print("  python -m polybot.main --mode live")

asyncio.run(verify())
