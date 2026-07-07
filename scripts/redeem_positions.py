"""Redeem every resolved Polymarket position so no shares sit around.

Polymarket's UI redeems only WINNERS; losing shares resolve to $0 and are left
in the wallet forever (no UI button, no auto-redeem). This burns EVERY resolved
position through the funder Gnosis Safe — winners credit USDC, losers burn to
$0 — leaving a spotless wallet.

Redemption cannot lose money: redeemPositions only burns YOUR tokens and pays
YOU. A malformed tx reverts (a no-op costing a fraction of a cent in gas).

Operator-run, needs polybot/config/.env:
  POLYMARKET_FUNDER       (always — used to list positions; a public read)
  POLYGON_RPC_URL         (to redeem — any Polygon RPC, e.g. https://polygon-rpc.com)
  POLYMARKET_PRIVATE_KEY  (to redeem — signs the Safe tx; the EOA pays ~$0.001 gas/redeem)

  python scripts/redeem_positions.py              # list what's redeemable (dry run)
  python scripts/redeem_positions.py --confirm    # actually redeem everything
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / "polybot" / "config" / ".env")

from polybot.execution.redeem import (
    PolygonRedeemer, RedeemerConfigError, fetch_redeemable,
)


def _print_list(winners, losers) -> None:
    if winners:
        total = sum(w.value_usd for w in winners)
        print(f"\n  WINNERS - REAL MONEY to claim (${total:.2f} across {len(winners)}):")
        for w in sorted(winners, key=lambda x: -x.value_usd):
            print(f"    ${w.value_usd:>8.2f}  {w.outcome:<4} {w.shares:>9.2f} sh  {w.title}")
    if losers:
        print(f"\n  $0 LOSERS - worthless dust to burn off ({len(losers)}):")
        for l in sorted(losers, key=lambda x: -x.shares):
            print(f"    {'$0.00':>9}  {l.outcome:<4} {l.shares:>9.2f} sh  {l.title}")
    if not winners and not losers:
        print("\n  Wallet is already clean — nothing resolved is sitting unredeemed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="actually send the redeem transactions (lists only without it)")
    args = parser.parse_args()

    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    if not funder:
        print("POLYMARKET_FUNDER not set in polybot/config/.env — cannot list positions.")
        return 2

    items = asyncio.run(fetch_redeemable(funder))
    winners = [i for i in items if i.is_winner]
    losers = [i for i in items if not i.is_winner]
    neg_risk = [i for i in items if i.negative_risk]

    print(f"Funder {funder}: {len(items)} redeemable position(s).")
    _print_list(winners, losers)
    if neg_risk:
        print(f"\n  NOTE: {len(neg_risk)} neg-risk position(s) skipped (not this bot's markets — "
              "redeem manually on Polymarket).")

    if not items or all(i.negative_risk for i in items):
        return 0

    if not args.confirm:
        print("\nDry run. Re-run with --confirm to redeem everything above.")
        if not os.environ.get("POLYGON_RPC_URL", "").strip():
            print("(First set POLYGON_RPC_URL in .env - e.g. https://polygon-rpc.com - "
                  "and keep a little POL in your wallet for gas.)")
        return 0

    try:
        redeemer = PolygonRedeemer(
            rpc_url=os.environ.get("POLYGON_RPC_URL", "").strip(),
            private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip(),
            funder=funder,
        )
    except RedeemerConfigError as e:
        print(f"\nCannot redeem — {e}. Set it in polybot/config/.env and retry.")
        return 2

    print(f"\nRedeeming through Safe {funder} (EOA {redeemer.eoa} pays gas)...")
    cleared = failed = 0
    for it in items:
        if it.negative_risk:
            continue
        res = redeemer.redeem_condition(it.condition_id, it.token_id, it.title)
        tag = "OK cleared" if res.cleared else f"FAILED ({res.error})"
        tx = f"  tx {res.tx_hashes[-1][:12]}..." if res.tx_hashes else ""
        print(f"  {tag}: {it.title}{tx}")
        cleared += res.cleared
        failed += not res.cleared

    print(f"\nDone — {cleared} cleared, {failed} failed. "
          f"Re-run to retry any failures; a clean run leaves zero shares behind.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
