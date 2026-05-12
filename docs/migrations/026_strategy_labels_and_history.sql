-- ============================================================================
-- 026_strategy_labels_and_history.sql
--
-- Round 8 (The Lens) — Per-Leader Strategy Fingerprinting.
--
-- Audit reference: docs/ROUND_8_STRATEGY_CLASSIFIER.md § 3.2 (hand-label
-- store) + § 3.3 (classifier output history) + § 4 (migration sequence).
--
-- Two append-only tables:
--
--   strategy_labels         — operator-curated ground truth. Per
--                             (wallet, 30-day-window) tuple, NOT per wallet.
--                             A single trader may switch strategies across
--                             windows. Inserts only; updates done by
--                             inserting a newer row with a more recent
--                             labelled_at timestamp.
--
--   leader_strategy_history — classifier outputs. Every daemon run inserts
--                             a row per leader; downstream code reads the
--                             latest (wallet ORDER BY classified_at DESC
--                             LIMIT 1). Drift detection joins back 30 days
--                             on this table.
--
-- Both tables use the same 9-class CHECK constraint so the taxonomy
-- can only drift in one direction (additive — add a class, run the
-- migration on both tables in lock-step).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Hand-label store (operator-curated ground truth, § 3.2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_labels (
    label_id            BIGSERIAL    PRIMARY KEY,
    wallet_address      VARCHAR(100) NOT NULL,
    -- 30-day window the label applies to. A wallet may have multiple
    -- non-overlapping label windows (caught switching strategies).
    label_window_start  DATE         NOT NULL,
    label_window_end    DATE         NOT NULL,
    primary_strategy    VARCHAR(20)  NOT NULL,
    -- Optional second-best label for mixed-strategy wallets (a wallet
    -- doing 70 % directional + 30 % info_leak gets both).
    secondary_strategy  VARCHAR(20),
    -- Labeller's self-reported 0-1 confidence. Used as a sample-weight
    -- knob when training (low-confidence rows down-weighted).
    confidence          NUMERIC(5,4) NOT NULL DEFAULT 1.0,
    -- Labeller's identifier — typically an operator handle, but for
    -- inter-labeller-κ measurement we need to know which person
    -- assigned each label.
    labeller            VARCHAR(50)  NOT NULL,
    -- Full ISO-8601 timestamp so we can compute κ over time-windowed
    -- subsets of the label set.
    labelled_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Free-form rationale ("> 6h holding period + low cancel-to-fill").
    -- Critical for auditing taxonomy fuzziness and refining the protocol.
    rationale           TEXT,
    CONSTRAINT chk_primary_strategy CHECK (
        primary_strategy IN (
            'directional', 'momentum', 'contrarian',
            'arb_2way', 'arb_3way', 'market_maker',
            'structural_bot', 'info_leak', 'social_driven'
        )
    ),
    CONSTRAINT chk_secondary_strategy CHECK (
        secondary_strategy IS NULL OR secondary_strategy IN (
            'directional', 'momentum', 'contrarian',
            'arb_2way', 'arb_3way', 'market_maker',
            'structural_bot', 'info_leak', 'social_driven'
        )
    ),
    CONSTRAINT chk_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT chk_window_order CHECK (label_window_end >= label_window_start)
);

-- Hot path: "give me the latest label for this wallet" — most recent
-- labelled_at wins, descend index for index-only LIMIT 1.
CREATE INDEX IF NOT EXISTS idx_strategy_labels_wallet
    ON strategy_labels (wallet_address, labelled_at DESC);

-- Cohen's κ computation: pull every labeller's set independently. The
-- κ query scans (labeller, wallet, labelled_at) so this index keeps
-- the per-labeller pull O(rows-per-labeller) not O(table).
CREATE INDEX IF NOT EXISTS idx_strategy_labels_labeller
    ON strategy_labels (labeller, wallet_address, labelled_at DESC);

-- Training-set assembly: stratified sampling by strategy class.
CREATE INDEX IF NOT EXISTS idx_strategy_labels_strategy
    ON strategy_labels (primary_strategy);


-- ---------------------------------------------------------------------------
-- Classifier output history (append-only, § 3.3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leader_strategy_history (
    history_id          BIGSERIAL    PRIMARY KEY,
    wallet_address      VARCHAR(100) NOT NULL,
    classified_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    primary_strategy    VARCHAR(20)  NOT NULL,
    -- Confidence in the primary class — the max of strategy_probs.
    confidence          NUMERIC(5,4) NOT NULL,
    -- Full 9-vector { class -> probability }. JSON-stored so future
    -- taxonomy growth doesn't require a column migration.
    strategy_probs      JSONB        NOT NULL,
    -- Model fingerprint for reproducibility — "sc.v1.0", "sc.v1.1", etc.
    model_version       VARCHAR(50)  NOT NULL,
    -- The asof_ts used for feature extraction. Critical for backtests:
    -- the classifier output for "wallet X on 2026-05-01" must be
    -- reproducible by re-running the feature extractor with that asof.
    asof_ts             TIMESTAMPTZ  NOT NULL,
    -- Drift-detector inputs. JS divergence vs 30-day rolling baseline;
    -- NULL when the wallet doesn't yet have 30 days of classified
    -- history.
    drift_js_divergence NUMERIC(8,6),
    drift_detected      BOOLEAN      NOT NULL DEFAULT FALSE,
    CONSTRAINT chk_history_primary_strategy CHECK (
        primary_strategy IN (
            'directional', 'momentum', 'contrarian',
            'arb_2way', 'arb_3way', 'market_maker',
            'structural_bot', 'info_leak', 'social_driven'
        )
    ),
    CONSTRAINT chk_history_confidence CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

-- Hot path: confidence_engine reads "latest classification for wallet".
CREATE INDEX IF NOT EXISTS idx_leader_strategy_history_wallet
    ON leader_strategy_history (wallet_address, classified_at DESC);

-- Drift detector range scan: "give me the last 30 days for this wallet".
CREATE INDEX IF NOT EXISTS idx_leader_strategy_history_wallet_time
    ON leader_strategy_history (wallet_address, classified_at);

-- Strategy-mix dashboards: "what's the distribution today by primary class?".
CREATE INDEX IF NOT EXISTS idx_leader_strategy_history_strategy_time
    ON leader_strategy_history (primary_strategy, classified_at DESC);

COMMIT;

-- ============================================================================
-- POST-MIGRATION NOTES (operator):
--
--   1. Both tables start empty. The hand-labelling sprint (Phase 8.A in
--      the spec) is the first writer to `strategy_labels`; the daemon
--      `python -m src.strategy_classifier` is the first writer to
--      `leader_strategy_history`. Neither path runs automatically as
--      part of an existing batch job until the operator flips
--      `strategy_conditional_confidence_enabled=true` in runtime_config.
--
--   2. Retention: INTENTIONALLY UNBOUNDED. The labelled set is the
--      single most valuable artifact the project produces (spec § 9.1).
--      The history table is small (~2000 wallets × 1 row/day × 365 days
--      = 730k rows, ~80 MB). Storage is cheap.
--
--   3. To add a new strategy class to the taxonomy (e.g., "shill"),
--      you must DROP and re-CREATE both CHECK constraints in lock-step.
--      Do that in a new migration (`030_strategy_taxonomy_extend.sql`)
--      so it's atomic. The classifier model itself has to be retrained
--      from scratch — the LightGBM head shape changes.
-- ============================================================================
