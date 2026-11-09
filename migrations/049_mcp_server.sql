-- 049_mcp_server.sql
-- Remote MCP server (streamable HTTP) for Sietch CRM.
--
-- Additive only. The MCP transport itself is /mcp in server.py; these tables
-- hold the per-token tool allowlist, the audit log and the human-in-the-loop
-- proposal queue (which doubles as the JSONL training-data source).
--
-- Tokens are NEVER stored here: audit/proposal rows carry the api_tokens id
-- and name only.

CREATE TABLE IF NOT EXISTS mcp_token_tools (
    token_id    INTEGER NOT NULL REFERENCES api_tokens(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (token_id, tool_name)
);

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    token_id     INTEGER,
    token_name   TEXT,
    tool         TEXT NOT NULL,
    mode         TEXT,
    args_summary TEXT,
    result       TEXT,
    status       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mcp_audit_created ON mcp_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_token ON mcp_audit_log (token_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mcp_pending_actions (
    id             BIGSERIAL PRIMARY KEY,
    token_id       INTEGER,
    token_name     TEXT,
    user_id        INTEGER,
    tool           TEXT NOT NULL,
    args           JSONB NOT NULL DEFAULT '{}'::jsonb,
    method         TEXT,
    path           TEXT,
    request_body   JSONB,
    restrictions   JSONB,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at     TIMESTAMPTZ,
    decided_by     INTEGER,
    decision_note  TEXT,
    result         JSONB
);

CREATE INDEX IF NOT EXISTS idx_mcp_pending_status ON mcp_pending_actions (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_pending_token ON mcp_pending_actions (token_id, created_at DESC);
