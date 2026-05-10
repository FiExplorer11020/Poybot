# Polymarket Leader Intelligence Bot — Master Context

> **Ce fichier est le contexte principal pour tous les agents Claude Code.**
> Lis-le entièrement avant d'écrire la moindre ligne de code.

---

## 1. WHAT WE ARE BUILDING

A Python bot that builds **deep knowledge** of every influential wallet on Polymarket
— their behavior patterns, follower networks, strengths, weaknesses — and uses that
knowledge to profit from both their correct AND incorrect trades.

**This is NOT a copy-trading bot.** It is a **leader intelligence engine** that:
1. Maps the complete social graph of Polymarket (who follows who, with what probability)
2. Profiles each leader's trading behavior (when, where, how they enter/exit)
3. Models each leader's error patterns (in what conditions they tend to lose)
4. Trades based on this knowledge: FOLLOW when the leader is reliable, FADE when they're likely wrong

**Core insight**: On Polymarket, shares can be sold at any time before market resolution.
Most top traders profit from price movements (swing trading), not from holding to resolution.
The feedback loop is as fast as the leader's holding period (days, not months).

---

## 2. KEY POLYMARKET MECHANICS (verified from official docs)

### Trading mechanics
- Binary outcome tokens: YES + NO per market. Prices sum to ~$1.00
- Shares can be bought and sold at any time via CLOB (Central Limit Order Book)
- Order types: limit orders (rest on book) and market orders (immediate fill)
- Three exit paths: sell shares on orderbook, merge YES+NO → $1.00, or hold to resolution

### Fee structure (as of March 2026)
- Geopolitical/world events: ZERO fees
- Crypto markets (1H, 4H, daily, weekly): variable fees, peak 1.56% at 50% probability
- Sports markets: lower fees, peak 0.44%
- Maker rebates redistributed daily to liquidity providers

### Verified trader strategies on Polymarket
1. **Directional**: Swing/position trading, hold until price target
2. **Structural**: Market making + arbitrage, bots with <100ms execution (NOT COPIABLE)
3. **Cognitive**: Rare, well-researched bets, long holding periods

### Key statistics
- Only 7.6% of wallets are profitable (~120K out of 1.5M+)
- Arbitrage bots capture ~70% of arb profits
- Structural/bot traders must be EXCLUDED from our watchlist (too fast to copy)

---

## 3. LEADER CLASSIFICATION — Dynamic, Not Fixed

Leaders are NOT pre-classified into fixed types. The bot discovers and classifies
each leader automatically based on observed behavior via Falcon API + trade tracking.

### Classification dimensions (learned per wallet)
```
Trading strategy:  directional | structural | cognitive  (from trade patterns)
Influence level:   whale | top_trader | community         (from Falcon Score + volume impact)
Time horizon:      scalper (<1h) | swing (1d-2w) | holder (>2w) (from holding period)
Copiability:       copiable | not_copiable                (from avg execution speed)
```

### Exclusion rules
- Structural/bot traders (execution speed < 1s consistently) → EXCLUDE from trading signals
- Wallets with < 10 trades observed → INSUFFICIENT DATA, observe only
- Wallets with Falcon Score = 0 or negative → SKIP

---

## 4. MODULE MAP

