-- 006_system_control.sql
-- Global killswitch + execution mode flags.
--
-- Singleton row (id=1, enforced via CHECK) holding the live state of the bot's
-- execution permissions. DB is the source of truth; Redis is a cache (TTL 2s).
--
-- Two-level switch:
--   - execution_enabled       : if FALSE, NEITHER paper nor real trades execute.
--   - real_execution_enabled  : if FALSE, only paper executes; if TRUE, both
--                               paper and real execute (paper always shadows).
--
-- An audit table tracks every flip with reason + actor so we know why the bot
-- was paused/resumed.

BEGIN;

CREATE TABLE IF NOT EXISTS system_control (
    id                       SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    execution_enabled        BOOLEAN     NOT NULL DEFAULT TRUE,
    real_execution_enabled   BOOLEAN     NOT NULL DEFAULT FALSE,
    paused_reason            TEXT,
    updated_by               TEXT        NOT NULL DEFAULT 'system',
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the singleton row if it doesn't exist.
INSERT INTO system_control (id, execution_enabled, real_execution_enabled, updated_by)
VALUES (1, TRUE, FALSE, 'migration_006')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS system_control_audit (
    id                          BIGSERIAL PRIMARY KEY,
    field_changed               TEXT        NOT NULL,
    old_value                   TEXT,
    new_value                   TEXT        NOT NULL,
    reason                      TEXT,
    changed_by                  TEXT        NOT NULL DEFAULT 'system',
    changed_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS system_control_audit_recent_idx
    ON system_control_audit (changed_at DESC);

COMMIT;
