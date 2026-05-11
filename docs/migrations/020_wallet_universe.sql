-- ============================================================================
-- 020_wallet_universe.sql
--
-- Round 6 (The Spine) / Phase 6.D — Universal Wallet Crawler.
--
-- Audit reference: docs/ROUND_6_THE_SPINE.md § 3.4 — every wallet that has
-- ever traded on Polymarket gets a row here, with light-touch metadata and
-- an adaptive depth tier. The on-chain CLOB listener (src/onchain/) and the
-- one-time historical backfill (src/crawler/universe.py::backfill_from_chain)
-- both write to this table via INSERT ... ON CONFLICT DO NOTHING / UPDATE.
--
-- Scale expectation: ~1.5M rows at full backfill. At ~120 bytes per row +
-- 3 B-tree indexes that's ~500 MB on disk — well within range for a single
-- unpartitioned PostgreSQL table on the production CX23 (and trivial post-R2
-- partitioning groundwork if ever needed).
--
-- ----------------------------------------------------------------------------
-- Depth tiers (see src/crawler/depth_tiers.py):
--   0 = FULL      — currently top ~200, full Falcon enrichment daily
--   1 = PERIODIC  — top ~2000 by recent 30d volume, weekly enrichment
--   2 = LIGHT     — everyone else (~1.5M), just on-chain timestamps + sizes
--
-- Promotion/demotion is run nightly by src/crawler/depth_tiers.py and
-- updates `depth_tier` + `last_tier_review`. Default tier on insert is 2
-- (LIGHT) — promotion only happens once observed volume justifies it.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS wallet_universe (
    wallet_address          VARCHAR(100)   PRIMARY KEY,
    first_seen              TIMESTAMPTZ    NOT NULL,
    last_active             TIMESTAMPTZ    NOT NULL,
    total_trades_ever       BIGINT         NOT NULL DEFAULT 0,
    total_volume_usdc_ever  NUMERIC(20, 2) NOT NULL DEFAULT 0,
    -- 0 = FULL, 1 = PERIODIC, 2 = LIGHT (see src/crawler/depth_tiers.py)
    depth_tier              SMALLINT       NOT NULL DEFAULT 2,
    last_tier_review        TIMESTAMPTZ,
    -- Useful for cross-source dedup with on-chain ingestion: the block we
    -- first observed this wallet trading. Nullable because the legacy
    -- REST-poll path doesn't always know it.
    first_seen_block        BIGINT,
    last_active_block       BIGINT
);

-- Hot path: nightly tier-review loop scans wallets by tier (typically
-- WHERE depth_tier = 1 to consider for promotion/demotion).
CREATE INDEX IF NOT EXISTS idx_wu_tier
    ON wallet_universe (depth_tier);

-- Hot path: "who's been active in the last N days?" queries. DESC because
-- the dashboard + classifiers all want the most-recently-active wallets.
CREATE INDEX IF NOT EXISTS idx_wu_last_active
    ON wallet_universe (last_active DESC);

-- Tier 0/1 enrichment loops scan by descending volume to prioritise the
-- highest-leverage wallets first. Partial index keeps the size tiny — at
-- full population only ~2200 wallets sit in tiers 0/1.
CREATE INDEX IF NOT EXISTS idx_wu_active_tier_volume
    ON wallet_universe (depth_tier, total_volume_usdc_ever DESC)
    WHERE depth_tier IN (0, 1);

COMMIT;

-- ============================================================================
-- POST-MIGRATION (OPERATOR STEP, NOT IN THIS FILE):
--
--   1. The table is empty after this migration. The one-time historical
--      backfill is operator-triggered:
--        python -m src.crawler.universe --backfill-from-block <N>
--      Runs against the paid RPC pool (Alchemy/QuickNode) — this is the
--      only time we hit them heavily again. Expect ~6-12h wall time.
--
--   2. Ongoing growth is handled automatically by CLOBChainListener via
--      WalletUniverse.add_wallet_if_new() on every decoded OrderFilled event.
--
--   3. Retention: this table is INTENTIONALLY UNBOUNDED. A wallet that
--      traded once 3 years ago and went dormant still gets a row. Storage
--      is cheap and the row is the FK anchor for any future per-wallet
--      tables. There is no retention policy.
-- ============================================================================
