-- 020_add_document_activity.sql
-- Tracks who opens/edits/saves/renames CRM documents so the new preview pane
-- can show "Last opened by X · 2h ago · 3 editors today" and co-editing
-- sessions are visible. Hook writes from:
--   * GET /api/v2/documents/{id}/editor-config         → action='opened'
--   * POST /api/v2/documents/{id}/callback (status 2/4) → action='saved'
--   * POST /api/v2/documents/{id}/command  (c='meta')   → action='renamed'
-- Open events are deduplicated server-side (skip if same user has an 'opened'
-- row in the last 60s) to avoid spamming the table on every iframe reload
-- from the preview pane.

CREATE TABLE IF NOT EXISTS document_activity (
    id          SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES project_documents(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    meta        JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_activity_doc
    ON document_activity(document_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_activity_user
    ON document_activity(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_activity_action_doc
    ON document_activity(document_id, action, created_at DESC);
