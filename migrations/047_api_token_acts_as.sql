-- Migration 047: API tokens can act as a chosen user ("Posts as").
--
-- A token normally acts as its creating admin. An admin may instead point a
-- token at another user: the token then authenticates AS that user, so
-- project activity, notes, history_events.created_by, personal_notes.owner
-- and every other created_by/owner column carry that user's id. The token's
-- owner (creator) is still recorded for the audit trail.
--
-- api_token_id is stored on the rows a token creates so the UI can render a
-- small "via API: <token name>" tag. ON DELETE SET NULL keeps the activity
-- (and its author) when a token is revoked/deleted; the tag simply drops.

ALTER TABLE api_tokens
    ADD COLUMN IF NOT EXISTS acts_as_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_api_tokens_acts_as
    ON api_tokens(acts_as_user_id) WHERE acts_as_user_id IS NOT NULL;

-- Audit trail: who acted, and as whom. `detail` carries the create-time
-- context (token name, chosen user) without a second table.
ALTER TABLE api_token_audit_log
    ADD COLUMN IF NOT EXISTS actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE api_token_audit_log
    ADD COLUMN IF NOT EXISTS acts_as_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE api_token_audit_log
    ADD COLUMN IF NOT EXISTS detail TEXT;

-- Attribution tag on the rows a token writes.
ALTER TABLE history_events
    ADD COLUMN IF NOT EXISTS api_token_id INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL;
ALTER TABLE personal_notes
    ADD COLUMN IF NOT EXISTS api_token_id INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_history_events_api_token
    ON history_events(api_token_id) WHERE api_token_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_personal_notes_api_token
    ON personal_notes(api_token_id) WHERE api_token_id IS NOT NULL;
