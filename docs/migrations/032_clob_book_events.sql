-- ============================================================================
-- 032_clob_book_events.sql
--
-- Round 11 (The Microscope) / Sub-Trade Order-Flow Intelligence.
--
-- Audit reference: docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md § 4.
--
-- Captures every order-life event from the Polymarket CLOB WebSocket at
-- L3 granularity (placement / modification / cancellation / partial fill
-- / fill). This is the highest-volume table in the system:
--
--   ~5,000 events/sec peak, ~1,000 events/sec sustained
--   1,000/s × 86,400 s = ~86M rows/day
--   ~150 bytes/row → ~13 GB/day raw
--
-- Mitigations:
--   * HOURLY partitions (not daily) — drop-partition retention bounds the
--     hot tier at 30 days = 720 partitions × ~540 MB ≈ 390 GB.
--   * Three targeted indexes: market+token+time DESC (read path), wallet+
--     time DESC partial (per-wallet rollup; mostly NULL — wallet is only
--     populated on fills, see § 3.1 of the spec), and order_hash partial
--     (event linkage for spoof/iceberg detection).
--   * Cold-tier Parquet export (R6 § 3.6) compresses this ~10× for
--     research queries.
--
-- Wallet attribution caveat (§ 3.1 of the spec):
--   * Polymarket WS does NOT include wallet on placement / modification /
--     cancellation events — only on fills. The `wallet_address` column is
--     therefore NULL on 4 of the 5 event types. Downstream features that
--     need wallet attribution either (a) wait for the matching fill, or
--     (b) join with `trades_observed` on (tx_hash, log_index) via the R6
--     on-chain reconciliation path.
--
-- Partition maintenance:
--   * Initial 24 h of partitions are created inline below so the table is
--     immediately writable from R11 daemon boot.
--   * Ongoing rotation is handled by `scripts/maintenance/
--     create_book_events_partitions.py` (companion to the R6 monthly
--     trades_observed roller). It must be cron'd hourly:
--
--       30 * * * * cd /opt/polymarket-bot && \
--         python -m scripts.maintenance.create_book_events_partitions
--
--     The script also DROPs partitions older than CLOB_BOOK_RETENTION_DAYS
--     (default 30) — that's the retention mechanism, not a separate
--     DELETE-by-time job. ALL the heavy lifting is the partition swap.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) Parent partitioned table.
--
--    Composite PK (event_id, event_time) — PG's declarative range
--    partitioning requires the partition key to be part of every unique
--    constraint. event_id stays a per-partition BIGSERIAL; rows are
--    uniquely identified across the table by (event_id, event_time).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clob_book_events (
    event_id        BIGSERIAL,
    event_time      TIMESTAMPTZ NOT NULL,
    market_id       VARCHAR(100) NOT NULL,
    token_id        VARCHAR(100) NOT NULL,
    -- event_type ∈ {'placed','modified','cancelled','partial_fill','filled'}.
    -- VARCHAR(20) chosen to leave a little headroom (e.g. future
    -- 'rejected' or 'expired' classifications).
    event_type      VARCHAR(20) NOT NULL,
    side            VARCHAR(4) NOT NULL,           -- 'buy' | 'sell'
    price           NUMERIC(10, 6),
    -- Signed size delta. For 'placed' events this is the resting size;
    -- for 'cancelled' it's the negative of remaining size; for fills it's
    -- the fill amount; for 'modified' it's the new size minus old size.
    size_delta      NUMERIC(20, 2),
    -- Polymarket's CLOB order hash, when present in the WS payload. NULL
    -- for older synthetic events (e.g. tests, replay) where the order
    -- identity isn't tracked across messages.
    order_hash      VARCHAR(100),
    -- Wallet attribution — NULL except on fills (see § 3.1 caveat above).
    -- The partial index `idx_cbe_wallet_time` skips NULLs.
    wallet_address  VARCHAR(100),
    -- source ∈ {'ws','onchain_reconciled'}. 'ws' = direct from the L3
    -- subscriber. 'onchain_reconciled' = backfilled by the R6 cross-
    -- source reconciler when a fill was observed on-chain but the WS
    -- missed it (gap recovery).
    source          VARCHAR(20) NOT NULL,
    -- Raw WS message JSON for forensic replay. JSONB compresses well
    -- under PG's TOAST and gives the operator the option to grep the
    -- raw event log when something weird happens (spec § 9 #2).
    raw_payload     JSONB,
    PRIMARY KEY (event_id, event_time)
) PARTITION BY RANGE (event_time);

-- ---------------------------------------------------------------------------
-- 2) Indexes on the partitioned parent. PG automatically creates matching
--    indexes on every child partition (existing and future) — operators
--    don't have to re-run this when the partition roller adds a new hour.
--
--    Read path coverage:
--    * idx_cbe_market_time   — feature_store rollup; primary scan.
--    * idx_cbe_wallet_time   — per-wallet signature rollup. PARTIAL on
--                              `wallet_address IS NOT NULL` so the index
--                              only carries the ~20% of fills that have
--                              attribution (4 of 5 event types are NULL).
--    * idx_cbe_order_hash    — order-lifecycle joins (placement→fill
--                              latency for the place-to-fill timing
--                              tracker). PARTIAL on
--                              `order_hash IS NOT NULL` for the same
--                              reason.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_cbe_market_time
    ON clob_book_events (market_id, token_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_cbe_wallet_time
    ON clob_book_events (wallet_address, event_time DESC)
    WHERE wallet_address IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cbe_order_hash
    ON clob_book_events (order_hash)
    WHERE order_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3) Default partition — safety net for events whose timestamp doesn't
