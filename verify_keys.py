"""Verify Polymarket CLOB credentials and connection."""
from dotenv import load_dotenv
load_dotenv("polybot/config/.env")

from polybot.execution.live_trader import verify_auth

ok, msg, balance = verify_auth()
print(msg)

if ok:
    print()
    print("Ready for live trading. Run:")
    print("  python -m polybot.main --mode live")
else:
    exit(1)
