# PolyBot Design Spec

**Date:** 2026-03-30
**Status:** Approved
**Architecture:** Modular monolith with agent threads

## Overview

PolyBot is a Polymarket micro-trading bot that uses Claude Sonnet 4.6 as its probability brain, pre-computed math for instant trade decisions, and a self-learning agent pipeline that continuously improves accuracy. It starts in paper trading mode on a local Windows machine and transitions to live trading on a Docker/VPS deployment.

**Core philosophy:** Lightning-fast entry when edge is found, fast exit when profit is locked, and a learning system that continuously improves accuracy autonomously.

## Constraints

- Starting capital: <$100, paper trading first
- Risk tolerance: Conservative (Quarter Kelly)
- Monthly budget: ~$5 VPS + ~$3 Claude API
- Runtime: 24/7 once deployed
- Language: Python 3.11+

---

## Layer 1: Data & Market Scanning

**Source:** Polymarket CLOB API (REST + WebSocket via `py-clob-client`)

**Scan cycle:** Every 5 minutes (configurable).

**Pre-filter criteria (all configurable via `config/settings.yaml`):**

| Filter | Default | Rationale |
|--------|---------|-----------|
| Min 24h volume | $1,000 | Filters dead markets |
| Min liquidity | $500 | Ensures orderbook depth |
| Days to expiry | 2–60 | Too short = resolution volatility, too long = capital locked |
| Max spread | 5% | Micro trading needs tight spreads |
| Category | Optional whitelist/blacklist | Skip low-signal categories |

**Data captured per market:** Market ID, question text, current price, 24h volume, liquidity, orderbook depth, bid-ask spread, days to expiry, category, scan timestamp.

**Why these filters:** Micro trading profits come from small, frequent edges. Low-liquidity or wide-spread markets eat the edge in slippage. The 2-day minimum expiry prevents buying into resolution volatility.

---

## Layer 2: AI Brain (Claude Integration)

**Model:** `claude-sonnet-4-6` (Sonnet 4.6) — best balance of speed, cost, and reasoning.

**Input to Claude:** For each market passing filters, Claude receives:
- The market question
- Current price, volume, liquidity, spread
- Days to expiry
- Relevant bias corrections from `memory/biases.json`
- Top 5 relevant lessons from `memory/lessons.json`
- Recent accuracy stats for self-awareness

**Output (structured JSON):**
```json
{
  "probability": 0.72,
  "confidence": "high",
  "reasoning": "Strong polling data supports YES, but market is pricing in uncertainty from...",
  "key_factors": ["polling data", "historical precedent", "time to resolution"],
  "base_rate_considered": true
}
```

**Confidence gating:** Only markets where Claude returns `"confidence": "high"` AND `probability >= 0.65` proceed to the math engine. Medium/low confidence trades are logged but skipped.

**Prompt management:**
- Prompts stored in `brain/prompts/` with version numbers (`v001.txt`, `v002.txt`, etc.)
- Active prompt version referenced in config
- Prompts include explicit instructions to: consider base rates, penalize extreme confidence, explain reasoning
- Every prompt change tracked for accuracy correlation

**API efficiency:**
- `httpx` async client with connection pooling
- ~15–30 Claude calls per scan cycle after filtering
- Estimated ~$2–4/month at Sonnet pricing

---

## Layer 3: Math Engine (Speed-First)

### Pre-Computed Decision Tables

Instead of calculating EV/Kelly on every scan, the bot maintains a live lookup table of pre-computed thresholds for every price point (1c to 99c). When Claude returns a probability, the trade decision is a single dictionary lookup — zero math at decision time.

```python
# Pre-computed at startup and on config change
decision_table[0.72] = {
    "max_buy_price": 0.62,
    "exit_price": 0.68,
    "kelly_fraction": 0.047,
}
```

### Formulas

**Expected Value (EV):**
```
EV = P(win) * Profit - P(lose) * Loss
```
Hard threshold: skip if EV < 5% edge. This filter eliminates ~90% of marginal trades.

**Kelly Criterion (Quarter Kelly):**
```
f* = 0.25 * (p * b - q) / b
```
Conservative enough to survive variance, aggressive enough to grow.

**Entry/Exit Rules:**
- **Entry:** `market_price <= model_probability * 0.85` (15% edge minimum for speed trades)
- **Exit (take profit):** `market_price >= model_probability * 0.90` (lock profit at 90% correction)
- **Stop loss:** Price drops 15% below entry — cut immediately
- **Time stop:** Position open >24 hours with <2% gain — exit and redeploy capital

