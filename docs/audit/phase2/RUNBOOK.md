# Phase 0 + 1 + 2 Deploy Runbook

---

**Phases covered**: Phase 0 (production-safety, audit commit `3756040`) +
Phase 1 (data-acquisition perf + observability, same commit) + Phase 2
(schema partitioning, partial indexes, persistent PositionTracker,
dedicated Redis subscribers — agents running in parallel; sections that
depend on Phase 2 outputs are marked `<!-- TODO: confirm against Phase 2 outputs -->`).

**Prerequisites**:
- SSH access to `polymarket@89.167.23.215` with `~/.ssh/hetzner_polymarket`
- Local repo at `/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot`
- Git HEAD at or after `3756040` (Phase 0+1 audit commit)
- Read [docs/DEPLOY.md](../../DEPLOY.md) at least once before this is your first deploy

**Expected duration**:
- Phase 0+1 only: ~30 min (rsync + rebuild + 2 migrations + validation)
- Phase 0+1+2 partition cutover (migration 013): +5–20 min downtime at current dev volume, ~1–3 min at 10× volume
- Phase 0+1+2 full: 60–90 min

**If things break**: see Section 7 (Rollback). Killswitch fallback in
[docs/DEPLOY.md](../../DEPLOY.md#option-c--killswitch-immédiat). Master
audit context: [docs/audit/MASTER_REPORT.md](../MASTER_REPORT.md).

---

## 0. Pre-Flight (~5 min)

```bash
cd "/Users/oscargrima/Documents/Claude/Projects/Polymarket trading bot/polymarket-bot"

# 0.1 — confirm git HEAD includes the audit commit
git log -3 --oneline
# Expect 3756040 (feat(audit): Phase 0 safety + Phase 1 perf + observability)
# and 0b81f8a (feat(dashboard)) in the log. Newer commits = your Phase 2 work.

# 0.2 — local tests
source .venv/bin/activate
pytest -q --ignore=tests/integration --ignore=tests/test_docker.py
# Pre-existing failures (APScheduler/fakeredis/pyyaml not installed) tolerated.
# See docs/audit/phase0/A_tx_and_fees.md §"Pre-existing failures".

# 0.3 — staging DB sanity (if applicable)
psql "$STAGING_DATABASE_URL" -c "SELECT version();"

# 0.4 — pre-deploy backup to R2 (uses existing backups container)
ssh -i ~/.ssh/hetzner_polymarket polymarket@89.167.23.215 \
  'cd /opt/polymarket-bot && docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backups python -m src.backups.dumper'
# Confirm the dump landed in R2 before continuing.
```

There is no host-level systemd timer for the bot — APScheduler lives
inside `polymarket_engine`. Stopping the engine container (Section 4.2)
is the only "disable cron" step.

---

## 1. Dependency update

Phase 1 adds **`prometheus-client==0.20.0`** to `pyproject.toml`. The
container's image is rebuilt during deploy (see Section 4), so the
dependency installs automatically via the multi-stage build. No host-side
`pip install` required.

Dev-only deps (`pyyaml`, `fakeredis`) are test-infra. Production
unaffected.

---

## 2. Environment variable changes

`.env` lives on the VM at `/opt/polymarket-bot/.env`. The rsync excludes
it deliberately ([DEPLOY.md §Étape 2](../../DEPLOY.md)). Edit in place:

```bash
ssh hetzner-polymarket
cd /opt/polymarket-bot
cp .env .env.bak.$(date +%Y%m%d-%H%M%S)
nano .env
```

| Key | Default | Purpose | Phase | Action |
|-----|---------|---------|-------|--------|
| `RETENTION_ENABLED` | `false` | Master switch for nightly retention sweep | 0 | Leave `false` for first deploy. Flip after `--dry-run` (Section 6). |
| `RETENTION_DECISION_LOG_DAYS` | 90 | Per-table override | 0 | Optional |
| `RETENTION_BOOK_QUALITY_SNAPSHOTS_DAYS` | 30 | Per-table override (highest growth) | 0 | Shorten if disk pressure |
| `RETENTION_PORTFOLIO_EQUITY_DAYS` | 180 | Per-table override | 0 | Optional |
| `RETENTION_DECISION_STATE_TRANSITIONS_DAYS` | 90 | Per-table override | 0 | Optional |
| `RETENTION_LIVE_ORDERS_DAYS` | 180 | Per-table override | 0 | Optional |
| `RETENTION_SIGNAL_AUDITS_DAYS` | 90 | Dormant table | 0 | Optional |
| `RETENTION_FEE_SNAPSHOTS_DAYS` | 90 | Dormant table | 0 | Optional |
| `RETENTION_SYSTEM_CONTROL_AUDIT_DAYS` | 365 | Per-table override | 0 | Optional |
| `RETENTION_RISK_CONFIG_HISTORY_DAYS` | 365 | Per-table override | 0 | Optional |
| `TRADE_OBSERVER_POLL_INTERVAL_S` | **5** (was 30) | REST poll cadence | 1 | Default fine |
| `TRADE_OBSERVER_QUEUE_MAX` | 10000 | Bounded producer/consumer queue | 1 | Default fine |
| `TRADE_OBSERVER_BATCH_MAX` | 200 | Rows per `executemany` flush | 1 | Default fine |
| `TRADE_OBSERVER_BATCH_FLUSH_MS` | 100 | Soft flush deadline (ms) | 1 | Default fine |
| `FALCON_MAX_CONCURRENCY` | 8 | Falcon semaphore (was 1; 60 RPM is real cap) | 1 | Default fine |
| `REGISTRY_BACKFILL_CONCURRENCY` | 20 | `_backfill_wallet_trades` gather | 1 | Default fine |
| `FALCON_MAX_REQUESTS_PER_MINUTE` | 60 | Falcon rate-limit token bucket | 1 | Pre-existing; only set if Falcon raises quota |
| Phase 2 keys | — | — | 2 | <!-- TODO: confirm against Phase 2 outputs --> see `phase2/{A,B,C,D}_*.md` |

**Consistency check against `.env.example`**: the Phase 1 keys
(`TRADE_OBSERVER_POLL_INTERVAL_S`, `TRADE_OBSERVER_QUEUE_MAX`,
`TRADE_OBSERVER_BATCH_MAX`, `TRADE_OBSERVER_BATCH_FLUSH_MS`,
`FALCON_MAX_CONCURRENCY`, `REGISTRY_BACKFILL_CONCURRENCY`,
`FALCON_MAX_REQUESTS_PER_MINUTE`) are **missing from
`.env.example`**. Code defaults (in `src/config.py`) apply when unset,
so production behaviour is correct. File a follow-up to append a Phase 1
block to `.env.example` so operators can see the knobs exist.

---

## 3. Migration apply order (the critical section)

Migrations live in `docs/migrations/*.sql`. The runner is
`scripts/setup_db.py`, executed inside the engine container at boot. It
records applied versions in `schema_migrations`.

Inspect what's already applied on prod:

```bash
ssh hetzner-polymarket
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  -c "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
```

Order of operations:

### 3.1 — `009_trades_category_denorm.sql` (already in repo)

Adds `category` denorm column to `trades_observed`. If `SELECT version FROM
schema_migrations WHERE version = 9;` returns nothing, this applies on
next boot. Safe, additive.

**Verify**: `\d trades_observed` shows a `category VARCHAR` column.

### 3.2 — `010_risk_config_history.sql` (already in repo)

Tracks every `runtime_config` mutation. Safe, additive.

**Verify**: `\d risk_config_history` exists.

### 3.3 — `011_retention_policies.sql` — Phase 0

Adds 3 B-tree indexes (`idx_live_orders_placed_at`,
`idx_signal_audits_created_at`, `idx_fee_snapshots_captured_at`). Tables
are dormant or near-empty, so non-`CONCURRENTLY` is fine. See
[phase0/D_retention.md](../phase0/D_retention.md).

**Verify**:
```sql
SELECT indexname FROM pg_indexes
WHERE indexname IN (
  'idx_live_orders_placed_at',
  'idx_signal_audits_created_at',
  'idx_fee_snapshots_captured_at');
-- expect 3 rows
```

**Rollback**: `DROP INDEX IF EXISTS <name>;` (per-index, individually).
No data risk.

### 3.4 — `012_liquidity_score_asof.sql` — Phase 0

Adds two columns + one index to `markets`:
`liquidity_score_updated_at`, `liquidity_score_source`,
`idx_markets_liq_updated_at`. See
[phase0/C_liquidity.md](../phase0/C_liquidity.md).

**Verify**:
```sql
\d markets
-- expect liquidity_score_updated_at, liquidity_score_source columns

SELECT COUNT(*) FILTER (WHERE liquidity_score_source = 'falcon_575') AS source_575,
       COUNT(*) FILTER (WHERE liquidity_score_source = 'falcon_574') AS source_574,
       COUNT(*) AS total
FROM markets;
-- after 1 registry cycle (~30 min), source_575 should be > 0 and growing
```

**Rollback**: `ALTER TABLE markets DROP COLUMN liquidity_score_updated_at;`
and `DROP COLUMN liquidity_score_source;`. Safe — no code path reads
these columns yet (only writes).

### 3.5 — `013_trades_observed_partition.sql` — Phase 2A

<!-- TODO: confirm against Phase 2 outputs -->

**DANGEROUS — table cutover.** Converts `trades_observed` to declarative
range-partitioned by `time` via the rebuild-and-swap recipe (see file
header). The migration is wrapped in a single transaction (the
`setup_db.py` runner forces this), so the `INSERT … SELECT` step holds a
lock on the source table for its duration.

**Downtime estimate**: < 1 s on current dev DB (~100k rows), 5–20 s at
10× (~1M rows), 1–3 min at 100× (~10M rows). Read
[`phase2/A_partition_cutover.md`](A_partition_cutover.md) (when
available) before applying — it carries the exact lock estimate against
the current prod row count.

**Operator action**: stop `polymarket_observer` and `polymarket_engine`
before applying (see Section 4). The legacy table is preserved as
`trades_observed_legacy` for 7 days. Drop it only after the soak window
passes.

**Verify**:
```sql
SELECT relname, relkind FROM pg_class WHERE relname = 'trades_observed';
-- expect relkind = 'p' (partitioned table), not 'r' (regular)

SELECT inhparent::regclass, inhrelid::regclass
FROM pg_inherits WHERE inhparent = 'trades_observed'::regclass
ORDER BY inhrelid::regclass::text;
-- expect 3+ monthly partitions
```

**Rollback**: `psql $DATABASE_URL -f docs/migrations/013_trades_observed_partition_DOWN.sql`
THEN `DELETE FROM schema_migrations WHERE version = 13;` Only safe while
`trades_observed_legacy` still exists.

### 3.6 — `014_partial_indexes.sql` — Phase 2B

<!-- TODO: confirm against Phase 2 outputs -->

Adds partial indexes (`paper_trades(opened_at) WHERE
economic_model_version='v1.0.0' AND invalidated_at IS NULL`;
`decision_log` equivalent; `follower_edges` high-probability filter).

**APPLY THIS VIA `psql -f`, NOT `setup_db.py`.** The migration uses
`CREATE INDEX CONCURRENTLY`, which Postgres forbids inside a transaction
block, and `setup_db.py` wraps every file in an implicit transaction.

```bash
ssh hetzner-polymarket
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  < /opt/polymarket-bot/docs/migrations/014_partial_indexes.sql
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  -c "INSERT INTO schema_migrations (version, applied_at) VALUES (14, NOW()) ON CONFLICT DO NOTHING;"
```

**Verify**: <!-- TODO: confirm against Phase 2 outputs --> see
[`phase2/B_partial_indexes.md`](B_partial_indexes.md).

**Rollback**: `DROP INDEX CONCURRENTLY IF EXISTS <name>;` per index.
Safe, no data risk.

### 3.7 — `015_position_tracker_state.sql` — Phase 2C

Adds new `position_tracker_state` table (persistent shadow of the
in-memory `_open_positions` dict). Safe, additive. See
[`phase2/C_position_tracker_state.md`](C_position_tracker_state.md).

**Verify**:
```sql
\d position_tracker_state
-- expect PRIMARY KEY (wallet_address, market_id, token_id, direction, open_time)
```

**Rollback**: `DROP TABLE position_tracker_state;` Safe — table is empty
until engine first calls `warm_start`/`upsert`.

---

## 4. Deploy procedure

Use the canonical rsync invocation from
[DEPLOY.md §Étape 2](../../DEPLOY.md#étape-2--sync-vers-la-vm). Then:

```bash
# 4.1 — Mac: rsync (see DEPLOY.md for the full --exclude list)
rsync -avz --delete --exclude '.git/' --exclude '.env' [...full list from DEPLOY.md...] \
  -e "ssh -i ~/.ssh/hetzner_polymarket" \
  ./ polymarket@89.167.23.215:/opt/polymarket-bot/

# 4.2 — VM: stop app containers (db + redis stay up for migrations)
ssh hetzner-polymarket
cd /opt/polymarket-bot
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE stop polymarket_engine polymarket_observer polymarket_api polymarket_registry

# 4.3 — Rebuild shared image (installs prometheus-client 0.20.0)
$COMPOSE build

# 4.4 — Apply migrations 009, 010, 011, 012 (and 013, 015 if Phase 2 landed)
#       via setup_db.py — wraps every file in one transaction
$COMPOSE run --rm polymarket_engine python scripts/setup_db.py
# Expect "applied 011_..." / "applied 012_..." log lines. Migration 013
# rolls back atomically if it fails mid-INSERT; trades_observed stays intact.

# 4.5 — Apply 014 manually (CONCURRENTLY forbidden inside a transaction)
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  < /opt/polymarket-bot/docs/migrations/014_partial_indexes.sql
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  -c "INSERT INTO schema_migrations (version, applied_at) VALUES (14, NOW()) ON CONFLICT DO NOTHING;"

# 4.6 — Start engine FIRST so Phase 2C warm_start runs before observers write
$COMPOSE up -d polymarket_engine
$COMPOSE logs --tail=100 -f polymarket_engine
# Watch ~60s. Confirm:
#   - "warm_start loaded N positions from position_tracker_state" (if Phase 2C landed)
#   - no transaction-error tracebacks
# Ctrl-C to detach.

# 4.7 — Start the rest
$COMPOSE up -d polymarket_observer polymarket_registry polymarket_api
docker ps --format "table {{.Names}}\t{{.Status}}"

# 4.8 — 2-minute soak, confirm ingestion
sleep 120
curl -s http://localhost:8080/metrics | grep -E '^polybot_trades_ingested_total' | head
# Counter must be > 0 and growing.
```

APScheduler jobs (killswitch sync, watchdog, refresh_thresholds, nightly
batch) restart with the engine container automatically.

---

## 5. Validation (10 min post-deploy)

The Prometheus contract is documented in
[phase1/M_metrics_foundation.md](../phase1/M_metrics_foundation.md). Run
each command from the VM:

```bash
# 5.1 — Headline metric: trade-to-react latency (Phase 1 Task O)
curl -s http://localhost:8080/metrics | grep '^polybot_trade_ingestion_latency_seconds'
# p50 target: 2–3s (was ~16s). p99: 5–7s (was ~32s). Compute via PromQL when Grafana lands.

# 5.2 — Falcon concurrency utilisation (Phase 1 Task F)
curl -s http://localhost:8080/metrics | grep '^polybot_falcon_concurrency'
# Values cycling 0..8. Always 1 = semaphore-bump didn't land.

# 5.3 — Observer queue health (Phase 1 Task O)
curl -s http://localhost:8080/metrics | grep -E '^polybot_observer_queue_(depth|drops_total)'
# depth ~ 0 in steady state; drops_total = 0 (non-zero = raise TRADE_OBSERVER_QUEUE_MAX).

# 5.4 — Killswitch strict-path (Phase 0 Task B)
curl -s http://localhost:8080/metrics | grep '^polybot_killswitch_strict_path_total'
# Increments only on live trades. Zero on a pure-paper deploy is expected.

# 5.5 — DB write batching (Phase 1 Task O)
curl -s http://localhost:8080/metrics | grep '^polybot_db_write_batch_size'
# Histogram mass should be in buckets >= 5. All in bucket 1 = batching broken.

# 5.6 — Liquidity score source switch (Phase 0 Task C, eventual)
docker exec -i polymarket_db psql -U polymarket -d polymarket -c "
  SELECT liquidity_score_source, COUNT(*) FROM markets WHERE active=true GROUP BY liquidity_score_source;"
# Within 30 min, falcon_575 should dominate. See C_liquidity.md §5 if not.

# 5.7 — Dashboard sanity
curl -s http://localhost:8080/healthz
curl -s http://localhost:8080/api/inspector/snapshot | python3 -m json.tool | head -30

# 5.8 — Phase 2C warm-start (if Phase 2C landed)
docker exec -i polymarket_db psql -U polymarket -d polymarket -c \
  "SELECT COUNT(*) FROM position_tracker_state WHERE shares_remaining > 0;"
# Cross-check against `docker logs polymarket_engine | grep warm_start`.
```

---

## 6. Retention rollout (separate, opt-in)

Phase 0 Task D adds retention but defaults to OFF. Roll it out *after*
the Phase 0+1+2 deploy is steady. See
[phase0/D_retention.md](../phase0/D_retention.md).

```bash
# 6.1 — Dry-run (no DELETE issued, gate bypassed)
ssh hetzner-polymarket
cd /opt/polymarket-bot
$COMPOSE run --rm polymarket_engine python scripts/batch_runner.py --dry-run
# "retention[<table>]: dry-run — would delete N rows ..."

# 6.2 — Review counts. book_quality_snapshots will dominate. Do NOT enable
#       if counts look insane.

# 6.3 — Enable
nano .env   # set RETENTION_ENABLED=true
$COMPOSE restart polymarket_engine

# 6.4 — Verify next morning (nightly batch runs at BATCH_HOUR_UTC=03:00 UTC)
$COMPOSE logs polymarket_engine --since 8h | grep 'retention\['
# Counts should match the dry-run within ~10%.
```

---

## 7. Rollback

### Phase 1 (perf + observability)

Pure code changes, no schema. Roll back with `git revert 3756040` then
re-rsync + rebuild + recreate (see [DEPLOY.md §Rollback option A](../../DEPLOY.md#option-a--revert-le-commit--redeploy)).
No data risk.

### Phase 0 migrations 011 / 012

Additive (columns + indexes). Roll back:
```sql
-- Migration 011
DROP INDEX IF EXISTS idx_live_orders_placed_at;
DROP INDEX IF EXISTS idx_signal_audits_created_at;
DROP INDEX IF EXISTS idx_fee_snapshots_captured_at;

-- Migration 012
ALTER TABLE markets
  DROP COLUMN IF EXISTS liquidity_score_updated_at,
  DROP COLUMN IF EXISTS liquidity_score_source;
DROP INDEX IF EXISTS idx_markets_liq_updated_at;

-- Update bookkeeping
DELETE FROM schema_migrations WHERE version IN (11, 12);
```

### Phase 2 migration 013 (THE DANGEROUS ONE)

<!-- TODO: confirm against Phase 2 outputs -->

`trades_observed` was rebuilt. Roll back ONLY while
`trades_observed_legacy` still exists (operator hasn't run `DROP TABLE
trades_observed_legacy`):

```bash
$COMPOSE stop polymarket_observer
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  < /opt/polymarket-bot/docs/migrations/013_trades_observed_partition_DOWN.sql
docker exec -i polymarket_db psql -U polymarket -d polymarket \
  -c "DELETE FROM schema_migrations WHERE version = 13;"
$COMPOSE start polymarket_observer
```

If `trades_observed_legacy` is already dropped, the only recovery is
restoring from the R2 pg_dump (Section 0.4).

### Phase 2 migrations 014 / 015

014: `DROP INDEX CONCURRENTLY IF EXISTS <name>;` per index. 015: `DROP
TABLE position_tracker_state;` — engine resumes with empty
`_open_positions` (next open will repopulate). Both safe.

### Emergency

Killswitch flip stops all new positions without code change:
```bash
curl -X POST http://localhost:8080/api/control/killswitch \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false, "reason": "deploy-incident", "actor": "ops"}'
```
See [DEPLOY.md §Option C](../../DEPLOY.md#option-c--killswitch-immédiat).

---

## 8. Known issues / things this runbook does NOT cover

- **Phase 2 Task D — dedicated Redis subscribers**. Refactors the 8
  subscriber sites (engine-container + API ws_bridge + telegram notifier)
  off the shared command client onto a new `Subscriber` utility with
  auto-reconnect + auto-resubscribe. If subscribers misbehave post-deploy,
  watch `polybot_redis_subscriber_reconnects_total{subscriber,reason}`.
  Non-zero on startup is normal (initial connect); steadily climbing is
  a bug. See [phase2/D_redis_pubsub.md](D_redis_pubsub.md).

- **Phase 3 work is NOT in this deploy.** The audit master report
  (Section 3, Phase 3) describes CDC out of `trades_observed`, the
  point-in-time feature store fix for `error_model._fetch_training_data`
  leakage, the bivariate Hawkes upgrade, the resolution-reconciliation
  job, the per-wallet authenticated CLOB WS user-channel, the LightGBM
  class-imbalance fix, and OpenTelemetry spans. None of these ship now.
  Don't expect the dashboard to show bivariate Hawkes outputs after this
  deploy.

- **Live trading is still gated.** `LIVE_TRADING_DRY_RUN=true` and
  `PAPER_TRADING=true` defaults in `.env` keep real money out of the
  flow. Phase 0 Task B closed the killswitch leak so flipping to live is
  safer, but the F-19 orphan-`live_trades` reconciliation is Phase 2
  Task <!-- TODO: confirm against Phase 2 outputs --> — confirm that
  landed before any live trading is enabled.

- **Hetzner cron + Grafana**. No host-level systemd timer; APScheduler
  in `polymarket_engine` is the only scheduler. Phase 1 exposes
  `/metrics` (names stable per
  [phase1/M_metrics_foundation.md](../phase1/M_metrics_foundation.md))
  but there is no Grafana instance yet — point any Prom server at
  `http://89.167.23.215:8080/metrics`. Phase 2 will add bearer-token
  auth before exposing beyond LAN.

- **`.env.example` does not list Phase 1 keys.** All 7 Phase 1 env
  variables (`TRADE_OBSERVER_*` × 4, `FALCON_MAX_CONCURRENCY`,
  `REGISTRY_BACKFILL_CONCURRENCY`, `FALCON_MAX_REQUESTS_PER_MINUTE`) are
  declared in `src/config.py` with sensible defaults but are not echoed
  in `.env.example`. File a docs follow-up.

---

*End of runbook.*
