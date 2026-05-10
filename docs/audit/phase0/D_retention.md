# Phase 0 Task D — Retention Policies for Unbounded Tables

**Audit refs**: R-6 in `docs/audit/01_data_inventory.md`, M11 (note on
non-CONCURRENTLY index builds) in `docs/audit/03_schema_evolution.md`.

## Scope

Before this task, only `trades_observed` had a cleanup job
(`step_cleanup_old_trades`, 90 d). The audit flagged 9 unbounded tables. This
PR adds **defensive, opt-in** retention for all 9 — plus the time-column
indexes needed to make the DELETE efficient — without touching any table
schema. Schema evolution (partitioning of the highest-growth tables) is
explicitly deferred to Phase 2.

## Per-table policy

| Table | Time column | Default (days) | Volume estimate | Rationale |
|---|---|---|---|---|
| `decision_log` | `time` | 90 | ~1–5 k rows/day | Hot dashboard window is days; 90 d matches `trades_observed`. |
| `book_quality_snapshots` | `observed_at` | 30 | 10–100 k rows/day (highest growth) | Audit calls this the "highest growth rate table". Short retention to bound disk. |
| `portfolio_equity` | `time` | 180 | ~1 440 rows/day | Equity curve users want quarterly history; 1 440 × 180 ≈ 260 k rows is trivial. |
| `decision_state_transitions` | `created_at` | 90 | unknown / market | Per-market state changes; 90 d matches `decision_log`. |
| `live_orders` | `placed_at` | 180 | 0 rows today | Once live trading flips on, audit value is forensic — keep 6 mo. |
| `signal_audits` | `created_at` | 90 | 0 rows (dormant) | Index in place pre-emptively; never written today (audit A.12). |
| `fee_snapshots` | `captured_at` | 90 | 0 rows (dormant) | Index in place pre-emptively; never written today (audit A.11). |
| `system_control_audit` | `changed_at` | 365 | 1 row per killswitch flip | Operational forensics — annual horizon. |
| `risk_config_history` | `changed_at` | 365 | 1 row per dashboard mutation | Same as above. |

All defaults are overridable per-table via `RETENTION_<TABLE>_DAYS` env vars
(see `.env.example`). The registry lives in `scripts/batch_runner.py` —
`RETENTION_POLICIES`.

## Indexes (migration 011)

`docs/migrations/011_retention_policies.sql` adds **three** new indexes:

| Index | On | Reason |
|---|---|---|
| `idx_live_orders_placed_at` | `live_orders(placed_at)` | No time index existed. |
| `idx_signal_audits_created_at` | `signal_audits(created_at)` | No time index existed (dormant table — pre-emptive). |
| `idx_fee_snapshots_captured_at` | `fee_snapshots(captured_at)` | The UNIQUE composite has `captured_at` as the 3rd column, so a leading-column range scan needs its own index. |

The other six tables already have a suitable time-column index from earlier
migrations (`idx_decisions_time`, `book_quality_snapshots_recent_idx`,
`portfolio_equity_time_idx`, `decision_state_transitions_recent_idx`,
`system_control_audit_recent_idx`, `idx_risk_history_time`).

**Why not `CREATE INDEX CONCURRENTLY`**: `scripts/setup_db.py` wraps each
migration in a single `conn.execute(sql)`, which runs in an implicit
transaction — Postgres forbids CONCURRENTLY there. We use plain
`CREATE INDEX IF NOT EXISTS` inside `BEGIN/COMMIT`, mirroring the explicit
choice in migration 007. The three target tables are empty or near-empty
today, so the non-concurrent build returns in well under a second. For a
future re-build on a multi-million-row table, run CONCURRENTLY manually
outside the migration runner (see comment block in `011_*.sql`).

## Operator runbook

### 1. Apply migration 011

```bash
python scripts/setup_db.py
```

Idempotent — re-running is safe.

