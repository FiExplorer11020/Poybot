# Phase 2 — Task B: Missing partial indexes & CHECK constraints

Migration: `docs/migrations/014_partial_indexes.sql`.
Audit traceability: `03_schema_evolution.md` §M12 (Section 2.7/2.8/2.12),
§M13 (CHECKs in §2.7/§2.8); `04_perf_hotpaths.md` HP-4 #4 (index audit);
`01_data_inventory.md` Red Flag #1 (signal_audits dormant).

## Summary

| Kind | Count | Tables touched |
|------|-------|----------------|
| Partial indexes (v1-active filter) | 4 | `paper_trades` × 2, `decision_log`, `positions_reconstructed` |
| Pre-emptive index (dormant table) | 1 | `signal_audits.decision_id` |
| Foreign key                       | 1 | `signal_audits.decision_id → decision_log.id` |
| CHECK constraints                 | 7 | `paper_trades` × 3, `positions_reconstructed` × 2, `decision_log` × 2 |

## Per-index rationale

### `idx_paper_trades_v1_active_opened` (partial, `opened_at DESC`)
Backs: `queries.py:990` (open positions feed) and `queries.py:1544` (live portfolio listing) which both `ORDER BY pt.opened_at DESC` under the V1 filter. Estimated gain: today every snapshot tick seq-scans paper_trades; with ~1k rows growing 10-50/day this is a few ms now but linear in row count — the index keeps it constant.

### `idx_paper_trades_v1_active_closed` (partial, `closed_at DESC`, `closed_at IS NOT NULL`)
Backs: `queries.py:1320-1324` "last 20 closed trades" used by `risk_panel`. Partial on `closed_at IS NOT NULL` so the index never carries the (large, growing) open subset. Replaces a filter+sort over the whole table.

### `idx_decision_log_v1_active_time` (partial, `time DESC`)
Backs: `queries.py:1125` (decisions list, `ORDER BY d.time DESC LIMIT $1 OFFSET $2`) — the single hottest decision_log query, fires every snapshot. `decision_log` is the highest-rate engine table (§A.8 audit) so this is the biggest win.

### `idx_positions_reconstructed_v1_active_opened` (partial, `open_time DESC`)
Backs: `queries.py:778-779` (per-wallet "10 latest open positions") via `V1_POSITION_SQL` (only call site of `valid_position_filter`). Small win today, but `positions_reconstructed` is the SoT for resolved PnL — read pattern will grow when the wallet detail page is widened.

### `idx_signal_audits_decision_id` (pre-emptive)
Dormant table per Red Flag #1 — no writer in src/ today. Added now (alongside the FK) so that when `_build_signal_audit()` is wired, lookups by `decision_id` don't seq-scan. Partial `WHERE decision_id IS NOT NULL` keeps it free until rows start arriving.

## CHECK constraints — value lists (verified against src/ writers)

| Table.column | Allowed values | Source path (writer) |
|---|---|---|
| `paper_trades.direction` | `'yes'`, `'no'` | `paper_trader.py:477,481` |
| `paper_trades.status` | `'open'`, `'closed'`, `'expired'`, `'cancelled'` | `paper_trader.py:188,652,864`; last two reserved per CLAUDE.md §6 |
| `paper_trades.strategy` | `'follow'`, `'fade'` | `paper_trader.py:378` |
| `positions_reconstructed.direction` | `'yes'`, `'no'` | `position_tracker.py:365` |
| `positions_reconstructed.close_method` | NULL, `'sell'`, `'merge'`, `'resolution'` | `position_tracker.py:179,238,258,412` |
| `decision_log.action` | `'follow'`, `'fade'`, `'skip'` | `confidence_engine.py:255` + skip path |
| `decision_log.outcome` | NULL, `'win'`, `'loss'` | `paper_trader.py:635` |

All ADD CHECK use `NOT VALID` (no full-table lock on add) followed by `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE — allows concurrent reads/writes). No defensive UPDATE is shipped because every writer was audited for value cleanliness. If VALIDATE raises in prod, fix the data and re-run — that is the desired behaviour.

## Apply procedure (CONCURRENTLY caveat)

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction block. The project's migration runner (`scripts/setup_db.py`) wraps each migration in an implicit asyncpg transaction. Therefore migration 014 must be applied OUT OF BAND with `psql` in autocommit:

```bash
# 1. Apply with psql (autocommit, statement-by-statement)
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f docs/migrations/014_partial_indexes.sql

# 2. Record the migration so setup_db.py skips it on next run
psql "$DATABASE_URL" -c "INSERT INTO schema_migrations (version) VALUES (14)"
```

The header comment in `014_partial_indexes.sql` documents this procedure. All statements use `IF NOT EXISTS` (indexes) or guarded `DO` blocks (constraints), so re-runs after a partial failure are safe.

### Expected wall-clock cost

| Statement | Lock | Estimated cost |
|---|---|---|
| 5 × `CREATE INDEX CONCURRENTLY` | `ROW SHARE` | seconds (paper_trades/decision_log are small today; few k rows) |
| 7 × `ADD CONSTRAINT NOT VALID` | `ACCESS EXCLUSIVE` on metadata | <1 ms each (no table scan) |
| 7 × `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` | one sequential scan per table, allows concurrent DML |
| `ADD CONSTRAINT FK ... NOT VALID` + `VALIDATE` | same | trivial (signal_audits is empty) |

Total: a few seconds at current volume. Replan via `EXPLAIN ANALYZE` after apply to confirm the planner picks the new indexes.

## Skipped (proposed in audit, but justified)

- `live_trades.{economic_model_version, invalidated_at}` partial — `invalidated_at` column does not exist on `live_trades` (per migration 008); no code reads it through a v1 filter.
- `live_trades.opened_at DESC` / `live_trades.tx_hash` — no dashboard reader cites them today (`live_trades` is still in shadow under `LIVE_TRADING_DRY_RUN`); add when the dashboard ships a "recent live trades" panel.
- `live_trades.status` / `live_orders.order_state` CHECKs — deferred per anti-goal: live-trading enum may still churn before flip.
- `trades_observed.side` / `trades_observed.source` CHECK + any new DESC index on it — owned by Phase 2 Task A (migration 013, `trades_observed` partition cutover).
- `follower_edges (follow_probability DESC, co_occurrences DESC) WHERE ...` (§M12 sketch) — no cited query in `src/api/queries.py` references this composite sort. Would be dead weight.
- `decision_log(action)` BRIN (§2.8) — read pattern is `time DESC` first; adding `action` alone is premature.
- `(target_table, target_id)` on `v1_label_invalidations` (§2.13) — table has zero readers in src/ today (audit Red Flag-adjacent). Defer.

## Tests

`tests/test_scripts/test_migration_014.py` asserts:
1. SQL parses (balanced parens, statement terminators, every CREATE/ALTER/DO ends in `;`).
2. Every named index in the migration has a unique name across all 001..014 migrations (collision check by scanning each `*.sql` file).
3. Every named constraint has a unique name across all migrations.
4. The migration file declares the `CONCURRENTLY` apply procedure in its header (the operator must read it before piping through `setup_db.py`).