--    fall in any explicit hourly partition (e.g. retroactive replay,
--    clock-skew incidents). In steady state stays empty; the partition
--    maintainer monitors its row count.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clob_book_events_default
    PARTITION OF clob_book_events DEFAULT;

-- ---------------------------------------------------------------------------
-- 4) Initial 24 hours of hourly partitions, anchored to 2026-05-12 00:00 UTC
--    (the R11 ship date). Naming convention: clob_book_events_YYYYMMDD_HH.
--
--    Ongoing rotation lives in
--    scripts/maintenance/create_book_events_partitions.py — run hourly via
--    cron. The script:
--      * Ensures the next N hours (default 24) of partitions exist
--        (CREATE TABLE IF NOT EXISTS — idempotent).
--      * DROPs partitions older than CLOB_BOOK_RETENTION_DAYS (default 30).
--      * Reports row counts via polybot_book_partition_rows_total.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS clob_book_events_20260512_00
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 00:00:00+00') TO ('2026-05-12 01:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_01
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 01:00:00+00') TO ('2026-05-12 02:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_02
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 02:00:00+00') TO ('2026-05-12 03:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_03
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 03:00:00+00') TO ('2026-05-12 04:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_04
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 04:00:00+00') TO ('2026-05-12 05:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_05
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 05:00:00+00') TO ('2026-05-12 06:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_06
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 06:00:00+00') TO ('2026-05-12 07:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_07
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 07:00:00+00') TO ('2026-05-12 08:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_08
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 08:00:00+00') TO ('2026-05-12 09:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_09
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 09:00:00+00') TO ('2026-05-12 10:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_10
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 10:00:00+00') TO ('2026-05-12 11:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_11
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 11:00:00+00') TO ('2026-05-12 12:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_12
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 12:00:00+00') TO ('2026-05-12 13:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_13
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 13:00:00+00') TO ('2026-05-12 14:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_14
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 14:00:00+00') TO ('2026-05-12 15:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_15
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 15:00:00+00') TO ('2026-05-12 16:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_16
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 16:00:00+00') TO ('2026-05-12 17:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_17
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 17:00:00+00') TO ('2026-05-12 18:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_18
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 18:00:00+00') TO ('2026-05-12 19:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_19
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 19:00:00+00') TO ('2026-05-12 20:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_20
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 20:00:00+00') TO ('2026-05-12 21:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_21
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 21:00:00+00') TO ('2026-05-12 22:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_22
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 22:00:00+00') TO ('2026-05-12 23:00:00+00');
CREATE TABLE IF NOT EXISTS clob_book_events_20260512_23
    PARTITION OF clob_book_events
    FOR VALUES FROM ('2026-05-12 23:00:00+00') TO ('2026-05-13 00:00:00+00');

COMMIT;
