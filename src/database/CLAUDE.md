# Module: database

## Purpose
All database interaction lives here. Every other module uses this module
to read/write data — never connects to the DB directly.

## Pattern: asyncpg pool + context manager
```python
from src.database.connection import get_db

async with get_db() as conn:
    rows = await conn.fetch("SELECT * FROM leaders WHERE on_watchlist = TRUE")
    leaders = [Leader.from_row(r) for r in rows]
```

## Key files
- connection.py: pool management, get_db() context manager
- models.py: dataclasses with from_row() and to_dict() for all tables
- ../docs/migrations/: SQL files, applied in order by scripts/setup_db.py

## Connection pool config
```python
pool = await asyncpg.create_pool(
    dsn=settings.DATABASE_URL,
    min_size=settings.DB_POOL_MIN,    # default: 2
    max_size=settings.DB_POOL_MAX,    # default: 10
    command_timeout=30,
    server_settings={"application_name": "polymarket_bot"}
)
```

## Models reference (new schema — 001_schema.sql)
8 tables, each with a corresponding dataclass:

| Table | Dataclass | Description |
|---|---|---|
| leaders | Leader | Wallets identified via Falcon API |
| trades_observed | TradeObserved | Raw trades on leader-active markets |
| positions_reconstructed | Position | Full OPEN→CLOSE cycles with PnL |
| follower_edges | FollowerEdge | Leader→Follower graph with probabilities |
| leader_profiles | LeaderProfile | Behavioral profiles + error models |
| markets | Market | Market metadata + liquidity |
| paper_trades | PaperTrade | Virtual portfolio trades |
| decision_log | Decision | Audit trail (Thompson, Kelly, outcome) |

Each dataclass has:
- `from_row(record: asyncpg.Record) -> Self`
- `to_dict() -> dict` (for INSERT statements)

## Redis usage
Redis is accessed directly by the runtime modules and the API layer:
- Leader profile cache (pre-computed for hot path)
- Recent trades ring buffer per market
- WebSocket subscription state
- Pub/sub for inter-module communication

## NEVER use
- SQLAlchemy (adds overhead, ORM pattern not needed here)
- Synchronous psycopg2
- Raw string concatenation for SQL values (use parameterized queries: $1, $2, ...)
- TimescaleDB extensions (volume doesn't justify hypertables)
