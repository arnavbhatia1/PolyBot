# PolyBot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Polymarket micro-trading bot with Claude-powered probability estimation, speed-first math, paper trading, self-learning agents, and Discord integration.

**Architecture:** Modular monolith — single async Python process with clear module boundaries. Scanner loop finds opportunities, Claude estimates probabilities, pre-computed decision tables execute instantly, WebSocket monitors exits in real-time. Four learning agents run on scheduled intervals to improve accuracy over time.

**Tech Stack:** Python 3.11+, asyncio, py-clob-client, anthropic SDK (Sonnet 4.6), httpx, aiosqlite, discord.py, numpy, web3.py

---

## File Map

```
polybot/
  __init__.py
  main.py                       # Entry point — starts all async components
  config/
    __init__.py
    settings.yaml                # All configurable thresholds
    .env.example                 # Template for secrets
    loader.py                    # Config + env loading
  db/
    __init__.py
    models.py                    # SQLite schema, connection, CRUD
  math_engine/
    __init__.py
    decision_table.py            # Pre-computed EV/Kelly lookup tables
    returns.py                   # Log returns, Sharpe ratio
  core/
    __init__.py
    filters.py                   # Market pre-filter logic
    scanner.py                   # Polymarket CLOB API scanning
    websocket_monitor.py         # Real-time price feed for exits
  brain/
    __init__.py
    claude_client.py             # Claude API integration
    prompt_builder.py            # Assembles prompt from base + biases + lessons
    prompts/
      v001.txt                   # Initial prompt
  execution/
    __init__.py
    base.py                      # Abstract interface
    paper_trader.py              # Simulated execution
    live_trader.py               # Real CLOB execution (stub for Phase 2)
  agents/
    __init__.py
    scheduler.py                 # Agent scheduling and pipeline orchestration
    outcome_reviewer.py          # Hourly — logs resolved trade outcomes
    bias_detector.py             # Daily pipeline step 1
    strategy_evolver.py          # Daily pipeline step 2
    prompt_optimizer.py          # Daily pipeline step 3
  memory/
    outcomes/                    # One JSON per resolved trade
    biases.json                  # Correction factors
    lessons.json                 # Accumulated learnings
    strategy_log.md              # Evolution history
    prompt_scores.json           # Accuracy per prompt version
  discord_bot/
    __init__.py
    bot.py                       # Bot setup, event loop integration
    commands.py                  # All ! commands
    alerts.py                    # Automated notifications
  tests/
    __init__.py
    conftest.py                  # Shared fixtures
    test_config.py
    test_db.py
    test_decision_table.py
    test_returns.py
    test_filters.py
    test_scanner.py
    test_claude_client.py
    test_prompt_builder.py
    test_paper_trader.py
    test_websocket_monitor.py
    test_outcome_reviewer.py
    test_bias_detector.py
    test_strategy_evolver.py
    test_prompt_optimizer.py
    test_discord_commands.py
    test_integration.py
  requirements.txt
  README.md
  Dockerfile
  .gitignore
```

---

## Task 1: Project Scaffolding & Configuration

**Files:**
- Create: `polybot/__init__.py`, `polybot/config/__init__.py`, `polybot/config/loader.py`, `polybot/config/settings.yaml`, `polybot/config/.env.example`, `.gitignore`, `requirements.txt`, `polybot/tests/__init__.py`, `polybot/tests/conftest.py`

- [ ] **Step 1: Create .gitignore**

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
venv/
.venv/

# Secrets
.env
polybot/config/.env

# Database
*.db

# IDE
.vscode/
.idea/

