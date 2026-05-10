-- 010_risk_config_history.sql
--
-- Audit log for runtime risk config changes.
-- Each row records a single key transition (old → new), the actor that
-- triggered it, and the source of the change (dashboard, api, cli).
-- Powers the "Audit log" panel in the Risk & Config cockpit.

BEGIN;

CREATE TABLE IF NOT EXISTS risk_config_history (
    id          BIGSERIAL PRIMARY KEY,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    key         VARCHAR(80) NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    actor       VARCHAR(100),
    source      VARCHAR(40)
);

CREATE INDEX IF NOT EXISTS idx_risk_history_time
    ON risk_config_history (changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_risk_history_key
    ON risk_config_history (key, changed_at DESC);

COMMIT;
