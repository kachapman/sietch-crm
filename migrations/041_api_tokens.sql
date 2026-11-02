-- Migration 041: api_tokens table — personal read-only API tokens.
--
-- Lets an admin mint long-lived Bearer tokens for bulk-read jobs. Only a
-- SHA-256 hash is stored (plaintext shown once at creation); scope is
-- 'read' only. Applied automatically at boot by db.py (_run_pending_migrations);
-- IF NOT EXISTS / IF NOT EXISTS index keep it idempotent with the inline
-- DDL in server.py's "Schema migrations" block.

CREATE TABLE IF NOT EXISTS api_tokens (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    token_prefix  TEXT NOT NULL DEFAULT '',
    scope         TEXT NOT NULL DEFAULT 'read',
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_hash
    ON api_tokens(token_hash) WHERE revoked_at IS NULL;