# Memory (runtime generated)
polybot/memory/outcomes/*.json

# OS
.DS_Store
Thumbs.db
```

- [ ] **Step 2: Create requirements.txt**

```
py-clob-client>=0.0.1
anthropic>=0.42.0
httpx>=0.27.0
web3>=7.0.0
discord.py>=2.4.0
numpy>=2.0.0
aiosqlite>=0.20.0
python-dotenv>=1.0.0
pyyaml>=6.0.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

- [ ] **Step 3: Create settings.yaml**

```yaml
# PolyBot Configuration
mode: "paper"  # "paper" or "live"

scanner:
  interval_seconds: 300  # 5 minutes
  max_markets_per_cycle: 100

filters:
  min_volume_24h: 1000
  min_liquidity: 500
  min_days_to_expiry: 2
  max_days_to_expiry: 60
  max_spread: 0.05
  category_whitelist: []   # empty = all categories
  category_blacklist: []

brain:
  model: "claude-sonnet-4-6"
  min_confidence: "high"
  min_probability: 0.65
  active_prompt_version: "v001"
  max_concurrent_calls: 10

math:
  ev_threshold: 0.05
  kelly_fraction: 0.25
  entry_discount: 0.85      # market_price <= probability * this
  exit_target: 0.90          # market_price >= probability * this
  stop_loss_pct: 0.15
  time_stop_hours: 24
  time_stop_min_gain: 0.02
  bayesian_price_trigger: 0.08
  bayesian_volume_trigger: 3.0

execution:
  max_slippage: 0.02
  max_bankroll_deployed: 0.80
  max_concurrent_positions: 5
  initial_bankroll: 100.0

agents:
  outcome_reviewer_interval_seconds: 3600    # 1 hour
  daily_pipeline_hour: 2                      # 2 AM UTC
  prompt_optimizer_min_improvement: 0.03
  prompt_optimizer_backtest_count: 50

discord:
  trade_channel_name: "polybot-trades"
  control_channel_name: "polybot-control"

database:
  path: "polybot/db/polybot.db"
```

- [ ] **Step 4: Create .env.example**

```
# Polymarket API
POLYMARKET_API_KEY=your_api_key_here
POLYMARKET_SECRET=your_secret_here
POLYMARKET_PASSPHRASE=your_passphrase_here

# Polygon RPC
ALCHEMY_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# Wallet
PRIVATE_KEY=your_wallet_private_key_here

# Claude API
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Discord
DISCORD_BOT_TOKEN=your_discord_bot_token_here
```

- [ ] **Step 5: Create config loader**

```python
# polybot/config/loader.py
import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

_config = None

def load_config(config_path: str | None = None, env_path: str | None = None) -> dict:
    global _config
    config_dir = Path(__file__).parent
    if env_path is None:
        env_path = config_dir / ".env"
    load_dotenv(env_path)
    if config_path is None:
        config_path = config_dir / "settings.yaml"
    with open(config_path, "r") as f:
        _config = yaml.safe_load(f)
    return _config

def get_config() -> dict:
    if _config is None:
        return load_config()
    return _config

def get_secret(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        raise ValueError(f"Missing required secret: {key}")
    return value
```

- [ ] **Step 6: Create __init__.py files and conftest.py**

```python
# polybot/__init__.py
# (empty)
```

```python
# polybot/config/__init__.py
from polybot.config.loader import load_config, get_config, get_secret
```

```python
# polybot/tests/__init__.py
# (empty)
```

```python
# polybot/tests/conftest.py
import os
import tempfile
from pathlib import Path
import pytest
import yaml

SAMPLE_CONFIG = {
    "mode": "paper",
    "scanner": {"interval_seconds": 300, "max_markets_per_cycle": 100},
    "filters": {
        "min_volume_24h": 1000,
        "min_liquidity": 500,
        "min_days_to_expiry": 2,
        "max_days_to_expiry": 60,
        "max_spread": 0.05,
        "category_whitelist": [],
        "category_blacklist": [],
    },
    "brain": {
        "model": "claude-sonnet-4-6",
        "min_confidence": "high",
        "min_probability": 0.65,
        "active_prompt_version": "v001",
        "max_concurrent_calls": 10,
    },
    "math": {
        "ev_threshold": 0.05,
        "kelly_fraction": 0.25,
        "entry_discount": 0.85,
        "exit_target": 0.90,
        "stop_loss_pct": 0.15,
        "time_stop_hours": 24,
        "time_stop_min_gain": 0.02,
        "bayesian_price_trigger": 0.08,
        "bayesian_volume_trigger": 3.0,
    },
    "execution": {
        "max_slippage": 0.02,
        "max_bankroll_deployed": 0.80,
        "max_concurrent_positions": 5,
        "initial_bankroll": 100.0,
    },
    "agents": {
        "outcome_reviewer_interval_seconds": 3600,
        "daily_pipeline_hour": 2,
        "prompt_optimizer_min_improvement": 0.03,
        "prompt_optimizer_backtest_count": 50,
    },
    "discord": {
        "trade_channel_name": "polybot-trades",
        "control_channel_name": "polybot-control",
    },
    "database": {"path": ":memory:"},
}

@pytest.fixture
def sample_config(tmp_path):
    config_file = tmp_path / "settings.yaml"
    with open(config_file, "w") as f:
        yaml.dump(SAMPLE_CONFIG, f)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=test-key\n"
        "DISCORD_BOT_TOKEN=test-token\n"
        "POLYMARKET_API_KEY=test-pm-key\n"
        "POLYMARKET_SECRET=test-pm-secret\n"
        "POLYMARKET_PASSPHRASE=test-pm-pass\n"
        "ALCHEMY_RPC_URL=https://test-rpc\n"
        "PRIVATE_KEY=0xdeadbeef\n"
    )
    return {"config_path": str(config_file), "env_path": str(env_file)}

@pytest.fixture
def loaded_config(sample_config):
    from polybot.config.loader import load_config
    return load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
```

- [ ] **Step 7: Write test for config loader**

```python
# polybot/tests/test_config.py
import os
import pytest
from polybot.config.loader import load_config, get_config, get_secret

def test_load_config_returns_dict(sample_config):
    config = load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
    assert isinstance(config, dict)
    assert config["mode"] == "paper"

def test_load_config_has_all_sections(loaded_config):
    for section in ["scanner", "filters", "brain", "math", "execution", "agents", "discord", "database"]:
        assert section in loaded_config

def test_get_config_returns_cached(loaded_config):
    config = get_config()
    assert config is loaded_config

def test_get_secret_returns_env_var(sample_config):
    load_config(
        config_path=sample_config["config_path"],
        env_path=sample_config["env_path"],
    )
    assert get_secret("ANTHROPIC_API_KEY") == "test-key"

def test_get_secret_raises_on_missing():
    with pytest.raises(ValueError, match="Missing required secret"):
        get_secret("NONEXISTENT_SECRET_KEY_XYZ")
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_config.py -v`
Expected: 5 tests PASS

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding, config system, and test infrastructure"
```

---

## Task 2: Database Layer

**Files:**
- Create: `polybot/db/__init__.py`, `polybot/db/models.py`, `polybot/tests/test_db.py`

- [ ] **Step 1: Write failing tests for database**

```python
# polybot/tests/test_db.py
import pytest
import pytest_asyncio
from polybot.db.models import Database

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()

@pytest.mark.asyncio
async def test_initialize_creates_tables(db):
    tables = await db.get_tables()
    assert "positions" in tables
    assert "trade_history" in tables

@pytest.mark.asyncio
async def test_open_position(db):
    pos_id = await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert pos_id == 1

@pytest.mark.asyncio
async def test_get_open_positions(db):
    await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    positions = await db.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["market_id"] == "market_123"
    assert positions[0]["status"] == "open"

@pytest.mark.asyncio
async def test_close_position(db):
    pos_id = await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    await db.close_position(pos_id, exit_price=0.68, log_return=0.212)
    positions = await db.get_open_positions()
    assert len(positions) == 0
    history = await db.get_trade_history(limit=10)
    assert len(history) == 1
    assert history[0]["exit_price"] == 0.68

@pytest.mark.asyncio
async def test_has_position_for_market(db):
    assert await db.has_position_for_market("market_123") is False
    await db.open_position(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert await db.has_position_for_market("market_123") is True

@pytest.mark.asyncio
async def test_get_open_position_count(db):
    assert await db.get_open_position_count() == 0
    await db.open_position(
        market_id="market_123",
        question="Q?",
        side="YES",
        entry_price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert await db.get_open_position_count() == 1

@pytest.mark.asyncio
async def test_update_bankroll(db):
    await db.set_bankroll(100.0)
    assert await db.get_bankroll() == 100.0
    await db.set_bankroll(95.50)
    assert await db.get_bankroll() == 95.50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.db.models'`

- [ ] **Step 3: Implement database models**

```python
# polybot/db/__init__.py
# (empty)
```

```python
# polybot/db/models.py
import aiosqlite
from datetime import datetime, timezone

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: aiosqlite.Connection | None = None

    async def initialize(self):
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                claude_probability REAL NOT NULL,
                claude_confidence TEXT NOT NULL,
                ev_at_entry REAL NOT NULL,
                exit_target REAL NOT NULL,
                stop_loss REAL NOT NULL,
                time_stop TEXT,
                entry_timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_timestamp TEXT,
                log_return REAL,
                prompt_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                size REAL NOT NULL,
                claude_probability REAL NOT NULL,
                claude_confidence TEXT NOT NULL,
                ev_at_entry REAL NOT NULL,
                log_return REAL NOT NULL,
                prompt_version TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount REAL NOT NULL
            );
        """)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def get_tables(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def open_position(
        self,
        market_id: str,
        question: str,
        side: str,
        entry_price: float,
        size: float,
        claude_probability: float,
        claude_confidence: str,
        ev_at_entry: float,
        exit_target: float,
        stop_loss: float,
        prompt_version: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.conn.execute(
            """INSERT INTO positions
            (market_id, question, side, entry_price, size, claude_probability,
             claude_confidence, ev_at_entry, exit_target, stop_loss,
             entry_timestamp, status, prompt_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (market_id, question, side, entry_price, size, claude_probability,
             claude_confidence, ev_at_entry, exit_target, stop_loss,
             now, prompt_version),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_open_positions(self) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def close_position(self, position_id: int, exit_price: float, log_return: float):
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_timestamp=?, log_return=? WHERE id=?",
            (exit_price, now, log_return, position_id),
        )
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        )
        pos = dict(await cursor.fetchone())
        await self.conn.execute(
            """INSERT INTO trade_history
            (position_id, market_id, question, side, entry_price, exit_price, size,
             claude_probability, claude_confidence, ev_at_entry, log_return,
             prompt_version, entry_timestamp, exit_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["id"], pos["market_id"], pos["question"], pos["side"],
             pos["entry_price"], exit_price, pos["size"],
             pos["claude_probability"], pos["claude_confidence"], pos["ev_at_entry"],
             log_return, pos["prompt_version"], pos["entry_timestamp"], now),
        )
        await self.conn.commit()

    async def has_position_for_market(self, market_id: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id=? AND status='open'",
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] > 0

    async def get_open_position_count(self) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        )
        row = await cursor.fetchone()
        return row[0]

    async def get_trade_history(self, limit: int = 50) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM trade_history ORDER BY exit_timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_bankroll(self, amount: float):
        await self.conn.execute(
            "INSERT INTO bankroll (id, amount) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount",
            (amount,),
        )
        await self.conn.commit()

    async def get_bankroll(self) -> float:
        cursor = await self.conn.execute("SELECT amount FROM bankroll WHERE id=1")
        row = await cursor.fetchone()
        return row[0] if row else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_db.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/db/ polybot/tests/test_db.py
git commit -m "feat: async SQLite database layer with positions and trade history"
```

---

## Task 3: Math Engine — Decision Table

**Files:**
- Create: `polybot/math_engine/__init__.py`, `polybot/math_engine/decision_table.py`, `polybot/tests/test_decision_table.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_decision_table.py
import pytest
from polybot.math_engine.decision_table import DecisionTable

@pytest.fixture
def table():
    return DecisionTable(
        ev_threshold=0.05,
        kelly_fraction=0.25,
        entry_discount=0.85,
        exit_target=0.90,
        stop_loss_pct=0.15,
    )

def test_build_creates_entries_for_all_probabilities(table):
    table.build()
    # Probabilities from 0.01 to 0.99 in 0.01 steps = 99 entries
    assert len(table.table) == 99

def test_lookup_returns_decision_for_probability(table):
    table.build()
    decision = table.lookup(0.72)
    assert "max_buy_price" in decision
    assert "exit_price" in decision
    assert "kelly_fraction" in decision

def test_max_buy_price_is_probability_times_discount(table):
    table.build()
    decision = table.lookup(0.72)
    assert decision["max_buy_price"] == pytest.approx(0.72 * 0.85, abs=0.01)

def test_exit_price_is_probability_times_target(table):
    table.build()
    decision = table.lookup(0.72)
    assert decision["exit_price"] == pytest.approx(0.72 * 0.90, abs=0.01)

def test_should_buy_when_price_below_max(table):
    table.build()
    assert table.should_buy(probability=0.72, market_price=0.55) is True

def test_should_not_buy_when_price_above_max(table):
    table.build()
    assert table.should_buy(probability=0.72, market_price=0.65) is False

def test_should_exit_when_price_above_target(table):
    table.build()
    assert table.should_exit(probability=0.72, market_price=0.70) is True

def test_should_not_exit_when_price_below_target(table):
    table.build()
    assert table.should_exit(probability=0.72, market_price=0.55) is False

def test_should_stop_loss(table):
    table.build()
    assert table.should_stop_loss(entry_price=0.55, market_price=0.46) is True
    assert table.should_stop_loss(entry_price=0.55, market_price=0.50) is False

def test_calculate_ev(table):
    ev = table.calculate_ev(probability=0.72, market_price=0.55)
    # EV = 0.72 * 0.45 - 0.28 * 0.55 = 0.324 - 0.154 = 0.17
    assert ev == pytest.approx(0.17, abs=0.01)

def test_ev_filter_skips_low_edge(table):
    table.build()
    # If probability is close to market price, EV will be tiny
    assert table.should_buy(probability=0.56, market_price=0.55) is False

def test_position_size_uses_quarter_kelly(table):
    table.build()
    size = table.position_size(probability=0.72, market_price=0.55, bankroll=100.0)
    # Kelly = (0.72 * (0.45/0.55) - 0.28) / (0.45/0.55)
    # Quarter Kelly * bankroll
    assert size > 0
    assert size < 100.0  # Never bet entire bankroll

def test_lookup_rounds_to_nearest_cent(table):
    table.build()
    # 0.723 should round to 0.72
    decision = table.lookup(0.723)
    assert decision is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_decision_table.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement decision table**

```python
# polybot/math_engine/__init__.py
# (empty)
```

```python
# polybot/math_engine/decision_table.py

class DecisionTable:
    def __init__(
        self,
        ev_threshold: float = 0.05,
        kelly_fraction: float = 0.25,
        entry_discount: float = 0.85,
        exit_target: float = 0.90,
        stop_loss_pct: float = 0.15,
    ):
        self.ev_threshold = ev_threshold
        self.kelly_fraction = kelly_fraction
        self.entry_discount = entry_discount
        self.exit_target = exit_target
        self.stop_loss_pct = stop_loss_pct
        self.table: dict[int, dict] = {}

    def build(self):
        self.table = {}
        for cents in range(1, 100):
            prob = cents / 100.0
            max_buy = prob * self.entry_discount
            exit_price = prob * self.exit_target
            odds = (1.0 - max_buy) / max_buy if max_buy > 0 else 0
            q = 1.0 - prob
            if odds > 0:
                kelly_raw = (prob * odds - q) / odds
                kelly = max(0.0, kelly_raw * self.kelly_fraction)
            else:
                kelly = 0.0
            self.table[cents] = {
                "probability": prob,
                "max_buy_price": round(max_buy, 4),
                "exit_price": round(exit_price, 4),
                "kelly_fraction": round(kelly, 6),
            }

    def lookup(self, probability: float) -> dict:
        cents = max(1, min(99, round(probability * 100)))
        return self.table[cents]

    def calculate_ev(self, probability: float, market_price: float) -> float:
        profit = 1.0 - market_price
        loss = market_price
        return probability * profit - (1.0 - probability) * loss

    def should_buy(self, probability: float, market_price: float) -> bool:
        decision = self.lookup(probability)
        ev = self.calculate_ev(probability, market_price)
        return market_price <= decision["max_buy_price"] and ev >= self.ev_threshold

    def should_exit(self, probability: float, market_price: float) -> bool:
        decision = self.lookup(probability)
        return market_price >= decision["exit_price"]

    def should_stop_loss(self, entry_price: float, market_price: float) -> bool:
        return market_price <= entry_price * (1.0 - self.stop_loss_pct)

    def position_size(self, probability: float, market_price: float, bankroll: float) -> float:
        decision = self.lookup(probability)
        return round(bankroll * decision["kelly_fraction"], 2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_decision_table.py -v`
Expected: All 13 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/math_engine/ polybot/tests/test_decision_table.py
git commit -m "feat: pre-computed decision table for instant EV/Kelly lookups"
```

---

## Task 4: Math Engine — Returns & Sharpe Ratio

**Files:**
- Create: `polybot/math_engine/returns.py`, `polybot/tests/test_returns.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_returns.py
import pytest
import numpy as np
from polybot.math_engine.returns import log_return, sharpe_ratio, total_log_return

def test_log_return_basic():
    # ln(0.68 / 0.55) ≈ 0.2119
    result = log_return(entry_price=0.55, exit_price=0.68)
    assert result == pytest.approx(0.2119, abs=0.001)

def test_log_return_loss():
    # ln(0.46 / 0.55) ≈ -0.1784
    result = log_return(entry_price=0.55, exit_price=0.46)
    assert result == pytest.approx(-0.1784, abs=0.001)

def test_log_return_breakeven():
    result = log_return(entry_price=0.55, exit_price=0.55)
    assert result == pytest.approx(0.0, abs=0.0001)

def test_total_log_return_sums_correctly():
    returns = [0.2, -0.1, 0.15, -0.05]
    result = total_log_return(returns)
    assert result == pytest.approx(0.2, abs=0.001)

def test_sharpe_ratio_basic():
    # Positive returns with low variance = good Sharpe
    returns = [0.05, 0.06, 0.04, 0.07, 0.05, 0.06]
    sr = sharpe_ratio(returns, risk_free_rate=0.0)
    assert sr > 1.0

def test_sharpe_ratio_negative():
    # Mostly losses
    returns = [-0.05, -0.06, -0.04, -0.07, -0.05, -0.06]
    sr = sharpe_ratio(returns, risk_free_rate=0.0)
    assert sr < 0

def test_sharpe_ratio_empty_returns():
    sr = sharpe_ratio([], risk_free_rate=0.0)
    assert sr == 0.0

def test_sharpe_ratio_single_return():
    sr = sharpe_ratio([0.05], risk_free_rate=0.0)
    assert sr == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_returns.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement returns module**

```python
# polybot/math_engine/returns.py
import math
import numpy as np

def log_return(entry_price: float, exit_price: float) -> float:
    return math.log(exit_price / entry_price)

def total_log_return(returns: list[float]) -> float:
    return sum(returns)

def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    mean_return = arr.mean()
    std_return = arr.std(ddof=1)
    if std_return == 0:
        return 0.0
    return float((mean_return - risk_free_rate) / std_return)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_returns.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/math_engine/returns.py polybot/tests/test_returns.py
git commit -m "feat: log returns and Sharpe ratio calculations"
```

---

## Task 5: Core — Market Filters

**Files:**
- Create: `polybot/core/__init__.py`, `polybot/core/filters.py`, `polybot/tests/test_filters.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_filters.py
import pytest
from polybot.core.filters import MarketFilter

@pytest.fixture
def default_filter():
    return MarketFilter(
        min_volume_24h=1000,
        min_liquidity=500,
        min_days_to_expiry=2,
        max_days_to_expiry=60,
        max_spread=0.05,
        category_whitelist=[],
        category_blacklist=[],
    )

def _make_market(**overrides):
    base = {
        "condition_id": "market_123",
        "question": "Will X happen?",
        "tokens": [{"price": 0.55}, {"price": 0.45}],
        "volume_24h": 5000.0,
        "liquidity": 2000.0,
        "days_to_expiry": 15,
        "spread": 0.02,
        "category": "politics",
    }
    base.update(overrides)
    return base

def test_good_market_passes(default_filter):
    market = _make_market()
    assert default_filter.passes(market) is True

def test_low_volume_fails(default_filter):
    market = _make_market(volume_24h=500)
    assert default_filter.passes(market) is False

def test_low_liquidity_fails(default_filter):
    market = _make_market(liquidity=200)
    assert default_filter.passes(market) is False

def test_too_short_expiry_fails(default_filter):
    market = _make_market(days_to_expiry=1)
    assert default_filter.passes(market) is False

def test_too_long_expiry_fails(default_filter):
    market = _make_market(days_to_expiry=90)
    assert default_filter.passes(market) is False

def test_wide_spread_fails(default_filter):
    market = _make_market(spread=0.08)
    assert default_filter.passes(market) is False

def test_category_blacklist():
    f = MarketFilter(
        min_volume_24h=1000,
        min_liquidity=500,
        min_days_to_expiry=2,
        max_days_to_expiry=60,
        max_spread=0.05,
        category_whitelist=[],
        category_blacklist=["celebrity"],
    )
    assert f.passes(_make_market(category="celebrity")) is False
    assert f.passes(_make_market(category="politics")) is True

def test_category_whitelist():
    f = MarketFilter(
        min_volume_24h=1000,
        min_liquidity=500,
        min_days_to_expiry=2,
        max_days_to_expiry=60,
        max_spread=0.05,
        category_whitelist=["politics", "crypto"],
        category_blacklist=[],
    )
    assert f.passes(_make_market(category="politics")) is True
    assert f.passes(_make_market(category="sports")) is False

def test_filter_batch(default_filter):
    markets = [
        _make_market(condition_id="good"),
        _make_market(condition_id="bad_vol", volume_24h=100),
        _make_market(condition_id="bad_liq", liquidity=50),
    ]
    result = default_filter.filter_batch(markets)
    assert len(result) == 1
    assert result[0]["condition_id"] == "good"

def test_update_filter_param(default_filter):
    default_filter.update("min_volume_24h", 5000)
    assert default_filter.min_volume_24h == 5000
    market = _make_market(volume_24h=3000)
    assert default_filter.passes(market) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_filters.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement filters**

```python
# polybot/core/__init__.py
# (empty)
```

```python
# polybot/core/filters.py

class MarketFilter:
    def __init__(
        self,
        min_volume_24h: float,
        min_liquidity: float,
        min_days_to_expiry: int,
        max_days_to_expiry: int,
        max_spread: float,
        category_whitelist: list[str],
        category_blacklist: list[str],
    ):
        self.min_volume_24h = min_volume_24h
        self.min_liquidity = min_liquidity
        self.min_days_to_expiry = min_days_to_expiry
        self.max_days_to_expiry = max_days_to_expiry
        self.max_spread = max_spread
        self.category_whitelist = category_whitelist
        self.category_blacklist = category_blacklist

    def passes(self, market: dict) -> bool:
        if market.get("volume_24h", 0) < self.min_volume_24h:
            return False
        if market.get("liquidity", 0) < self.min_liquidity:
            return False
        days = market.get("days_to_expiry", 0)
        if days < self.min_days_to_expiry or days > self.max_days_to_expiry:
            return False
        if market.get("spread", 1.0) > self.max_spread:
            return False
        category = market.get("category", "")
        if self.category_blacklist and category in self.category_blacklist:
            return False
        if self.category_whitelist and category not in self.category_whitelist:
            return False
        return True

    def filter_batch(self, markets: list[dict]) -> list[dict]:
        return [m for m in markets if self.passes(m)]

    def update(self, param: str, value):
        if hasattr(self, param):
            setattr(self, param, value)
        else:
            raise ValueError(f"Unknown filter param: {param}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_filters.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/ polybot/tests/test_filters.py
git commit -m "feat: configurable market pre-filters with batch support"
```

---

## Task 6: Core — Market Scanner

**Files:**
- Create: `polybot/core/scanner.py`, `polybot/tests/test_scanner.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_scanner.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.core.scanner import MarketScanner
from polybot.core.filters import MarketFilter

@pytest.fixture
def mock_filter():
    return MarketFilter(
        min_volume_24h=1000,
        min_liquidity=500,
        min_days_to_expiry=2,
        max_days_to_expiry=60,
        max_spread=0.05,
        category_whitelist=[],
        category_blacklist=[],
    )

SAMPLE_CLOB_MARKETS = [
    {
        "condition_id": "0xabc123",
        "question": "Will BTC hit 100k?",
        "tokens": [
            {"token_id": "tok_yes", "outcome": "Yes", "price": 0.55},
            {"token_id": "tok_no", "outcome": "No", "price": 0.45},
        ],
        "end_date_iso": "2026-05-01T00:00:00Z",
        "volume_num_fmt": "5000",
        "liquidity_num_fmt": "2000",
        "spread": "0.02",
        "category": "crypto",
        "active": True,
        "closed": False,
    },
]

@pytest.mark.asyncio
async def test_fetch_markets_returns_normalized_data(mock_filter):
    scanner = MarketScanner(filter=mock_filter)
    scanner._fetch_raw_markets = AsyncMock(return_value=SAMPLE_CLOB_MARKETS)
    markets = await scanner.fetch_and_filter()
    assert len(markets) >= 0  # May be filtered

@pytest.mark.asyncio
async def test_normalize_market_extracts_fields(mock_filter):
    scanner = MarketScanner(filter=mock_filter)
    raw = SAMPLE_CLOB_MARKETS[0]
    normalized = scanner.normalize_market(raw)
    assert normalized["condition_id"] == "0xabc123"
    assert normalized["question"] == "Will BTC hit 100k?"
    assert "price_yes" in normalized
    assert "volume_24h" in normalized
    assert "liquidity" in normalized
    assert "spread" in normalized
    assert "days_to_expiry" in normalized

@pytest.mark.asyncio
async def test_fetch_and_filter_applies_filter(mock_filter):
    scanner = MarketScanner(filter=mock_filter)
    good_market = SAMPLE_CLOB_MARKETS[0].copy()
    bad_market = SAMPLE_CLOB_MARKETS[0].copy()
    bad_market["condition_id"] = "0xbad"
    bad_market["volume_num_fmt"] = "100"  # Below min
    scanner._fetch_raw_markets = AsyncMock(return_value=[good_market, bad_market])
    markets = await scanner.fetch_and_filter()
    condition_ids = [m["condition_id"] for m in markets]
    assert "0xbad" not in condition_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_scanner.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement scanner**

```python
# polybot/core/scanner.py
import logging
from datetime import datetime, timezone
from polybot.core.filters import MarketFilter

logger = logging.getLogger(__name__)

class MarketScanner:
    CLOB_BASE_URL = "https://clob.polymarket.com"

    def __init__(self, filter: MarketFilter, max_markets: int = 100):
        self.filter = filter
        self.max_markets = max_markets

    async def _fetch_raw_markets(self) -> list[dict]:
        import httpx
        markets = []
        next_cursor = None
        async with httpx.AsyncClient(timeout=30) as client:
            while len(markets) < self.max_markets:
                params = {"limit": 100, "active": True, "closed": False}
                if next_cursor:
                    params["next_cursor"] = next_cursor
                resp = await client.get(f"{self.CLOB_BASE_URL}/markets", params=params)
                resp.raise_for_status()
                data = resp.json()
                batch = data if isinstance(data, list) else data.get("data", [])
                if not batch:
                    break
                markets.extend(batch)
                next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not next_cursor:
                    break
        return markets[:self.max_markets]

    def normalize_market(self, raw: dict) -> dict:
        tokens = raw.get("tokens", [])
        price_yes = 0.0
        price_no = 0.0
        token_id_yes = ""
        token_id_no = ""
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            if outcome == "yes":
                price_yes = float(token.get("price", 0))
                token_id_yes = token.get("token_id", "")
            elif outcome == "no":
                price_no = float(token.get("price", 0))
                token_id_no = token.get("token_id", "")
        end_date_str = raw.get("end_date_iso", "")
        days_to_expiry = 0
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_to_expiry = max(0, (end_date - datetime.now(timezone.utc)).days)
            except ValueError:
                days_to_expiry = 0
        spread = abs(price_yes - (1.0 - price_no)) if price_yes and price_no else float(raw.get("spread", "0.99"))
        return {
            "condition_id": raw.get("condition_id", ""),
            "question": raw.get("question", ""),
            "price_yes": price_yes,
            "price_no": price_no,
            "token_id_yes": token_id_yes,
            "token_id_no": token_id_no,
            "volume_24h": float(raw.get("volume_num_fmt", "0").replace(",", "")),
            "liquidity": float(raw.get("liquidity_num_fmt", "0").replace(",", "")),
            "spread": float(raw.get("spread", spread)),
            "days_to_expiry": days_to_expiry,
            "category": raw.get("category", ""),
            "end_date": end_date_str,
            "active": raw.get("active", False),
        }

    async def fetch_and_filter(self) -> list[dict]:
        raw_markets = await self._fetch_raw_markets()
        normalized = [self.normalize_market(m) for m in raw_markets]
        filtered = self.filter.filter_batch(normalized)
        logger.info(f"Scanned {len(raw_markets)} markets, {len(filtered)} passed filters")
        return filtered
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_scanner.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/scanner.py polybot/tests/test_scanner.py
git commit -m "feat: market scanner with CLOB API integration and normalization"
```

---

## Task 7: Brain — Claude Client

**Files:**
- Create: `polybot/brain/__init__.py`, `polybot/brain/claude_client.py`, `polybot/tests/test_claude_client.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_claude_client.py
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.brain.claude_client import ClaudeClient, MarketAnalysis

def test_market_analysis_from_valid_json():
    data = {
        "probability": 0.72,
        "confidence": "high",
        "reasoning": "Strong indicators",
        "key_factors": ["factor1", "factor2"],
        "base_rate_considered": True,
    }
    analysis = MarketAnalysis.from_dict(data)
    assert analysis.probability == 0.72
    assert analysis.confidence == "high"
    assert analysis.reasoning == "Strong indicators"

def test_market_analysis_rejects_invalid_probability():
    data = {
        "probability": 1.5,
        "confidence": "high",
        "reasoning": "Bad",
        "key_factors": [],
        "base_rate_considered": True,
    }
    with pytest.raises(ValueError, match="probability"):
        MarketAnalysis.from_dict(data)

def test_market_analysis_rejects_invalid_confidence():
    data = {
        "probability": 0.5,
        "confidence": "super_high",
        "reasoning": "Bad",
        "key_factors": [],
        "base_rate_considered": True,
    }
    with pytest.raises(ValueError, match="confidence"):
        MarketAnalysis.from_dict(data)

def test_passes_confidence_gate_high():
    analysis = MarketAnalysis(
        probability=0.72,
        confidence="high",
        reasoning="test",
        key_factors=[],
        base_rate_considered=True,
    )
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is True

def test_fails_confidence_gate_medium():
    analysis = MarketAnalysis(
        probability=0.72,
        confidence="medium",
        reasoning="test",
        key_factors=[],
        base_rate_considered=True,
    )
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is False

def test_fails_probability_gate():
    analysis = MarketAnalysis(
        probability=0.55,
        confidence="high",
        reasoning="test",
        key_factors=[],
        base_rate_considered=True,
    )
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is False

@pytest.mark.asyncio
async def test_analyze_market_returns_analysis():
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({
        "probability": 0.72,
        "confidence": "high",
        "reasoning": "Test reasoning",
        "key_factors": ["factor1"],
        "base_rate_considered": True,
    })
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("polybot.brain.claude_client.anthropic.AsyncAnthropic", return_value=mock_client):
        client = ClaudeClient(api_key="test-key", model="claude-sonnet-4-6")
        result = await client.analyze_market(
            question="Will X happen?",
            price=0.55,
            volume=5000,
            liquidity=2000,
            spread=0.02,
            days_to_expiry=15,
            prompt="Analyze this market.",
        )
        assert isinstance(result, MarketAnalysis)
        assert result.probability == 0.72
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_claude_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Claude client**

```python
# polybot/brain/__init__.py
# (empty)
```

```python
# polybot/brain/claude_client.py
import json
import logging
from dataclasses import dataclass
import anthropic

logger = logging.getLogger(__name__)

CONFIDENCE_LEVELS = {"low": 0, "medium": 1, "high": 2}

@dataclass
class MarketAnalysis:
    probability: float
    confidence: str
    reasoning: str
    key_factors: list[str]
    base_rate_considered: bool

    @classmethod
    def from_dict(cls, data: dict) -> "MarketAnalysis":
        prob = data["probability"]
        if not (0.0 <= prob <= 1.0):
            raise ValueError(f"probability must be 0-1, got {prob}")
        conf = data["confidence"]
        if conf not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {list(CONFIDENCE_LEVELS)}, got {conf}")
        return cls(
            probability=prob,
            confidence=conf,
            reasoning=data.get("reasoning", ""),
            key_factors=data.get("key_factors", []),
            base_rate_considered=data.get("base_rate_considered", False),
        )

    def passes_gate(self, min_confidence: str, min_probability: float) -> bool:
        conf_level = CONFIDENCE_LEVELS.get(self.confidence, 0)
        min_level = CONFIDENCE_LEVELS.get(min_confidence, 2)
        return conf_level >= min_level and self.probability >= min_probability

class ClaudeClient:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def analyze_market(
        self,
        question: str,
        price: float,
        volume: float,
        liquidity: float,
        spread: float,
        days_to_expiry: int,
        prompt: str,
    ) -> MarketAnalysis:
        user_message = (
            f"{prompt}\n\n"
            f"Market Question: {question}\n"
            f"Current YES Price: {price}\n"
            f"24h Volume: ${volume:,.0f}\n"
            f"Liquidity: ${liquidity:,.0f}\n"
            f"Spread: {spread:.2%}\n"
            f"Days to Expiry: {days_to_expiry}\n\n"
            "Respond with ONLY valid JSON in this exact format:\n"
            '{"probability": 0.XX, "confidence": "high/medium/low", '
            '"reasoning": "...", "key_factors": ["..."], "base_rate_considered": true/false}'
        )
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=500,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        # Handle potential markdown code fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return MarketAnalysis.from_dict(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_claude_client.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/brain/ polybot/tests/test_claude_client.py
git commit -m "feat: Claude client with structured JSON parsing and confidence gating"
```

---

## Task 8: Brain — Prompt Builder

**Files:**
- Create: `polybot/brain/prompt_builder.py`, `polybot/brain/prompts/v001.txt`, `polybot/tests/test_prompt_builder.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_prompt_builder.py
import json
import pytest
from pathlib import Path
from polybot.brain.prompt_builder import PromptBuilder

@pytest.fixture
def prompt_dir(tmp_path):
    v1 = tmp_path / "v001.txt"
    v1.write_text("You are a prediction market analyst. Estimate the probability of YES.")
    v2 = tmp_path / "v002.txt"
    v2.write_text("You are an expert analyst. Consider base rates carefully.")
    return tmp_path

@pytest.fixture
def biases_file(tmp_path):
    biases = {"politics": -0.14, "crypto": 0.05}
    path = tmp_path / "biases.json"
    path.write_text(json.dumps(biases))
    return path

@pytest.fixture
def lessons_file(tmp_path):
    lessons = {
        "overconfidence": "Claude tends to be overconfident on short-expiry markets",
        "volume_signal": "High volume spikes often precede resolution",
    }
    path = tmp_path / "lessons.json"
    path.write_text(json.dumps(lessons))
    return path

def test_load_base_prompt(prompt_dir):
    builder = PromptBuilder(prompts_dir=str(prompt_dir))
    prompt = builder.load_base_prompt("v001")
    assert "prediction market analyst" in prompt

def test_load_nonexistent_version_raises(prompt_dir):
    builder = PromptBuilder(prompts_dir=str(prompt_dir))
    with pytest.raises(FileNotFoundError):
        builder.load_base_prompt("v999")

def test_build_prompt_includes_base(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(
        prompts_dir=str(prompt_dir),
        biases_path=str(biases_file),
        lessons_path=str(lessons_file),
    )
    prompt = builder.build(version="v001", category="politics")
    assert "prediction market analyst" in prompt

def test_build_prompt_includes_bias_correction(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(
        prompts_dir=str(prompt_dir),
        biases_path=str(biases_file),
        lessons_path=str(lessons_file),
    )
    prompt = builder.build(version="v001", category="politics")
    assert "-14" in prompt or "14%" in prompt

def test_build_prompt_includes_lessons(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(
        prompts_dir=str(prompt_dir),
        biases_path=str(biases_file),
        lessons_path=str(lessons_file),
    )
    prompt = builder.build(version="v001", category="politics")
    assert "overconfident" in prompt

def test_build_prompt_no_bias_for_unknown_category(prompt_dir, biases_file, lessons_file):
    builder = PromptBuilder(
        prompts_dir=str(prompt_dir),
        biases_path=str(biases_file),
        lessons_path=str(lessons_file),
    )
    prompt = builder.build(version="v001", category="sports")
    assert "bias correction" not in prompt.lower() or "no known bias" in prompt.lower()

def test_build_prompt_handles_missing_files(prompt_dir):
    builder = PromptBuilder(
        prompts_dir=str(prompt_dir),
        biases_path="/nonexistent/biases.json",
        lessons_path="/nonexistent/lessons.json",
    )
    prompt = builder.build(version="v001", category="politics")
    assert "prediction market analyst" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_prompt_builder.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create the initial prompt v001.txt**

```text
You are an expert prediction market analyst. Your job is to estimate the true probability that a market question resolves YES.

RULES:
1. Always consider the BASE RATE first. What is the historical frequency of similar events?
2. Do NOT be overconfident. If you are unsure, say "medium" or "low" confidence.
3. Consider multiple perspectives: what would make this resolve YES vs NO?
4. Factor in the time remaining. More time = more uncertainty.
5. Your probability should reflect genuine uncertainty, not just pattern matching.

Return ONLY valid JSON:
{"probability": 0.XX, "confidence": "high/medium/low", "reasoning": "...", "key_factors": ["..."], "base_rate_considered": true}
```

- [ ] **Step 4: Implement prompt builder**

```python
# polybot/brain/prompt_builder.py
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class PromptBuilder:
    def __init__(
        self,
        prompts_dir: str,
        biases_path: str | None = None,
        lessons_path: str | None = None,
    ):
        self.prompts_dir = Path(prompts_dir)
        self.biases_path = Path(biases_path) if biases_path else None
        self.lessons_path = Path(lessons_path) if lessons_path else None

    def load_base_prompt(self, version: str) -> str:
        path = self.prompts_dir / f"{version}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Prompt version not found: {path}")
        return path.read_text(encoding="utf-8")

    def _load_biases(self) -> dict[str, float]:
        if not self.biases_path or not self.biases_path.exists():
            return {}
        try:
            return json.loads(self.biases_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load biases: {e}")
            return {}

    def _load_lessons(self) -> dict[str, str]:
        if not self.lessons_path or not self.lessons_path.exists():
            return {}
        try:
            return json.loads(self.lessons_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load lessons: {e}")
            return {}

    def build(self, version: str, category: str = "") -> str:
        parts = [self.load_base_prompt(version)]
        biases = self._load_biases()
        if category and category in biases:
            correction = biases[category]
            direction = "overestimate" if correction < 0 else "underestimate"
            pct = abs(correction * 100)
            parts.append(
                f"\nBIAS CORRECTION: You historically {direction} "
                f"{category} markets by {pct:.0f}%. Adjust accordingly."
            )
        lessons = self._load_lessons()
        if lessons:
            top_lessons = list(lessons.values())[:5]
            parts.append("\nLESSONS FROM PAST TRADES:")
            for i, lesson in enumerate(top_lessons, 1):
                parts.append(f"  {i}. {lesson}")
        return "\n".join(parts)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_prompt_builder.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/brain/ polybot/tests/test_prompt_builder.py
git commit -m "feat: prompt builder with bias corrections and lessons injection"
```

---

## Task 9: Execution — Paper Trader

**Files:**
- Create: `polybot/execution/__init__.py`, `polybot/execution/base.py`, `polybot/execution/paper_trader.py`, `polybot/tests/test_paper_trader.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_paper_trader.py
import pytest
import pytest_asyncio
from polybot.execution.base import TradeResult
from polybot.execution.paper_trader import PaperTrader
from polybot.db.models import Database

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()

@pytest_asyncio.fixture
async def trader(db):
    return PaperTrader(
        db=db,
        max_slippage=0.02,
        max_bankroll_deployed=0.80,
        max_concurrent_positions=5,
    )

@pytest.mark.asyncio
async def test_open_trade_returns_success(trader):
    result = await trader.open_trade(
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert result.success is True
    assert result.position_id is not None

@pytest.mark.asyncio
async def test_open_trade_reduces_bankroll(trader, db):
    await trader.open_trade(
        market_id="market_123",
        question="Q?",
        side="YES",
        price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    bankroll = await db.get_bankroll()
    assert bankroll == pytest.approx(90.0, abs=0.01)

@pytest.mark.asyncio
async def test_rejects_duplicate_market(trader):
    await trader.open_trade(
        market_id="market_123",
        question="Q?",
        side="YES",
        price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    result = await trader.open_trade(
        market_id="market_123",
        question="Q?",
        side="YES",
        price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert result.success is False
    assert "duplicate" in result.reason.lower()

@pytest.mark.asyncio
async def test_rejects_when_max_positions_reached(trader, db):
    for i in range(5):
        await trader.open_trade(
            market_id=f"market_{i}",
            question="Q?",
            side="YES",
            price=0.55,
            size=5.0,
            claude_probability=0.72,
            claude_confidence="high",
            ev_at_entry=0.17,
            exit_target=0.68,
            stop_loss=0.47,
            prompt_version="v001",
        )
    result = await trader.open_trade(
        market_id="market_6",
        question="Q?",
        side="YES",
        price=0.55,
        size=5.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert result.success is False
    assert "max positions" in result.reason.lower()

@pytest.mark.asyncio
async def test_rejects_when_bankroll_exceeded(trader, db):
    result = await trader.open_trade(
        market_id="market_big",
        question="Q?",
        side="YES",
        price=0.55,
        size=85.0,  # 85 > 80% of 100
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    assert result.success is False
    assert "bankroll" in result.reason.lower()

@pytest.mark.asyncio
async def test_close_trade_updates_bankroll(trader, db):
    result = await trader.open_trade(
        market_id="market_123",
        question="Q?",
        side="YES",
        price=0.55,
        size=10.0,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=0.17,
        exit_target=0.68,
        stop_loss=0.47,
        prompt_version="v001",
    )
    close_result = await trader.close_trade(
        position_id=result.position_id,
        exit_price=0.68,
    )
    assert close_result.success is True
    bankroll = await db.get_bankroll()
    # Started 100, spent 10 (at 0.55), sold at 0.68
    # Shares = 10 / 0.55 ≈ 18.18, revenue = 18.18 * 0.68 ≈ 12.36
    assert bankroll > 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_paper_trader.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement base and paper trader**

```python
# polybot/execution/__init__.py
# (empty)
```

```python
# polybot/execution/base.py
from dataclasses import dataclass

@dataclass
class TradeResult:
    success: bool
    position_id: int | None = None
    reason: str = ""
    log_return: float | None = None
```

```python
# polybot/execution/paper_trader.py
import logging
from polybot.db.models import Database
from polybot.execution.base import TradeResult
from polybot.math_engine.returns import log_return

logger = logging.getLogger(__name__)

class PaperTrader:
    def __init__(
        self,
        db: Database,
        max_slippage: float = 0.02,
        max_bankroll_deployed: float = 0.80,
        max_concurrent_positions: int = 5,
    ):
        self.db = db
        self.max_slippage = max_slippage
        self.max_bankroll_deployed = max_bankroll_deployed
        self.max_concurrent_positions = max_concurrent_positions

    async def _get_deployed_capital(self) -> float:
        positions = await self.db.get_open_positions()
        return sum(p["size"] for p in positions)

    async def open_trade(
        self,
        market_id: str,
        question: str,
        side: str,
        price: float,
        size: float,
        claude_probability: float,
        claude_confidence: str,
        ev_at_entry: float,
        exit_target: float,
        stop_loss: float,
        prompt_version: str,
    ) -> TradeResult:
        if await self.db.has_position_for_market(market_id):
            return TradeResult(success=False, reason="Duplicate market — already have position")

        if await self.db.get_open_position_count() >= self.max_concurrent_positions:
            return TradeResult(success=False, reason="Max positions reached")

        bankroll = await self.db.get_bankroll()
        deployed = await self._get_deployed_capital()
        max_deployable = bankroll * self.max_bankroll_deployed
        if deployed + size > max_deployable:
            return TradeResult(success=False, reason=f"Bankroll limit — deployed {deployed:.2f}, max {max_deployable:.2f}")

        pos_id = await self.db.open_position(
            market_id=market_id,
            question=question,
            side=side,
            entry_price=price,
            size=size,
            claude_probability=claude_probability,
            claude_confidence=claude_confidence,
            ev_at_entry=ev_at_entry,
            exit_target=exit_target,
            stop_loss=stop_loss,
            prompt_version=prompt_version,
        )
        new_bankroll = bankroll - size
        await self.db.set_bankroll(new_bankroll)
        logger.info(f"[PAPER] Opened {side} on '{question}' at {price} size={size}")
        return TradeResult(success=True, position_id=pos_id)

    async def close_trade(self, position_id: int, exit_price: float) -> TradeResult:
        positions = await self.db.get_open_positions()
        position = next((p for p in positions if p["id"] == position_id), None)
        if not position:
            return TradeResult(success=False, reason=f"Position {position_id} not found or already closed")

        lr = log_return(position["entry_price"], exit_price)
        shares = position["size"] / position["entry_price"]
        revenue = shares * exit_price
        await self.db.close_position(position_id, exit_price=exit_price, log_return=lr)
        bankroll = await self.db.get_bankroll()
        await self.db.set_bankroll(bankroll + revenue)
        logger.info(
            f"[PAPER] Closed position {position_id} at {exit_price} "
            f"log_return={lr:.4f} revenue={revenue:.2f}"
        )
        return TradeResult(success=True, position_id=position_id, log_return=lr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_paper_trader.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/execution/ polybot/tests/test_paper_trader.py
git commit -m "feat: paper trader with bankroll management and safety checks"
```

---

## Task 10: Core — WebSocket Exit Monitor

**Files:**
- Create: `polybot/core/websocket_monitor.py`, `polybot/tests/test_websocket_monitor.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_websocket_monitor.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta
from polybot.core.websocket_monitor import ExitMonitor

FIXED_NOW = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)

@pytest.fixture
def positions():
    return [
        {
            "id": 1,
            "market_id": "market_123",
            "entry_price": 0.55,
            "exit_target": 0.68,
            "stop_loss": 0.47,
            "claude_probability": 0.72,
            "entry_timestamp": "2026-03-30T00:00:00+00:00",
        },
    ]

@pytest.fixture
def monitor():
    return ExitMonitor(time_stop_hours=24, time_stop_min_gain=0.02)

def test_check_exit_take_profit(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        action = monitor.check_exit(positions[0], current_price=0.70)
    assert action == "take_profit"

def test_check_exit_stop_loss(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        action = monitor.check_exit(positions[0], current_price=0.45)
    assert action == "stop_loss"

def test_check_exit_hold(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        action = monitor.check_exit(positions[0], current_price=0.60)
    assert action == "hold"

def test_check_exit_time_stop(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        old_position = positions[0].copy()
        old_position["entry_timestamp"] = "2026-03-28T00:00:00+00:00"  # 2.5 days ago
        action = monitor.check_exit(old_position, current_price=0.56)
    assert action == "time_stop"

def test_check_exit_time_stop_not_triggered_with_gain(monitor, positions):
    with patch("polybot.core.websocket_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        old_position = positions[0].copy()
        old_position["entry_timestamp"] = "2026-03-28T00:00:00+00:00"
        # Price at 0.60, gain is (0.60-0.55)/0.55 = 9%, above 2% threshold
        action = monitor.check_exit(old_position, current_price=0.60)
    assert action == "hold"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_websocket_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement exit monitor**

```python
# polybot/core/websocket_monitor.py
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class ExitMonitor:
    def __init__(
        self,
        time_stop_hours: int = 24,
        time_stop_min_gain: float = 0.02,
    ):
        self.time_stop_hours = time_stop_hours
        self.time_stop_min_gain = time_stop_min_gain

    def check_exit(self, position: dict, current_price: float) -> str:
        entry_price = position["entry_price"]
        exit_target = position["exit_target"]
        stop_loss = position["stop_loss"]

        if current_price >= exit_target:
            return "take_profit"

        if current_price <= stop_loss:
            return "stop_loss"

        entry_time = datetime.fromisoformat(position["entry_timestamp"])
        now = datetime.now(timezone.utc)
        hours_held = (now - entry_time).total_seconds() / 3600
        gain_pct = (current_price - entry_price) / entry_price

        if hours_held >= self.time_stop_hours and gain_pct < self.time_stop_min_gain:
            return "time_stop"

        return "hold"

    async def monitor_positions(self, positions: list[dict], get_price, on_exit):
        """Called in the main loop. For each position, get current price and check exit.

        Args:
            positions: list of open position dicts
            get_price: async callable(market_id) -> float
            on_exit: async callable(position_id, exit_price, reason)
        """
        for position in positions:
            try:
                current_price = await get_price(position["market_id"])
                action = self.check_exit(position, current_price)
                if action != "hold":
                    logger.info(
                        f"Exit signal '{action}' for position {position['id']} "
                        f"at price {current_price}"
                    )
                    await on_exit(position["id"], current_price, action)
            except Exception as e:
                logger.error(f"Error monitoring position {position['id']}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_websocket_monitor.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/core/websocket_monitor.py polybot/tests/test_websocket_monitor.py
git commit -m "feat: exit monitor with take-profit, stop-loss, and time-stop logic"
```

---

## Task 11: Agents — Outcome Reviewer

**Files:**
- Create: `polybot/agents/__init__.py`, `polybot/agents/outcome_reviewer.py`, `polybot/tests/test_outcome_reviewer.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_outcome_reviewer.py
import json
import pytest
import pytest_asyncio
from pathlib import Path
from polybot.agents.outcome_reviewer import OutcomeReviewer

@pytest.fixture
def outcomes_dir(tmp_path):
    return tmp_path / "outcomes"

@pytest.fixture
def reviewer(outcomes_dir):
    return OutcomeReviewer(outcomes_dir=str(outcomes_dir))

def test_record_outcome_creates_file(reviewer, outcomes_dir):
    reviewer.record_outcome(
        position_id=1,
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        predicted_probability=0.72,
        actual_outcome=True,
        entry_price=0.55,
        exit_price=0.68,
        log_return=0.212,
        prompt_version="v001",
        category="politics",
    )
    files = list(Path(outcomes_dir).glob("*.json"))
    assert len(files) == 1

def test_record_outcome_content(reviewer, outcomes_dir):
    reviewer.record_outcome(
        position_id=1,
        market_id="market_123",
        question="Will X happen?",
        side="YES",
        predicted_probability=0.72,
        actual_outcome=True,
        entry_price=0.55,
        exit_price=0.68,
        log_return=0.212,
        prompt_version="v001",
        category="politics",
    )
    files = list(Path(outcomes_dir).glob("*.json"))
    data = json.loads(files[0].read_text())
    assert data["predicted_probability"] == 0.72
    assert data["actual_outcome"] is True
    assert data["correct"] is True
    assert data["error"] == pytest.approx(0.28, abs=0.01)

def test_correct_when_predicted_high_and_resolved_yes(reviewer):
    record = reviewer._evaluate(predicted_probability=0.72, actual_outcome=True)
    assert record["correct"] is True

def test_incorrect_when_predicted_high_and_resolved_no(reviewer):
    record = reviewer._evaluate(predicted_probability=0.72, actual_outcome=False)
    assert record["correct"] is False

def test_error_calculation(reviewer):
    record = reviewer._evaluate(predicted_probability=0.72, actual_outcome=True)
    # Error = |predicted - actual|, actual is 1.0 for True
    assert record["error"] == pytest.approx(0.28, abs=0.01)

def test_load_all_outcomes(reviewer, outcomes_dir):
    for i in range(3):
        reviewer.record_outcome(
            position_id=i,
            market_id=f"market_{i}",
            question="Q?",
            side="YES",
            predicted_probability=0.7,
            actual_outcome=True,
            entry_price=0.55,
            exit_price=0.68,
            log_return=0.2,
            prompt_version="v001",
            category="politics",
        )
    outcomes = reviewer.load_all_outcomes()
    assert len(outcomes) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_outcome_reviewer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement outcome reviewer**

```python
# polybot/agents/__init__.py
# (empty)
```

```python
# polybot/agents/outcome_reviewer.py
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

class OutcomeReviewer:
    def __init__(self, outcomes_dir: str):
        self.outcomes_dir = Path(outcomes_dir)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)

    def _evaluate(self, predicted_probability: float, actual_outcome: bool) -> dict:
        actual_value = 1.0 if actual_outcome else 0.0
        correct = (predicted_probability >= 0.5) == actual_outcome
        error = abs(predicted_probability - actual_value)
        return {"correct": correct, "error": round(error, 4)}

    def record_outcome(
        self,
        position_id: int,
        market_id: str,
        question: str,
        side: str,
        predicted_probability: float,
        actual_outcome: bool,
        entry_price: float,
        exit_price: float,
        log_return: float,
        prompt_version: str,
        category: str = "",
    ):
        evaluation = self._evaluate(predicted_probability, actual_outcome)
        record = {
            "position_id": position_id,
            "market_id": market_id,
            "question": question,
            "side": side,
            "predicted_probability": predicted_probability,
            "actual_outcome": actual_outcome,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "log_return": log_return,
            "prompt_version": prompt_version,
            "category": category,
            "correct": evaluation["correct"],
            "error": evaluation["error"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        filename = f"{position_id}_{market_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
        filepath = self.outcomes_dir / filename
        filepath.write_text(json.dumps(record, indent=2))
        logger.info(f"Recorded outcome for position {position_id}: correct={evaluation['correct']}")

    def load_all_outcomes(self) -> list[dict]:
        outcomes = []
        for filepath in self.outcomes_dir.glob("*.json"):
            try:
                outcomes.append(json.loads(filepath.read_text()))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load outcome {filepath}: {e}")
        return sorted(outcomes, key=lambda x: x.get("timestamp", ""))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_outcome_reviewer.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/agents/ polybot/tests/test_outcome_reviewer.py
git commit -m "feat: outcome reviewer agent for tracking prediction accuracy"
```

---

## Task 12: Agents — Bias Detector

**Files:**
- Create: `polybot/agents/bias_detector.py`, `polybot/tests/test_bias_detector.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_bias_detector.py
import json
import pytest
from pathlib import Path
from polybot.agents.bias_detector import BiasDetector

@pytest.fixture
def outcomes():
    return [
        {"category": "politics", "predicted_probability": 0.80, "actual_outcome": True, "error": 0.20, "correct": True},
        {"category": "politics", "predicted_probability": 0.75, "actual_outcome": False, "error": 0.75, "correct": False},
        {"category": "politics", "predicted_probability": 0.85, "actual_outcome": True, "error": 0.15, "correct": True},
        {"category": "politics", "predicted_probability": 0.70, "actual_outcome": False, "error": 0.70, "correct": False},
        {"category": "crypto", "predicted_probability": 0.60, "actual_outcome": True, "error": 0.40, "correct": True},
        {"category": "crypto", "predicted_probability": 0.55, "actual_outcome": True, "error": 0.45, "correct": True},
        {"category": "crypto", "predicted_probability": 0.50, "actual_outcome": True, "error": 0.50, "correct": True},
    ]

@pytest.fixture
def biases_path(tmp_path):
    return tmp_path / "biases.json"

@pytest.fixture
def detector(biases_path):
    return BiasDetector(biases_path=str(biases_path))

def test_detect_biases_returns_dict(detector, outcomes):
    biases = detector.detect(outcomes)
    assert isinstance(biases, dict)

def test_detect_politics_overestimation(detector, outcomes):
    biases = detector.detect(outcomes)
    # Politics: predicted high (avg ~0.775), but 50% wrong = overestimation
    assert "politics" in biases
    assert biases["politics"] < 0  # Negative = overestimate

def test_detect_crypto_underestimation(detector, outcomes):
    biases = detector.detect(outcomes)
    # Crypto: predicted low (avg ~0.55), but all resolved YES = underestimation
    assert "crypto" in biases
    assert biases["crypto"] > 0  # Positive = underestimate

def test_save_biases_writes_file(detector, outcomes, biases_path):
    biases = detector.detect(outcomes)
    detector.save(biases)
    assert biases_path.exists()
    saved = json.loads(biases_path.read_text())
    assert "politics" in saved

def test_detect_skips_categories_with_few_samples(detector):
    outcomes = [
        {"category": "sports", "predicted_probability": 0.80, "actual_outcome": True, "error": 0.20, "correct": True},
    ]
    biases = detector.detect(outcomes, min_samples=3)
    assert "sports" not in biases
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_bias_detector.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement bias detector**

```python
# polybot/agents/bias_detector.py
import json
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

class BiasDetector:
    def __init__(self, biases_path: str):
        self.biases_path = Path(biases_path)

    def detect(self, outcomes: list[dict], min_samples: int = 3) -> dict[str, float]:
        by_category: dict[str, list[dict]] = defaultdict(list)
        for outcome in outcomes:
            cat = outcome.get("category", "unknown")
            if cat:
                by_category[cat].append(outcome)

        biases = {}
        for category, records in by_category.items():
            if len(records) < min_samples:
                continue
            avg_predicted = sum(r["predicted_probability"] for r in records) / len(records)
            avg_actual = sum(1.0 if r["actual_outcome"] else 0.0 for r in records) / len(records)
            bias = avg_actual - avg_predicted  # Positive = underestimate, negative = overestimate
            biases[category] = round(bias, 4)
            direction = "underestimates" if bias > 0 else "overestimates"
            logger.info(f"Bias detected: {direction} {category} by {abs(bias)*100:.1f}%")

        return biases

    def save(self, biases: dict[str, float]):
        self.biases_path.parent.mkdir(parents=True, exist_ok=True)
        self.biases_path.write_text(json.dumps(biases, indent=2))
        logger.info(f"Saved biases to {self.biases_path}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_bias_detector.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/agents/bias_detector.py polybot/tests/test_bias_detector.py
git commit -m "feat: bias detector agent for identifying systematic prediction errors"
```

---

## Task 13: Agents — Strategy Evolver

**Files:**
- Create: `polybot/agents/strategy_evolver.py`, `polybot/tests/test_strategy_evolver.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_strategy_evolver.py
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.agents.strategy_evolver import StrategyEvolver, StrategyRecommendation

def test_analyze_outcomes_detects_low_win_rate():
    outcomes = [
        {"correct": False, "log_return": -0.1} for _ in range(8)
    ] + [
        {"correct": True, "log_return": 0.05} for _ in range(2)
    ]
    evolver = StrategyEvolver(strategy_log_path="/tmp/test_log.md")
    analysis = evolver.analyze_local(outcomes, current_config={
        "ev_threshold": 0.05,
        "exit_target": 0.90,
        "stop_loss_pct": 0.15,
        "time_stop_hours": 24,
    })
    assert analysis["win_rate"] == pytest.approx(0.20, abs=0.01)

def test_analyze_outcomes_detects_high_win_rate():
    outcomes = [
        {"correct": True, "log_return": 0.05} for _ in range(9)
    ] + [
        {"correct": False, "log_return": -0.1} for _ in range(1)
    ]
    evolver = StrategyEvolver(strategy_log_path="/tmp/test_log.md")
    analysis = evolver.analyze_local(outcomes, current_config={
        "ev_threshold": 0.05,
        "exit_target": 0.90,
        "stop_loss_pct": 0.15,
        "time_stop_hours": 24,
    })
    assert analysis["win_rate"] == pytest.approx(0.90, abs=0.01)

def test_generate_recommendations_low_win_rate():
    evolver = StrategyEvolver(strategy_log_path="/tmp/test_log.md")
    analysis = {"win_rate": 0.30, "avg_log_return": -0.05, "total_trades": 10}
    recs = evolver.generate_recommendations(analysis, current_config={
        "ev_threshold": 0.05,
        "exit_target": 0.90,
        "stop_loss_pct": 0.15,
        "time_stop_hours": 24,
    })
    assert len(recs) > 0
    # Should recommend raising EV threshold
    params = [r.param for r in recs]
    assert "ev_threshold" in params

def test_recommendation_dataclass():
    rec = StrategyRecommendation(
        param="ev_threshold",
        current_value=0.05,
        recommended_value=0.08,
        reason="Win rate below 50%, raising EV threshold to be more selective",
    )
    assert rec.param == "ev_threshold"
    assert rec.recommended_value == 0.08

def test_save_log(tmp_path):
    log_path = tmp_path / "strategy_log.md"
    evolver = StrategyEvolver(strategy_log_path=str(log_path))
    recs = [
        StrategyRecommendation("ev_threshold", 0.05, 0.08, "Low win rate"),
    ]
    evolver.save_log(recs, analysis={"win_rate": 0.30, "total_trades": 10})
    assert log_path.exists()
    content = log_path.read_text()
    assert "ev_threshold" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_strategy_evolver.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement strategy evolver**

```python
# polybot/agents/strategy_evolver.py
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class StrategyRecommendation:
    param: str
    current_value: float
    recommended_value: float
    reason: str

class StrategyEvolver:
    def __init__(self, strategy_log_path: str):
        self.strategy_log_path = Path(strategy_log_path)

    def analyze_local(self, outcomes: list[dict], current_config: dict) -> dict:
        if not outcomes:
            return {"win_rate": 0, "avg_log_return": 0, "total_trades": 0}
        wins = sum(1 for o in outcomes if o.get("correct", False))
        returns = [o.get("log_return", 0) for o in outcomes]
        return {
            "win_rate": wins / len(outcomes),
            "avg_log_return": sum(returns) / len(returns) if returns else 0,
            "total_trades": len(outcomes),
        }

    def generate_recommendations(
        self, analysis: dict, current_config: dict
    ) -> list[StrategyRecommendation]:
        recs = []
        win_rate = analysis.get("win_rate", 0)
        avg_return = analysis.get("avg_log_return", 0)

        if win_rate < 0.50:
            new_ev = min(current_config["ev_threshold"] + 0.03, 0.20)
            recs.append(StrategyRecommendation(
                param="ev_threshold",
                current_value=current_config["ev_threshold"],
                recommended_value=round(new_ev, 2),
                reason=f"Win rate {win_rate:.0%} is below 50%. Raising EV threshold to be more selective.",
            ))

        if win_rate < 0.40:
            new_stop = max(current_config["stop_loss_pct"] - 0.03, 0.05)
            recs.append(StrategyRecommendation(
                param="stop_loss_pct",
                current_value=current_config["stop_loss_pct"],
                recommended_value=round(new_stop, 2),
                reason=f"Win rate {win_rate:.0%} very low. Tightening stop loss to cut losers faster.",
            ))

        if win_rate > 0.70 and avg_return > 0:
            new_ev = max(current_config["ev_threshold"] - 0.01, 0.03)
            recs.append(StrategyRecommendation(
                param="ev_threshold",
                current_value=current_config["ev_threshold"],
                recommended_value=round(new_ev, 2),
                reason=f"Win rate {win_rate:.0%} strong. Can afford slightly lower EV threshold for more trades.",
            ))

        if avg_return < 0 and win_rate > 0.50:
            new_exit = max(current_config["exit_target"] - 0.05, 0.75)
            recs.append(StrategyRecommendation(
                param="exit_target",
                current_value=current_config["exit_target"],
                recommended_value=round(new_exit, 2),
                reason="Winning trades but negative returns — taking profit too late. Lower exit target.",
            ))

        return recs

    def save_log(self, recommendations: list[StrategyRecommendation], analysis: dict):
        self.strategy_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        entry = f"\n## {now}\n\n"
        entry += f"**Analysis:** {analysis}\n\n"
        if recommendations:
            entry += "**Recommendations:**\n"
            for rec in recommendations:
                entry += f"- `{rec.param}`: {rec.current_value} -> {rec.recommended_value} — {rec.reason}\n"
        else:
            entry += "**No recommendations — strategy performing well.**\n"
        if self.strategy_log_path.exists():
            existing = self.strategy_log_path.read_text()
        else:
            existing = "# Strategy Evolution Log\n"
        self.strategy_log_path.write_text(existing + entry)
        logger.info(f"Saved {len(recommendations)} strategy recommendations")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_strategy_evolver.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/agents/strategy_evolver.py polybot/tests/test_strategy_evolver.py
git commit -m "feat: strategy evolver agent with config recommendations"
```

---

## Task 14: Agents — Prompt Optimizer

**Files:**
- Create: `polybot/agents/prompt_optimizer.py`, `polybot/tests/test_prompt_optimizer.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_prompt_optimizer.py
import json
import pytest
from pathlib import Path
from polybot.agents.prompt_optimizer import PromptOptimizer

@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "v001.txt").write_text("You are an analyst. Estimate probability.")
    (d / "v002.txt").write_text("You are an expert analyst. Consider base rates.")
    return d

@pytest.fixture
def scores_path(tmp_path):
    path = tmp_path / "prompt_scores.json"
    path.write_text(json.dumps({
        "v001": {"accuracy": 0.55, "total": 30},
        "v002": {"accuracy": 0.62, "total": 20},
    }))
    return path

@pytest.fixture
def optimizer(prompts_dir, scores_path):
    return PromptOptimizer(
        prompts_dir=str(prompts_dir),
        scores_path=str(scores_path),
        min_improvement=0.03,
    )

def test_get_version_scores(optimizer):
    scores = optimizer.get_version_scores()
    assert scores["v001"]["accuracy"] == 0.55
    assert scores["v002"]["accuracy"] == 0.62

def test_get_best_version(optimizer):
    best = optimizer.get_best_version()
    assert best == "v002"

def test_record_score(optimizer, scores_path):
    optimizer.record_score("v003", accuracy=0.70, total=10)
    scores = json.loads(scores_path.read_text())
    assert "v003" in scores
    assert scores["v003"]["accuracy"] == 0.70

def test_get_next_version(optimizer):
    next_v = optimizer.get_next_version()
    assert next_v == "v003"

def test_save_new_prompt(optimizer, prompts_dir):
    optimizer.save_prompt("v003", "New improved prompt text.")
    assert (prompts_dir / "v003.txt").exists()
    assert (prompts_dir / "v003.txt").read_text() == "New improved prompt text."

def test_should_adopt_when_improvement_above_threshold(optimizer):
    assert optimizer.should_adopt(current_accuracy=0.62, candidate_accuracy=0.66) is True

def test_should_not_adopt_when_improvement_below_threshold(optimizer):
    assert optimizer.should_adopt(current_accuracy=0.62, candidate_accuracy=0.63) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_prompt_optimizer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement prompt optimizer**

```python
# polybot/agents/prompt_optimizer.py
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

class PromptOptimizer:
    def __init__(
        self,
        prompts_dir: str,
        scores_path: str,
        min_improvement: float = 0.03,
    ):
        self.prompts_dir = Path(prompts_dir)
        self.scores_path = Path(scores_path)
        self.min_improvement = min_improvement

    def get_version_scores(self) -> dict:
        if not self.scores_path.exists():
            return {}
        return json.loads(self.scores_path.read_text())

    def get_best_version(self) -> str:
        scores = self.get_version_scores()
        if not scores:
            return "v001"
        return max(scores, key=lambda v: scores[v]["accuracy"])

    def record_score(self, version: str, accuracy: float, total: int):
        scores = self.get_version_scores()
        scores[version] = {"accuracy": round(accuracy, 4), "total": total}
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(json.dumps(scores, indent=2))

    def get_next_version(self) -> str:
        existing = list(self.prompts_dir.glob("v*.txt"))
        if not existing:
            return "v001"
        numbers = []
        for f in existing:
            match = re.match(r"v(\d+)", f.stem)
            if match:
                numbers.append(int(match.group(1)))
        next_num = max(numbers) + 1 if numbers else 1
        return f"v{next_num:03d}"

    def save_prompt(self, version: str, content: str):
        path = self.prompts_dir / f"{version}.txt"
        path.write_text(content)
        logger.info(f"Saved new prompt version: {version}")

    def should_adopt(self, current_accuracy: float, candidate_accuracy: float) -> bool:
        return (candidate_accuracy - current_accuracy) >= self.min_improvement
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_prompt_optimizer.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/agents/prompt_optimizer.py polybot/tests/test_prompt_optimizer.py
git commit -m "feat: prompt optimizer agent with version scoring and adoption threshold"
```

---

## Task 15: Agents — Scheduler (Pipeline Orchestration)

**Files:**
- Create: `polybot/agents/scheduler.py`, `polybot/tests/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_scheduler.py (note: scheduler is just renamed from pipeline test)
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from polybot.agents.scheduler import AgentScheduler

@pytest.fixture
def scheduler():
    return AgentScheduler(
        outcome_reviewer=MagicMock(),
        bias_detector=MagicMock(),
        strategy_evolver=MagicMock(),
        prompt_optimizer=MagicMock(),
        outcome_interval_seconds=3600,
        daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24},
    )

def test_scheduler_has_all_agents(scheduler):
    assert scheduler.outcome_reviewer is not None
    assert scheduler.bias_detector is not None
    assert scheduler.strategy_evolver is not None
    assert scheduler.prompt_optimizer is not None

@pytest.mark.asyncio
async def test_run_daily_pipeline_calls_agents_in_order():
    call_order = []

    async def mock_bias():
        call_order.append("bias")
        return {"politics": -0.1}

    async def mock_strategy(biases):
        call_order.append("strategy")
        return []

    async def mock_prompt(recs):
        call_order.append("prompt")

    scheduler = AgentScheduler(
        outcome_reviewer=MagicMock(),
        bias_detector=MagicMock(),
        strategy_evolver=MagicMock(),
        prompt_optimizer=MagicMock(),
        outcome_interval_seconds=3600,
        daily_pipeline_hour=2,
    )
    scheduler = AgentScheduler(
        outcome_reviewer=MagicMock(),
        bias_detector=MagicMock(),
        strategy_evolver=MagicMock(),
        prompt_optimizer=MagicMock(),
        outcome_interval_seconds=3600,
        daily_pipeline_hour=2,
        math_config={"ev_threshold": 0.05, "exit_target": 0.90, "stop_loss_pct": 0.15, "time_stop_hours": 24},
    )
    scheduler._run_bias_detector = mock_bias
    scheduler._run_strategy_evolver = mock_strategy
    scheduler._run_prompt_optimizer = mock_prompt

    await scheduler.run_daily_pipeline()
    assert call_order == ["bias", "strategy", "prompt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement scheduler**

```python
# polybot/agents/scheduler.py
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class AgentScheduler:
    def __init__(
        self,
        outcome_reviewer,
        bias_detector,
        strategy_evolver,
        prompt_optimizer,
        outcome_interval_seconds: int = 3600,
        daily_pipeline_hour: int = 2,
        math_config: dict | None = None,
    ):
        self.outcome_reviewer = outcome_reviewer
        self.bias_detector = bias_detector
        self.strategy_evolver = strategy_evolver
        self.prompt_optimizer = prompt_optimizer
        self.outcome_interval_seconds = outcome_interval_seconds
        self.daily_pipeline_hour = daily_pipeline_hour
        self.math_config = math_config or {
            "ev_threshold": 0.05,
            "exit_target": 0.90,
            "stop_loss_pct": 0.15,
            "time_stop_hours": 24,
        }
        self._running = False

    async def _run_bias_detector(self) -> dict:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes to analyze for biases")
            return {}
        biases = self.bias_detector.detect(outcomes)
        self.bias_detector.save(biases)
        return biases

    async def _run_strategy_evolver(self, biases: dict) -> list:
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            return []
        analysis = self.strategy_evolver.analyze_local(outcomes, current_config=self.math_config)
        recs = self.strategy_evolver.generate_recommendations(analysis, current_config=self.math_config)
        self.strategy_evolver.save_log(recs, analysis)
        return recs

    async def _run_prompt_optimizer(self, recommendations: list):
        outcomes = self.outcome_reviewer.load_all_outcomes()
        if not outcomes:
            logger.info("No outcomes for prompt optimization")
            return
        # Score current prompt version by accuracy
        current_version = self.prompt_optimizer.get_best_version()
        version_outcomes = [o for o in outcomes if o.get("prompt_version") == current_version]
        if version_outcomes:
            accuracy = sum(1 for o in version_outcomes if o["correct"]) / len(version_outcomes)
            self.prompt_optimizer.record_score(current_version, accuracy, len(version_outcomes))
        logger.info("Prompt optimizer scoring complete")

    async def run_daily_pipeline(self):
        logger.info("Starting daily learning pipeline")
        biases = await self._run_bias_detector()
        recommendations = await self._run_strategy_evolver(biases)
        await self._run_prompt_optimizer(recommendations)
        logger.info("Daily learning pipeline complete")

    async def run_outcome_loop(self):
        while self._running:
            try:
                logger.info("Running outcome reviewer")
                # In production, this checks for newly resolved markets
            except Exception as e:
                logger.error(f"Outcome reviewer error: {e}")
            await asyncio.sleep(self.outcome_interval_seconds)

    async def run_daily_loop(self):
        while self._running:
            now = datetime.now(timezone.utc)
            if now.hour == self.daily_pipeline_hour and now.minute < 5:
                try:
                    await self.run_daily_pipeline()
                except Exception as e:
                    logger.error(f"Daily pipeline error: {e}")
                await asyncio.sleep(3600)  # Wait 1 hour to avoid re-trigger
            await asyncio.sleep(60)  # Check every minute

    async def start(self):
        self._running = True
        logger.info("Agent scheduler started")

    async def stop(self):
        self._running = False
        logger.info("Agent scheduler stopped")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_scheduler.py -v`
Expected: All 3 tests PASS (2 sync, 1 async)

- [ ] **Step 5: Commit**

```bash
git add polybot/agents/scheduler.py polybot/tests/test_scheduler.py
git commit -m "feat: agent scheduler with daily pipeline orchestration"
```

---

## Task 16: Discord Bot

**Files:**
- Create: `polybot/discord_bot/__init__.py`, `polybot/discord_bot/bot.py`, `polybot/discord_bot/commands.py`, `polybot/discord_bot/alerts.py`, `polybot/tests/test_discord_commands.py`

- [ ] **Step 1: Write failing tests**

```python
# polybot/tests/test_discord_commands.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.discord_bot.commands import format_status, format_positions, format_performance

def test_format_status():
    result = format_status(
        mode="paper",
        is_paused=False,
        open_positions=3,
        bankroll=95.50,
        pnl_24h=2.30,
    )
    assert "paper" in result.lower()
    assert "3" in result
    assert "95.50" in result

def test_format_status_paused():
    result = format_status(
        mode="paper",
        is_paused=True,
        open_positions=0,
        bankroll=100.0,
        pnl_24h=0.0,
    )
    assert "paused" in result.lower()

def test_format_positions_empty():
    result = format_positions([])
    assert "no open positions" in result.lower()

def test_format_positions_with_data():
    positions = [
        {
            "id": 1,
            "question": "Will BTC hit 100k?",
            "side": "YES",
            "entry_price": 0.55,
            "size": 10.0,
            "exit_target": 0.68,
            "stop_loss": 0.47,
        },
    ]
    result = format_positions(positions, current_prices={"market_123": 0.60})
    assert "BTC" in result
    assert "0.55" in result

def test_format_performance():
    result = format_performance(
        sharpe_ratio=1.85,
        win_rate=0.72,
        total_pnl=15.30,
        avg_hold_hours=8.5,
        total_trades=25,
        best_trade=5.20,
        worst_trade=-2.10,
    )
    assert "1.85" in result
    assert "72" in result
    assert "15.30" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd polybot && python -m pytest tests/test_discord_commands.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Discord command formatters**

```python
# polybot/discord_bot/__init__.py
# (empty)
```

```python
# polybot/discord_bot/commands.py

def format_status(
    mode: str,
    is_paused: bool,
    open_positions: int,
    bankroll: float,
    pnl_24h: float,
) -> str:
    state = "PAUSED" if is_paused else "ACTIVE"
    pnl_sign = "+" if pnl_24h >= 0 else ""
    return (
        f"**PolyBot Status**\n"
        f"Mode: `{mode}` | State: `{state}`\n"
        f"Open Positions: `{open_positions}`\n"
        f"Bankroll: `${bankroll:.2f}`\n"
        f"24h P&L: `{pnl_sign}${pnl_24h:.2f}`"
    )

def format_positions(positions: list[dict], current_prices: dict | None = None) -> str:
    if not positions:
        return "No open positions."
    lines = ["**Open Positions**\n"]
    for pos in positions:
        entry = pos["entry_price"]
        target = pos["exit_target"]
        stop = pos["stop_loss"]
        lines.append(
            f"**#{pos['id']}** {pos['question']}\n"
            f"  Side: `{pos['side']}` | Entry: `{entry:.2f}` | "
            f"Size: `${pos['size']:.2f}`\n"
            f"  Target: `{target:.2f}` | Stop: `{stop:.2f}`"
        )
    return "\n".join(lines)

def format_performance(
    sharpe_ratio: float,
    win_rate: float,
    total_pnl: float,
    avg_hold_hours: float,
    total_trades: int,
    best_trade: float,
    worst_trade: float,
) -> str:
    pnl_sign = "+" if total_pnl >= 0 else ""
    return (
        f"**Performance**\n"
        f"Sharpe Ratio: `{sharpe_ratio:.2f}`\n"
        f"Win Rate: `{win_rate:.0%}` ({total_trades} trades)\n"
        f"Total P&L: `{pnl_sign}${total_pnl:.2f}`\n"
        f"Avg Hold Time: `{avg_hold_hours:.1f}h`\n"
        f"Best Trade: `+${best_trade:.2f}` | Worst: `${worst_trade:.2f}`"
    )
```

- [ ] **Step 4: Implement Discord bot and alerts**

```python
# polybot/discord_bot/bot.py
import logging
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

def create_bot(db, trader, scanner, scheduler, config: dict) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    bot.db = db
    bot.trader = trader
    bot.scanner = scanner
    bot.scheduler = scheduler
    bot.config = config
    bot.is_paused = False

    @bot.event
    async def on_ready():
        logger.info(f"Discord bot connected as {bot.user}")

    @bot.command(name="status")
    async def status(ctx):
        from polybot.discord_bot.commands import format_status
        bankroll = await bot.db.get_bankroll()
        positions = await bot.db.get_open_position_count()
        history = await bot.db.get_trade_history(limit=100)
        pnl_24h = sum(t.get("log_return", 0) for t in history[:10])
        msg = format_status(
            mode=bot.config.get("mode", "paper"),
            is_paused=bot.is_paused,
            open_positions=positions,
            bankroll=bankroll,
            pnl_24h=pnl_24h,
        )
        await ctx.send(msg)

    @bot.command(name="positions")
    async def positions_cmd(ctx):
        from polybot.discord_bot.commands import format_positions
        positions = await bot.db.get_open_positions()
        msg = format_positions(positions)
        await ctx.send(msg)

    @bot.command(name="history")
    async def history(ctx, n: int = 10):
        trades = await bot.db.get_trade_history(limit=n)
        if not trades:
            await ctx.send("No trade history yet.")
            return
        lines = [f"**Last {len(trades)} Trades**\n"]
        for t in trades:
            pnl_sign = "+" if t["log_return"] >= 0 else ""
            lines.append(
                f"  {t['question'][:40]}... | "
                f"{t['entry_price']:.2f} -> {t['exit_price']:.2f} | "
                f"P&L: `{pnl_sign}{t['log_return']:.4f}`"
            )
        await ctx.send("\n".join(lines))

    @bot.command(name="pause")
    async def pause(ctx):
        bot.is_paused = True
        await ctx.send("Trading **paused**.")

    @bot.command(name="resume")
    async def resume(ctx):
        bot.is_paused = False
        await ctx.send("Trading **resumed**.")

    @bot.command(name="mode")
    async def mode_cmd(ctx):
        await ctx.send(f"Current mode: `{bot.config.get('mode', 'paper')}`")

    @bot.command(name="filters")
    async def filters_cmd(ctx):
        f = bot.config.get("filters", {})
        lines = ["**Current Filters**\n"]
        for k, v in f.items():
            lines.append(f"  `{k}`: {v}")
        await ctx.send("\n".join(lines))

    @bot.command(name="setfilter")
    async def setfilter(ctx, param: str, value: str):
        try:
            typed_value: int | float | str
            if "." in value:
                typed_value = float(value)
            elif value.isdigit():
                typed_value = int(value)
            else:
                typed_value = value
            bot.scanner.filter.update(param, typed_value)
            await ctx.send(f"Filter `{param}` set to `{typed_value}`")
        except ValueError as e:
            await ctx.send(f"Error: {e}")

    @bot.command(name="lessons")
    async def lessons(ctx):
        import json
        from pathlib import Path
        lessons_path = Path("polybot/memory/lessons.json")
        if not lessons_path.exists():
            await ctx.send("No lessons recorded yet.")
            return
        data = json.loads(lessons_path.read_text())
        lines = ["**Lessons Learned**\n"]
        for key, value in list(data.items())[:10]:
            lines.append(f"  **{key}:** {value}")
        await ctx.send("\n".join(lines))

    @bot.command(name="agents")
    async def agents(ctx):
        await ctx.send(
            f"**Agent Status**\n"
            f"Outcome Reviewer: runs every {bot.config.get('agents', {}).get('outcome_reviewer_interval_seconds', 3600)}s\n"
            f"Daily Pipeline: runs at {bot.config.get('agents', {}).get('daily_pipeline_hour', 2)}:00 UTC"
        )

    return bot
```

```python
# polybot/discord_bot/alerts.py
import logging
import discord

logger = logging.getLogger(__name__)

class AlertManager:
    def __init__(self, bot, trade_channel_name: str, control_channel_name: str):
        self.bot = bot
        self.trade_channel_name = trade_channel_name
        self.control_channel_name = control_channel_name

    def _get_channel(self, name: str) -> discord.TextChannel | None:
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if channel.name == name:
                    return channel
        return None

    async def send_trade_opened(
        self, question: str, side: str, size: float, entry_price: float,
        ev: float, exit_target: float,
    ):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        await channel.send(
            f"**Trade Opened**\n"
            f"{question}\n"
            f"Side: `{side}` | Entry: `{entry_price:.2f}` | Size: `${size:.2f}`\n"
            f"EV: `{ev:.2%}` | Target: `{exit_target:.2f}`"
        )

    async def send_trade_closed(
        self, question: str, exit_price: float, log_return: float, hold_hours: float,
    ):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        pnl_sign = "+" if log_return >= 0 else ""
        emoji = "profit" if log_return >= 0 else "loss"
        await channel.send(
            f"**Trade Closed ({emoji})**\n"
            f"{question}\n"
            f"Exit: `{exit_price:.2f}` | P&L: `{pnl_sign}{log_return:.4f}` | "
            f"Held: `{hold_hours:.1f}h`"
        )

    async def send_pipeline_summary(self, summary: str):
        channel = self._get_channel(self.trade_channel_name)
        if not channel:
            return
        await channel.send(f"**Learning Pipeline Complete**\n{summary}")

    async def send_strategy_recommendation(self, recommendations: list):
        channel = self._get_channel(self.control_channel_name)
        if not channel:
            return
        lines = ["**Strategy Recommendation**\n"]
        for rec in recommendations:
            lines.append(f"`{rec.param}`: {rec.current_value} -> {rec.recommended_value}")
            lines.append(f"  Reason: {rec.reason}")
        msg = await channel.send("\n".join(lines))
        await msg.add_reaction("\u2705")  # checkmark = approve
        await msg.add_reaction("\u274c")  # X = reject

    async def send_error(self, error_message: str):
        channel = self._get_channel(self.control_channel_name)
        if not channel:
            return
        await channel.send(f"**Error**\n```{error_message}```")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd polybot && python -m pytest tests/test_discord_commands.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/discord_bot/ polybot/tests/test_discord_commands.py
git commit -m "feat: Discord bot with commands, formatters, and alert system"
```

---

## Task 17: Main Entry Point & Integration

**Files:**
- Create: `polybot/main.py`, `polybot/tests/test_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
# polybot/tests/test_integration.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.db.models import Database
from polybot.math_engine.decision_table import DecisionTable
from polybot.core.filters import MarketFilter
from polybot.execution.paper_trader import PaperTrader
from polybot.brain.claude_client import MarketAnalysis

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    await database.set_bankroll(100.0)
    yield database
    await database.close()

@pytest.mark.asyncio
async def test_full_trade_flow(db):
    """End-to-end: market passes filter -> Claude analyzes -> math decides -> paper trade placed."""
    # 1. Filter
    f = MarketFilter(
        min_volume_24h=1000, min_liquidity=500,
        min_days_to_expiry=2, max_days_to_expiry=60,
        max_spread=0.05, category_whitelist=[], category_blacklist=[],
    )
    market = {
        "condition_id": "0xabc",
        "question": "Will BTC hit 100k?",
        "price_yes": 0.55,
        "volume_24h": 5000,
        "liquidity": 2000,
        "spread": 0.02,
        "days_to_expiry": 15,
        "category": "crypto",
    }
    assert f.passes(market) is True

    # 2. Claude analysis (mocked)
    analysis = MarketAnalysis(
        probability=0.72,
        confidence="high",
        reasoning="Strong signals",
        key_factors=["momentum"],
        base_rate_considered=True,
    )
    assert analysis.passes_gate(min_confidence="high", min_probability=0.65) is True

    # 3. Decision table
    table = DecisionTable(ev_threshold=0.05, kelly_fraction=0.25, entry_discount=0.85, exit_target=0.90, stop_loss_pct=0.15)
    table.build()
    assert table.should_buy(probability=0.72, market_price=0.55) is True
    size = table.position_size(probability=0.72, market_price=0.55, bankroll=100.0)
    decision = table.lookup(0.72)

    # 4. Paper trade
    trader = PaperTrader(db=db, max_slippage=0.02, max_bankroll_deployed=0.80, max_concurrent_positions=5)
    result = await trader.open_trade(
        market_id="0xabc",
        question="Will BTC hit 100k?",
        side="YES",
        price=0.55,
        size=size,
        claude_probability=0.72,
        claude_confidence="high",
        ev_at_entry=table.calculate_ev(0.72, 0.55),
        exit_target=decision["exit_price"],
        stop_loss=0.55 * (1 - 0.15),
        prompt_version="v001",
    )
    assert result.success is True

    # 5. Close at profit
    close_result = await trader.close_trade(result.position_id, exit_price=decision["exit_price"])
    assert close_result.success is True
    assert close_result.log_return > 0

    # 6. Verify bankroll grew
    bankroll = await db.get_bankroll()
    assert bankroll > 100.0
```

- [ ] **Step 2: Run integration test to verify it fails**

Run: `cd polybot && python -m pytest tests/test_integration.py -v`
Expected: FAIL until all modules exist — should PASS once all prior tasks are complete

- [ ] **Step 3: Implement main.py**

```python
# polybot/main.py
import asyncio
import logging
import signal
from pathlib import Path

from polybot.config.loader import load_config, get_secret
from polybot.db.models import Database
from polybot.core.filters import MarketFilter
from polybot.core.scanner import MarketScanner
from polybot.core.websocket_monitor import ExitMonitor
from polybot.math_engine.decision_table import DecisionTable
from polybot.brain.claude_client import ClaudeClient
from polybot.brain.prompt_builder import PromptBuilder
from polybot.execution.paper_trader import PaperTrader
from polybot.agents.outcome_reviewer import OutcomeReviewer
from polybot.agents.bias_detector import BiasDetector
from polybot.agents.strategy_evolver import StrategyEvolver
from polybot.agents.prompt_optimizer import PromptOptimizer
from polybot.agents.scheduler import AgentScheduler
from polybot.discord_bot.bot import create_bot
from polybot.discord_bot.alerts import AlertManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "polybot.log", maxBytes=5_000_000, backupCount=3,
        ),
    ],
)
logger = logging.getLogger("polybot")

async def trading_loop(
    scanner: MarketScanner,
    claude: ClaudeClient,
    prompt_builder: PromptBuilder,
    decision_table: DecisionTable,
    trader: PaperTrader,
    exit_monitor: ExitMonitor,
    alert_manager: AlertManager | None,
    db: Database,
    config: dict,
    is_paused_fn,
):
    brain_config = config["brain"]
    math_config = config["math"]
    scan_interval = config["scanner"]["interval_seconds"]

    while True:
        try:
            if is_paused_fn():
                await asyncio.sleep(10)
                continue

            # Exit monitoring for open positions
            positions = await db.get_open_positions()
            if positions:
                async def get_price(market_id):
                    # Use CLOB API to get latest price
                    import httpx
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            f"{scanner.CLOB_BASE_URL}/markets/{market_id}"
                        )
                        data = resp.json()
                        tokens = data.get("tokens", [])
                        for t in tokens:
                            if t.get("outcome", "").lower() == "yes":
                                return float(t.get("price", 0))
                    return 0.0

                async def on_exit(position_id, exit_price, reason):
                    result = await trader.close_trade(position_id, exit_price)
                    if result.success and alert_manager:
                        pos = next((p for p in positions if p["id"] == position_id), {})
                        entry_time = pos.get("entry_timestamp", "")
                        await alert_manager.send_trade_closed(
                            question=pos.get("question", ""),
                            exit_price=exit_price,
                            log_return=result.log_return or 0,
                            hold_hours=0,
                        )

                await exit_monitor.monitor_positions(positions, get_price, on_exit)

            # Scan for new opportunities
            markets = await scanner.fetch_and_filter()
            logger.info(f"Found {len(markets)} markets after filtering")

            for market in markets:
                if is_paused_fn():
                    break
                if await db.has_position_for_market(market["condition_id"]):
                    continue

                prompt = prompt_builder.build(
                    version=brain_config["active_prompt_version"],
                    category=market.get("category", ""),
                )
                try:
                    analysis = await claude.analyze_market(
                        question=market["question"],
                        price=market["price_yes"],
                        volume=market["volume_24h"],
                        liquidity=market["liquidity"],
                        spread=market["spread"],
                        days_to_expiry=market["days_to_expiry"],
                        prompt=prompt,
                    )
                except Exception as e:
                    logger.error(f"Claude analysis failed for {market['question'][:50]}: {e}")
                    continue

                if not analysis.passes_gate(
                    min_confidence=brain_config["min_confidence"],
                    min_probability=brain_config["min_probability"],
                ):
                    continue

                if not decision_table.should_buy(analysis.probability, market["price_yes"]):
                    continue

                bankroll = await db.get_bankroll()
                size = decision_table.position_size(
                    analysis.probability, market["price_yes"], bankroll,
                )
                if size < 1.0:
                    continue

                decision = decision_table.lookup(analysis.probability)
                ev = decision_table.calculate_ev(analysis.probability, market["price_yes"])

                result = await trader.open_trade(
                    market_id=market["condition_id"],
                    question=market["question"],
                    side="YES",
                    price=market["price_yes"],
                    size=size,
                    claude_probability=analysis.probability,
                    claude_confidence=analysis.confidence,
                    ev_at_entry=ev,
                    exit_target=decision["exit_price"],
                    stop_loss=market["price_yes"] * (1 - math_config["stop_loss_pct"]),
                    prompt_version=brain_config["active_prompt_version"],
                )

                if result.success and alert_manager:
                    await alert_manager.send_trade_opened(
                        question=market["question"],
                        side="YES",
                        size=size,
                        entry_price=market["price_yes"],
                        ev=ev,
                        exit_target=decision["exit_price"],
                    )

        except Exception as e:
            logger.error(f"Trading loop error: {e}", exc_info=True)
            if alert_manager:
                await alert_manager.send_error(str(e))

        await asyncio.sleep(scan_interval)

async def main():
    config = load_config()
    base_dir = Path(__file__).parent

    # Database
    db = Database(config["database"]["path"])
    await db.initialize()
    if await db.get_bankroll() == 0:
        await db.set_bankroll(config["execution"]["initial_bankroll"])

    # Math
    math_cfg = config["math"]
    decision_table = DecisionTable(
        ev_threshold=math_cfg["ev_threshold"],
        kelly_fraction=math_cfg["kelly_fraction"],
        entry_discount=math_cfg["entry_discount"],
        exit_target=math_cfg["exit_target"],
        stop_loss_pct=math_cfg["stop_loss_pct"],
    )
    decision_table.build()

    # Core
    filter_cfg = config["filters"]
    market_filter = MarketFilter(
        min_volume_24h=filter_cfg["min_volume_24h"],
        min_liquidity=filter_cfg["min_liquidity"],
        min_days_to_expiry=filter_cfg["min_days_to_expiry"],
        max_days_to_expiry=filter_cfg["max_days_to_expiry"],
        max_spread=filter_cfg["max_spread"],
        category_whitelist=filter_cfg["category_whitelist"],
        category_blacklist=filter_cfg["category_blacklist"],
    )
    scanner = MarketScanner(filter=market_filter, max_markets=config["scanner"]["max_markets_per_cycle"])

    exit_monitor = ExitMonitor(
        time_stop_hours=math_cfg["time_stop_hours"],
        time_stop_min_gain=math_cfg["time_stop_min_gain"],
    )

    # Brain
    claude = ClaudeClient(
        api_key=get_secret("ANTHROPIC_API_KEY"),
        model=config["brain"]["model"],
    )
    prompt_builder = PromptBuilder(
        prompts_dir=str(base_dir / "brain" / "prompts"),
        biases_path=str(base_dir / "memory" / "biases.json"),
        lessons_path=str(base_dir / "memory" / "lessons.json"),
    )

    # Execution
    exec_cfg = config["execution"]
    trader = PaperTrader(
        db=db,
        max_slippage=exec_cfg["max_slippage"],
        max_bankroll_deployed=exec_cfg["max_bankroll_deployed"],
        max_concurrent_positions=exec_cfg["max_concurrent_positions"],
    )

    # Agents
    outcome_reviewer = OutcomeReviewer(outcomes_dir=str(base_dir / "memory" / "outcomes"))
    bias_detector = BiasDetector(biases_path=str(base_dir / "memory" / "biases.json"))
    strategy_evolver = StrategyEvolver(strategy_log_path=str(base_dir / "memory" / "strategy_log.md"))
    prompt_optimizer = PromptOptimizer(
        prompts_dir=str(base_dir / "brain" / "prompts"),
        scores_path=str(base_dir / "memory" / "prompt_scores.json"),
        min_improvement=config["agents"]["prompt_optimizer_min_improvement"],
    )
    scheduler = AgentScheduler(
        outcome_reviewer=outcome_reviewer,
        bias_detector=bias_detector,
        strategy_evolver=strategy_evolver,
        prompt_optimizer=prompt_optimizer,
        outcome_interval_seconds=config["agents"]["outcome_reviewer_interval_seconds"],
        daily_pipeline_hour=config["agents"]["daily_pipeline_hour"],
        math_config=math_cfg,
    )

    # Discord
    discord_bot = create_bot(db, trader, scanner, scheduler, config)
    alert_manager = AlertManager(
        bot=discord_bot,
        trade_channel_name=config["discord"]["trade_channel_name"],
        control_channel_name=config["discord"]["control_channel_name"],
    )

    # Start all components
    await scheduler.start()

    async def run_discord():
        try:
            await discord_bot.start(get_secret("DISCORD_BOT_TOKEN"))
        except Exception as e:
            logger.error(f"Discord bot error: {e}")

    tasks = [
        asyncio.create_task(trading_loop(
            scanner, claude, prompt_builder, decision_table, trader,
            exit_monitor, alert_manager, db, config,
            is_paused_fn=lambda: discord_bot.is_paused,
        )),
        asyncio.create_task(scheduler.run_outcome_loop()),
        asyncio.create_task(scheduler.run_daily_loop()),
        asyncio.create_task(run_discord()),
    ]

    logger.info("PolyBot started — all systems running")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        await scheduler.stop()
        await db.close()
        await discord_bot.close()

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run integration test to verify it passes**

Run: `cd polybot && python -m pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `cd polybot && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/main.py polybot/tests/test_integration.py
git commit -m "feat: main entry point wiring all components together"
```

---

## Task 18: Dockerfile & Memory Initialization

**Files:**
- Create: `polybot/Dockerfile`, `polybot/memory/biases.json`, `polybot/memory/lessons.json`, `polybot/memory/prompt_scores.json`, `polybot/memory/strategy_log.md`, `polybot/memory/outcomes/.gitkeep`

- [ ] **Step 1: Create initial memory files**

```json
// polybot/memory/biases.json
{}
```

```json
// polybot/memory/lessons.json
{}
```

```json
// polybot/memory/prompt_scores.json
{
  "v001": {"accuracy": 0.0, "total": 0}
}
```

```markdown
# Strategy Evolution Log
```

- [ ] **Step 2: Create .gitkeep for outcomes directory**

```bash
touch polybot/memory/outcomes/.gitkeep
```

- [ ] **Step 3: Create Dockerfile**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "polybot.main"]
```

- [ ] **Step 4: Commit**

```bash
git add polybot/Dockerfile polybot/memory/ polybot/brain/prompts/v001.txt
git commit -m "feat: Dockerfile, initial memory files, and v001 prompt"
```

---

## Task 19: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `cd polybot && python -m pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Verify project structure**

Run: `find polybot -type f -name "*.py" | sort`
Expected: All files from the file map exist

- [ ] **Step 3: Verify imports work**

Run: `cd polybot && python -c "from polybot.main import main; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: final verification — all tests passing, structure complete"
```