**Bayesian Re-evaluation:**
```
P(H|E) = P(E|H) * P(H) / P(E)
```
Triggered when price moves >8% from entry OR volume spikes >3x average. Re-asks Claude with updated data. If updated probability drops below exit threshold, immediate exit.

**Log Returns (reporting only, not in hot path):**
```
log_return = ln(P1 / P0)
```
Used for Sharpe ratio and performance reporting. Not in the trading decision path.

### Speed Architecture

```
ENTRY PATH (scan cycle, every 5 min):
  Filter markets -> Claude probability -> table lookup -> GTC order
  Target: <2 seconds from Claude response to order placed

EXIT PATH (WebSocket, real-time):
  Price update received -> check vs exit/stop thresholds -> immediate order
  Target: <500ms from price trigger to order placed
```

---

## Layer 4: Execution

### Two Modes, One Interface

**Paper Trading Mode (Phase 1):**
- Simulates all trades against real market prices
- Records what would have happened: entry price, exit price, fees, slippage estimate
- Same decision logic as live — only order placement is mocked
- SQLite stores all paper trades with full metadata
- This is where the learning agents train before real money is at risk

**Live Trading Mode (Phase 2):**
- Uses `py-clob-client` for real GTC orders on Polymarket CLOB
- Pre-checks on-chain USDC.e balance via Alchemy RPC before every trade
- `web3.py` for token approvals and direct contract interactions

**Mode switch:** Single config change `mode: "paper"` to `mode: "live"`. No code changes.

### Order Execution Rules

| Rule | Value | Rationale |
|------|-------|-----------|
| Order type | GTC only | FOK fails ~40% on thin books |
| Max slippage | 2% | Skip if orderbook too thin |
| Max bankroll deployed | 80% | 20% stays liquid for opportunities |
| Max concurrent positions | 5 | Capital spread, not concentrated |
| Duplicate protection | 1 position per market | Prevents double-ordering |

### Position Schema (SQLite)

```sql
positions (
  id INTEGER PRIMARY KEY,
  market_id TEXT,
  question TEXT,
  side TEXT,
  entry_price REAL,
  size REAL,
  claude_probability REAL,
  claude_confidence TEXT,
  ev_at_entry REAL,
  exit_target REAL,
  stop_loss REAL,
  time_stop TIMESTAMP,
  entry_timestamp TIMESTAMP,
  status TEXT,
  exit_price REAL,
  exit_timestamp TIMESTAMP,
  log_return REAL,
  prompt_version TEXT
)
```

---

## Layer 5: Self-Learning Agents

Four background agents. Outcome Reviewer runs independently. The other three form a daily pipeline chain.

### Agent 1: Outcome Reviewer (every hour)

- Checks all resolved markets against the bot's predictions
- For each resolved trade, records: predicted probability vs actual outcome, P&L, prompt version
- Writes structured outcome record to `memory/outcomes/`
- Question: "Did we get this right, and by how much?"

### Agent 2: Bias Detector (daily, pipeline step 1)

- Analyzes full outcome history for systematic patterns
- Examples: "Overestimate political markets by 14%", "Underestimate sports markets within 3 days of expiry"
- Stores correction factors in `memory/biases.json`
- Corrections injected into future Claude prompts

### Agent 3: Strategy Evolver (daily, pipeline step 2 — runs after Bias Detector)

- Reviews last 48 hours of trades holistically using Claude
- Can recommend adjustments to: EV threshold, exit targets, filter parameters, time stops
- Writes recommendations to `memory/strategy_log.md`
- **Does NOT auto-apply during paper trading** — flags in Discord for approval via reaction buttons
- Can be set to auto-apply once trusted

### Agent 4: Prompt Optimizer (daily, pipeline step 3 — runs after Strategy Evolver)

- Correlates prediction accuracy with prompt versions
- Identifies which prompt instructions improve or hurt accuracy
- Generates candidate new prompt version
- Backtests against last 50 resolved markets using Claude
- If backtest accuracy improves by >= 3%, saves as new version and flags in Discord

### Daily Pipeline Chain

```
Bias Detector -> Strategy Evolver -> Prompt Optimizer
     |                  |                   |
  writes           reads biases,       reads both,
  biases.json      writes strategy     generates prompt
```

### Memory Structure

```
memory/
  outcomes/           # One JSON file per resolved trade
  biases.json         # Correction factors by category/market type
  strategy_log.md     # Evolution history with timestamps
  lessons.json        # Accumulated learnings (key-value)
  prompt_scores.json  # Accuracy per prompt version
```

