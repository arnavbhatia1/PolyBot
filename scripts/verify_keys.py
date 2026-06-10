"""Verify Polymarket CLOB credentials and connection. Runnable from any directory."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / "polybot" / "config" / ".env")

from polybot.execution.live_trader import verify_auth

ok, msg, balance = verify_auth()
print(msg)
sys.exit(0 if ok else 1)
