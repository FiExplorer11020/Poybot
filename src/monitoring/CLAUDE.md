# Monitoring Module — Health Checks + Batch Orchestration

**Purpose**: Health checks for all system components, structured logging, and the nightly
batch orchestrator that runs cold-path models (Hawkes, LogReg, LightGBM).

See parent [CLAUDE.md](../CLAUDE.md) for full context.

---

## Components

- **metrics.py**: Structured logging via loguru. Health check functions for DB, Redis,
  Falcon API reachability, WebSocket status, data freshness. No Prometheus (removed from stack).

---

## Health Checks

```python
async def health_check() -> dict:
    return {
        "db": await check_db_connectivity(),
        "redis": await check_redis_connectivity(),
        "falcon": await check_falcon_reachable(),
        "websocket": check_ws_connected(),
        "data_freshness": await check_latest_trade_age(),
        "leader_count": await count_active_leaders(),
        "paper_pnl": await get_paper_pnl_summary(),
    }
```

### Data Freshness Alert
If `trades_observed.time` is > 5 minutes old for any active leader → alert.
Indicates WebSocket may have dropped or Falcon backfill is stale.

---

## Batch Orchestrator (Cold Path)

Runs at `BATCH_HOUR_UTC` (3 AM) daily. Implemented in `scripts/batch_runner.py`.

Sequential steps:
1. Refresh leader registry from Falcon (agents 584, 581, 579)
2. Backfill missing trades from Falcon agent 556
3. Re-fit Hawkes for confirmed edges (up to `BATCH_HAWKES_LEADERS`)
4. Re-fit Bayesian LogReg for leaders in error model phase 2
5. Re-fit LightGBM for leaders in phase 3 (weekly only)
6. Precompute Redis cache for hot path
7. Run CUSUM drift check on all active leaders
8. Cleanup: delete `trades_observed` older than `RETENTION_TRADES_DAYS`

Each step logs timing. If any step fails: log error, continue with next step.

---

## Logging Convention

All modules use loguru with structured context:

```python
from loguru import logger

logger.info("Trade observed", wallet=wallet, market=market_id, size=size_usdc)
logger.warning("Falcon timeout", agent_id=584, attempt=3)
logger.error("Position close failed", position_id=pid, error=str(e))
```

No Prometheus metrics (removed from stack). Health checks are queried via
`scripts/health_check.py` and logged to stdout/file.

---

## References
- Constants: `BATCH_HOUR_UTC`, `BATCH_HAWKES_LEADERS`, `RETENTION_TRADES_DAYS` from config.py
- All module entry points: `src/registry/main.py`, `src/observer/main.py`, `src/engine/main.py`
- Health check script: `scripts/health_check.py`
- Batch runner: `scripts/batch_runner.py`
