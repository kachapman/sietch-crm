-- Migration 030: Fix history_attachments uniqueness — same file can appear on multiple events
--
-- Migration 029 created a GLOBAL unique index on crm_file_id, assuming each
-- OnlyOffice file appears on exactly one history event. That assumption is
-- wrong: the same file (e.g. file 18133 on opp 1243) is routinely referenced
-- by two or more events (forwarded emails, re-sent estimates, etc.).
--
-- The live migration died on opp 1243 with:
--   duplicate key value violates unique constraint "uq_history_attachments_crm_file_id"
--   Key (crm_file_id)=(18133) already exists.
-- and the aborted transaction then killed the entire run.
--
-- Correct model: dedup per (event_id, crm_file_id). The same file MAY appear
-- on different events (different rows, different file_path dirs), but MUST NOT
-- appear twice on the SAME event (re-run idempotency).

DROP INDEX IF EXISTS uq_history_attachments_crm_file_id;

-- Per-event dedup (supports ON CONFLICT (event_id, crm_file_id) DO NOTHING).
CREATE UNIQUE INDEX IF NOT EXISTS uq_history_attachments_event_file
    ON history_attachments(event_id, crm_file_id) WHERE crm_file_id IS NOT NULL;

-- Non-unique lookup index for "do we already have this file anywhere?" checks
-- and for joining history_attachments -> project_documents by crm_file_id.
CREATE INDEX IF NOT EXISTS idx_history_attachments_crm_file_id
    ON history_attachments(crm_file_id) WHERE crm_file_id IS NOT NULL;
