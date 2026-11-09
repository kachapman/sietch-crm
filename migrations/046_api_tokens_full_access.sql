-- Migration 046: full-access API tokens with optional restrictions + write audit.
-- Default scope becomes 'full' for NEW tokens. Existing 'read' tokens keep
-- their stored scope. Optional columns: expires_at, restrictions (JSONB).
-- Applied at boot by db.py; IF NOT EXISTS / ADD COLUMN IF NOT EXISTS keep it
-- idempotent with the inline DDL in server.py.

ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS restrictions JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE api_tokens ALTER COLUMN scope SET DEFAULT 'full';

CREATE TABLE IF NOT EXISTS api_token_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    token_id    INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_token_audit_token_time
    ON api_token_audit_log(token_id, created_at DESC);
