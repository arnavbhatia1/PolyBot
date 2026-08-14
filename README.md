# PolyBot

A 5-minute BTC Up/Down trader for Polymarket, built for the TWAP era.

Every 5 minutes Polymarket asks whether BTC's **30-second TWAP** at the window
close is at or above its value at the open. Up/Down tokens trade $0–$1 and the
winner pays $1. PolyBot trades that market from one idea: **compute the
resolving average before the book finishes pricing it** — never be fast, never
be first in a queue, only be right about an average the book prices from spot.

## The strategy

Two legs, both holding to resolution. There is no exit engine and no third leg.

**Lock-dip taker.** In the final 30 seconds the resolving average is mostly
already observed, so `w·A + (1−w)·spot` pins the outcome once displacement
clears a frozen error margin. When a late whipsaw panics someone into selling
the winner cheap, we take it. Fires only on the **max tier** and only with
**6+ seconds left** — below that the margin knots are sub-$4 tail bounds that
564 windows cannot pin, and the one realized max-tier breach was a k=1.1s fire
on a window that resolved by $0.0007.

**Deep-projection ladder — the earner.** Reverse-engineered from the market's
best late maker (+$12.9k in 4.5 days): our projection's sign matches its side
on 89% of its deep fills, and its edge vanishes where the projection
disagrees. Deep GTC bids (0.80/0.65/0.50/0.35/0.20 — its own fill distribution) rest on the projection-favored
side in the final 25 seconds and hold through the close while the verified
winner matches. Break-even equals the price paid, so rung losses are
priced-in. Paper fills only count when the tape prints *strictly below* a
rung — a live probe proved that at any shared price level we sit behind size
no book snapshot shows.

What the bot deliberately does *not* do: predict spot, race anyone, quote
two-sided market-making, rest bids at the post-close cap (102 live placements,
zero fills against a ~290k-share wall), or fade the book on coin-flip windows
(refuted over 14,897 windows). Each refutation is preserved in `CLAUDE.md`.

## Quick start

```bash
pip install -r requirements.txt
cp polybot/config/.env.example polybot/config/.env   # DISCORD_BOT_TOKEN required

python -m polybot.main --mode paper     # paper trading
python -m polybot.main --mode live      # real USDC (needs allowance)
python -m polybot.main --run-pipeline   # one nightly cycle, no trading
python -m pytest polybot/tests/         # full suite
```

The bot runs on the VPS under systemd (`scripts/run_polybot.sh`), never on a
workstation — there is no cross-host lock.

## Layout

| path | what |
|---|---|
| `polybot/main.py` | trading loop, entry/sizing orchestration |
| `polybot/core/signal_engine.py` | the lock math, margins, Kelly |
| `polybot/execution/maker_bid.py` | the resting ladder |
| `polybot/feeds/` | Chainlink (strike + resolution), CLOB books/tape, Gamma |
| `polybot/config/settings.yaml` | the single config source |
| `scripts/analyze_twap_lock.py` | kill-bar harness (tape replay) |

**[`CLAUDE.md`](./CLAUDE.md) is the single source of truth** for gates, sizing,
kill bars, recorders, and the measured evidence behind every number above. It
is updated in the same commit as any behavioural change.