```
src/
├── registry/            # Leader identification and enrichment (Falcon API)
│   ├── falcon_client.py        # Unified Falcon API client (all agent_ids)
│   ├── leader_registry.py      # Leaderboard refresh, enrichment, sync_markets;
│   │                           # stamps falcon_no_data leaders excluded=TRUE
│   └── models.py               # Pydantic models for Falcon responses
│
├── observer/            # Real-time trade observation (dual-source)
│   ├── websocket_client.py     # Polymarket WS with auto-reconnect + ping/pong
│   ├── trade_observer.py       # WS + REST polling, dedup Redis + DB UNIQUE INDEX,
│   │                           # publishes trades:observed on Redis pub/sub
│   ├── position_tracker.py     # Reconstructs OPEN→CLOSE position cycles
│   └── models.py               # Trade/Position dataclasses
│
├── graph/               # Leader→Follower social graph
│   ├── graph_engine.py         # Hot path: Beta-Binomial follower edges
│   ├── hawkes_fitter.py        # Cold path: Hawkes MLE batch nightly
│   └── models.py
│
├── profiler/            # Behavioral profiling + error modeling
│   ├── behavior_profiler.py    # Dirichlet (size-weighted), EWMA, KDE,
│   │                           # accuracy Beta posteriors (size-weighted)
│   ├── error_model.py          # 3 phases: Beta-Binomial → BayesianRidge → LightGBM+Platt
│   └── models.py
│
├── engine/              # Decision and execution
│   ├── confidence_engine.py    # Thompson Sampling: FOLLOW vs FADE vs SKIP
│   ├── decision_router.py      # Routes decisions to paper / live / dual
│   ├── paper_trader.py         # Virtual portfolio + RiskManager integration
│   ├── live_trader.py          # py-clob-client wrapper (gated by killswitch)
│   ├── risk_manager.py         # Reads thresholds from RuntimeConfig (mutable)
│   ├── scheduler.py            # APScheduler wrapper
│   ├── watchdog.py             # Supervises long-running coroutines
│   ├── neural_readiness.py     # Per-market readiness score for the dashboard
│   ├── portfolio_state.py      # Tracks bankroll + peak + drawdown
│   ├── jobs/                   # Cron job factories
│   └── main.py                 # Engine entry point
│
├── control/             # Cross-cutting runtime control
│   ├── killswitch.py           # Global execution gate (DB + Redis cache)
│   └── runtime_config.py       # Mutable risk knobs (Redis-backed, validated)
│
├── api/                 # FastAPI dashboard
│   ├── main.py                 # Lifespan + endpoints + WS bridge wiring
│   ├── queries.py              # SQL builders for the snapshot
│   ├── terminal_snapshot.py    # Composes the live JSON snapshot
│   ├── ws_bridge.py            # WebSocket fan-out for /ws/live
│   └── readiness_persistence.py
│
├── execution/           # Order routing helpers (used by live_trader)
│
├── economics/           # Versioning of the model + paper-trade filters
│
├── database/            # asyncpg pool + queries shared by services
│
├── backups/             # pg_dump → Cloudflare R2 (idle if BACKUPS_ENABLED=false)
│
├── telegram_bot/        # Notifier (sortant) + Bot (commandes /status, /pnl, ...)
│
├── monitoring/          # Health checks + metrics
│
├── logging_setup.py     # Loguru sinks (stdout JSON + file)
└── config.py            # Pydantic settings, env-driven defaults
```

---

## 5. DATA SOURCES

### Falcon API (polymarketanalytics.com)
```
Base URL: https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized
Auth:     Bearer token (free API key)
Format:   POST with agent_id + params + pagination + formatter_config
```

| Agent ID | Name | Usage in bot |
|----------|------|-------------|
| **584** | Falcon Score Leaderboard | Identify leaders (quality ranking) |
| **581** | Wallet 360 | 60+ metrics per wallet (enrichment) |
| **556** | Polymarket Trades | Historical trades by wallet/market/time |
| **569** | Polymarket PnL | Realized PnL time series per wallet |
| **574** | Polymarket Markets | Market data (volume, status, slug) |
| **575** | Market Insights | Liquidity, trend, concentration signals |
| **568** | Polymarket Candlesticks | OHLCV at 1m/5m/1h/1d |
| **572** | Polymarket Orderbook | Historical orderbook snapshots |
| **585** | Social Pulse | X/Twitter momentum + sentiment |
| **579** | Polymarket Leaderboard | Official PnL leaderboard |

### Polymarket Direct APIs
```
CLOB API:     https://clob.polymarket.com
WebSocket:    wss://ws-subscriptions-clob.polymarket.com/ws/
Gamma API:    https://gamma-api.polymarket.com
Data API:     https://data-api.polymarket.com
```

### WebSocket subscription format
```json
{
  "auth": {},
  "markets": ["token_id_1", "token_id_2"],
  "type": "Market"
}
```

---

