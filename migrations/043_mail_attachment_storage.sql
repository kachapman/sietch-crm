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
--   * mail_attachments.remote_part_id  — a SHORT, stable identifier for the
--                                        MIME part: `cid:<Content-ID>` when
--                                        the part has one, otherwise
--                                        `sha256:<16 hex>:<filename>:<size>`
--                                        derived from the payload.
--                                        ⚠ NOT `str(att.part)`: in
--                                        imap_tools 1.14.0 `att.part` is the
--                                        whole EmailMessage, whose string
--                                        form includes the base64 payload
--                                        (a 300 KB PDF -> 405,411 chars).
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
--
-- ⚠ ON DELETE SET NULL, NOT ON DELETE CASCADE.
-- `mail_attachments` rows are deleted when the mail account is deleted,
-- when a message is emptied from Trash, and when the mail retention job
-- purges old mail. With CASCADE that would delete the PROJECT's timeline
-- file rows — the Oct 29 2026 bug all over again ("deleting the mail
-- account cascaded mail_attachments away and the project's files
-- vanished"). SET NULL keeps the project's copy: the timeline row and the
-- file on disk survive, and only the link back to the mail attachment is
-- dropped (a later relink of the same file is then recognised as a legacy
-- copy by filename + size instead).
ALTER TABLE history_attachments
  ADD COLUMN IF NOT EXISTS mail_attachment_id INTEGER
    REFERENCES mail_attachments(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_history_attachments_mail_attachment
    ON history_attachments (event_id, mail_attachment_id)
    WHERE mail_attachment_id IS NOT NULL;

-- Backfill scans: "links whose project has no copied file for this
-- attachment yet?"
CREATE INDEX IF NOT EXISTS idx_history_attachments_event_mail_att
    ON history_attachments (event_id, mail_attachment_id)
    WHERE mail_attachment_id IS NOT NULL;

-- NOTE: the unique index on project_documents(opportunity_id, file_path) is
-- NOT here — it is in migration 044, which checks for existing duplicates
-- first and refuses to build the index (without touching any data) when it
-- finds them. That check needs a DO block, which cannot live in a plain
-- `IF NOT EXISTS` migration file.
