# Phase 2 / Task A — `trades_observed` partition cutover

**Audit traceability**: M11 in `docs/audit/03_schema_evolution.md` (architect's
#1 ROI move). Replaces the nightly `DELETE FROM trades_observed WHERE time <
cutoff` with `DROP PARTITION`, eliminates vacuum churn at 10x scale, and lets
every `WHERE time > NOW() - X` dashboard query skip cold partitions entirely.

**Migration files**:
- UP: `docs/migrations/013_trades_observed_partition.sql`
- DOWN: `docs/migrations/013_trades_observed_partition_DOWN.sql` (manual)
- Maintenance: `scripts/maintenance/create_trades_partitions.py`
- Retention adapter: `scripts/batch_runner.py::step_cleanup_old_trades`

**Coordination with other Phase 2 tasks**: this migration claims version
**013**. Tasks B (partial indexes), C (PositionTracker state), D (Redis
pubsub) use **014+**.

---

## 1. Estimated downtime

The migration runs as a single transaction (the `setup_db.py` runner wraps
each file in one `conn.execute`). The dominant cost is the
`INSERT INTO trades_observed_new SELECT * FROM trades_observed` data copy.

| trades_observed rows | Expected lock window |
|---------------------:|----------------------|
|             ~100,000 | < 5 s                |
|           ~1,000,000 | 10 – 30 s            |
|          ~10,000,000 | 1 – 3 min            |
|         ~100,000,000 | 15 – 30 min — split off-hours |

Lock type during the copy: `AccessShareLock` on the source (live reads OK,
no writes block). Lock type during the rename swap at the end:
`AccessExclusiveLock` (milliseconds).

Net writer downtime: copy duration + a few hundred ms for the swap. The
trade observer's `_db_writer_loop` will retry on its internal queue; you
will see at most one round of `database is starting up` warnings in its
log and no data loss (Redis dedup TTL is 7 days).

---

## 2. Pre-flight (30 minutes before cutover)

1. **Verify table size and current row count**:
   ```sql
   SELECT
       pg_size_pretty(pg_total_relation_size('trades_observed')) AS total,
       (SELECT COUNT(*) FROM trades_observed) AS rows;
   ```
   Note both numbers — you'll compare after the swap.

2. **Verify the Phase 0 DELETE-based retention is OFF for trades_observed**:
   `step_cleanup_old_trades` runs unconditionally today, but the wider
   `step_apply_retention_policies` sweep is gated by `RETENTION_ENABLED`.
   For the cutover window, set:
   ```bash
   RETENTION_ENABLED=false  # in /opt/polymarket-bot/.env on prod
   ```
   This prevents the nightly batch from clobbering rows while we cut over.
   Restore to your previous value (or `true`) after the soak.

3. **Take a fresh backup** via `scripts/backup_db.py`. Verify it landed in
   R2 before proceeding. The cutover is reversible (see §6), but a
   pg_dump is your last line of defense if the soak window expires and
   you've already dropped `trades_observed_legacy`.

4. **Stop optional writers** — recommended but not strictly required:
   ```bash
   docker compose stop observer
   ```
   The CLOB WebSocket will reconnect cleanly when observer comes back;
   trade dedup is Redis-side with 7-day TTL.

---

## 3. Apply migration 013

Two paths — pick one.

### 3a. Recommended: via the standard migration runner

```bash
cd /opt/polymarket-bot
python scripts/setup_db.py
```

This will pick up 013 automatically and apply it as one transaction.
Expected log line: `Applying 013_trades_observed_partition.sql (v13)...`.

### 3b. Manual: via psql (large DBs, for visibility into the copy)

```bash
psql "$DATABASE_URL" -f docs/migrations/013_trades_observed_partition.sql
psql "$DATABASE_URL" -c \
  "INSERT INTO schema_migrations (version) VALUES (13) ON CONFLICT DO NOTHING;"
```

You can `\set VERBOSITY verbose` and `\timing on` first to see per-statement
durations.

---

## 4. Verification

Run all of these. Each is independent.

### 4a. Row-count parity

```sql
SELECT
    (SELECT COUNT(*) FROM trades_observed)        AS partitioned,
    (SELECT COUNT(*) FROM trades_observed_legacy) AS legacy;
```

The two columns must be **identical**. If they differ, jump to §6 rollback.

### 4b. Partition inventory

```sql
SELECT relname, n_live_tup
FROM pg_stat_user_tables
WHERE relname LIKE 'trades_observed%'
ORDER BY relname;
```

You should see:
- `trades_observed` — partitioned parent (n_live_tup is approximate, may be 0)
- `trades_observed_legacy` — the original heap, n_live_tup = old row count
- `trades_observed_default` — should be 0 rows
- `trades_observed_YYYYMM` × 13 — one per covered month

### 4c. Indexes propagated to each partition

```sql
SELECT
    c.relname AS partition,
    COUNT(*)  AS idx_count
FROM pg_inherits i
JOIN pg_class p ON p.oid = i.inhparent AND p.relname = 'trades_observed'
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_index x ON x.indrelid = c.oid
GROUP BY c.relname
ORDER BY c.relname;
```

Every partition (including `_default`) should report **7 indexes**
(the original count from migrations 001 + 002 + 007 + 009).

### 4d. Application smoke test

Restart the observer and watch for a clean insert:

```bash
docker compose start observer
docker compose logs -f observer | head -100
```

Expected log lines after a fresh leader trade arrives:
- `Observed trade ...`
- (no `IntegrityError`, no `relation does not exist`)

### 4e. Query plans use partition pruning

```sql
EXPLAIN
SELECT COUNT(*) FROM trades_observed
WHERE time > NOW() - INTERVAL '7 days';
```

Look for `Append` over a small subset of partitions (e.g. just
`trades_observed_202605` if today is mid-May 2026), **not** every monthly
child. If pruning isn't kicking in, check `enable_partition_pruning =
on` in `postgresql.conf` (default on, but worth verifying).

---

## 5. The 7-day soak and final drop

For 7 days, monitor:
- Observer insertion rate (should match pre-cutover baseline within ~5%)
- Dashboard query timings (should drop on time-windowed queries)
- `trades_observed_default` row count (should stay at 0)

After the soak with no regressions:

```sql
DROP TABLE trades_observed_legacy;
```

Approximate space reclaimed: equal to the pre-cutover `pg_total_relation_size`
(the data was copied, not moved).

Also cron the maintenance script so the rolling-forward window stays
populated:

```cron
# /etc/cron.d/polymarket-partitions
30 0 1 * * polymarket cd /opt/polymarket-bot && \
    /opt/polymarket-bot/.venv/bin/python -m \
    scripts.maintenance.create_trades_partitions --months 3 \
    >> /var/log/polymarket/partitions.log 2>&1
```

---

## 6. Rollback

### 6a. During §4 verification (before observer restart)

If row counts mismatch or indexes are missing, apply the DOWN script
immediately:

```bash
psql "$DATABASE_URL" -f docs/migrations/013_trades_observed_partition_DOWN.sql
psql "$DATABASE_URL" -c "DELETE FROM schema_migrations WHERE version = 13;"
```

This renames `trades_observed_legacy` back to `trades_observed`. The
partitioned table is preserved as `trades_observed_partitioned` so you
can manually forward-port any rows it received in the meantime (see the
DOWN script's footer for the SQL).

### 6b. After the observer has restarted but before the §5 DROP

Same procedure as 6a — but BEFORE running the rename, manually
forward-port the rows the partitioned table received from the observer
during the cutover window:

```sql
-- Read the post-cutover row count
SELECT COUNT(*) FROM trades_observed;  -- partitioned
SELECT COUNT(*) FROM trades_observed_legacy;  -- the original
-- Forward-port deltas (see DOWN script footer)
```

### 6c. After `DROP TABLE trades_observed_legacy`

You are past the soak. The DOWN script is no longer useful — the original
heap is gone. Restore from R2 backup via `scripts/restore_db.py`. Expect
the dump to be the size of the pre-cutover table; restore time is
proportional.

---

## 7. After the cutover — what changed

**Retention path** (`scripts/batch_runner.py::step_cleanup_old_trades`):
- Was: `DELETE FROM trades_observed WHERE time < $1` once a night.
- Now: iterates child partitions, drops those whose upper bound has
  fully aged past `RETENTION_TRADES_DAYS`, and falls back to a bounded
  DELETE only on `trades_observed_default` (which should stay empty).
- The non-partitioned path is preserved for older schema snapshots / CI.

**Trade observer**: unchanged. Multi-row `INSERT INTO trades_observed`
continues to work — PG routes each row to the right partition
transparently.

**Dashboard queries**: unchanged. Partition pruning is automatic on any
`WHERE time {<,>,BETWEEN} …` predicate.

**Future migrations**: tasks B/C/D claim 014+. Phase 2.2 (audit M12) will
swap the BTREE on `(time)` for a per-partition BRIN — that's a separate
migration to land after this one beds in.
