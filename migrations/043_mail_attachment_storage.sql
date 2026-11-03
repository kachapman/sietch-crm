-- 043: mail attachment local storage + link-time copy bookkeeping.
--
-- Bug fix 2026-11-03: a linked email's PDFs/documents never reached the
-- project's Documents tab.
--
-- Root cause (verified in code):
--   * At sync time the scanner stored attachment METADATA ONLY
--     (scanner/mail_scanner.py `_store_attachments`) — no bytes on disk.
--   * At link time `_copy_mail_attachments_to_project` needs bytes; with no
--     local file it fell back to a live mailbox download
--     (`_fetch_mail_attachment_bytes`). Any failure returned None and the
--     file was SILENTLY SKIPPED while the UI still reported success.
--   * Relinking did not help: `create_email_history_snapshot` returned early
--     when the timeline entry already existed, so the copy never re-ran.
--   * Side bug: the fallback matched on `imap_part_id`, which actually
--     holds a Content-ID (see `_fetch_messages`: "part_id": att.content_id).
--
-- Fix:
--   1. Store attachment BYTES on disk at sync time, with a sha256 content
--      hash, so the link-time copy has a local source and does not depend on
--      a live mailbox round-trip.
--   2. Record the copy bookkeeping so the link-time copy is idempotent and
--      so a skipped file is visible instead of silent.
--
-- This migration adds:
--   * mail_attachments.content_hash    — sha256 of the stored bytes (dedupe).
--   * mail_attachments.stored_size     — bytes actually written to disk.
--   * mail_attachments.download_error  — last local-store failure reason
--                                        (size limit, write error, ...).
--   * mail_attachments.remote_part_id  — the REAL IMAP part id (NOT the
--                                        Content-ID that imap_part_id holds).
--                                        Lets the fallback match the right
--                                        MIME part instead of relying on the
--                                        filename fallback.
--   * history_attachments.mail_attachment_id — the dedupe key: one row per
--                                        (project event, mail attachment).
--                                        Makes relinking/backfilling twice
--                                        a no-op.
--
-- Idempotent: every statement is IF NOT EXISTS / guarded.

ALTER TABLE mail_attachments
  ADD COLUMN IF NOT EXISTS content_hash     TEXT,
  ADD COLUMN IF NOT EXISTS stored_size      BIGINT,
  ADD COLUMN IF NOT EXISTS download_error   TEXT,
  ADD COLUMN IF NOT EXISTS remote_part_id   TEXT;

-- Dedupe lookup: "do we already hold this attachment's bytes?"
CREATE INDEX IF NOT EXISTS idx_mail_attachments_content_hash
    ON mail_attachments (content_hash)
    WHERE content_hash IS NOT NULL;

-- The link-time copy is idempotent through this column: one
-- history_attachments row per (event, mail attachment).
ALTER TABLE history_attachments
  ADD COLUMN IF NOT EXISTS mail_attachment_id INTEGER
    REFERENCES mail_attachments(id) ON DELETE CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_history_attachments_mail_attachment
    ON history_attachments (event_id, mail_attachment_id)
    WHERE mail_attachment_id IS NOT NULL;

-- Backfill scans: "links whose project has no copied file for this
-- attachment yet?"
CREATE INDEX IF NOT EXISTS idx_history_attachments_event_mail_att
    ON history_attachments (event_id, mail_attachment_id)
    WHERE mail_attachment_id IS NOT NULL;
