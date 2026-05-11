-- Phase 3 Round 3: enforce uniqueness on `book_quality_snapshots`.
--
-- Background: Agent Z (Round 2 OB-imbalance pipeline) surfaced that
-- `book_quality_snapshots` has no UNIQUE constraint. Under WebSocket
-- retry or backfill-on-reconnect, the same book update can be written
-- twice, which subtly inflates per-minute rollups in
-- `orderbook_features_minute`.
--
-- Fix: partial UNIQUE index on (market_id, token_id, source_timestamp)
-- WHERE source_timestamp IS NOT NULL. The partial-WHERE clause:
--   * Catches the WS-retry duplicate class (same upstream `t`, written
--     twice by us with different `observed_at`).
--   * Doesn't constrain rows where the source didn't expose a
--     timestamp (legacy / degraded mode) — we accept potential dupes
--     there in exchange for not failing inserts.
--
-- Idempotency: `CREATE INDEX CONCURRENTLY` cannot run inside an
-- explicit transaction. The project's setup_db.py wraps each migration
-- in a tx; for this migration the operator should run with `psql -f`
-- (same convention as migration 014).
--
-- Dedup pass first — INSERT-ON-CONFLICT can't deduplicate retroactively.
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY market_id, token_id, source_timestamp
            ORDER BY observed_at, id
        ) AS rn
    FROM book_quality_snapshots
    WHERE source_timestamp IS NOT NULL
)
DELETE FROM book_quality_snapshots
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS uq_book_quality_snapshots_source_ts
    ON book_quality_snapshots (market_id, token_id, source_timestamp)
    WHERE source_timestamp IS NOT NULL;

COMMENT ON INDEX uq_book_quality_snapshots_source_ts IS
    'Round 3: dedupe WS-retry duplicate book snapshots. Partial — only '
    'enforced where source_timestamp is populated, so legacy rows still '
    'insert without raising UniqueViolation. See migration 019 header.';