## 6. DATABASE SCHEMA

All tables in PostgreSQL (standard, NOT TimescaleDB — volume doesn't justify it).

### leaders
```sql
wallet_address      VARCHAR(100) PRIMARY KEY
falcon_score        NUMERIC(10,4)
wallet360_json      JSONB           -- Raw 60+ metrics from Falcon Wallet 360
classification_json JSONB           -- {strategy, influence, horizon, copiable}
first_seen          TIMESTAMPTZ DEFAULT NOW()
last_refresh        TIMESTAMPTZ     -- Last Falcon data pull
on_watchlist        BOOLEAN DEFAULT TRUE
excluded            BOOLEAN DEFAULT FALSE  -- True if bot/structural trader
exclude_reason      VARCHAR(100)
```

### trades_observed
```sql
id                  BIGSERIAL PRIMARY KEY
time                TIMESTAMPTZ NOT NULL
market_id           VARCHAR(100) NOT NULL
token_id            VARCHAR(100) NOT NULL
wallet_address      VARCHAR(100) NOT NULL
side                VARCHAR(4)      -- 'buy' or 'sell'
price               NUMERIC(10,6) NOT NULL
size_usdc           NUMERIC(20,2) NOT NULL
source              VARCHAR(10)     -- 'websocket' or 'falcon'
is_leader           BOOLEAN DEFAULT FALSE
```
Index: (wallet_address, time), (market_id, time), (time) for cleanup

### positions_reconstructed
```sql
id                  BIGSERIAL PRIMARY KEY
wallet_address      VARCHAR(100) NOT NULL
market_id           VARCHAR(100) NOT NULL
token_id            VARCHAR(100) NOT NULL
direction           VARCHAR(3)      -- 'yes' or 'no'
open_time           TIMESTAMPTZ NOT NULL
close_time          TIMESTAMPTZ     -- NULL if still open
entry_price         NUMERIC(10,6) NOT NULL
exit_price          NUMERIC(10,6)
size_usdc           NUMERIC(20,2) NOT NULL
pnl_usdc            NUMERIC(20,2)   -- NULL if still open
pnl_pct             NUMERIC(10,4)
holding_period_s    INTEGER         -- seconds from open to close
close_method        VARCHAR(10)     -- 'sell', 'merge', 'resolution', NULL
```
Index: (wallet_address, open_time), (market_id, open_time)

### follower_edges
```sql
id                  BIGSERIAL PRIMARY KEY
leader_wallet       VARCHAR(100) NOT NULL
follower_wallet     VARCHAR(100) NOT NULL
co_occurrences      INTEGER DEFAULT 0
hawkes_alpha_mu     NUMERIC(10,6)   -- Hawkes excitation ratio (causal strength)
follow_probability  NUMERIC(5,4)    -- Beta posterior mean
follow_beta_a       NUMERIC(10,4)   -- Beta distribution alpha param
follow_beta_b       NUMERIC(10,4)   -- Beta distribution beta param
avg_delay_s         NUMERIC(10,2)
same_direction_rate NUMERIC(5,4)
trapped_rate        NUMERIC(5,4)    -- P(follower still in when leader exits)
first_observed      TIMESTAMPTZ
last_observed       TIMESTAMPTZ
UNIQUE(leader_wallet, follower_wallet)
```
Index: (leader_wallet), (follower_wallet)

### leader_profiles
```sql
wallet_address          VARCHAR(100) PRIMARY KEY REFERENCES leaders
profile_json            JSONB NOT NULL
-- Contains: {
--   preferred_categories: {cat: Dirichlet_params},
--   entry_patterns: {contrarian_rate, momentum_rate, time_distribution},
--   sizing: {avg_size, ewma_size, kde_params},
--   accuracy: {
--     overall: float,
--     by_category: {cat: {wins, losses, beta_a, beta_b}},
--     resolved_count: int
--   },
--   follower_impact: {avg_volume_induced, avg_price_move, followers_activated}
-- }
error_model_phase       INTEGER DEFAULT 1  -- 1=Beta, 2=LogReg, 3=LightGBM
error_model_blob        BYTEA              -- Serialized model (phases 2-3)
profile_maturity        NUMERIC(5,4)       -- 0-1
trades_observed         INTEGER DEFAULT 0
positions_resolved      INTEGER DEFAULT 0
last_updated            TIMESTAMPTZ
```

### markets
```sql
market_id       VARCHAR(100) PRIMARY KEY
question        TEXT NOT NULL
category        VARCHAR(50)
token_yes       VARCHAR(100)
token_no        VARCHAR(100)
end_date        TIMESTAMPTZ
volume_24h      NUMERIC(20,2)
liquidity_score NUMERIC(10,4)   -- From Falcon Market Insights (agent 575)
active          BOOLEAN DEFAULT TRUE
fee_rate_pct    NUMERIC(5,4)    -- Current fee rate for this market type
updated_at      TIMESTAMPTZ DEFAULT NOW()
```

### paper_trades
```sql
id              SERIAL PRIMARY KEY
opened_at       TIMESTAMPTZ NOT NULL
closed_at       TIMESTAMPTZ
market_id       VARCHAR(100) NOT NULL
token_id        VARCHAR(100) NOT NULL
direction       VARCHAR(3)          -- 'yes' or 'no'
entry_price     NUMERIC(10,6) NOT NULL
exit_price      NUMERIC(10,6)
size_usdc       NUMERIC(20,2) NOT NULL
pnl_usdc        NUMERIC(20,2)
fee_paid_usdc   NUMERIC(20,2)       -- Estimated fees
strategy        VARCHAR(10)         -- 'follow' or 'fade'
leader_wallet   VARCHAR(100)
leader_context  JSONB               -- Snapshot of why this trade was taken
confidence      NUMERIC(5,4)        -- Thompson sample value at decision time
status          VARCHAR(10)         -- 'open','closed','expired','cancelled'
close_reason    VARCHAR(50)
```

### decision_log
```sql
id              BIGSERIAL PRIMARY KEY
time            TIMESTAMPTZ NOT NULL
leader_wallet   VARCHAR(100) NOT NULL
market_id       VARCHAR(100) NOT NULL
action          VARCHAR(10)         -- 'follow', 'fade', 'skip'
thompson_follow NUMERIC(5,4)        -- Thompson sample for follow
thompson_fade   NUMERIC(5,4)        -- Thompson sample for fade
kelly_fraction  NUMERIC(5,4)
confidence      NUMERIC(5,4)
reason          TEXT                -- Human-readable decision explanation
outcome         VARCHAR(10)         -- 'win', 'loss', NULL (pending)
```

---

## 7. STATISTICAL MODELS REFERENCE

### Chemin chaud (real-time, < 100ms per decision)
All parameters pre-computed in Redis cache, decision = lookup + 2 random samples.

### Chemin tiède (per trade observed, O(1) per update)
- **Beta-Binomial** (follower edges, error model phase 1): α += 1 or β += 1
- **Dirichlet** (market preferences): category counter += 1
- **EWMA** (sizing, timing): μ = λ·μ_prev + (1-λ)·x_new, λ=0.94
- **CUSUM** (drift detection): S = max(0, S_prev + error - baseline - slack)

### Chemin froid (batch, 1x/24h at 3 AM, ~10 min total)
- **Hawkes Process** (follower detection): MLE fit on 30-day trade timestamps via scipy
  - Library: `tick` or custom scipy.optimize
  - Key output: α/μ ratio (>1 = follower confirmed, <0.3 = coincidence)
- **Bayesian Logistic Regression** (behavior + error phase 2): fit on 90-day data
  - Library: `numpyro` or `sklearn.linear_model.BayesianRidge`
- **LightGBM + Platt calibration** (error phase 3, weekly): fit on all resolved data
  - Library: `lightgbm` + `sklearn.calibration.CalibratedClassifierCV`

### Decision engine
- **Thompson Sampling**: Beta(α_follow, β_follow) vs Beta(α_fade, β_fade) per leader
  - Exploration floor: max(0.1, 1/√n_observations)
- **Bayesian Kelly**: f* = (p·b - q) / b × shrinkage, shrinkage = 1 - σ²_p/p²
  - Hard cap: 2% of bankroll per trade, FADE sizing = 50% of FOLLOW max
- **Price impact**: ΔP/P ≈ σ_daily × √(Q / V_daily)  (square-root law)

### Error model progression
| Phase | Trigger | Model | Update frequency |
|-------|---------|-------|-----------------|
| 1 | 0-99 resolved positions | Beta-Binomial per category | O(1) per resolution |
| 2 | 100-499 resolved | Bayesian LogReg | Re-fit every 24h |
| 3 | 500+ resolved | LightGBM + Platt calibration | Re-fit every 7 days |

### Drift detection
CUSUM on rolling error rate. If drift detected → downgrade error model one phase,
reduce position sizes, accumulate fresh data.

---

## 8. TECH STACK (exact versions)

```
Python              3.11+
PostgreSQL          15           (asyncpg, no TimescaleDB)
Redis               7.2-alpine   (cache + pub/sub)
asyncpg             0.29.0
aiohttp             3.9.3        # Falcon API + Polymarket REST
websockets          12.0         # Polymarket CLOB WS client
pydantic            2.6.0        # Validation
pydantic-settings                # .env loading
numpy               1.26.4
scipy               1.12.0       # Hawkes MLE
numpyro             0.14.0       # Bayesian LogReg phase 2
lightgbm            4.3.0        # Error model phase 3
scikit-learn        1.4.0        # Calibrated classifier, BayesianRidge fallback
redis               5.0.1        # Async client
loguru              0.7.2        # Structured logging
APScheduler         3.10.4       # Cron jobs in the engine container
python-telegram-bot              # Telegram alerts + commands
py-clob-client                   # Live trading CLOB wrapper
boto3                            # Cloudflare R2 (S3-compat)
FastAPI + uvicorn                # Dashboard backend
React               18.3         # Dashboard frontend (Babel-on-the-fly via CDN)
pytest              8.1.0
pytest-asyncio      0.23.5
```

Removed from old stack: TimescaleDB extension, hdbscan, pandas, polars, prometheus-client.

---

## 9. KEY CONSTANTS (src/config.py, overridable via .env)

> Two layers of config: env-driven defaults (immutable at runtime, listed
> below) and runtime-mutable overrides for risk knobs in
> `src/control/runtime_config.py`. The dashboard's RISK & CONFIG cockpit
> writes to the runtime layer via `POST /api/risk/update`; values are
> validated against `BOUNDS` and persisted in Redis. Currently mutable:
> `risk_per_trade_pct`, `max_total_exposure_pct`, `kelly_fraction`,
> `max_drawdown_stop_pct`, `min_signal_strength`, `max_concurrent_positions`,
> `cooldown_seconds`, `max_consecutive_losses`,
> `max_recent_losses_per_market`, `fade_size_ratio`. RiskManager reads
> from the runtime layer first, falls back to settings on miss.


```python
# Falcon API
FALCON_API_URL = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
FALCON_API_KEY = ""                     # Required, from .env
FALCON_REFRESH_INTERVAL_S = 3600       # Refresh leader registry every hour
FALCON_CACHE_TTL_S = 172800            # 48h cache (survive Falcon downtime)

# Leader registry
INITIAL_LEADER_COUNT = 200             # Start with top 200 by Falcon Score
MAX_LEADER_COUNT = 2000                # Maximum leaders to track
MIN_FALCON_SCORE = 0.0                 # Minimum score to include

# Trade observation
TOP_MARKETS_COUNT = 50                 # Track N most active markets
WEBSOCKET_PING_INTERVAL_S = 30
WEBSOCKET_PONG_TIMEOUT_S = 10

# Graph engine
FOLLOWER_WINDOW_S = 300                # 5 min window after leader trade
MIN_CO_OCCURRENCES = 5                 # Minimum to consider an edge
MIN_SAME_DIRECTION_RATE = 0.7          # Minimum to confirm follower
HAWKES_LOOKBACK_DAYS = 30              # Data window for Hawkes fit

# Profiler
EWMA_LAMBDA = 0.94                     # ~15 day half-life
MIN_TRADES_FOR_PROFILE = 20            # Minimum to start profiling
MIN_RESOLVED_FOR_ERROR_P2 = 100        # Trigger phase 2 error model
MIN_RESOLVED_FOR_ERROR_P3 = 500        # Trigger phase 3 error model

# Confidence engine
FOLLOW_MIN_TRADES = 50                 # Minimum trades to activate FOLLOW
FOLLOW_MIN_FOLLOWERS = 5               # Minimum confirmed followers
FADE_MIN_RESOLVED = 50                 # Minimum resolved positions for FADE
FADE_MIN_CONFIDENCE = 0.75             # Higher threshold for FADE
THOMPSON_EXPLORATION_FLOOR = 0.10      # Minimum exploration rate

# Paper trading
PAPER_CAPITAL_USDC = 10_000
MAX_POSITION_PCT = 0.02                # Max 2% of capital per trade (Kelly hard cap)
FADE_SIZE_RATIO = 0.50                 # FADE position = 50% of equivalent FOLLOW
MAX_MARKET_EXPOSURE_PCT = 0.25         # No single market > 25% of open positions
MIN_POSITION_USDC = 50                 # Floor for minimum trade size

# Batch processing
BATCH_HOUR_UTC = 3                     # Run batch at 3 AM UTC
BATCH_HAWKES_LEADERS = 200             # Max leaders for Hawkes refit per batch
RETENTION_TRADES_DAYS = 90             # Keep observed trades for 90 days
```

---

## 10. CODING CONVENTIONS

### Async everywhere
```python
# ALL I/O must be async. Never use sync DB calls or sync HTTP calls.
async def fetch_trades(market_id: str) -> list[Trade]:
    async with get_db() as conn:
        ...
```

### Error handling
```python
from loguru import logger
try:
    result = await risky_operation()
except Exception as e:
    logger.exception(f"Failed to do X for market={market_id}: {e}")
    raise  # or return None if non-critical
```

### Pydantic models for all external data (Falcon API, Polymarket API)
```python
class FalconResponse(BaseModel):
    data: list[dict]
    pagination: dict | None = None
```

### Database access — parameterized queries only
```python
from src.database.connection import get_db
async with get_db() as conn:
    await conn.execute(
        "INSERT INTO trades_observed (time, market_id, wallet_address, side, price, size_usdc) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        trade.time, trade.market_id, trade.wallet_address, trade.side, trade.price, trade.size_usdc
    )
```

### Absolute imports only
```python
from src.registry.falcon_client import FalconClient
# NOT: from .falcon_client import ...
```

### Logging (loguru, structured)
```python
from loguru import logger
logger.info("Leader trade detected", wallet=wallet, market=market_id, size=size_usdc)
```

---

## 11. TESTING CONVENTIONS

- Every public function/class has at least one unit test in `tests/`
- Mirror src/ structure: `tests/test_registry/test_falcon_client.py`
- Use `pytest-asyncio` for async tests (`@pytest.mark.asyncio`)
- Mock external calls (Falcon API, Polymarket API, DB) in unit tests
- Integration tests use real local PostgreSQL via Docker
- Run: `pytest tests/unit/` for fast, `pytest tests/integration/` for DB

---

## 12. ENVIRONMENT VARIABLES

See `.env.example`. Required at runtime:
```
DATABASE_URL          postgresql://user:pass@localhost:5432/polymarket
REDIS_URL             redis://localhost:6379/0
FALCON_API_KEY        your_falcon_api_key_here
LOG_LEVEL             INFO
PAPER_TRADING         true
```

---

## 13. RUNNING LOCALLY

```bash
# Backends only (postgres + redis)
docker compose up -d postgres redis

# Apply DB migrations
python scripts/setup_db.py

# Start leader registry (pulls from Falcon, sync_markets every cycle)
python -m src.registry.main

# Start trade observer (WS + REST polling, dual-source dedup)
python -m src.observer.main

# Start intelligence engine (graph + profiler + decisions + scheduler + watchdog)
python -m src.engine.main

# Start dashboard API
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Or full stack in Docker (recommended for verifying compose wiring):

```bash
docker compose up -d --build
docker compose logs -f engine
```

## 13b. PRODUCTION (Hetzner Helsinki)

The bot lives at `/opt/polymarket-bot/` on `polymarket-prod`
(89.167.23.215). The deploy workflow is rsync (the prod path is **not**
a git checkout). Postgres user/db are `polymarket` (NOT `postgres`).

→ **Single source of truth for the deploy procedure: [docs/DEPLOY.md](docs/DEPLOY.md).**
   Pre-flight, rsync command with the right excludes, rebuild rules,
   verification commands, rollback options, troubleshooting.

→ Infrastructure (specs, memory map, healthchecks): [docs/INFRA.md](docs/INFRA.md).

---

## 14. CRITICAL PITFALLS — DO NOT DO THESE

1. **Never use sync DB calls** (psycopg2, synchronous SQLAlchemy). Only asyncpg.
2. **Never call Falcon API without rate limit protection** — cache responses, respect limits.
3. **Never treat market resolution as the only success metric** — leaders profit from selling before resolution. Track POSITION PnL (entry→exit), not prediction accuracy.
4. **Never try to copy structural/bot traders** — they execute in <100ms with colocated infra. Detect and EXCLUDE them.
5. **Never store raw JSON from API directly** — validate with Pydantic first.
6. **Never hardcode wallet addresses or API keys** — use `settings` from `src/config.py`.
7. **Never implement live trading** during paper trading phase.
8. **The CLOB WebSocket drops silently** — always implement ping/pong and reconnect.
9. **Polymarket uses proxy wallets** — wallet_address in trades may differ from account.
10. **Fees matter for PnL** — crypto market fees can reach 1.56%. Always include fees in paper trade PnL calculation.
11. **Never assume a leader holds to resolution** — most profitable leaders are swing traders. Track the FULL position cycle (open→close).
12. **Merge exits are invisible on the orderbook** — a leader can exit by buying the complementary token and merging. Monitor BOTH token trades per wallet.

---

## 15. CURRENT IMPLEMENTATION STATUS

> Keep this section aligned with the actual repo, not the original build plan.

| Module                       | Status     | Notes                                                                                     |
|-----------------------------|------------|-------------------------------------------------------------------------------------------|
| database/connection         | IMPLEMENTED | asyncpg pool used by runtime and API                                                      |
| database/models             | IMPLEMENTED | dataclasses and row mapping present                                                       |
| database/queries            | PARTIAL    | SQL still split across services; api/queries.py is the canonical place for snapshot SQL |
| registry/falcon_client      | IMPLEMENTED | Falcon auth, caching (48h TTL), retry, normalization                                      |
| registry/leader_registry    | IMPLEMENTED | refresh_leaderboard, enrich_leaders (excludes falcon_no_data), sync_markets (skips expired) |
| observer/websocket_client   | IMPLEMENTED | live subscriptions, reconnects with exp backoff, ping/pong, metrics                       |
| observer/trade_observer     | IMPLEMENTED | WS + REST polling, dedup Redis 7d TTL + DB UNIQUE INDEX, publish trades:observed          |
| observer/position_tracker   | IMPLEMENTED | open/close reconstruction, sell/merge/resolution close methods                            |
| graph/graph_engine          | IMPLEMENTED | follower edges hot path, replay, Beta posterior updates                                   |
| graph/hawkes_fitter         | IMPLEMENTED | batch Hawkes MLE fitting for confirmed edges                                              |
| profiler/behavior_profiler  | IMPLEMENTED | size-weighted Dirichlet, EWMA sizing, KDE timing, size-weighted Beta accuracy             |
| profiler/error_model        | IMPLEMENTED | phase progression Beta → BayesianRidge → LightGBM, drift detection                        |
| engine/confidence_engine    | IMPLEMENTED | Thompson Sampling + Bayesian Kelly with shrinkage                                         |
| engine/decision_router      | IMPLEMENTED | paper / live / dual routing                                                               |
| engine/paper_trader         | IMPLEMENTED | paper portfolio, monitoring, feedback loop, RiskManager integration                       |
| engine/live_trader          | IMPLEMENTED | py-clob-client wrapper, gated by killswitch + LIVE_TRADING_DRY_RUN flags                  |
| engine/risk_manager         | IMPLEMENTED | reads thresholds from runtime_config (mutable), warm + hard breakers                      |
| engine/scheduler            | IMPLEMENTED | APScheduler: nightly_batch, redis_cleanup, killswitch_sync, watchdog, refresh_thresholds  |
| engine/watchdog             | IMPLEMENTED | supervises long-running coroutines, restart on heartbeat miss                             |
| engine/neural_readiness     | IMPLEMENTED | per-market readiness scoring for the dashboard                                            |
| control/killswitch          | IMPLEMENTED | DB singleton + Redis cache, propagation < 5 min                                           |
| control/runtime_config      | IMPLEMENTED | mutable risk knobs in Redis with validation + pub/sub propagation                         |
| api/main                    | IMPLEMENTED | 22 endpoints + WS bridge, terminal snapshot cache 1 s TTL                                 |
| api/queries                 | IMPLEMENTED | SQL builders (~2500 LOC) for all dashboard sections                                       |
| api/terminal_snapshot       | IMPLEMENTED | composes the live JSON snapshot from query outputs                                        |
| api/inspector               | IMPLEMENTED | /api/inspector/snapshot exposes raw trades + decisions + source mix + pipeline metrics    |
| backups/dumper              | IMPLEMENTED | wired but idle until BACKUPS_ENABLED=true and R2 creds populated                          |
| telegram_bot/notifier       | IMPLEMENTED | sortant: alerts opening/closing/killswitch/crash                                          |
| telegram_bot/bot            | IMPLEMENTED | entrant: /status, /pnl, /positions, /mode, /killswitch, /pause, /resume                   |
| monitoring/metrics          | IMPLEMENTED | health and runtime support utilities                                                      |
| Frontend (8 tabs)           | IMPLEMENTED | Alpha Terminal, ML Progression, Wallet Graph (incl. Wallet Scanner table), Live Portfolio, Decision Engine, Inspector, Risk & Config, Bot Health |

---

## 16. RECENT CHANGES (May 10, 2026 session)

The following items were delivered this session and are reflected in the
sections above. Keep this changelog concise — old entries get pruned
when they become "the way it has always worked".

- **Data quality** : `enrich_leaders` now stamps `excluded=TRUE,
  on_watchlist=FALSE` for `falcon_no_data` wallets. `sync_markets` skips
  expired markets (`end_date < NOW() - 24h`). `data_quality()` only
  counts unmapped tokens for live markets. New `unmapped_expired_skipped`
  field exposes the silent-skip count.
- **Wallet Scanner** : the Market Scanner tab was removed from nav. The
  Wallet Graph tab now hosts a Graph / Wallet Scanner toggle. The
  scanner table is leader-centric (phase, strategy, falcon, trades 24h,
  resolved, win rate, PnL, readiness composite, last action).
- **Risk & Config cockpit** : new `src/control/runtime_config.py`,
  `POST /api/risk/update` endpoint, RiskManager reads from runtime
  layer. Frontend inputs are no longer disabled.
- **Pipeline coherence** : Dirichlet category and Beta accuracy
  posteriors are now size-weighted via
  `_size_weight(size_usdc, ewma_size)` (sqrt scaling, clamped to
  [0.5, 3.0]).
- **Inspector tab** : new tab + `GET /api/inspector/snapshot` exposes
  raw trades, decisions, source mix, pipeline health (Redis reachable,
  WS lag, msgs/min, pubsub subscribers).
- **Full-width UI** : dashboard-app.jsx wraps the active tab in an
  absolutely-positioned container so all tabs fill the viewport.
- **Dockerfile + .dockerignore** : `docs/migrations/` is now copied
  into the runtime image so `setup_db.py` can apply pending schema
  changes inside the container.