### 2. Preview impact with --dry-run (RECOMMENDED before enabling)

```bash
# RETENTION_ENABLED can stay false here; --dry-run bypasses the gate.
python scripts/batch_runner.py --dry-run
```

Reports the row count that WOULD be deleted per table, e.g.:

```
INFO retention[decision_log]: dry-run — would delete 12345 rows older than ... (retention=90d)
INFO retention[book_quality_snapshots]: dry-run — would delete 987654 rows older than ... (retention=30d)
...
```

No DELETE statement is issued in dry-run mode.

### 3. Enable retention

Edit `.env` on the production host:

```bash
RETENTION_ENABLED=true
```

Optionally override defaults:

```bash
RETENTION_BOOK_QUALITY_SNAPSHOTS_DAYS=14   # shorter window if disk is tight
```

Restart the engine container (the in-process scheduler picks up the env on
next boot). Or wait for the next scheduled nightly batch.

### 4. Verify after the first run

Check loguru output for the `retention[<table>]:` lines. Each should report
its deleted-row count and the cutoff timestamp.

### 5. Roll back

To disable: set `RETENTION_ENABLED=false` (or remove the line) and restart.
The migration's indexes are harmless to leave in place — they're small and
do no harm to writes. If you must drop them:

```sql
-- as superuser:
DROP INDEX IF EXISTS idx_live_orders_placed_at;
DROP INDEX IF EXISTS idx_signal_audits_created_at;
DROP INDEX IF EXISTS idx_fee_snapshots_captured_at;
```

There is no migration `011_down.sql` (consistent with current project
convention — no DOWN scripts exist for any migration).

### 6. Safety nets in the sweep

- **OFF by default**: `RETENTION_ENABLED` must be explicitly true.
- **Per-table independence**: a failing policy is logged at ERROR; the rest
  continue. One bad table won't kill the batch.
- **Bounded per-round delete**: `DELETE ... WHERE ctid IN (SELECT ctid ...
  LIMIT 10 000)` keeps each round short and yields back to the event loop
  via `await asyncio.sleep(0)`. Loop terminates naturally when a round
  returns less than `batch_size` rows, and has a hard `max_batches=10 000`
  guard against pathological cases.
- **Garbage env values**: non-integer, zero, or negative
  `RETENTION_<TABLE>_DAYS` values fall back to the default with a WARN log
  rather than nuking everything.

## Deferred tables (not in scope this phase)

- **`positions_reconstructed`** — open positions have `close_time IS NULL`.
  A naive cutoff DELETE on `open_time` would drop in-flight positions and
  break the profiler. Retention here needs lifecycle-aware logic; deferred
  to **Phase 1**.
- **`trades_observed`** — already has the 90-day cleanup; partitioning is
  Phase 2 per audit roadmap §4.1.
- **`market_belief_states`** — `UNIQUE(market_id, strategy_track)` caps
  growth at ≤ a few hundred rows; no retention needed.
- **`data_cache/*.parquet`** — flagged in R-6 of the data inventory but
  this is filesystem state, not DB. Out of scope for a SQL-only PR; track
  separately.

## Files changed

- `docs/migrations/011_retention_policies.sql` (new)
- `scripts/batch_runner.py` (extended with `RETENTION_POLICIES` registry,
  per-policy sweep, `--dry-run` flag)
- `.env.example` (appended `RETENTION_ENABLED` plus per-table overrides)
- `tests/test_scripts/__init__.py` (new package)
- `tests/test_scripts/test_batch_retention.py` (new — 15 unit tests)

## Test summary

```
tests/test_scripts/test_batch_retention.py  15 passed
tests/test_database/                        17 passed (no regression)
```

Tests cover: disabled-by-default no-op, dry-run COUNT-only behaviour,
batched-DELETE loop termination, max-batches safety cap, env-override
parsing including garbage/zero/negative, per-table independence on
exception, CLI flag parsing, and a registry-vs-audit coverage check.
