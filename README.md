# PolyBot

A 5-minute BTC Up/Down trader for Polymarket, built for the TWAP era.

Every 5 minutes Polymarket asks whether BTC's **30-second TWAP** at the window
close is at or above its value at the open. Up/Down tokens trade $0–$1 and the
winner pays $1. PolyBot trades that market from one idea: **compute the
resolving average before the book finishes pricing it, and get paid for
resting a bid** — never for being fast.

## The strategy

Two legs, both maker-first, both holding to resolution. There is no exit
engine and no third leg.

**Post-close certainty — the earner.** The market keeps accepting orders for
minutes after the close, and the winner's book shows bid levels with **zero
asks**: nothing can be lifted, so only a resting bid works. By then the outcome
is settled fact, read from the two official TWAP boundary captures
(`final >= strike`, tie goes Up), never from a projection. Sellers who haven't
read the result yet dump the winner into our bid. Measured over 150 windows and
1,364 post-close sales: ~$475/window of supply, present in 149 of 150 windows,
printing at 0.9900. We rest at 0.992 and collect ~0.8¢ a share, riskless, in
~73% of windows. This leg cannot suffer a projection failure — there is no tail
left to be wrong about.

**Lock-dip taker + ladder — the small one.** In the final 30 seconds the
resolving average is mostly already observed, so `w·A + (1−w)·spot` pins the
outcome once displacement clears a frozen error margin. When a late whipsaw
panics someone into selling the winner cheap, we take it. This leg fires only
on the **max tier** — the bound that has never been breached in 583+ windows —
because the thinner p99.5 tier has broken three times and one breach costs
roughly 55 post-close wins.

What the bot deliberately does *not* do: predict spot, race anyone, quote
two-sided market-making, or trade an outcome that isn't already decided. Each
of those was measured and refuted; the reasoning is preserved in `CLAUDE.md`.

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
| `polybot/execution/maker_bid.py` | the resting ladder + post-close phase |
| `polybot/feeds/` | Chainlink (strike + resolution), CLOB books/tape, Gamma |
| `polybot/config/settings.yaml` | the single config source |
| `scripts/analyze_twap_lock.py` | kill-bar harness (tape replay) |

**[`CLAUDE.md`](./CLAUDE.md) is the single source of truth** for gates, sizing,
kill bars, recorders, and the measured evidence behind every number above. It
is updated in the same commit as any behavioural change.