### Learning Feedback Loop

Each Claude prompt includes:
1. Base prompt (versioned)
2. Relevant bias corrections from `biases.json`
3. Top 5 most relevant lessons from `lessons.json`
4. Recent accuracy stats for self-awareness

---

## Layer 6: Discord Bot

### Two Channels

- `#polybot-trades` — trade alerts and daily digests
- `#polybot-control` — commands and strategy approval requests

### Commands

| Command | Description |
|---------|-------------|
| `!status` | Current state, open position count, bankroll, 24h P&L |
| `!positions` | All open positions with entry price, current price, unrealized P&L, exit targets |
| `!history [n]` | Last n closed trades with outcomes |
| `!performance` | Sharpe ratio, win rate, total P&L, avg hold time, best/worst trade |
| `!filters` | Show current market filter settings |
| `!setfilter <param> <value>` | Adjust a filter at runtime |
| `!pause` / `!resume` | Pause/resume trading without killing the bot |
| `!mode` | Show current mode (paper/live) |
| `!lessons` | Show top learnings from the memory system |
| `!agents` | Status of learning agents — last run, next run, latest findings |

### Automated Alerts

- **Trade opened:** Market, side, size, entry price, EV, exit target
- **Trade closed:** Market, outcome, P&L, hold time
- **Learning pipeline complete:** Summary of findings from all 3 chained agents
- **Strategy recommendation:** Posted with approve/reject reaction buttons
- **Errors:** API failures, balance issues, unexpected states
- **Daily digest:** 24h summary — trades, P&L, win rate, active positions

---

## Layer 7: Infrastructure & Project Structure

### Project Layout

```
polybot/
  core/
    scanner.py            # Market scanning + pre-filtering
    filters.py            # Filter logic, configurable thresholds
    websocket.py          # Real-time price feed for exit monitoring
  brain/
    claude_client.py      # Claude API integration (httpx async)
    prompt_builder.py     # Assembles prompt from base + biases + lessons
    prompts/
      v001.txt            # Versioned prompt files
  math_engine/
    decision_table.py     # Pre-computed EV/Kelly lookup tables
    returns.py            # Log returns, Sharpe ratio
    bayesian.py           # Mid-trade probability updates
  execution/
    base.py               # Abstract interface for both modes
    paper_trader.py       # Simulated execution against real prices
    live_trader.py        # Real CLOB execution (Phase 2)
  agents/
    outcome_reviewer.py   # Hourly - logs resolved trade outcomes
    bias_detector.py      # Daily pipeline step 1
    strategy_evolver.py   # Daily pipeline step 2
    prompt_optimizer.py   # Daily pipeline step 3
  memory/
    outcomes/             # One JSON per resolved trade
    biases.json
    strategy_log.md
    lessons.json
    prompt_scores.json
  discord_bot/
    bot.py                # Bot setup, event loop integration
    commands.py           # All ! commands
    alerts.py             # Automated notifications
  db/
    models.py             # SQLite schema + queries
  config/
    settings.yaml         # All configurable thresholds
    .env.example          # Template for secrets
  tests/
    ...                   # Unit tests per module
  Dockerfile              # For VPS deployment later
  requirements.txt
  main.py                 # Entry point - starts all async components
  README.md
```

### Dependencies

```
py-clob-client     # Polymarket CLOB SDK
anthropic          # Claude API (Sonnet 4.6)
httpx              # Async HTTP
web3               # Blockchain interactions
discord.py         # Discord bot
numpy              # Math operations
aiosqlite          # Async SQLite
python-dotenv      # Environment variables
pyyaml             # Config parsing
```

### Runtime

- Python 3.11+
- Single async process via `asyncio`
- `main.py` starts: scanner loop, WebSocket exit monitor, Discord bot, learning agent scheduler — all as concurrent async tasks
- Local development on Windows
- Docker for VPS deployment later

### Config-Driven

All thresholds, intervals, fractions, and filters live in `settings.yaml` and are adjustable via Discord commands at runtime.

---

## Deployment Path

1. **Phase 1 (now):** Paper trading on local Windows machine. Build learning infrastructure. Iterate prompts.
2. **Phase 2 (when ready):** Containerize with Docker. Deploy to $5/month VPS. Switch config to `mode: "live"`. Start with minimal capital (<$100).
3. **Phase 3 (proven):** Scale capital based on Sharpe ratio and win rate metrics. Enable auto-apply on Strategy Evolver.
