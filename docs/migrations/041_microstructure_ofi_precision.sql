-- ============================================================================
-- 041_microstructure_ofi_precision.sql
--
-- Round 11 / Microstructure — Post-Sprint 4 regression fix.
--
-- The Sprint 4 R11 decoder fan-out (commit dc93bdd) shipped the rollup
-- pipeline end-to-end, but the rollup writer started hitting
--
--     numeric field overflow
--     DETAIL: A field with precision 10, scale 4 must round to an absolute
--     value less than 10^6.
--
-- on the very first bucket and on every subsequent bucket (logged in
-- the microstructure daemon as "MicrostructureRollup flush failed").
-- Migration 033 declared the OFI columns as NUMERIC(10, 4) — max
-- value 999,999.9999. In practice Polymarket book ladders carry
-- resting sizes that routinely exceed this (a single $1M position
-- adjustment overshoots; even a calm market has ofi_max in the
-- millions over a 1-min bucket of accumulated deltas).
--
-- We bump the precision to NUMERIC(20, 6) which gives us 10^14
-- absolute headroom and 6 decimal digits — sufficient for both the
-- accumulated USDC totals and the floating-point std-dev tail.
-- Iceberg / spoof totals were already NUMERIC(20, 2) so they are
-- unaffected.
--
-- Idempotent: the ALTER TYPE is a no-op if the column already
-- matches. PostgreSQL accepts column type widening on a non-empty
-- table without a full rewrite when the new type is a strict
-- superset of the old.
-- ============================================================================

BEGIN;

ALTER TABLE microstructure_features
    ALTER COLUMN ofi_mean TYPE NUMERIC(20, 6),
    ALTER COLUMN ofi_max  TYPE NUMERIC(20, 6),
    ALTER COLUMN ofi_min  TYPE NUMERIC(20, 6),
    ALTER COLUMN ofi_std  TYPE NUMERIC(20, 6);

COMMIT;
